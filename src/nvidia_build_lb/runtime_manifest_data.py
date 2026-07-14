"""Immutable hashes and module selections for the pinned provider runtime."""

from typing import Final

ISOLATED_NAMESPACE: Final = "nvidia_build_lb._pinned_httpcore2_h1"
MODULE_ORDER: Final = (
    "_exceptions",
    "_utils",
    "_models",
    "_ssl",
    "_synchronization",
    "_trace",
    "_backends.base",
    "_backends.anyio",
    "_backends.auto",
    "_async.interfaces",
    "_async.http11",
    "_async.connection",
    "_async.connection_pool",
)
EXPECTED_DISTRIBUTIONS: Final = {
    "h11": (
        "0.16.0",
        "3779e13c17d8e21c769ed9ee8780af123cc3f8656185f5f8d3af258ff5639b3e",
        11,
        "2143eb70ef5d69f4e332bbb011802c2dcad2c8f2b201906891b5213b5020aadb",
    ),
    "httpcore2": (
        "2.5.0",
        "eba8930fc891f9c5f8199bc6d60926eaef374582234a1cb0e8fcae473ce164ed",
        31,
        "dda7665cd5f0c8548f3884b45fbae5c52f71018bfa5a40271feb822450c6b829",
    ),
    "httpx2": (
        "2.5.0",
        "59fb5023bfd715c1229298a0410e2e428b70f70cd8d0bb50ba6ece0dc046d5db",
        24,
        "2d75489ed8eaa8f72279b5e804133fc6c9c7f9653122452f3bef14a60acffdf9",
    ),
}
APPROVED_CLOSURE: Final = (
    "h11/__init__.py",
    "h11/_abnf.py",
    "h11/_connection.py",
    "h11/_events.py",
    "h11/_headers.py",
    "h11/_readers.py",
    "h11/_receivebuffer.py",
    "h11/_state.py",
    "h11/_util.py",
    "h11/_version.py",
    "h11/_writers.py",
    "httpcore2/_async/connection.py",
    "httpcore2/_async/connection_pool.py",
    "httpcore2/_async/http11.py",
    "httpcore2/_async/interfaces.py",
    "httpcore2/_backends/anyio.py",
    "httpcore2/_backends/auto.py",
    "httpcore2/_backends/base.py",
    "httpcore2/_exceptions.py",
    "httpcore2/_models.py",
    "httpcore2/_ssl.py",
    "httpcore2/_synchronization.py",
    "httpcore2/_trace.py",
    "httpcore2/_utils.py",
    "httpx2/__init__.py",
    "httpx2/__version__.py",
    "httpx2/_api.py",
    "httpx2/_auth.py",
    "httpx2/_client.py",
    "httpx2/_config.py",
    "httpx2/_content.py",
    "httpx2/_decoders.py",
    "httpx2/_exceptions.py",
    "httpx2/_models.py",
    "httpx2/_multipart.py",
    "httpx2/_sse.py",
    "httpx2/_status_codes.py",
    "httpx2/_transports/__init__.py",
    "httpx2/_transports/asgi.py",
    "httpx2/_transports/base.py",
    "httpx2/_transports/default.py",
    "httpx2/_transports/mock.py",
    "httpx2/_transports/wsgi.py",
    "httpx2/_types.py",
    "httpx2/_urlparse.py",
    "httpx2/_urls.py",
    "httpx2/_utils.py",
)
EXPECTED_CLOSURE_MERKLE: Final = "70370d8b264ad3dbae6079bec1df15a0cdffbb3e921aeaec5bf722f97438c505"
EXPECTED_LOGGER_NAMES: Final = frozenset({"httpcore2.connection", "httpcore2.http11", "httpx2"})
FORBIDDEN_MODULE_PREFIXES: Final = (
    "httpcore2",
    "httpcore2._async.http2",
    "httpcore2._sync.http2",
    "httpcore2._async.http_proxy",
    "httpcore2._sync.http_proxy",
    "httpcore2._async.socks_proxy",
    "httpcore2._sync.socks_proxy",
)
NON_PROVIDER_HASHES: Final = {
    "starlette/middleware/base.py": (
        "starlette",
        "abdd13dec1d08e1af9912209c8ce07357262f405acb6548d9a8cea4f6fca51cd",
    ),
    "starlette/responses.py": (
        "starlette",
        "5d52ab008ef7d9ce4c514f13b8ec62e15a1ea78f29a116f0e8cbee6f4eea9112",
    ),
    "uvicorn/protocols/http/h11_impl.py": (
        "uvicorn",
        "0c10c4fc79e742da96e746c06f8396ddd28a0e01004d448ebcda9f0c20c11764",
    ),
}
