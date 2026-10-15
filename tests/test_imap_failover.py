import socket
import ssl
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import imap_failover as f  # noqa: E402

BAD = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 993))
BAD2 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.2", 993))
GOOD = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 993, 0, 0))


class _FakeSock:
    def __init__(self, family, socktype, proto):
        self.sa = None

    def settimeout(self, timeout):
        pass

    def connect(self, sockaddr):
        if sockaddr in (BAD[4], BAD2[4]):
            raise OSError("connection reset")
        self.sa = sockaddr

    def close(self):
        pass


def _fake_socket(family, socktype, proto):
    return _FakeSock(family, socktype, proto)


class FailoverIMAP4SSLTest(unittest.TestCase):
    def _make(self, addresses, wrap):
        obj = f._FailoverIMAP4SSL.__new__(f._FailoverIMAP4SSL)
        obj._addresses = list(addresses)
        obj._logger = None
        obj._label = "test"
        obj._attempts = []
        obj.host = "imap.example.com"
        obj.ssl_context = types.SimpleNamespace(wrap_socket=wrap)
        return obj

    def test_skips_address_that_resets_tcp(self):
        obj = self._make(
            [BAD, GOOD],
            lambda sock, server_hostname=None: ("wrapped", sock.sa),
        )
        with mock.patch.object(f.socket, "socket", _fake_socket):
            out = obj._create_socket(10)
        self.assertEqual(obj.connected_address, GOOD[4])
        self.assertEqual(out, ("wrapped", GOOD[4]))

    def test_skips_address_that_fails_tls_handshake(self):
        class _ConnectsAnyway(_FakeSock):
            def connect(self, sockaddr):
                self.sa = sockaddr

        def wrap(sock, server_hostname=None):
            if sock.sa == BAD[4]:
                raise ssl.SSLError("[SSL: RECORD_LAYER_FAILURE] record layer failure")
            return ("wrapped", sock.sa)

        def fake_socket(family, socktype, proto):
            return _ConnectsAnyway(family, socktype, proto)

        obj = self._make([BAD, GOOD], wrap)
        with mock.patch.object(f.socket, "socket", fake_socket):
            out = obj._create_socket(10)
        self.assertEqual(obj.connected_address, GOOD[4])
        self.assertEqual(out, ("wrapped", GOOD[4]))

    def test_raises_last_error_when_all_addresses_fail(self):
        def wrap(sock, server_hostname=None):
            raise ssl.SSLError("boom")

        obj = self._make([BAD, BAD2], wrap)
        with mock.patch.object(f.socket, "socket", _fake_socket):
            with self.assertRaises((OSError, ssl.SSLError)):
                obj._create_socket(10)
        self.assertEqual(len(obj._attempts), 2)
        self.assertEqual(obj._attempts[0][0], BAD[4])


class FailoverIMAP4Test(unittest.TestCase):
    def test_plain_tcp_failover(self):
        obj = f._FailoverIMAP4.__new__(f._FailoverIMAP4)
        obj._addresses = [BAD, GOOD]
        obj._logger = None
        obj._label = "test"
        obj._attempts = []
        obj.host = "imap.example.com"
        with mock.patch.object(f.socket, "socket", _fake_socket):
            sock = obj._create_socket(10)
        self.assertEqual(obj.connected_address, GOOD[4])
        self.assertIsInstance(sock, _FakeSock)


if __name__ == "__main__":
    unittest.main()
