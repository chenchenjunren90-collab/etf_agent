from __future__ import annotations

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import public_gateway


class EchoHandler(BaseHTTPRequestHandler):
    def _reply(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        payload = json.dumps(
            {
                "method": self.command,
                "path": self.path,
                "body": body,
                "forwarded_for": self.headers.get("X-Forwarded-For"),
            },
            ensure_ascii=True,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = _reply
    do_POST = _reply

    def log_message(self, fmt: str, *args: object) -> None:
        pass


def start_server(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def request(
    port: int,
    method: str,
    path: str,
    body: str = "",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict | str, dict[str, str]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    request_headers = dict(headers or {})
    if body:
        request_headers["Content-Type"] = "application/json"
    connection.request(method, path, body=body or None, headers=request_headers)
    response = connection.getresponse()
    raw = response.read().decode("utf-8")
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    connection.close()
    try:
        payload: dict | str = json.loads(raw)
    except json.JSONDecodeError:
        payload = raw
    return response.status, payload, response_headers


def main() -> None:
    assert public_gateway.PublicGatewayServer.request_queue_size >= 64
    dashboard, _ = start_server(EchoHandler)
    chat, _ = start_server(EchoHandler)

    original = public_gateway.resolve_upstream

    def test_resolve(raw_path: str):
        result = original(raw_path)
        if result is None:
            return None
        name, _, target = result
        port = chat.server_port if name == "chat" else dashboard.server_port
        return name, port, target

    public_gateway.resolve_upstream = test_resolve
    original_review_loader = public_gateway.load_current_review
    public_gateway.load_current_review = lambda date: {
        "status": "ok",
        "date": date,
        "strategy_version": "test-v1",
        "official_submission_unchanged": True,
        "competition_output": [],
    }
    gateway, _ = start_server(public_gateway.PublicGatewayHandler)
    try:
        status, _, headers = request(gateway.server_port, "GET", "/")
        assert status == 302 and headers["location"] == "/etf-agent/chat/"

        status, payload, _ = request(
            gateway.server_port,
            "GET",
            "/etf-agent/api/status?date=2026-07-16",
        )
        assert status == 200
        assert payload["path"] == "/api/status?date=2026-07-16"

        status, payload, _ = request(
            gateway.server_port,
            "GET",
            "/etf-agent/chat/docs",
        )
        assert status == 200
        assert payload["path"] == "/etf-agent/chat/docs"

        status, payload, _ = request(
            gateway.server_port,
            "POST",
            "/etf-agent/chat/api/chat",
            body='{"message":"ETF"}',
            headers={"X-Forwarded-For": "203.0.113.50"},
        )
        assert status == 200
        assert payload["method"] == "POST"
        assert payload["body"] == '{"message":"ETF"}'
        assert payload["forwarded_for"] == "127.0.0.1"

        status, payload, _ = request(gateway.server_port, "GET", "/healthz")
        assert status == 200 and payload["status"] == "ok"

        status, payload, headers = request(
            gateway.server_port,
            "GET",
            "/etf-agent/api/current-review?date=2026-07-17",
        )
        assert status == 200
        assert payload["date"] == "2026-07-17"
        assert payload["official_submission_unchanged"] is True
        assert headers["cache-control"] == "no-store"

        status, payload, _ = request(
            gateway.server_port,
            "POST",
            "/etf-agent/api/current-review",
        )
        assert status == 405 and payload["status"] == "method_not_allowed"
    finally:
        public_gateway.resolve_upstream = original
        public_gateway.load_current_review = original_review_loader
        gateway.shutdown()
        dashboard.shutdown()
        chat.shutdown()
    print("PUBLIC GATEWAY OK")


if __name__ == "__main__":
    main()
