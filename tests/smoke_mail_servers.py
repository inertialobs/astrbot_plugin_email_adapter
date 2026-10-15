"""Connectivity smoke test for common IMAP/SMTP servers.

For every configured provider this resolves the hostname and performs a plain
TCP connect + TLS handshake (no authentication) against every returned address.
It mirrors what the plugin's failover path does per address, so a scheduled run
can catch DNS/reachability/backend regressions of popular mail providers.

Usage:
    python tests/smoke_mail_servers.py [--strict] [--timeout SECONDS]

Exit code is 0 by default (informational). With ``--strict`` the run fails if
any provider has zero reachable addresses.
"""

import argparse
import socket
import ssl
import sys
from concurrent.futures import ThreadPoolExecutor

SERVERS = [
    ("imap.gmail.com", 993),
    ("imap.qq.com", 993),
    ("imap.163.com", 993),
    ("outlook.office365.com", 993),
    ("imap.mail.me.com", 993),
    ("smtp.gmail.com", 465),
    ("smtp.qq.com", 465),
]


def probe(host: str, port: int, timeout: float) -> list[tuple[tuple, str, str]]:
    """Probe every resolved address of a server.

    Args:
        host: Target hostname.
        port: Target port.
        timeout: Per-address timeout in seconds.

    Returns:
        A list of ``(sockaddr, status, detail)`` tuples.

    Raises:
        socket.gaierror: If the hostname cannot be resolved.
    """
    entries = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    results: list[tuple[tuple, str, str]] = []
    for family, socktype, proto, _canonname, sockaddr in entries:
        context = ssl.create_default_context()
        sock = socket.socket(family, socktype, proto)
        if timeout is not None:
            sock.settimeout(timeout)
        try:
            sock.connect(sockaddr)
            tls = context.wrap_socket(sock, server_hostname=host)
            version = tls.version()
            tls.close()
            results.append((sockaddr, "ok", version))
        except Exception as e:  # noqa: BLE001 - report any failure
            try:
                sock.close()
            except OSError:
                pass
            results.append((sockaddr, "fail", f"{type(e).__name__}: {e}"))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail if a provider has no reachable address.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Per-address timeout in seconds (default 15).",
    )
    args = parser.parse_args()

    unreachable: list[str] = []
    jobs: dict[str, list[tuple[tuple, str, str]]] = {}

    def run(server: tuple[str, int]) -> tuple[str, list[tuple[tuple, str, str]] | str]:
        host, port = server
        try:
            return (host, probe(host, port, args.timeout))
        except socket.gaierror as e:
            return (host, f"DNS error: {e}")

    with ThreadPoolExecutor(max_workers=len(SERVERS)) as pool:
        for host, result in pool.map(run, SERVERS):
            if isinstance(result, str):
                print(f"{host:28} ERROR  {result}")
                unreachable.append(host)
                continue
            jobs[host] = result
            ok = [r for r in result if r[1] == "ok"]
            print(f"{host:28} {len(ok)}/{len(result)} addresses reachable")
            for sockaddr, status, detail in result:
                print(f"    {status:4} {sockaddr} {detail}")
            if not ok:
                unreachable.append(host)

    print()
    if unreachable:
        print(f"Unreachable providers: {', '.join(unreachable)}")
        if args.strict:
            return 1
    else:
        print("All providers reachable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
