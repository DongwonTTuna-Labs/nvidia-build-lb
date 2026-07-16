"""Closed administration path shapes shared by auth and route fallback."""

from typing import Final

_ADMIN_PREFIX: Final = "/admin/api/v1"
_EXACT_METHODS: Final = {
    "/admin/api/v1/dashboard": frozenset({"GET"}),
    "/admin/api/v1/overview": frozenset({"GET"}),
    "/admin/api/v1/upstream-keys": frozenset({"GET", "POST"}),
    "/admin/api/v1/downstream-tokens": frozenset({"GET", "POST"}),
    "/admin/api/v1/events": frozenset({"GET"}),
    "/admin/api/v1/operator-readiness": frozenset({"GET"}),
}


def admin_allowed_methods(path: str) -> frozenset[str] | None:
    """Return allowed methods for one known admin resource path shape."""
    exact = _EXACT_METHODS.get(path)
    if exact is not None:
        return exact
    match path.split("/"):
        case ["", "admin", "api", "v1", "upstream-keys", resource_id] if resource_id:
            return frozenset({"DELETE"})
        case [
            "",
            "admin",
            "api",
            "v1",
            "upstream-keys",
            resource_id,
            action,
        ] if resource_id and action in {"enable", "disable", "probe"}:
            return frozenset({"POST"})
        case ["", "admin", "api", "v1", "downstream-tokens", resource_id] if resource_id:
            return frozenset({"DELETE"})
        case _:
            return None


def requires_admin_authentication(method: str, path: str) -> bool:
    """Authenticate valid admin operations and every unknown admin resource."""
    allowed = admin_allowed_methods(path)
    if allowed is not None:
        return method in allowed
    return method != "OPTIONS" and (path == _ADMIN_PREFIX or path.startswith(f"{_ADMIN_PREFIX}/"))


def is_admin_mutation(method: str, path: str) -> bool:
    """Recognize only the seven authenticated event-producing operations."""
    if method not in {"POST", "DELETE"}:
        return False
    match method, path.split("/"):
        case "POST", ["", "admin", "api", "v1", "upstream-keys"]:
            mutation = True
        case "POST", ["", "admin", "api", "v1", "downstream-tokens"]:
            mutation = True
        case "DELETE", ["", "admin", "api", "v1", "upstream-keys", resource_id]:
            mutation = bool(resource_id)
        case "DELETE", ["", "admin", "api", "v1", "downstream-tokens", resource_id]:
            mutation = bool(resource_id)
        case (
            "POST",
            ["", "admin", "api", "v1", "upstream-keys", resource_id, action],
        ):
            mutation = bool(resource_id) and action in {"enable", "disable", "probe"}
        case _:
            mutation = False
    return mutation
