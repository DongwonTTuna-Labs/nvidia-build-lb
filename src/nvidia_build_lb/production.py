"""Safe process entrypoint for the single-worker production gateway."""

import sys

import anyio
import uvicorn

from nvidia_build_lb.config import load_settings
from nvidia_build_lb.production_graph import ProductionResources, build_production_resources

_CONTAINER_BIND_HOST = "0.0.0.0"  # noqa: S104 - Docker publishes host loopback only.


async def serve() -> None:
    """Run one Uvicorn worker inside the lifecycle fail-stop cancel scope."""
    settings = load_settings()
    resources: ProductionResources | None = None
    async with anyio.create_task_group() as tasks:
        root_scope = anyio.CancelScope()
        resources = await build_production_resources(settings, tasks, root_scope)
        server = uvicorn.Server(
            uvicorn.Config(
                resources.app,
                host=_CONTAINER_BIND_HOST,
                port=2456,
                workers=1,
                access_log=False,
                log_config=None,
                lifespan="off",
                server_header=False,
                date_header=False,
                timeout_graceful_shutdown=10,
            )
        )
        try:
            with root_scope:
                await server.serve()
        finally:
            with anyio.CancelScope(shield=True):
                await resources.close()
            tasks.cancel_scope.cancel()


def main() -> None:
    """Exit with one stable diagnostic instead of a sensitive traceback."""
    status = 0
    try:
        anyio.run(serve)
    except BaseException:  # noqa: BLE001 - process boundary emits only a fixed code.
        status = 1
        _ = sys.stderr.write("runtime_failed\n")
    raise SystemExit(status)


if __name__ == "__main__":
    main()
