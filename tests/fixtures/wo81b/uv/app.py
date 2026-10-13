"""WO-8.1b golden app: a stdlib HTTP app whose ONLY third-party
dependency is six, installed by the variant's committed lock
(uv.lock / poetry.lock / pdm.lock). /health reports the installed
six version, so a READY provision proves the manager's frozen
install landed in the checkout venv the app runs against."""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

import six


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = json.dumps(
                {"ok": True, "six": six.__version__}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
