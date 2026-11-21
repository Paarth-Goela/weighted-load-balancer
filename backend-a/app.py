import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


HOST = "0.0.0.0"
PORT = 8000


class BackendHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        response = {
            "message": "Request successfully processed",
            "backend": "Backend A",
            "version": "1.0",
            "hostname": socket.gethostname(),
            "path": self.path,
        }
        body = json.dumps(response).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        print(
            f'[{socket.gethostname()}] {self.command} {self.path} '
            f'{format % args}',
            flush=True,
        )


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), BackendHandler)
    print(f"Backend A listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()
