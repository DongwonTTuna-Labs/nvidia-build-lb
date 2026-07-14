import re

from fastapi.testclient import TestClient

_RAW_COLOR = re.compile(
    r"#[0-9a-fA-F]{3,8}\b|(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch)\([^)]*\)"
)
_ROOT_BLOCK = re.compile(r":root\s*\{[^}]*\}", flags=re.DOTALL)
_REQUIRED_STATE_SELECTORS = {
    '[data-state="hover"]',
    '[data-state="focus"]',
    '[data-state="active"]',
    '[data-state="disabled"]',
    '[data-state="loading"]',
    '[data-state="empty"]',
    '[data-state="error"]',
}


def _stylesheet(showcase_client: TestClient) -> str:
    return showcase_client.get("/assets/showcase.css").text


def _declaration_blocks(stylesheet: str, selector: str) -> tuple[str, ...]:
    matches: list[str] = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", stylesheet):
        selector_list = match.group(1)
        if selector in {item.strip() for item in selector_list.split(",")}:
            matches.append(match.group(2))
    assert matches
    return tuple(matches)


def test_raw_colors_exist_only_in_one_root_token_block_when_styles_are_served(
    showcase_client: TestClient,
) -> None:
    # Given: the external showcase stylesheet.
    stylesheet = _stylesheet(showcase_client)

    # When: the single root declaration block is separated from later rules.
    token_match = _ROOT_BLOCK.search(stylesheet)
    assert token_match is not None
    assert _ROOT_BLOCK.search(stylesheet, token_match.end()) is None
    token_block = token_match.group(0)
    later_rules = stylesheet.replace(token_block, "", 1)

    # Then: raw colors are declared as tokens once and every use after that is indirect.
    assert _RAW_COLOR.search(token_block) is not None
    assert _RAW_COLOR.search(later_rules) is None
    assert "var(--" in later_rules


def test_stylesheet_has_explicit_visual_rules_for_required_states(
    showcase_client: TestClient,
) -> None:
    # Given: the state specimens in the external stylesheet.
    stylesheet = _stylesheet(showcase_client)

    # When: required state selectors are enumerated.
    present = {selector for selector in _REQUIRED_STATE_SELECTORS if selector in stylesheet}

    # Then: every state has an explicit visual treatment and reduced motion is honored.
    assert present == _REQUIRED_STATE_SELECTORS
    assert "prefers-reduced-motion: reduce" in stylesheet
    assert ":focus-visible" in stylesheet


def test_live_controls_use_locked_hover_and_pressed_feedback_when_interacted(
    showcase_client: TestClient,
) -> None:
    # Given: real enabled controls and the separate static state specimens.
    stylesheet = _stylesheet(showcase_client)

    # When: pointer interaction and demo-state declarations are inspected.
    hover = next(
        block
        for block in _declaration_blocks(stylesheet, ".control:not(:disabled):hover")
        if "--surface-hover" in block
    )
    active = next(
        block
        for block in _declaration_blocks(stylesheet, ".control:not(:disabled):active")
        if "--surface-pressed" in block
    )
    demo_hover = next(
        block
        for block in _declaration_blocks(stylesheet, '.control[data-state="hover"]')
        if "--surface-hover" in block
    )
    demo_active = next(
        block
        for block in _declaration_blocks(stylesheet, '.control[data-state="active"]')
        if "--surface-pressed" in block
    )
    no_preference_active = next(
        block
        for block in _declaration_blocks(stylesheet, ".control:not(:disabled):active")
        if "translateY(1px)" in block
    )

    # Then: live pointer states use the same locked tokens without replacing the harness states.
    assert "background: var(--surface-hover)" in hover
    assert "border-color: var(--signal-hover)" in hover
    assert "background: var(--surface-pressed)" in active
    assert "border-color: var(--signal-active)" in active
    assert "transform: translateY(1px)" in no_preference_active
    assert "background: var(--surface-hover)" in demo_hover
    assert "background: var(--surface-pressed)" in demo_active


def test_reduced_motion_removes_the_live_pressed_transform_when_requested(
    showcase_client: TestClient,
) -> None:
    # Given: the reduced-motion media query.
    stylesheet = _stylesheet(showcase_client)

    # When: motion-bearing and reduced-motion blocks are inspected.
    no_preference = stylesheet.split("@media (prefers-reduced-motion: no-preference)", maxsplit=1)[
        1
    ].split("@media (prefers-reduced-motion: reduce)", maxsplit=1)[0]
    reduced_motion = stylesheet.split("@media (prefers-reduced-motion: reduce)", maxsplit=1)[1]

    # Then: transforms exist only for operators who did not request reduced motion.
    assert ".control:not(:disabled):active" in no_preference
    assert '.control[data-state="active"]' in no_preference
    assert "transform: translateY(1px)" in no_preference
    assert "transform:" not in reduced_motion


def test_mobile_showcase_captions_span_the_table_surface(
    showcase_client: TestClient,
) -> None:
    stylesheet = _stylesheet(showcase_client)
    mobile = stylesheet.split("@media (max-width: 767px)", maxsplit=1)[1].split(
        "@media (min-width: 768px)", maxsplit=1
    )[0]

    caption = re.search(r"caption\s*\{([^}]*)\}", mobile)

    assert caption is not None
    assert "display: block" in caption.group(1)
    assert "inline-size: 100%" in caption.group(1)


def test_korean_copy_uses_semantic_wrapping_without_cjk_tracking(
    showcase_client: TestClient,
) -> None:
    # Given: explicitly Korean prose in the showcase.
    stylesheet = _stylesheet(showcase_client)

    # When: the locale-specific declarations are read.
    korean = _declaration_blocks(stylesheet, '[lang="ko"]')[0]

    # Then: word groups stay together, emergency wrapping remains safe, and CJK is untracked.
    assert "word-break: keep-all" in korean
    assert "overflow-wrap: anywhere" in korean
    assert "letter-spacing" not in korean
    assert "block-size" not in korean
    assert "overflow: hidden" not in korean


def test_native_navigation_disclosure_reflows_without_horizontal_scrolling(
    showcase_client: TestClient,
) -> None:
    # Given: the single responsive native navigation disclosure.
    stylesheet = _stylesheet(showcase_client)

    # When: summary target and navigation layout declarations are inspected.
    summary = next(
        block
        for block in _declaration_blocks(stylesheet, ".navigation-disclosure > summary")
        if "min-block-size" in block
    )
    navigation = _declaration_blocks(stylesheet, "nav")[0]
    link = _declaration_blocks(stylesheet, "nav a")[0]

    # Then: the disclosure is touch-sized and its links wrap instead of becoming a local scroller.
    assert "min-block-size: 44px" in summary
    assert "flex-wrap: wrap" in navigation
    assert "overflow-x: auto" not in navigation
    assert "white-space: nowrap" not in link


def test_stylesheet_loads_no_external_or_embedded_asset_when_served(
    showcase_client: TestClient,
) -> None:
    # Given: the complete static stylesheet.
    lowered = _stylesheet(showcase_client).lower()

    # When: network, font, imagery, and forbidden depth primitives are searched.
    forbidden = (
        "@import",
        "@font-face",
        "url(",
        "data:",
        "http://",
        "https://",
        "gradient(",
        "box-shadow",
        "filter:",
        "animation:",
    )

    # Then: the stylesheet remains code-native, same-origin, and static.
    assert not any(token in lowered for token in forbidden)
