"""Keeping JEV requests inside the model's documented context length.

Live bug: in Spotify, after expanding the library and opening Liked Songs, the next
request came back HTTP 400 max_tokens_exceeded and the run died. Jev 1.13 takes 32k
tokens for the state plus the longest question (docs.typesafe.ai/models), and a busy
screen sends every element twice: in the state, and again as a click option.
"""

from __future__ import annotations

import json

import httpx
import pytest

from jev_windows_agent import ActionKind, DesktopElement, DesktopSnapshot, Subtask, TerminalKind
from jev_windows_agent.errors import JevProviderError
from jev_windows_agent.policies import TypeSafeJevPolicy
from jev_windows_agent.policies.typesafe import _request_fits, _trim_order

TASK = Subtask(goal="Play the Liked Songs playlist", verification=("Playback is running",))


def track(i: int, parent: str = "tracklist") -> DesktopElement:
    return DesktopElement(
        id=f"uia_{i:014x}", role="DataItem", name=f"Track {i} Some Song Title, Artist Name, Album Name 3:{i % 60:02d}",
        actions=(ActionKind.CLICK,), source="windows_uia", parent_id=parent,
    )


def now_playing() -> tuple[DesktopElement, ...]:
    return tuple(
        DesktopElement(id=f"np_{name}", role="Button", name=name, actions=(ActionKind.CLICK,),
                       source="windows_uia", parent_id="now_playing_bar")
        for name in ("Previous", "Play", "Next")
    )


def snapshot(elements: tuple[DesktopElement, ...]) -> DesktopSnapshot:
    return DesktopSnapshot(application="Spotify", window="Spotify", revision="r", elements=elements, context={})


def policy(transport: httpx.MockTransport | None = None) -> TypeSafeJevPolicy:
    client = httpx.Client(transport=transport) if transport else None
    return TypeSafeJevPolicy(api_key="test-key", client=client)


def shown(body: dict) -> list[str]:
    return [e["id"] for e in body["state"]["desktop"]["elements"]]


def test_a_screen_that_fits_is_sent_unchanged() -> None:
    elements = tuple(track(i) for i in range(20)) + now_playing()
    body, _ = policy()._fitted_request(TASK, snapshot(elements), ())
    assert shown(body) == [e.id for e in elements]
    assert "elements_omitted_for_context_budget" not in body["state"]["candidate_truncation"]


def test_an_oversized_screen_is_trimmed_to_fit_and_says_so() -> None:
    elements = tuple(track(i) for i in range(400)) + now_playing()
    full, _ = policy()._request(TASK, snapshot(elements), (), list(elements))
    assert not _request_fits(full)  # the reported failure: this would be HTTP 400

    body, maps = policy()._fitted_request(TASK, snapshot(elements), ())

    assert _request_fits(body)
    kept = shown(body)
    omitted = body["state"]["candidate_truncation"]["elements_omitted_for_context_budget"]
    assert omitted == len(elements) - len(kept) > 0
    # Kept elements stay in screen order, and every option offered is one that's shown.
    assert kept == [e.id for e in elements if e.id in set(kept)]
    assert set(maps["CLICK_target"]) <= set(kept)


def test_trimming_thins_a_long_list_instead_of_dropping_what_comes_last() -> None:
    # The play/pause bar comes after the track list in Spotify's tree; cutting a
    # prefix would drop exactly the control this task needs.
    elements = tuple(track(i) for i in range(400)) + now_playing()
    body, maps = policy()._fitted_request(TASK, snapshot(elements), ())
    kept = set(shown(body))
    assert {"np_Previous", "np_Play", "np_Next"} <= kept
    assert "np_Play" in maps["CLICK_target"]


def test_long_lists_give_up_items_round_robin() -> None:
    sidebar = tuple(track(i, parent="library") for i in range(30))
    tracks = tuple(track(100 + i, parent="tracklist") for i in range(30))
    elements = sidebar + tracks
    order = _trim_order(elements)
    first_ten = [elements[i].parent_id for i in order[:10]]
    # Both lists are represented from the start, alternating, not one list first.
    assert first_ten.count("library") == first_ten.count("tracklist") == 5


def test_current_state_outlives_anonymous_structure() -> None:
    focused = DesktopElement(id="search", role="Edit", name="", focused=True, source="windows_uia")
    anonymous = tuple(DesktopElement(id=f"pane_{i}", role="Pane", source="windows_uia") for i in range(5))
    labelled = DesktopElement(id="label", role="Text", name="Liked Songs", source="windows_uia")
    elements = anonymous + (labelled, focused)
    order = [elements[i].id for i in _trim_order(elements)]
    assert order[0] == "search"
    assert order[1] == "label"
    assert set(order[2:]) == {e.id for e in anonymous}


def complete(request: httpx.Request) -> httpx.Response:
    offered = json.loads(request.content)["questions"]["operation"]["criteria"]
    probabilities = {option: 0.0 for option in offered}
    probabilities["SUBTASK_COMPLETE"] = 1.0
    answer = {"type": "choice", "choice": "SUBTASK_COMPLETE", "probabilities": probabilities, "confidence": 0.9}
    return httpx.Response(200, json={"answers": {"operation": answer}})


def test_a_context_overflow_is_retried_once_with_a_smaller_request() -> None:
    sent: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(len(json.loads(request.content)["state"]["desktop"]["elements"]))
        if len(sent) == 1:
            detail = {"error": {"message": 'HTTP 400: {"detail":{"error_type":"max_tokens_exceeded"}}', "code": 400}}
            return httpx.Response(400, json=detail)
        return complete(request)

    elements = tuple(track(i) for i in range(20)) + now_playing()
    decision = policy(httpx.MockTransport(handler)).decide(subtask=TASK, snapshot=snapshot(elements), history=())

    assert decision.terminal == TerminalKind.SUBTASK_COMPLETE
    assert len(sent) == 2 and sent[1] < sent[0]


def test_other_provider_errors_are_not_retried_and_carry_the_providers_detail() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(401, json={"detail": {"error_type": "authentication_error"}})

    with pytest.raises(JevProviderError) as caught:
        policy(httpx.MockTransport(handler)).decide(subtask=TASK, snapshot=snapshot(now_playing()), history=())

    assert len(sent) == 1
    assert caught.value.status_code == 401
    assert "authentication_error" in str(caught.value)
    assert not caught.value.context_exceeded
    # Existing callers catch RuntimeError; the richer error must still be one.
    assert isinstance(caught.value, RuntimeError)


def test_the_api_key_never_appears_in_the_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request for key test-key")

    with pytest.raises(JevProviderError) as caught:
        policy(httpx.MockTransport(handler)).decide(subtask=TASK, snapshot=snapshot(now_playing()), history=())
    assert "test-key" not in str(caught.value)
