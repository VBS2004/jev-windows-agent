from __future__ import annotations

import json

import httpx
import pytest

from jev_windows_agent import ActionKind, DesktopElement, DesktopSnapshot, Subtask, TerminalKind
from jev_windows_agent.policies import TypeSafeJevPolicy
from jev_windows_agent.policies.typesafe import OPENROUTER_DECISIONS_URL, OPENROUTER_JEV_MODEL, TYPESAFE_SYSTEM_ONE_URL


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "TYPESAFE_MODEL"):
        monkeypatch.delenv(name, raising=False)


def complete_everything(seen: list[httpx.Request]) -> httpx.MockTransport:
    """Answer every request with SUBTASK_COMPLETE, shaped like the Decisions API response."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        offered = json.loads(request.content)["questions"]["operation"]["criteria"]
        probabilities = {option: 0.0 for option in offered}
        probabilities["SUBTASK_COMPLETE"] = 1.0
        answer = {"type": "choice", "choice": "SUBTASK_COMPLETE", "probabilities": probabilities, "confidence": 0.9}
        return httpx.Response(200, json={"answers": {"operation": answer}, "model": "x", "provider": "TypeSafe"})

    return httpx.MockTransport(handler)


def decide(policy: TypeSafeJevPolicy):
    snapshot = DesktopSnapshot(
        application="Notepad",
        window="a.txt",
        revision="1",
        elements=(DesktopElement(id="save", role="Button", name="Save", actions=(ActionKind.CLICK,)),),
    )
    return policy.decide(subtask=Subtask(goal="Save", verification=("Saved",)), snapshot=snapshot, history=())


def test_via_openrouter_posts_the_decisions_request() -> None:
    seen: list[httpx.Request] = []
    policy = TypeSafeJevPolicy.via_openrouter(
        api_key="sk-or-v1-test", client=httpx.Client(transport=complete_everything(seen))
    )

    decision = decide(policy)

    assert decision.terminal == TerminalKind.SUBTASK_COMPLETE
    assert decision.confidence == 0.9
    (request,) = seen
    assert str(request.url) == OPENROUTER_DECISIONS_URL
    assert request.headers["Authorization"] == "Bearer sk-or-v1-test"
    body = json.loads(request.content)
    assert body["model"] == OPENROUTER_JEV_MODEL == "~typesafe/jev-latest"
    assert set(body) == {"model", "state", "questions"}


def test_via_openrouter_reads_the_openrouter_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-env")
    policy = TypeSafeJevPolicy.via_openrouter()
    assert (policy.api_key, policy.base_url) == ("sk-or-v1-env", OPENROUTER_DECISIONS_URL)


def test_via_openrouter_requires_a_key() -> None:
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        TypeSafeJevPolicy.via_openrouter()


def test_default_policy_falls_back_to_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    # Existing TypeSafeJevPolicy() call sites work with only an OpenRouter key set.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-env")
    policy = TypeSafeJevPolicy()
    assert policy.base_url == OPENROUTER_DECISIONS_URL
    assert policy.model == OPENROUTER_JEV_MODEL
    assert policy.api_key == "sk-or-v1-env"


def test_typesafe_key_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-env")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-direct")
    policy = TypeSafeJevPolicy()
    assert (policy.api_key, policy.base_url, policy.model) == ("ts-direct", TYPESAFE_SYSTEM_ONE_URL, "jev-latest")


def test_explicit_key_or_url_disables_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-env")
    assert TypeSafeJevPolicy(api_key="ts-explicit").base_url == TYPESAFE_SYSTEM_ONE_URL
    # An OpenRouter key is only ever sent to OpenRouter, never to a caller-chosen URL.
    with pytest.raises(ValueError):
        TypeSafeJevPolicy(base_url="https://proxy.example/v1/systemone")


def test_missing_keys_name_both_options() -> None:
    with pytest.raises(ValueError, match="TYPESAFE_API_KEY or OPENROUTER_API_KEY"):
        TypeSafeJevPolicy()


def test_an_openrouter_key_pasted_into_typesafe_api_key_still_routes_to_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A live bug: TYPESAFE_API_KEY left set to an OpenRouter key from earlier in a
    # shell session shadowed OPENROUTER_API_KEY and posted to api.typesafe.ai, which
    # can only ever answer that key with 401. Its unmistakable "sk-or-v1-" shape is
    # now caught even when it's the only key set.
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-or-v1-stray-in-typesafe-slot")
    policy = TypeSafeJevPolicy()
    assert policy.base_url == OPENROUTER_DECISIONS_URL
    assert policy.model == OPENROUTER_JEV_MODEL
    assert policy.api_key == "sk-or-v1-stray-in-typesafe-slot"


def test_a_real_openrouter_key_still_wins_over_a_misplaced_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-or-v1-stray-in-typesafe-slot")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-actual-openrouter-key")
    policy = TypeSafeJevPolicy()
    assert policy.api_key == "sk-or-v1-actual-openrouter-key"


def test_explicit_api_key_is_never_second_guessed_even_if_openrouter_shaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The shape check only applies to what's read from TYPESAFE_API_KEY; an explicit
    # api_key= is trusted as-is, matching test_explicit_key_or_url_disables_the_fallback.
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-or-v1-should-be-ignored")
    policy = TypeSafeJevPolicy(api_key="sk-or-v1-explicit-but-trusted")
    assert (policy.api_key, policy.base_url) == ("sk-or-v1-explicit-but-trusted", TYPESAFE_SYSTEM_ONE_URL)
