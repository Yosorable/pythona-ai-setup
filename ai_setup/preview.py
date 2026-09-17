"""Serve the exact setup page with a browser-only, in-memory preview adapter."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .page import render_page


def serve(port=8879, language="en"):
    page = render_page(language, preview=True).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/", "/index.html"):
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"Preview: http://127.0.0.1:{server.server_port} (simulated responses; Ctrl-C to stop)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
