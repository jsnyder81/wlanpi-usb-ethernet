"""Socket-activated read-only HTTP status page (stdlib only, no cgi)."""

from __future__ import annotations

import html
import io
import json
import os
import socket
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer

from wlanpi_usb_gadget.events import read_events
from wlanpi_usb_gadget.report import write_report_tgz
from wlanpi_usb_gadget.status import collect_status, format_status_human


class StatusRequestHandler(BaseHTTPRequestHandler):
    """Read-only HTTP handler serving /, /status.json, /events.json, /report.tgz."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        if path == "/status.json":
            body = json.dumps(collect_status(), indent=2).encode("utf-8")
            self._send(HTTPStatus.OK, "application/json; charset=utf-8", body)
        elif path == "/events.json":
            body = json.dumps(read_events(newest_first=True), indent=2).encode("utf-8")
            self._send(HTTPStatus.OK, "application/json; charset=utf-8", body)
        elif path == "/report.tgz":
            buf = io.BytesIO()
            write_report_tgz(buf)
            self._send(
                HTTPStatus.OK,
                "application/gzip",
                buf.getvalue(),
                extra_headers={"Content-Disposition": 'attachment; filename="wlanpi-usb-gadget-report.tgz"'},
            )
        elif path in ("/", "/index.html"):
            st = collect_status()
            human = html.escape(format_status_human(st))
            page = (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<title>WLAN Pi USB Gadget Status</title>"
                "<style>body{font-family:monospace;max-width:60rem;margin:2rem auto;padding:0 1rem;}</style>"
                "</head><body>"
                "<h1>WLAN Pi USB Gadget</h1>"
                "<p><a href='/status.json'>status.json</a> | "
                "<a href='/events.json'>events.json</a> | "
                "<a href='/report.tgz'>Download report.tgz</a></p>"
                f"<pre>{human}</pre>"
                "</body></html>"
            ).encode("utf-8")
            self._send(HTTPStatus.OK, "text/html; charset=utf-8", page)
        else:
            self._send(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"Not Found\n")

    def _send(
        self,
        code: HTTPStatus,
        ctype: str,
        body: bytes,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


def serve_status_http(bind_host: str = "198.18.42.1", port: int = 8088, *, oneshot: bool = False) -> None:
    """Serve status HTTP either from systemd LISTEN_FDS or a bound TCP socket."""
    listen_fds = int(os.environ.get("LISTEN_FDS", "0"))
    if listen_fds >= 1:
        # systemd socket activation passes the accepted or listening socket on fd 3
        sock = socket.fromfd(3, socket.AF_INET, socket.SOCK_STREAM)
        server = HTTPServer((bind_host, port), StatusRequestHandler, bind_and_activate=False)
        server.socket = sock
        server.server_address = sock.getsockname()
        if oneshot:
            server.handle_request()
        else:
            server.serve_forever()
        return

    server = HTTPServer((bind_host, port), StatusRequestHandler)
    if oneshot:
        server.handle_request()
    else:
        server.serve_forever()
