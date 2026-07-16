"""User-space public gateway for the ETF dashboard and chat application."""

from __future__ import annotations

import argparse
import http.client
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, urlunsplit


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 3004
MAX_REQUEST_BODY = 2 * 1024 * 1024
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def resolve_upstream(raw_path: str) -> tuple[str, int, str] | None:
    """Map a public path to the corresponding loopback service and path."""
    parsed = urlsplit(raw_path)
    path = parsed.path
    if path.startswith("/etf-agent/chat/"):
        return "chat", 8766, raw_path
    if path.startswith("/etf-agent/"):
        upstream_path = path[len("/etf-agent") :] or "/"
        return (
            "dashboard",
            8765,
            urlunsplit(("", "", upstream_path, parsed.query, "")),
        )
    return None


class PublicGatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


class PublicGatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/healthz":
            self._json_response({"status": "ok", "port": self.server.server_port})
            return
        if parsed.path in {"", "/"}:
            self._redirect("/etf-agent/chat/")
            return
        if parsed.path == "/etf-agent":
            self._redirect("/etf-agent/")
            return
        if parsed.path == "/etf-agent/chat":
            self._redirect("/etf-agent/chat/")
            return

        upstream = resolve_upstream(self.path)
        if upstream is None:
            self.send_error(404)
            return
        _, port, target = upstream
        self._proxy(port, target)

    def _proxy(self, port: int, target: str) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            self.send_error(400, "Invalid Content-Length")
            return
        if length < 0 or length > MAX_REQUEST_BODY:
            self.send_error(413, "Request body too large")
            return
        body = self.rfile.read(length) if length else None

        headers: dict[str, str] = {}
        for key, value in self.headers.items():
            if key.lower() in HOP_BY_HOP_HEADERS or key.lower() == "host":
                continue
            headers[key] = value
        headers["Host"] = f"127.0.0.1:{port}"
        headers["X-Real-IP"] = self.client_address[0]
        # This process is the public trust boundary, so never accept a
        # client-supplied forwarding chain for rate-limit or admin checks.
        headers["X-Forwarded-For"] = self.client_address[0]
        headers["X-Forwarded-Proto"] = "http"

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=370)
        response_started = False
        try:
            connection.request(self.command, target, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() in HOP_BY_HOP_HEADERS:
                    continue
                self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            response_started = True
            if self.command != "HEAD":
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (ConnectionError, http.client.HTTPException, socket.timeout) as exc:
            if not response_started and not self.wfile.closed:
                try:
                    self._json_response(
                        {"status": "upstream_unavailable", "detail": str(exc)},
                        status=502,
                    )
                except (BrokenPipeError, ConnectionError):
                    pass
        finally:
            connection.close()
            self.close_connection = True

    def _redirect(self, target: str) -> None:
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json_response(self, payload: dict, *, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} [{self.log_date_time_string()}] {fmt % args}")


def main() -> int:
    parser = argparse.ArgumentParser(description="ETF public gateway")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    server = PublicGatewayServer((args.host, args.port), PublicGatewayHandler)
    print(f"ETF public gateway listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
