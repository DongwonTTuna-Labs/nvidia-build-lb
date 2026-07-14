import re
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient

_NOTICE = (
    "NVIDIA Build LB is an independent operations tool. It is not affiliated with, "
    "endorsed by, or sponsored by NVIDIA."
)
_PRIMITIVES = {"button", "input", "status", "table", "dialog", "loading", "empty", "error"}
_STATES = {
    "Default",
    "Hover",
    "Focus",
    "Active",
    "Disabled",
    "Loading",
    "Empty",
    "Error",
    "Healthy",
    "Degraded",
    "Cooldown",
    "Stale",
    "Failed",
    "No data",
    "Revoked",
}
_SECRET_PATTERNS = (
    re.compile(r"nblb_(?:admin|ds)_[a-f0-9]{64}", re.IGNORECASE),
    re.compile(r"nvapi-[a-z0-9_-]{16,}", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{16,}"),
    re.compile(r"\b[a-f0-9]{64}\b", re.IGNORECASE),
)


def _document(showcase_client: TestClient) -> tuple[str, ET.Element]:
    response = showcase_client.get("/showcase")
    document = response.text
    static_markup = document.removeprefix("<!doctype html>\n")
    return document, ET.fromstring(static_markup)  # noqa: S314 - app-owned static test fixture.


def test_document_uses_semantic_landmarks_when_rendered(showcase_client: TestClient) -> None:
    # Given: the rendered showcase document.
    # When: its semantic tree is parsed.
    _, root = _document(showcase_client)

    # Then: the current rail contract defines the exact reading-order containers.
    assert root.tag == "html"
    assert root.attrib["lang"] == "en"
    body = root.find("body")
    assert body is not None
    assert [child.tag for child in body] == ["a", "div", "footer"]
    assert body[0].attrib == {"class": "skip-link", "href": "#main-content"}
    shell = root.find("body/div[@class='shell']")
    assert shell is not None
    assert [child.tag for child in shell] == ["aside", "main"]
    assert shell.find("aside/header") is not None
    assert shell.find("aside/details/nav") is not None
    assert shell.find("main") is not None
    assert root.find("body/footer") is not None


def test_document_uses_one_native_disclosure_for_navigation_when_rendered(
    showcase_client: TestClient,
) -> None:
    # Given: the responsive showcase navigation.
    _, root = _document(showcase_client)

    # When: disclosure and navigation nodes are enumerated.
    disclosures = root.findall("body/div/aside/details")
    navigations = root.findall(".//nav")

    # Then: one user-toggleable native disclosure owns the only navigation landmark.
    assert len(disclosures) == 1
    assert len(navigations) == 1
    disclosure = disclosures[0]
    assert disclosure.attrib == {"class": "navigation-disclosure", "open": "open"}
    summary = disclosure.find("summary")
    assert summary is not None
    assert "".join(summary.itertext()).strip() == "Showcase sections"
    assert disclosure.find("nav") is navigations[0]
    assert navigations[0].attrib == {"aria-label": "Showcase sections"}
    links = navigations[0].findall("a")
    assert [("".join(link.itertext()).strip(), link.attrib["href"]) for link in links] == [
        ("Buttons", "#buttons"),
        ("Inputs", "#inputs"),
        ("Statuses", "#statuses"),
        ("Tables", "#tables"),
        ("Dialogs", "#dialogs"),
        ("System states", "#system-states"),
    ]
    assert len({link.attrib["href"] for link in links}) == len(links)
    assert all(root.find(f".//*[@id='{link.attrib['href'][1:]}']") is not None for link in links)


def test_document_contains_meaningful_korean_operator_copy_when_rendered(
    showcase_client: TestClient,
) -> None:
    # Given: the secret-free bilingual wrapping specimen.
    text, root = _document(showcase_client)

    # When: explicitly Korean content is located.
    korean_elements = [element for element in root.iter() if element.attrib.get("lang") == "ko"]

    # Then: visible operator prose, not filler, exercises the CJK rendering boundary.
    assert len(korean_elements) == 1
    korean_text = " ".join("".join(element.itertext()).strip() for element in korean_elements)
    assert "두 키가 모두 사용 가능한 상태입니다." in korean_text
    assert "요청이 실패하면" in korean_text
    assert sum("\uac00" <= character <= "\ud7a3" for character in korean_text) >= 60
    assert '<section class="locale-sample" lang="ko"' in text


def test_document_names_every_primitive_and_required_state_when_rendered(
    showcase_client: TestClient,
) -> None:
    # Given: the showcase contract's primitive and state vocabulary.
    text, root = _document(showcase_client)

    # When: visible specimens are enumerated.
    primitive_names = {
        section.attrib["data-primitive"] for section in root.findall(".//section[@data-primitive]")
    }

    # Then: every primitive and state is visibly named, not conveyed by color alone.
    assert primitive_names == _PRIMITIVES
    for state in _STATES:
        assert f"State: {state}" in text


def test_document_contains_notice_tables_and_copyright_boundary_when_rendered(
    showcase_client: TestClient,
) -> None:
    # Given: the operator-facing showcase.
    text, root = _document(showcase_client)

    # When: identity and table anatomy are inspected.
    captions = {"".join(caption.itertext()).strip() for caption in root.findall(".//caption")}

    # Then: affiliation is disclaimed and all data examples are explicitly synthetic.
    assert _NOTICE in text
    assert "Copyright 2026 DongwonTTuna-Labs" in text
    assert captions == {
        "Synthetic upstream keys",
        "Synthetic downstream tokens",
        "Synthetic events",
    }
    assert "Synthetic non-secret examples" in text


def test_document_has_no_inline_executable_or_brand_asset_when_rendered(
    showcase_client: TestClient,
) -> None:
    # Given: the raw static document.
    text, root = _document(showcase_client)
    lowered = text.lower()

    # When: executable, embedded, and brand-asset surfaces are searched.
    attributes = [attribute for element in root.iter() for attribute in element.attrib]

    # Then: only the external same-origin stylesheet provides presentation.
    assert "<style" not in lowered
    assert "<script" not in lowered
    assert "<svg" not in lowered
    assert "<img" not in lowered
    assert "logo" not in lowered
    assert "style" not in attributes
    assert not any(attribute.startswith("on") for attribute in attributes)
    links = root.findall("head/link")
    assert [link.attrib for link in links] == [
        {"href": "/showcase", "rel": "icon"},
        {"href": "/assets/showcase.css", "rel": "stylesheet"},
    ]


def test_document_contains_no_secret_shaped_text_when_rendered(showcase_client: TestClient) -> None:
    # Given: all response headers and body text from the unauthenticated showcase.
    response = showcase_client.get("/showcase")
    corpus = str(response.headers) + response.text

    # When: known credential shapes are searched.
    matches = [pattern.pattern for pattern in _SECRET_PATTERNS if pattern.search(corpus)]

    # Then: the static response is safe to save as evidence.
    assert matches == []
    assert "/opt/" not in corpus
    assert "vault_master_key" not in corpus
