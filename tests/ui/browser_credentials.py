import json
from typing import ClassVar, Final, Protocol

from playwright.sync_api import Page
from pydantic import BaseModel, ConfigDict

from .browser_checks import BrowserAssertionError, evaluate_string


class _StrictModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class SecretAbsenceObservation(_StrictModel):
    rendered_text_absent: bool
    serialized_dom_absent: bool
    attribute_values_absent: bool
    form_values_absent: bool
    fragments_absent: bool


class CredentialStateObservation(_StrictModel):
    admin_bearer_present: bool
    one_time_token_present: bool
    copied_credential: bool


class _ArgumentEvaluator(Protocol):
    def evaluate(self, expression: str, arg: str) -> str: ...


_CREDENTIAL_ABSENCE_SCRIPT: Final = """(queryJson) => {
  const query = JSON.parse(queryJson);
  const rendered = document.body.innerText;
  const serialized = document.documentElement.outerHTML;
  const attributes = [...document.querySelectorAll("*")].flatMap(
    (element) => [...element.attributes].map((attribute) => attribute.value)
  ).join("\\u0000");
  const forms = [...document.querySelectorAll("input, textarea, select")].map(
    (control) => control.value
  ).join("\\u0000");
  const corpora = [rendered, serialized, attributes, forms];
  return JSON.stringify({
    rendered_text_absent: !rendered.includes(query.secret),
    serialized_dom_absent: !serialized.includes(query.secret),
    attribute_values_absent: !attributes.includes(query.secret),
    form_values_absent: !forms.includes(query.secret),
    fragments_absent: query.fragments.every(
      (fragment) => corpora.every((corpus) => !corpus.includes(fragment))
    )
  });
}"""


def _evaluate_with_argument(
    page: _ArgumentEvaluator,
    expression: str,
    argument: str,
) -> str:
    return page.evaluate(expression, argument)


def credential_state_observation(page: Page) -> CredentialStateObservation:
    result = evaluate_string(
        page,
        "async () => JSON.stringify((await import('/assets/admin.js')).credentialState())",
    )
    return CredentialStateObservation.model_validate_json(result)


def assert_secret_absent(page: _ArgumentEvaluator, secret: str) -> None:
    fragment_size = min(12, max(4, len(secret) // 3))
    midpoint = len(secret) // 2
    fragments = {
        secret[:fragment_size],
        secret[midpoint - (fragment_size // 2) : midpoint + (fragment_size // 2)],
        secret[-fragment_size:],
    }
    query = {"secret": secret, "fragments": sorted(fragments)}
    result = _evaluate_with_argument(page, _CREDENTIAL_ABSENCE_SCRIPT, json.dumps(query))
    observation = SecretAbsenceObservation.model_validate_json(result)
    observed = (
        ("rendered_text_absent", observation.rendered_text_absent),
        ("serialized_dom_absent", observation.serialized_dom_absent),
        ("attribute_values_absent", observation.attribute_values_absent),
        ("form_values_absent", observation.form_values_absent),
        ("fragments_absent", observation.fragments_absent),
    )
    failed_surfaces = tuple(name for name, absent in observed if not absent)
    if failed_surfaces:
        surfaces = ",".join(failed_surfaces)
        reason = f"credential remained in browser state surfaces: {surfaces}"
        raise BrowserAssertionError(reason)
