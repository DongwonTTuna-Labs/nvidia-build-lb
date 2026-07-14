from .browser_prod_models import NativeStableProjection
from .browser_prod_native import _stable_dom_hash  # pyright: ignore[reportPrivateUsage]
from .browser_prod_verify import _stable_projection  # pyright: ignore[reportPrivateUsage]


def test_stable_dom_hash_normalizes_only_declared_runtime_values() -> None:
    first = """
    <tr id="event-1c640055-e9a7-4d1f-8a3a-95561ca654ae">
      <td>downstream_token_revoked · 1c640055-e9a7-4d1f-8a3a-95561ca654ae</td>
      <td>b2cc462ee715401eb80b9c5c072c3e68</td>
      <td>sha256:bc68b4f8747af2ee...</td>
      <td>succeeded · success · 38</td>
      <td>2026-07-14T04:19:05.568569Z</td>
    </tr>
    """
    second = """
    <tr id="event-c789d184-2960-4eab-838c-c48d723b0aae">
      <td>downstream_token_revoked · c789d184-2960-4eab-838c-c48d723b0aae</td>
      <td>0d54b2e031584aecbdf6f21746995893</td>
      <td>sha256:216266498ce62995...</td>
      <td>succeeded · success · 42</td>
      <td>2026-07-14T04:23:33.054393Z</td>
    </tr>
    """

    assert _stable_dom_hash(first) == _stable_dom_hash(second)
    assert _stable_dom_hash(first) != _stable_dom_hash(first.replace("succeeded", "failed"))
    assert _stable_dom_hash(first) != _stable_dom_hash(first.replace("<tr", "<tr hidden"))


def test_verifier_projection_retains_canonical_dom_hash() -> None:
    observed = _stable_projection(
        NativeStableProjection(
            label="dashboard",
            dom_hash="a" * 64,
            focus_stops=13,
            axe_serious=0,
            axe_critical=0,
            axe_network_requests=0,
            focus_clipped=0,
            focus_hidden=0,
            focus_covered=0,
        )
    )

    assert observed.dom_hash == "a" * 64
