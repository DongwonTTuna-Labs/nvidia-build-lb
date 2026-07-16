"""Minimal public routes used only by isolated credential-scope tests."""

from fastapi import FastAPI, Response

from nvidia_build_lb.schemas import ModelListResponse


def register_scope_test_routes(app: FastAPI) -> None:
    """Register fixed models and body-free chat scope probes."""

    @app.get("/v1/models")
    async def _models() -> ModelListResponse:
        return ModelListResponse.fixed_model()

    @app.post("/v1/chat/completions", status_code=204)
    async def _chat_scope_only() -> Response:
        return Response(status_code=204)

    _ = (_models, _chat_scope_only)
