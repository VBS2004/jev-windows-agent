from __future__ import annotations

from jev_windows_agent import (
    ActionKind,
    Decision,
    DesktopElement,
    DesktopExecutor,
    DesktopSnapshot,
    RuntimeConfig,
    Subtask,
    TerminalKind,
)
from jev_windows_agent.backends import StateMachineBackend
from jev_windows_agent.policies import ScriptedPolicy


def snapshot(state: dict) -> DesktopSnapshot:
    return DesktopSnapshot(
        application="Test App",
        window="Main",
        revision=state["value"],
        elements=(
            DesktopElement(
                id="search",
                role="text_field",
                name="Search",
                value=state["value"],
                actions=(ActionKind.TYPE_TEXT,),
                source="test",
            ),
        ),
    )


def transition(state: dict, action) -> None:
    if action.kind == ActionKind.TYPE_TEXT:
        state["value"] = action.value


def task() -> Subtask:
    return Subtask(
        goal="Search for Gaussian Blur",
        verification=("Search contains Gaussian Blur",),
        inputs={"query": "Gaussian Blur"},
    )


def executor(
    decisions: list[Decision],
    config: RuntimeConfig,
) -> tuple[DesktopExecutor, StateMachineBackend]:
    # StateMachineBackend deepcopies the initial state, so assert against its own copy.
    backend = StateMachineBackend({"value": ""}, snapshot, transition)
    return DesktopExecutor(backend, ScriptedPolicy(decisions), config=config), backend


def test_low_confidence_action_escalates_without_executing() -> None:
    runner, backend = executor(
        [Decision(kind=ActionKind.TYPE_TEXT, target_id="search", input_key="query", confidence=0.31)],
        RuntimeConfig(confidence_thresholds={ActionKind.TYPE_TEXT: 0.7}),
    )

    result = runner.run(task())

    assert result.status == TerminalKind.NEEDS_AGENT
    assert result.history == ()
    # The gate fires before the backend is touched at all.
    assert backend.state["value"] == ""
    assert "0.31" in (result.reason or "")
    assert "TYPE_TEXT" in (result.reason or "")


def test_confident_action_passes_the_gate() -> None:
    runner, backend = executor(
        [
            Decision(kind=ActionKind.TYPE_TEXT, target_id="search", input_key="query", confidence=0.93),
            Decision(terminal=TerminalKind.SUBTASK_COMPLETE, confidence=0.9),
        ],
        RuntimeConfig(confidence_thresholds={ActionKind.TYPE_TEXT: 0.7, TerminalKind.SUBTASK_COMPLETE: 0.75}),
    )

    result = runner.run(task())

    assert result.status == TerminalKind.SUBTASK_COMPLETE
    assert backend.state["value"] == "Gaussian Blur"


def test_low_confidence_completion_is_not_reported_as_done() -> None:
    runner, _ = executor(
        [Decision(terminal=TerminalKind.SUBTASK_COMPLETE, confidence=0.4)],
        RuntimeConfig(confidence_thresholds={TerminalKind.SUBTASK_COMPLETE: 0.75}),
    )

    result = runner.run(task())

    assert result.status == TerminalKind.NEEDS_AGENT
    assert result.observations == ()
    assert "SUBTASK_COMPLETE" in (result.reason or "")


def test_needs_agent_is_never_gated() -> None:
    runner, _ = executor(
        [Decision(terminal=TerminalKind.NEEDS_AGENT, confidence=0.05)],
        RuntimeConfig(confidence_thresholds={TerminalKind.NEEDS_AGENT: 0.9}),
    )

    result = runner.run(task())

    assert result.status == TerminalKind.NEEDS_AGENT
    # Passed through as the policy's own decision, not rewritten by the gate.
    assert result.reason is None


def test_unlisted_kinds_are_not_gated() -> None:
    runner, backend = executor(
        [
            Decision(kind=ActionKind.TYPE_TEXT, target_id="search", input_key="query", confidence=0.02),
            Decision(terminal=TerminalKind.SUBTASK_COMPLETE, confidence=0.99),
        ],
        RuntimeConfig(confidence_thresholds={TerminalKind.SUBTASK_COMPLETE: 0.75}),
    )

    result = runner.run(task())

    assert result.status == TerminalKind.SUBTASK_COMPLETE
    assert backend.state["value"] == "Gaussian Blur"


def test_policy_without_confidence_is_not_gated() -> None:
    runner, backend = executor(
        [
            Decision(kind=ActionKind.TYPE_TEXT, target_id="search", input_key="query"),
            Decision(terminal=TerminalKind.SUBTASK_COMPLETE),
        ],
        RuntimeConfig(confidence_thresholds={ActionKind.TYPE_TEXT: 0.7, TerminalKind.SUBTASK_COMPLETE: 0.75}),
    )

    result = runner.run(task())

    assert result.status == TerminalKind.SUBTASK_COMPLETE
    assert backend.state["value"] == "Gaussian Blur"


def test_gating_is_off_by_default() -> None:
    runner, backend = executor(
        [
            Decision(kind=ActionKind.TYPE_TEXT, target_id="search", input_key="query", confidence=0.01),
            Decision(terminal=TerminalKind.SUBTASK_COMPLETE, confidence=0.01),
        ],
        RuntimeConfig(),
    )

    result = runner.run(task())

    assert result.status == TerminalKind.SUBTASK_COMPLETE
    assert backend.state["value"] == "Gaussian Blur"


def test_gate_yields_a_terminal_step_event() -> None:
    runner, _ = executor(
        [Decision(kind=ActionKind.TYPE_TEXT, target_id="search", input_key="query", confidence=0.1)],
        RuntimeConfig(confidence_thresholds={ActionKind.TYPE_TEXT: 0.7}),
    )

    events = list(runner.run_iter(task()))

    assert len(events) == 1
    assert events[0].terminal
    assert events[0].action is None
    assert events[0].decision.terminal == TerminalKind.NEEDS_AGENT
    # The gated decision's own confidence is preserved for the planner to inspect.
    assert events[0].decision.confidence == 0.1
