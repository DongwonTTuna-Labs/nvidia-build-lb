"""Deterministic TLS NVIDIA wire fake used only by the candidate compose."""

import json
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

_JSON_BODY = json.dumps(
    {
        "id": "qa-candidate-json",
        "object": "chat.completion",
        "created": 1767225600,
        "model": "z-ai/glm-5.2",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "candidate fake upstream ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    },
    separators=(",", ":"),
).encode()
_SSE_BODY = b"".join(
    (
        b'data: {"id":"qa-candidate-stream","object":"chat.completion.chunk",',
        b'"created":1767225600,"model":"z-ai/glm-5.2","choices":[{"index":0,',
        b'"delta":{"content":"candidate fake upstream ok"},"finish_reason":"stop"}]}\n\n',
        b"data: [DONE]\n\n",
    )
)
_CONTAINER_BIND_HOST = "0.0.0.0"  # noqa: S104 - isolated internal QA network only.


class Handler(BaseHTTPRequestHandler):
    """Return one strict JSON or SSE response without logging request data."""

    protocol_version = "HTTP/1.1"
    server_version = ""
    sys_version = ""
    responses: ClassVar = BaseHTTPRequestHandler.responses

    def do_POST(self) -> None:
        """Handle the one fixed NVIDIA chat path."""
        if self.path != "/v1/chat/completions":
            self._respond(404, b"{}", "application/json")
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            request = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._respond(400, b"{}", "application/json")
            return
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            self._respond(401, b"{}", "application/json")
            return
        if request.get("stream") is True:
            self._respond(200, _SSE_BODY, "text/event-stream")
            return
        self._respond(200, _JSON_BODY, "application/json")

    def _respond(self, status: int, body: bytes, media_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, message: str, *args: object) -> None:
        """Suppress request and header logging entirely."""
        del message, args


def main() -> None:
    """Serve the fixed fake on the NVIDIA HTTPS authority port."""
    server = ThreadingHTTPServer(
        (_CONTAINER_BIND_HOST, 443),
        Handler,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(
        "/run/nvidia-build-lb/qa-tls/server_cert",
        "/run/nvidia-build-lb/qa-tls/server_key",
    )
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
