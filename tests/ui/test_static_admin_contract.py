import importlib.util
import re
from html.parser import HTMLParser
from typing import override

import pytest

from nvidia_build_lb.web.resources import WebResource, load_web_resource

pytestmark = pytest.mark.ui_fake

_NOTICE = "Independent operations tool; not affiliated with or endorsed by NVIDIA."
_RAW_COLOR = re.compile(
    r"#[0-9a-fA-F]{3,8}\b|(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch)\([^)]*\)"
)
_ROOT_BLOCK = re.compile(r":root\s*\{[^}]*\}", flags=re.DOTALL)
_SECRET_PATTERNS = (
    re.compile(r"nblb_(?:admin|ds)_[a-f0-9]{64}", re.IGNORECASE),
    re.compile(r"nvapi-[a-z0-9_-]{16,}", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{16,}"),
)


class _AdminDocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.captions: list[str] = []
        self.definition_terms: list[str] = []
        self.elements: list[tuple[tuple[str, ...], dict[str, str | None]]] = []
        self.navigation_labels: list[str] = []
        self.stack: list[str] = []

    @override
    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.stack.append(tag)
        self.elements.append((tuple(self.stack), dict(attrs)))

    @override
    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.elements.append(((*self.stack, tag), dict(attrs)))

    @override
    def handle_endtag(self, tag: str) -> None:
        assert self.stack.pop() == tag

    @override
    def handle_data(self, data: str) -> None:
        if self.stack and self.stack[-1] == "caption" and data.strip():
            self.captions.append(data.strip())
        if self.stack and self.stack[-1] == "dt" and data.strip():
            self.definition_terms.append(data.strip())
        if self.stack and self.stack[-1] == "a" and "nav" in self.stack and data.strip():
            self.navigation_labels.append(data.strip())


def _document(resource_name: str) -> tuple[str, _AdminDocumentParser]:
    document = load_web_resource(WebResource(resource_name))
    parser = _AdminDocumentParser()
    parser.feed(document)
    parser.close()
    assert parser.stack == []
    return document, parser


def _admin_document() -> tuple[str, _AdminDocumentParser]:
    return _document("admin-document")


def test_admin_shell_has_exact_external_assets_and_semantic_identity() -> None:
    # Given: the complete unauthenticated administration shell.
    document, parsed = _admin_document()

    # When: executable surfaces, landmarks, and identity content are inspected.
    attributes = [name for _, attrs in parsed.elements for name in attrs]
    links = [attrs for path, attrs in parsed.elements if path == ("html", "head", "link")]
    scripts = [attrs for path, attrs in parsed.elements if path == ("html", "head", "script")]
    paths = {path for path, _ in parsed.elements}
    root_attributes = next(attrs for path, attrs in parsed.elements if path == ("html",))

    # Then: only the exact same-origin stylesheet and script execute.
    assert root_attributes == {"lang": "en"}
    assert _NOTICE in document
    assert links == [
        {"href": "/assets/favicon.svg", "rel": "icon", "type": "image/svg+xml"},
        {"href": "/assets/admin.css", "rel": "stylesheet"},
    ]
    assert scripts == [{"defer": "defer", "src": "/assets/admin.js", "type": "module"}]
    assert ("html", "body", "a") in paths
    assert ("html", "body", "div", "aside", "details", "nav") in paths
    assert ("html", "body", "div", "main") in paths
    assert "<style" not in document.lower()
    assert not any(name.startswith("on") or name == "style" for name in attributes)
    assert all(tag not in document.lower() for tag in ("<img", "<svg", "<iframe"))


def test_admin_login_is_server_rendered_and_fails_closed_until_module_init() -> None:
    document, parsed = _admin_document()
    script = load_web_resource(WebResource("admin-script"))
    login_form = next(
        attrs
        for path, attrs in parsed.elements
        if path[-1] == "form" and attrs.get("id") == "login-form"
    )
    bearer = next(
        attrs
        for path, attrs in parsed.elements
        if path[-1] == "input" and attrs.get("id") == "admin-bearer"
    )
    submit = next(
        attrs
        for path, attrs in parsed.elements
        if path[-1] == "button" and attrs.get("id") == "login-submit"
    )

    assert login_form == {
        "class": "login-form",
        "id": "login-form",
        "novalidate": "novalidate",
    }
    assert 'id="login-template"' not in document
    assert bearer["disabled"] == "disabled"
    assert "name" not in bearer
    assert submit["disabled"] == "disabled"
    assert 'const loginTemplate = byId("login-form").cloneNode(true);' in script
    assert 'byId("login-slot").replaceChildren(loginTemplate.cloneNode(true));' in script
    assert "field.disabled = false;" in script
    assert "submit.disabled = false;" in script


def test_admin_shell_contains_native_secret_and_destructive_workflows() -> None:
    # Given: the static shell and its inert templates and dialogs.
    document, parsed = _admin_document()

    # When: sensitive inputs and action surfaces are located.
    password_inputs = [
        attrs
        for path, attrs in parsed.elements
        if path[-1] == "input" and attrs.get("type") == "password"
    ]
    dialogs = {attrs["id"] for path, attrs in parsed.elements if path[-1] == "dialog"}
    credential = next(
        attrs
        for path, attrs in parsed.elements
        if path[-1] == "textarea" and attrs.get("id") == "one-time-token"
    )
    credential_id = next(
        attrs
        for path, attrs in parsed.elements
        if path[-1] == "input" and attrs.get("id") == "credential-id"
    )

    # Then: entry is password-only and every owner journey has a native semantic surface.
    assert {attrs["id"] for attrs in password_inputs} == {
        "admin-bearer",
        "upstream-key",
    }
    assert all("value" not in attrs for attrs in password_inputs)
    assert credential["readonly"] == "readonly"
    assert credential["rows"] == "3"
    assert "role" not in credential
    assert "aria-readonly" not in credential
    assert credential_id["readonly"] == "readonly"
    assert credential_id["aria-describedby"] == "credential-id-help"
    assert "Store this Internal ID with the client record" in document
    assert "Hermes token-ID prompt" not in document
    assert dialogs == {
        "upstream-dialog",
        "confirm-dialog",
        "downstream-dialog",
        "credential-dialog",
    }
    assert set(parsed.captions) == {
        "Upstream key routing, health, and actions",
        "Downstream client access, state, and actions",
        "Recent events",
    }
    assert any(path[-2:] == ("fieldset", "legend") for path, _ in parsed.elements)
    button_ids = {attrs.get("id") for path, attrs in parsed.elements if path[-1] == "button"}
    assert {"logout", "refresh-dashboard"} <= button_ids
    assert all(attrs.get("id") != "clipboard-recovery" for _, attrs in parsed.elements)


def test_first_client_credential_requires_two_registered_eligible_keys() -> None:
    script = load_web_resource(WebResource("admin-script"))

    assert "snapshot.upstreams.length !== 2" in script
    assert "snapshot.overview.upstream_keys.eligible !== 2" in script
    assert "upstreams.length === 2 && overview.upstream_keys.eligible === 2" in script
    assert "2 registered · ${overview.upstream_keys.eligible} eligible" not in script
    assert (
        "${overview.upstream_keys.total} registered · ${overview.upstream_keys.eligible} eligible"
        in script
    )
    assert "items.length === 2 && !replacementContext" in script
    assert 'createButton("Replace", `key-${item.id}-replace`' in script
    assert "if (upstreams.length > 2 && !replacementContext)" in script
    assert 'setControlPrerequisite("add-upstream", "add-upstream-reason", message)' in script
    assert "else if (snapshot.upstreams.length > 2)" in script
    assert 'setRecommendation("upstreams", "Review extra upstream keys")' in script
    assert script.index("reconcileReplacementContext(snapshot.upstreams)") < script.index(
        "renderUpstreams(snapshot.upstreams, reference)"
    )


def test_global_error_reads_action_before_result_and_then_evidence() -> None:
    document, _ = _admin_document()
    error = document[
        document.index('id="global-error"') : document.index(
            "</div>", document.index('id="global-error"')
        )
    ]

    assert error.index('id="global-error-state"') < error.index('id="global-error-message"')
    assert error.index('id="global-error-message"') < error.index(
        'id="global-unconfirmed-operation"'
    )
    assert error.index('id="global-unconfirmed-operation"') < error.index('id="retry-dashboard"')
    assert error.index('id="retry-dashboard"') < error.index('id="global-confirmed-result"')
    assert error.index('id="global-confirmed-result"') < error.index('id="global-error-evidence"')


def test_initial_service_outage_is_not_rendered_as_authentication_failure() -> None:
    document, _ = _admin_document()
    script = load_web_resource(WebResource("admin-script"))
    login = script[
        script.index("async function login") : script.index("async function refreshDashboard")
    ]

    assert 'id="login-error"' in document
    assert 'id="login-offline"' in document
    assert "Authentication failed." in document
    assert "Service unavailable." in document
    assert 'const result = await api("/dashboard", {}, "dashboard", readDeadlineMs)' in login
    assert "mountLoginWithRecovery(true, false)" in login
    assert 'showProblem({code: "offline"' in login
    assert '"Loading administration state", true)' in login
    assert "mountLoginWithRecovery(false, true)" not in login


def test_management_action_failures_preserve_details_and_force_401_reauthentication() -> None:
    script = load_web_resource(WebResource("admin-script"))
    show_problem = script[script.index("function showProblem") : script.index("async function api")]
    clear_sensitive = script[
        script.index("function clearSensitiveUi") : script.index("function mountLogin")
    ]
    login_with_recovery = script[
        script.index("function mountLoginWithRecovery") : script.index("async function login")
    ]

    assert 'setText("global-error-message", message)' in show_problem
    assert 'byId("global-error").hidden = false' in show_problem
    assert (
        'markSnapshotStale("The last request did not confirm current administration state.")'
        in show_problem
    )
    assert "Success was not assumed" in show_problem
    assert "resetSessionController()" in clear_sensitive
    assert 'document.querySelectorAll("dialog")' in clear_sensitive
    assert 'byId("one-time-token").value = ""' in clear_sensitive
    assert "activeMutationContext ?? unresolvedRecoveryContext()" in login_with_recovery
    assert "mountLogin(authFailed, serviceOffline, context, orphanRecoveryMessage())" in (
        login_with_recovery
    )
    for start, end in (
        ("async function runKeyAction", "function openConfirmation"),
        ("async function confirmPendingAction", "async function submitUpstream"),
        ("async function submitUpstream", "async function submitDownstream"),
        ("async function submitDownstream", "async function copyToken"),
    ):
        consumer = script[script.index(start) : script.index(end)]
        assert "result.status === 401" in consumer
        assert "mountLoginWithRecovery(true, false, context)" in consumer


def test_admin_static_sources_have_no_embedded_secret_or_unsafe_sink() -> None:
    # Given: all three production browser resources.
    resources = tuple(
        load_web_resource(WebResource(name))
        for name in ("admin-document", "admin-stylesheet", "admin-script")
    )
    corpus = "\n".join(resources)

    # When: credential shapes and browser persistence or injection sinks are searched.
    secret_hits = [pattern.pattern for pattern in _SECRET_PATTERNS if pattern.search(corpus)]
    forbidden_javascript = (
        "localStorage",
        "sessionStorage",
        "document.cookie",
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "console.",
        "eval(",
        "new Function",
    )

    # Then: the surface is secret-free, in-memory only, and uses safe DOM construction.
    assert secret_hits == []
    assert not any(token in resources[2] for token in forbidden_javascript)
    assert "textContent" in resources[2]
    assert "Authorization" in resources[2]
    assert "AbortController" in resources[2]


def test_desktop_shell_and_locked_information_order_are_explicit_in_both_documents() -> None:
    # Given: the administration and showcase documents before CSS layout is applied.
    _, admin = _admin_document()
    showcase_document, showcase = _document("showcase-document")

    # When: rail containment and operator-facing ordered labels are projected.
    admin_paths = {path for path, _ in admin.elements}
    showcase_paths = {path for path, _ in showcase.elements}

    # Then: both use the rail shell and expose only their exact ordered information sets.
    assert ("html", "body", "div", "aside", "header") in admin_paths
    assert ("html", "body", "div", "aside", "details", "nav") in admin_paths
    assert ("html", "body", "div", "main") in admin_paths
    assert ("html", "body", "div", "aside", "header") in showcase_paths
    assert ("html", "body", "div", "aside", "details", "nav") in showcase_paths
    assert ("html", "body", "div", "main") in showcase_paths
    assert admin.definition_terms == [
        "Gateway readiness",
        "Eligible keys",
        "Cooling keys",
        "Logical requests",
        "Last event/freshness",
    ]
    assert showcase.navigation_labels == [
        "Buttons",
        "Inputs",
        "Statuses",
        "Tables",
        "Dialogs",
        "System states",
    ]
    assert "Overview" not in showcase.navigation_labels
    assert "summary-grid" not in showcase_document


def test_admin_navigation_and_main_order_match_the_operator_contract() -> None:
    # Given: the current rail-based administration shell.
    document, parsed = _admin_document()

    # When: navigation containment and direct main order are inspected.
    logout_paths = [path for path, attrs in parsed.elements if attrs.get("id") == "logout"]
    navigation_attributes = [attrs for path, attrs in parsed.elements if path[-2:] == ("nav", "a")]
    section_headings = re.findall(r'<h2[^>]*id="([^"]+)"[^>]*>([^<]+)</h2>', document)
    dashboard = document[document.index('<section hidden="hidden" id="dashboard"') :]

    # Then: logout belongs to nav and refresh follows the current Overview status.
    assert logout_paths == [("html", "body", "div", "aside", "details", "nav", "button")]
    assert parsed.navigation_labels == [
        "Overview",
        "Upstream keys",
        "Downstream tokens",
        "Events",
    ]
    assert navigation_attributes[0]["aria-current"] == "location"
    assert all("aria-current" not in attrs for attrs in navigation_attributes[1:])
    assert section_headings[:4] == [
        ("overview-heading", "Overview"),
        ("upstream-heading", "Upstream keys"),
        ("downstream-heading", "Downstream tokens"),
        ("events-heading", "Events"),
    ]
    assert dashboard.index('id="overview"') < dashboard.index('id="refresh-dashboard"')


@pytest.mark.parametrize("resource_name", ["admin-stylesheet", "showcase-stylesheet"])
def test_desktop_styles_lock_rail_main_grid_gutters_and_inset(resource_name: str) -> None:
    # Given: one complete route stylesheet.
    stylesheet = load_web_resource(WebResource(resource_name))

    # When: the unzoomed desktop geometry declarations are inspected.
    desktop_contract = (
        "grid-template-columns: 224px minmax(0, 1fr)",
        "grid-template-columns: repeat(12, minmax(0, 1fr))",
        "column-gap: var(--space-6)",
        "padding-inline: var(--space-8)",
    )

    # Then: rail width, 12 tracks, 24px token gutter, and 32px token inset are explicit.
    assert all(declaration in stylesheet for declaration in desktop_contract)


def test_desktop_internal_grid_counts_match_the_locked_route_contracts() -> None:
    # Given: exact admin and showcase stylesheets.
    admin = load_web_resource(WebResource("admin-stylesheet"))
    showcase = load_web_resource(WebResource("showcase-stylesheet"))

    # When/Then: the five-cell Overview and showcase 3/4/3 grids are explicit.
    assert ".summary-grid { grid-template-columns: repeat(5, minmax(0, 1fr)); }" in admin
    assert ".specimen-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }" in showcase
    assert ".status-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }" in showcase
    assert ".state-sections { grid-template-columns: repeat(3, minmax(0, 1fr)); }" in showcase


def test_admin_mobile_table_caption_owns_the_full_reflow_width() -> None:
    # Given: tables switch their structural boxes to block layout below 768px.
    stylesheet = load_web_resource(WebResource("admin-stylesheet"))
    mobile_rules = stylesheet.split("@media (max-width: 767px) {", maxsplit=1)[1].split(
        "@media (min-width: 768px)", maxsplit=1
    )[0]

    # When/Then: the caption also becomes a full-width block instead of a table-cell sliver.
    caption_rule = re.search(r"caption\s*\{([^}]*)\}", mobile_rules)
    assert caption_rule is not None
    assert "display: block" in caption_rule.group(1)
    assert "inline-size: 100%" in caption_rule.group(1)


def test_admin_human_labels_and_machine_ids_have_separate_wrapping_paths() -> None:
    script = load_web_resource(WebResource("admin-script"))
    stylesheet = load_web_resource(WebResource("admin-stylesheet"))

    assert 'className = "human-label"' in script
    assert 'className = "machine-id"' in script
    for term in (
        "Internal ID",
        "Fingerprint",
        "Cooldown until",
        "Requests",
        "Succeeded",
        "Failed",
        "Last used",
        "Updated",
        "Created",
        "Revoked",
    ):
        assert f'"{term}"' in script
    assert "machineEvidenceTerms.has(term)" in script
    assert ".human-label" in stylesheet
    assert ".machine-id" in stylesheet
    assert "body { margin: 0;" in stylesheet
    global_breaking = (
        "body { margin: 0; min-block-size: 100dvh; background: var(--surface-canvas); "
        "font-size: 15px; line-height: 1.55; overflow-wrap: anywhere; }"
    )
    assert global_breaking not in stylesheet


def test_desktop_event_rows_bound_random_opaque_identifier_reflow() -> None:
    # Given: production event IDs are opaque and vary on every fresh database.
    stylesheet = load_web_resource(WebResource("admin-stylesheet"))
    desktop = stylesheet.split("@media (min-width: 1280px) {", maxsplit=1)[1].split(
        "@media (prefers-reduced-motion", maxsplit=1
    )[0]

    # When/Then: desktop event rows reserve one tokenized maximum-content block.
    assert "--event-row-desktop-block: 96px;" in stylesheet
    assert "#events tbody tr { block-size: var(--event-row-desktop-block); }" in desktop


def test_programmatic_focus_targets_use_the_design_focus_token() -> None:
    stylesheet = load_web_resource(WebResource("admin-stylesheet"))

    focus_rule = next(
        selector
        for selector in stylesheet.split("{")
        if '[tabindex="-1"]:focus-visible' in selector
    )
    assert "--focus-ring" in stylesheet
    assert '[tabindex="-1"]:focus-visible' in focus_rule


def test_tablet_admin_tables_keep_comparison_and_mobile_reflows() -> None:
    stylesheet = load_web_resource(WebResource("admin-stylesheet"))

    tablet = stylesheet.split("@media (max-width: 1279px)", maxsplit=1)[1].split(
        "@media (max-width: 767px)", maxsplit=1
    )[0]
    mobile = stylesheet.split("@media (max-width: 767px)", maxsplit=1)[1].split(
        "@media (min-width: 768px)", maxsplit=1
    )[0]
    assert "table, thead, tbody, tr, th, td { display: block; }" not in tablet
    assert "table, thead, tbody, tr, th, td { display: block; }" in mobile


@pytest.mark.parametrize("resource_name", ["admin-stylesheet", "showcase-stylesheet"])
def test_browser_styles_use_only_design_tokens_after_root_declaration(resource_name: str) -> None:
    # Given: one exact external browser stylesheet.
    stylesheet = load_web_resource(WebResource(resource_name))

    # When: its sole token block is removed from the later rules.
    token_match = _ROOT_BLOCK.search(stylesheet)
    assert token_match is not None
    later_rules = stylesheet.replace(token_match.group(0), "", 1)

    # Then: raw colors stay in the token declaration and required adaptations are explicit.
    assert _ROOT_BLOCK.search(stylesheet, token_match.end()) is None
    assert _RAW_COLOR.search(token_match.group(0)) is not None
    assert _RAW_COLOR.search(later_rules) is None
    assert "@media (max-width: 767px)" in stylesheet
    assert "@media (min-width: 1280px)" in stylesheet
    assert "prefers-reduced-motion: reduce" in stylesheet
    assert "forced-colors: active" in stylesheet
    assert "word-break: keep-all" in stylesheet
    assert "overflow-wrap: anywhere" in stylesheet
    assert "box-shadow" not in stylesheet
    assert "gradient(" not in stylesheet


def test_admin_resource_router_module_exists_before_composition() -> None:
    # Given: Todo 4 owns a UI-only router seam, not the application root.
    module_name = "nvidia_build_lb.web.admin_resources"

    # When: import metadata is resolved without importing the composition root.
    specification = importlib.util.find_spec(module_name)

    # Then: the resource-only router module is available for Todo 5 composition.
    assert specification is not None
