import pytest

from .browser_prod_models import NativeStableProjection
from .browser_prod_native import _stable_dom_hash  # pyright: ignore[reportPrivateUsage]
from .browser_prod_verify import _stable_projection  # pyright: ignore[reportPrivateUsage]


def test_stable_dom_hash_normalizes_only_declared_runtime_values() -> None:
    first = """
    <tr id="event-1c640055-e9a7-4d1f-8a3a-95561ca654ae">
      <td>downstream_token_revoked · 1c640055-e9a7-4d1f-8a3a-95561ca654ae</td>
      <td>b2cc462ee715401eb80b9c5c072c3e68</td>
      <td>Key bc68b4f8</td>
      <td>sha256:bc68b4f8747af2ee3fb6594604b190e2d33fe04d8227722eedcdbf9ee4beb1f5</td>
      <td>succeeded · success · 38</td>
      <td>2026-07-14T04:19:05.568569Z</td>
    </tr>
    """
    second = """
    <tr id="event-c789d184-2960-4eab-838c-c48d723b0aae">
      <td>downstream_token_revoked · c789d184-2960-4eab-838c-c48d723b0aae</td>
      <td>0d54b2e031584aecbdf6f21746995893</td>
      <td>Key 21626649</td>
      <td>sha256:216266498ce629954bbdd7c6d64677ff40c10f6f32bba49e54758f764b51c2cd</td>
      <td>succeeded · success · 42</td>
      <td>2026-07-14T04:23:33.054393Z</td>
    </tr>
    """

    first_fingerprint = "sha256:bc68b4f8747af2ee3fb6594604b190e2d33fe04d8227722eedcdbf9ee4beb1f5"
    second_fingerprint = "sha256:216266498ce629954bbdd7c6d64677ff40c10f6f32bba49e54758f764b51c2cd"
    assert _stable_dom_hash(first, (first_fingerprint,)) == _stable_dom_hash(
        second,
        (second_fingerprint,),
    )
    assert _stable_dom_hash(first) != _stable_dom_hash(first.replace("succeeded", "failed"))
    assert _stable_dom_hash(first) != _stable_dom_hash(first.replace("<tr", "<tr hidden"))
    assert _stable_dom_hash("<p>Key bc68b4f8</p>") != _stable_dom_hash("<p>Key 21626649</p>")
    assert _stable_dom_hash(first) != _stable_dom_hash(
        first.replace("Key bc68b4f8", "Client bc68b4f8")
    )


def test_stable_dom_hash_normalizes_deleted_key_only_with_exact_sidecar() -> None:
    first = "<p>Key 8510b924 was deleted</p>"
    second = "<p>Key a7147808 was deleted</p>"
    first_fingerprint = "sha256:8510b924b3c149ccf384874979778e485f2e7e943607fed3d7ceb5f08df9917a"
    second_fingerprint = "sha256:a7147808dac356228a81e531582a4d99aa537defe9135676523d1907883846de"

    assert _stable_dom_hash(first) != _stable_dom_hash(second)
    assert _stable_dom_hash(first, (first_fingerprint,)) == _stable_dom_hash(
        second,
        (second_fingerprint,),
    )
    with pytest.raises(AssertionError, match="invalid fingerprint sidecar"):
        _ = _stable_dom_hash(first, ("sha256:not-a-fingerprint",))


def test_stable_dom_hash_requires_ready_ordinary_and_native_key_sidecars() -> None:
    first = "<p>Key bc68b4f8 ready; Key 076a62a2 and Key 8510b924 deleted</p>"
    second = "<p>Key 21626649 ready; Key 605819a4 and Key a7147808 deleted</p>"
    first_fingerprints = (
        "sha256:bc68b4f8747af2ee3fb6594604b190e2d33fe04d8227722eedcdbf9ee4beb1f5",
        "sha256:076a62a2c9d7040fef1288a4f11b4b34a6db44d5438b4b9621533b01418da7f7",
        "sha256:8510b924b3c149ccf384874979778e485f2e7e943607fed3d7ceb5f08df9917a",
    )
    second_fingerprints = (
        "sha256:216266498ce629954bbdd7c6d64677ff40c10f6f32bba49e54758f764b51c2cd",
        "sha256:605819a44e9def43c6025157176b0a4f5b359f3123ce25411f3bc2455d81a4e3",
        "sha256:a7147808dac356228a81e531582a4d99aa537defe9135676523d1907883846de",
    )

    assert _stable_dom_hash(first, first_fingerprints[::2]) != _stable_dom_hash(
        second,
        second_fingerprints[::2],
    )
    assert _stable_dom_hash(first, first_fingerprints) == _stable_dom_hash(
        second,
        second_fingerprints,
    )


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
