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
_EMBEDDING_BODY = json.dumps(
    {
        "object": "list",
        "data": [
            {
                "object": "embedding",
                "index": 0,
                "embedding": [0.0] * 1024,
            }
        ],
        "model": "nvidia/nvclip",
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    },
    separators=(",", ":"),
).encode()
_TRANSCRIPTION_BODY = b'{"text":"candidate fake transcription","model":"nvidia/parakeet-ctc-1.1b"}'
_IMAGE_BODY = b'{"artifacts":[{"base64":"/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAH/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAEFAqf/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAEDAQE/AX//xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAECAQE/AX//xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAY/Aqf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAE/IV//2gAMAwEAAgADAAAAEP/EABQRAQAAAAAAAAAAAAAAAAAAABD/2gAIAQMBAT8QH//EABQRAQAAAAAAAAAAAAAAAAAAABD/2gAIAQIBAT8QH//EABQQAQAAAAAAAAAAAAAAAAAAABD/2gAIAQEAAT8QH//Z","finishReason":"SUCCESS","seed":1}]}'
_VIDEO_BODY = b'{"video":"AAAAEGZ0eXBpc29tAAACAAAAAAhtb292","finish_reason":"SUCCESS","seed":1}'
_VILA_BODY = b'{"id":"qa-vila-json","object":"chat.completion","model":"nvidia/vila","choices":[{"index":0,"message":{"role":"assistant","content":"candidate fake vila ok"},"finish_reason":"stop"}]}'
_AUDIO_BODY = b"RIFF&\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x44\xac\x00\x00\x88\x58\x01\x00\x02\x00\x10\x00data\x02\x00\x00\x00\x00\x00"
_CONTAINER_BIND_HOST = "0.0.0.0"  # noqa: S104 - isolated internal QA network only.


class Handler(BaseHTTPRequestHandler):
    """Return one strict JSON or SSE response without logging request data."""

    protocol_version = "HTTP/1.1"
    server_version = ""
    sys_version = ""
    responses: ClassVar = BaseHTTPRequestHandler.responses

    def do_GET(self) -> None:
        """Expose a small liveness endpoint for the compose healthcheck."""
        if self.path == "/health":
            self._respond(200, b'{"status":"ok"}', "application/json")
            return
        self._respond(404, b"{}", "application/json")

    def do_POST(self) -> None:
        """Handle deterministic JSON, multipart, and binary modality fixtures."""
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            self._respond(401, b"{}", "application/json")
            return
        if self.path == "/v1/audio/transcriptions":
            if not body or b'name="file"' not in body:
                self._respond(400, b"{}", "application/json")
                return
            self._respond(200, _TRANSCRIPTION_BODY, "application/json")
            return
        if self.path in {"/v1/audio/synthesize", "/v1/audio/speech"}:
            self._respond(200, _AUDIO_BODY, "audio/wav")
            return
        if self.path == "/v1/embeddings":
            self._respond(200, _EMBEDDING_BODY, "application/json")
            return
        if "/flux.1-kontext-dev" in self.path:
            self._respond(200, _IMAGE_BODY, "application/json")
            return
        if "/stable-video-diffusion" in self.path:
            self._respond(200, _VIDEO_BODY, "application/json")
            return
        if self.path == "/v1/vlm/nvidia/vila":
            self._respond(200, _VILA_BODY, "application/json")
            return
        if self.path != "/v1/chat/completions":
            self._respond(404, b"{}", "application/json")
            return
        try:
            request = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._respond(400, b"{}", "application/json")
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
