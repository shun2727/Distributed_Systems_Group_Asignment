"""Serve the AirnStay web client on http://localhost:3000."""

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import http.client
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parent


class WebsiteHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def translate_path(self, path):
        request_path = unquote(urlsplit(path).path)
        if request_path.startswith("/assets/"):
            assets_root = (ROOT.parent / "assets").resolve()
            asset_path = (assets_root / request_path.removeprefix("/assets/")).resolve()
            if asset_path.is_relative_to(assets_root):
                return str(asset_path)
            return str(assets_root / "__not_found__")
        return super().translate_path(path)

    def _proxy_booking(self):
        """Forward same-origin /api requests to the private Booking process."""
        parsed = urlsplit(self.path)
        if not parsed.path.startswith("/api/") and parsed.path != "/api":
            self.send_error(404)
            return
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        body = None
        if self.command in ("POST", "PUT", "PATCH", "DELETE"):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else None
        connection = http.client.HTTPConnection(
            "127.0.0.1", int(os.environ.get("BOOKING_PORT", "5001")), timeout=30
        )
        headers = {
            key: value for key, value in self.headers.items()
            if key.lower() not in ("host", "connection", "content-length")
        }
        try:
            connection.request(self.command, target, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in ("connection", "transfer-encoding", "content-length", "date", "server"):
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            self.send_error(502, "Booking service unavailable")
        finally:
            connection.close()

    def do_GET(self):
        if urlsplit(self.path).path == "/api" or urlsplit(self.path).path.startswith("/api/"):
            self._proxy_booking()
        else:
            super().do_GET()

    def do_POST(self):
        self._proxy_booking()

    def do_PUT(self):
        self._proxy_booking()

    def do_PATCH(self):
        self._proxy_booking()

    def do_DELETE(self):
        self._proxy_booking()

    def do_OPTIONS(self):
        self._proxy_booking()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "3000"))
    print(f"AirnStay client: http://localhost:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), WebsiteHandler).serve_forever()
