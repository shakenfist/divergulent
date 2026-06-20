'''Offline tests for the keep-alive file fetcher in apt_patches.

These run a real ``http.server`` on localhost in a background thread so the
keep-alive reuse, proxy absolute-URI handling, and reconnect-on-drop behaviour
are exercised end to end without touching the network.
'''
from __future__ import annotations

import http.server
import os
import tempfile
import threading

import testtools

from divergulent.sources.apt_patches import _KeepAliveFetcher


class _Server:
    '''A localhost HTTP server that records connections and request lines.

    ``connections`` counts accepted TCP connections (one per handler instance),
    which proves keep-alive reuse: several sequential fetches over a reused
    connection increment it once. ``request_lines`` records each request's first
    line so a proxied fetch can be checked for an absolute-URI request target.
    ``close_after`` makes the server close the connection after each request to
    exercise the reconnect path.
    '''

    def __init__(self, *, close_after: bool = False) -> None:
        self.connections = 0
        self.request_lines: list[str] = []
        self._lock = threading.Lock()
        self._close_after = close_after
        server_self = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def setup(self) -> None:  # one handler instance per accepted connection
                super().setup()
                with server_self._lock:
                    server_self.connections += 1

            def do_GET(self) -> None:
                with server_self._lock:
                    server_self.request_lines.append(self.requestline)
                body = b'hello-body'
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(len(body)))
                if server_self._close_after:
                    self.send_header('Connection', 'close')
                    self.close_connection = True
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:  # silence test output
                pass

        self._httpd = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


class KeepAliveFetcherTestCase(testtools.TestCase):

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _dest(self, name: str) -> str:
        return os.path.join(self._tmp.name, name)

    def test_reuses_single_connection(self):
        server = _Server()
        self.addCleanup(server.stop)
        fetcher = _KeepAliveFetcher()

        for i in range(4):
            dest = self._dest('f%d' % i)
            fetcher.fetch('http://127.0.0.1:%d/file%d' % (server.port, i), dest)
            with open(dest, 'rb') as handle:
                self.assertEqual(b'hello-body', handle.read())

        # Four sequential fetches, but only one accepted TCP connection: the
        # keep-alive socket was reused rather than reopened per file.
        self.assertEqual(1, server.connections)
        self.assertEqual(4, len(server.request_lines))
        # Direct (unproxied) fetches send origin-form request targets.
        self.assertTrue(all(line.startswith('GET /file') for line in server.request_lines),
                        server.request_lines)

    def test_proxy_uses_absolute_uri(self):
        server = _Server()
        self.addCleanup(server.stop)
        # Point http_proxy at the local server; getproxies() reads this env.
        proxy = 'http://127.0.0.1:%d' % server.port
        old = {k: os.environ.get(k) for k in ('http_proxy', 'HTTP_PROXY', 'no_proxy', 'NO_PROXY')}

        def restore() -> None:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(restore)
        os.environ['http_proxy'] = proxy
        os.environ['HTTP_PROXY'] = proxy
        os.environ.pop('no_proxy', None)
        os.environ.pop('NO_PROXY', None)

        fetcher = _KeepAliveFetcher()
        dest = self._dest('proxied')
        fetcher.fetch('http://example.invalid/some/path', dest)

        with open(dest, 'rb') as handle:
            self.assertEqual(b'hello-body', handle.read())
        self.assertEqual(1, len(server.request_lines))
        # Through a proxy the request line carries the full absolute URI.
        self.assertEqual('GET http://example.invalid/some/path HTTP/1.1', server.request_lines[0])

    def test_reconnects_after_server_closes(self):
        server = _Server(close_after=True)
        self.addCleanup(server.stop)
        fetcher = _KeepAliveFetcher()

        for i in range(3):
            dest = self._dest('r%d' % i)
            fetcher.fetch('http://127.0.0.1:%d/file%d' % (server.port, i), dest)
            with open(dest, 'rb') as handle:
                self.assertEqual(b'hello-body', handle.read())

        # The server closed the connection after each request, so each fetch had
        # to open a fresh one -- and all three transparently succeeded.
        self.assertEqual(3, server.connections)
        self.assertEqual(3, len(server.request_lines))

    def test_non_2xx_raises(self):
        class ErrorHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_GET(self) -> None:
                body = b'nope'
                self.send_response(404)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        httpd = http.server.HTTPServer(('127.0.0.1', 0), ErrorHandler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)

        fetcher = _KeepAliveFetcher()
        dest = self._dest('err')
        # A non-2xx raises (so the caller's retry/error handling sees it) and no
        # error body is written to dest.
        self.assertRaises(Exception, fetcher.fetch, 'http://127.0.0.1:%d/x' % port, dest)
        self.assertFalse(os.path.exists(dest))
