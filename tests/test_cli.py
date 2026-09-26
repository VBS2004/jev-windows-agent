"""Pure-logic tests for the `jev` CLI: rendering and argument parsing, no desktop or network.

cli.py's argparse structure and formatting helpers are importable on any platform
(WindowsUIABackend is only ever touched once a run actually starts, inside
runner.run_windowed_subtask), so these run everywhere, same as the rest of this
project's platform-independent logic tests.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console
from rich.text import Text

from jev_windows_agent import (
    ActionKind,
    Decision,
    DesktopSnapshot,
    ExecutableAction,
    ExecutionResult,
    StepEvent,
    Subtask,
    TerminalKind,
    cli,
)
from jev_windows_agent.models import ActionRecord
from jev_windows_agent.planner import PlannerError
from jev_windows_agent.policies import TypeSafeJevPolicy
from jev_windows_agent.runner import WindowScope


def plain_console() -> tuple[Console, io.StringIO]:
    """A Console that writes to a buffer with no color codes, for asserting on text."""
    buf = io.StringIO()
    return Console(file=buf, theme=cli.THEME, no_color=True, width=200, highlight=False), buf


# -- banner --------------------------------------------------------------------------


def test_banner_spells_jev_and_is_legible() -> None:
    # Live: a hand-rolled ASCII banner didn't actually read as "JEV" until fixed.
    assert len(cli._BANNER_LINES) == len(cli._BANNER_GRADIENT)
    widths = {len(line) for line in cli._BANNER_LINES}
    assert len(widths) == 1  # every line the same width, or it renders ragged


def test_print_banner_does_not_crash_on_a_plain_console() -> None:
    console, buf = plain_console()
    cli.print_banner(console)
    assert "powered by JEV" in buf.getvalue()


# -- confidence rendering --------------------------------------------------------------


@pytest.mark.parametrize(
    ("confidence", "style"),
    [(None, "jev.dim"), (0.79, "jev.conf.mid"), (0.8, "jev.conf.high"), (0.99, "jev.conf.high"),
     (0.5, "jev.conf.mid"), (0.49, "jev.conf.low"), (0.0, "jev.conf.low")],
)
def test_confidence_style_thresholds(confidence: float | None, style: str) -> None:
    assert cli.confidence_style(confidence) == style


def test_confidence_text_formats_as_a_percentage() -> None:
    text = cli.confidence_text(0.873)
    assert text.plain == "87%"
    assert cli.confidence_text(None).plain == "n/a"


# -- action icons ----------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(ActionKind))
def test_every_action_kind_has_an_icon(kind: ActionKind) -> None:
    assert cli.action_icon(kind.value) != "•"


@pytest.mark.parametrize("kind", list(TerminalKind))
def test_every_terminal_kind_has_an_icon(kind: TerminalKind) -> None:
    assert cli.action_icon(kind.value) != "•"


def test_unknown_action_name_falls_back_to_a_bullet() -> None:
    assert cli.action_icon("NOT_A_REAL_ACTION") == "•"


# -- event -> table row ------------------------------------------------------------------


def snapshot() -> DesktopSnapshot:
    return DesktopSnapshot(application="Notepad", window="Untitled", revision="1", elements=())


def click_event(
    *, target_id: str | None, confidence: float, step: int = 1, target_name: str | None = None
) -> StepEvent:
    decision = Decision(
        kind=ActionKind.CLICK, target_id=target_id, confidence=confidence, latency_ms=120,
        raw={"answers": {"operation": {"probabilities": {"CLICK": confidence, "BLOCKED": 1 - confidence}}}},
    )
    action = ExecutableAction(kind=ActionKind.CLICK, target_id=target_id) if target_id else None
    record = None
    if target_name is not None:
        record = ActionRecord(
            step=step, decision=decision, action=action, before_revision="1", after_revision="2",
            state_changed=True, elapsed_ms=150, target_name=target_name,
        )
    return StepEvent(step=step, snapshot=snapshot(), decision=decision, action=action, record=record)


def test_event_row_uses_the_runtimes_own_target_name_when_present() -> None:
    event = click_event(target_id="e1", confidence=0.9, target_name="Save")
    row = cli.event_row(event, WindowScope(inner=None, hwnd=1))
    assert row is not None
    assert row[2] == "Save"


def test_event_row_labels_an_unnamed_target_from_what_was_seen() -> None:
    from jev_windows_agent import DesktopElement

    scope = WindowScope(inner=None, hwnd=1)
    scope.seen["e1"] = DesktopElement(id="e1", role="Button", name="", source="windows_uia")
    event = click_event(target_id="e1", confidence=0.6)
    row = cli.event_row(event, scope)
    assert row is not None
    assert row[2] == "(unnamed Button)"


def test_event_row_falls_back_for_a_target_never_seen_in_this_scope() -> None:
    event = click_event(target_id="e1", confidence=0.6)
    row = cli.event_row(event, WindowScope(inner=None, hwnd=1))
    assert row is not None
    assert row[2] == "(unnamed element)"


def test_event_row_confidence_cell_matches_the_style_function() -> None:
    event = click_event(target_id="e1", confidence=0.91)
    row = cli.event_row(event, WindowScope(inner=None, hwnd=1))
    assert isinstance(row[3], Text)
    assert row[3].plain == "91%"


def test_event_row_is_none_for_the_trailing_terminal_reyield() -> None:
    # run_iter re-yields the last step's StepEvent alongside the terminal result;
    # rendering it a second time would duplicate the last row.
    decision = Decision(kind=ActionKind.CLICK, target_id="e1", confidence=0.9)
    action = ExecutableAction(kind=ActionKind.CLICK, target_id="e1")
    record = ActionRecord(step=1, decision=decision, action=action, before_revision="1", after_revision="2",
                          state_changed=True, elapsed_ms=100)
    result = ExecutionResult(status=TerminalKind.SUBTASK_COMPLETE, subtask=Subtask(goal="g", verification=("v",)),
                             final_snapshot=snapshot(), history=(record,))
    event = StepEvent(step=1, snapshot=snapshot(), decision=decision, action=action, record=record, result=result)
    assert cli.event_row(event, WindowScope(inner=None, hwnd=1)) is None


def test_event_row_for_a_terminal_decision_shows_top_candidates() -> None:
    decision = Decision(
        terminal=TerminalKind.SUBTASK_COMPLETE, confidence=0.95,
        raw={"answers": {"operation": {"probabilities": {"SUBTASK_COMPLETE": 0.95, "CLICK": 0.05}}}},
    )
    event = StepEvent(step=3, snapshot=snapshot(), decision=decision)
    row = cli.event_row(event, WindowScope(inner=None, hwnd=1))
    assert row is not None
    assert "SUBTASK_COMPLETE" in row[4]


# -- result panel --------------------------------------------------------------------


@pytest.mark.parametrize("status", list(TerminalKind))
def test_result_panel_renders_every_terminal_status(status: TerminalKind) -> None:
    result = ExecutionResult(status=status, subtask=Subtask(goal="g", verification=("v",)),
                             final_snapshot=snapshot(), history=(), reason="because")
    console, buf = plain_console()
    console.print(cli.result_panel(result))
    out = buf.getvalue()
    assert status.value in out
    assert "because" in out


# -- argument parsing ------------------------------------------------------------------


def test_run_requires_goal_and_verify() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--process", "notepad"])
    args = parser.parse_args(["run", "--process", "notepad", "--goal", "g", "--verify", "v"])
    assert args.func is cli.cmd_run
    assert args.goal == "g" and args.verify == ["v"]


def test_run_input_and_shortcut_parse_as_key_value_pairs() -> None:
    parser = cli.build_parser()
    args = parser.parse_args([
        "run", "--process", "notepad", "--goal", "g", "--verify", "v",
        "--input", "line=hello world", "--shortcut", "MOD+S=Save",
    ])
    assert args.input == [("line", "hello world")]
    assert args.shortcut == [("MOD+S", "Save")]


def test_run_bad_key_value_is_rejected_at_parse_time() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--process", "notepad", "--goal", "g", "--verify", "v", "--input", "no-equals-sign"])


def test_plan_requires_request_and_defaults_the_model() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["plan"])
    args = parser.parse_args(["plan", "--request", "do the thing"])
    assert args.func is cli.cmd_plan
    assert args.deepseek_model == "deepseek-chat"
    assert args.max_actions == 15


def test_no_command_is_not_an_error() -> None:
    args = cli.build_parser().parse_args([])
    assert args.command is None


# -- error paths: no key, no target, never touch the desktop or network ------------------


def test_cmd_run_without_a_target_returns_2_without_touching_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*a, **k):
        raise AssertionError("should not construct a policy before checking the target")

    monkeypatch.setattr(cli, "TypeSafeJevPolicy", fail)
    parser = cli.build_parser()
    args = parser.parse_args(["run", "--goal", "g", "--verify", "v"])
    console, buf = plain_console()
    assert cli.cmd_run(args, console) == 2
    assert "--window or --process" in buf.getvalue()


def test_cmd_run_reports_a_missing_key_without_touching_the_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*a, **k):
        raise ValueError("Set TYPESAFE_API_KEY or OPENROUTER_API_KEY, or pass api_key=...")

    monkeypatch.setattr(cli, "load_project_env", lambda: None)
    monkeypatch.setattr(cli, "TypeSafeJevPolicy", fail)
    monkeypatch.setattr(cli, "run_one", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run")))
    parser = cli.build_parser()
    args = parser.parse_args(["run", "--process", "notepad", "--goal", "g", "--verify", "v"])
    console, buf = plain_console()
    assert cli.cmd_run(args, console) == 1
    assert "TYPESAFE_API_KEY" in buf.getvalue()


def test_cmd_plan_reports_a_missing_deepseek_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail():
        raise PlannerError("Set DEEPSEEK_API_KEY first (or put it in .env.local).")

    monkeypatch.setattr(cli, "load_deepseek_key", fail)
    parser = cli.build_parser()
    args = parser.parse_args(["plan", "--request", "do the thing"])
    console, buf = plain_console()
    assert cli.cmd_plan(args, console) == 1
    assert "DEEPSEEK_API_KEY" in buf.getvalue()


def test_cmd_plan_reports_an_empty_plan_without_running_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "load_deepseek_key", lambda: "sk-test")
    monkeypatch.setattr(TypeSafeJevPolicy, "__init__", lambda self, **k: setattr(self, "base_url", "test") or None)
    monkeypatch.setattr(cli, "plan_request", lambda *a, **k: (_ for _ in ()).throw(PlannerError("no usable steps")))
    parser = cli.build_parser()
    args = parser.parse_args(["plan", "--request", "vague request"])
    console, buf = plain_console()
    assert cli.cmd_plan(args, console) == 1
    assert "no usable steps" in buf.getvalue()
