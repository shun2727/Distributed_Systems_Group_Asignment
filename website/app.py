"""Serve the AirnStay web client on http://localhost:3000."""

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "3000"))
    print(f"AirnStay client: http://localhost:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), WebsiteHandler).serve_forever()
