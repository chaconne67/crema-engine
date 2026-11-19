"""Loopback SOCKS5 transport for private-network-restricted Chromium sessions.

Chrome sends URL connections (including redirects, workers and WebSockets)
through this listener. Only validated numeric destinations are dialed.
"""

import select
import socket
import struct
import threading
from socketserver import StreamRequestHandler, ThreadingTCPServer

from tools.url_safety import resolve_public_connect_ips

_TIMEOUT = 15
_SOCKS_REPLY_ADDRESS = b"\x00\x01\x00\x00\x00\x00\x00\x00"
_proxy = None
_proxy_lock = threading.Lock()


def _connect(host: str, port: int) -> socket.socket:
    for address in resolve_public_connect_ips(host, port):
        try:
            return socket.create_connection((address, port), timeout=_TIMEOUT)
        except OSError:
            continue
    raise OSError("No public address accepted the connection")


class _ProxyRequest(StreamRequestHandler):
    timeout = _TIMEOUT

    def handle(self):
        try:
            self._connect_and_relay()
        except (OSError, ValueError, struct.error):
            # Invalid, closed or timed out SOCKS connections are per-request failures.
            return

    def _connect_and_relay(self):
        greeting = self.rfile.read(2)
        if len(greeting) != 2 or greeting[0] != 5:
            return
        if 0 not in self.rfile.read(greeting[1]):
            self.wfile.write(b"\x05\xff")
            return
        self.wfile.write(b"\x05\x00")
        request = self.rfile.read(4)
        if len(request) != 4 or request[:3] != b"\x05\x01\x00":
            return  # TCP CONNECT is the only supported command.
        address_type = request[3]
        if address_type == 3:
            length = self.rfile.read(1)
            if not length:
                return
            host = self.rfile.read(length[0]).decode("ascii")
        elif address_type in (1, 4):
            family, size = (socket.AF_INET, 4) if address_type == 1 else (socket.AF_INET6, 16)
            host = socket.inet_ntop(family, self.rfile.read(size))
        else:
            return
        port, = struct.unpack("!H", self.rfile.read(2))
        try:
            upstream = _connect(host, port)
        except (OSError, ValueError):
            self.wfile.write(b"\x05\x02" + _SOCKS_REPLY_ADDRESS)
            return
        with upstream:
            self.wfile.write(b"\x05\x00" + _SOCKS_REPLY_ADDRESS)
            peers = {self.connection: upstream, upstream: self.connection}
            while readable := select.select(list(peers), [], [], 120)[0]:
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    peers[source].sendall(data)


class _ProxyServer(ThreadingTCPServer):
    daemon_threads = True
    request_queue_size = 128
    _slots = threading.BoundedSemaphore(128)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def chromium_proxy_url() -> str:
    """Return the process-owned listener, starting it before Chromium can connect."""
    global _proxy
    with _proxy_lock:
        if _proxy is None:
            server = _ProxyServer(("127.0.0.1", 0), _ProxyRequest)
            threading.Thread(target=server.serve_forever, name="browser-public-proxy", daemon=True).start()
            _proxy = server
        return f"socks5://127.0.0.1:{_proxy.server_address[1]}"
