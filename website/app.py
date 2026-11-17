"""Serve the hotel booking web client on http://localhost:3000."""

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class WebsiteHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)


if __name__ == "__main__":
    print("Hotel booking client: http://localhost:3000")
    ThreadingHTTPServer(("0.0.0.0", 3000), WebsiteHandler).serve_forever()
