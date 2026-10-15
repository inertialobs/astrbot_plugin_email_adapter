"""IMAP connection helpers that survive per-address backend failures.

Large providers (for example QQ Mail) resolve their IMAP hostname to several
backend addresses and rotate them over DNS. A subset of those addresses may be
unreachable from a given network: the TCP connection succeeds but the TLS
handshake is reset, surfacing as ``ssl.SSLError: ... RECORD_LAYER_FAILURE``.

``socket.create_connection`` stops at the first address whose TCP connect
succeeds, so it never tries the remaining addresses when the handshake fails.
The subclasses in this module walk the whole ``getaddrinfo`` result instead and
remember the address that actually worked.

An optional ``logger`` (any object exposing ``debug``/``info``/``warning``) and
``label`` can be supplied to trace every attempt.
"""

import imaplib
import socket
import ssl


class _FailoverIMAP4(imaplib.IMAP4):
    """IMAP4 that walks every resolved address until one connects.

    Attributes:
        connected_address: The sockaddr that produced a working connection, or
            ``None`` when the default address resolution was used.
    """

    def __init__(
        self,
        host: str = "",
        port: int = imaplib.IMAP4_PORT,
        timeout: float | None = None,
        *,
        addresses: list | None = None,
        logger=None,
        label: str = "imap",
        attempts: list | None = None,
    ) -> None:
        """Initialize the failover IMAP client.

        Args:
            host: Target hostname (used for SNI and greeting parsing).
            port: Target port.
            timeout: Socket timeout in seconds.
            addresses: Pre-resolved ``getaddrinfo`` entries to try in order.
                When empty, the default single-address behavior is used.
            logger: Optional logger used to trace each connection attempt.
            label: Prefix used in log messages.
            attempts: Optional list that receives ``(sockaddr, error)`` pairs for
                every failed address.
        """
        self._addresses = addresses or []
        self._logger = logger
        self._label = label
        self._attempts = attempts if attempts is not None else []
        self.connected_address: tuple | None = None
        super().__init__(host, port, timeout=timeout)

    def _trace(self, level: str, message: str, *args) -> None:
        if self._logger is not None:
            getattr(self._logger, level)("[%s] " + message, self._label, *args)

    def _create_socket(self, timeout):
        if not self._addresses:
            return super()._create_socket(timeout)
        last_error: Exception | None = None
        total = len(self._addresses)
        for index, (family, socktype, proto, _canonname, sockaddr) in enumerate(
            self._addresses, 1
        ):
            if total > 1:
                self._trace("debug", "TCP 连接 %s (%d/%d)…", sockaddr, index, total)
            sock = socket.socket(family, socktype, proto)
            if timeout is not None:
                sock.settimeout(timeout)
            try:
                sock.connect(sockaddr)
            except OSError as e:
                last_error = e
                self._attempts.append((sockaddr, f"{type(e).__name__}: {e}"))
                self._trace("debug", "地址 %s TCP 连接失败: %s", sockaddr, e)
                try:
                    sock.close()
                except OSError:
                    pass
                continue
            self.connected_address = sockaddr
            return sock
        raise last_error


class _FailoverIMAP4SSL(imaplib.IMAP4_SSL):
    """TLS variant of :class:`_FailoverIMAP4`.

    The whole TCP + TLS handshake is retried per address, so a backend that
    accepts the TCP connection but aborts the handshake is skipped.
    """

    def __init__(
        self,
        host: str = "",
        port: int = imaplib.IMAP4_SSL_PORT,
        *,
        ssl_context: ssl.SSLContext | None = None,
        timeout: float | None = None,
        addresses: list | None = None,
        logger=None,
        label: str = "imap",
        attempts: list | None = None,
    ) -> None:
        """Initialize the failover IMAP-over-TLS client.

        Args:
            host: Target hostname (used for SNI and certificate checks).
            port: Target port.
            ssl_context: SSL context used to wrap each candidate socket.
            timeout: Socket timeout in seconds.
            addresses: Pre-resolved ``getaddrinfo`` entries to try in order.
                When empty, the default single-address behavior is used.
            logger: Optional logger used to trace each connection attempt.
            label: Prefix used in log messages.
            attempts: Optional list that receives ``(sockaddr, error)`` pairs for
                every failed address.
        """
        self._addresses = addresses or []
        self._logger = logger
        self._label = label
        self._attempts = attempts if attempts is not None else []
        self.connected_address: tuple | None = None
        super().__init__(host, port, ssl_context=ssl_context, timeout=timeout)

    def _trace(self, level: str, message: str, *args) -> None:
        if self._logger is not None:
            getattr(self._logger, level)("[%s] " + message, self._label, *args)

    def _create_socket(self, timeout):
        if not self._addresses:
            return super()._create_socket(timeout)
        last_error: Exception | None = None
        total = len(self._addresses)
        for index, (family, socktype, proto, _canonname, sockaddr) in enumerate(
            self._addresses, 1
        ):
            if total > 1:
                self._trace("debug", "TCP 连接 %s (%d/%d)…", sockaddr, index, total)
            sock = socket.socket(family, socktype, proto)
            if timeout is not None:
                sock.settimeout(timeout)
            try:
                sock.connect(sockaddr)
            except OSError as e:
                last_error = e
                self._attempts.append((sockaddr, f"{type(e).__name__}: {e}"))
                self._trace("debug", "地址 %s TCP 连接失败: %s", sockaddr, e)
                try:
                    sock.close()
                except OSError:
                    pass
                continue
            try:
                wrapped = self.ssl_context.wrap_socket(sock, server_hostname=self.host)
            except (OSError, ssl.SSLError) as e:
                last_error = e
                self._attempts.append((sockaddr, f"TLS {type(e).__name__}: {e}"))
                self._trace("debug", "地址 %s TLS 握手失败: %s", sockaddr, e)
                try:
                    sock.close()
                except OSError:
                    pass
                continue
            self.connected_address = sockaddr
            return wrapped
        raise last_error
