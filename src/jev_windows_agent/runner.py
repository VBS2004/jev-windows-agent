"""Run one bounded Subtask against one named Windows window, with JEV.

This is the reusable core behind examples/windows_task.py, examples/planner.py, and
the `jev` CLI (cli.py): resolve a target window (launching the app first if asked),
pin JEV to it so it can never act on any other window, run the bounded loop, and
report what happened. Nothing here is Windows-agnostic -- it imports WindowsUIABackend
directly -- so it is not re-exported from the package's top level.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from . import (
    SUGGESTED_CONFIDENCE_THRESHOLDS,
    ActionKind,
    DesktopElement,
    DesktopExecutor,
    DesktopSnapshot,
    ExecutableAction,
    ExecutionResult,
    RuntimeConfig,
    StepEvent,
    Subtask,
    TerminalKind,
)
from .backends import WindowsUIABackend
from .backends.windows_uia import _revision, activate_window, foreground_window, resolve_window
from .errors import UnsupportedDesktopAction
from .policies import TypeSafeJevPolicy

PhaseCallback = Callable[[str], None]
EventCallback = Callable[[StepEvent, "WindowScope"], None]


class TargetWindowError(RuntimeError):
    """The task never started: no single window to run it in, or it wouldn't stay in front."""


class WindowScope:
    """A DesktopBackend limited to one top-level window.

    The backend observes whatever window is in front. This wrapper returns an empty
    snapshot for any other window, so the policy has nothing to choose there, and
    refuses to execute anything while the pinned window is not in front.
    `last_observation_hidden` records whether the most recent observation was
    blanked that way, so a run that ends on it can say why. `seen` accumulates every
    element ever observed, keyed by id, so a caller can describe an action's target
    after the fact even when the runtime's own trace has moved on.
    """

    def __init__(self, inner: WindowsUIABackend, hwnd: int) -> None:
        self.inner = inner
        self.hwnd = hwnd
        self.last_observation_hidden = False
        self.seen: dict[str, DesktopElement] = {}

    def observe(self) -> DesktopSnapshot:
        snapshot = self.inner.observe()
        self.last_observation_hidden = snapshot.context.get("hwnd") != self.hwnd
        if self.last_observation_hidden:
            return replace(snapshot, elements=(), revision=_revision(()))
        self.seen.update((e.id, e) for e in snapshot.elements)
        return snapshot

    def is_fresh(self, snapshot: DesktopSnapshot, action: ExecutableAction) -> bool:
        return self.inner.is_fresh(snapshot, action)

    def execute(self, snapshot: DesktopSnapshot, action: ExecutableAction) -> None:
        if action.kind != ActionKind.WAIT and not self.observe().elements:
            raise UnsupportedDesktopAction("The target window is no longer in front; refusing to act")
        self.inner.execute(snapshot, action)


def load_env_file(path: Path) -> None:
    """Fill unset environment variables from simple KEY=value lines; values are never printed."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.removeprefix("export ").split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip("\"'"))


def load_project_env() -> None:
    """Fill unset environment variables from .env.local at the repository root, if present."""
    load_env_file(Path(__file__).resolve().parents[2] / ".env.local")


def describe_target(scope: WindowScope, action: ExecutableAction | None) -> tuple[str, bool] | None:
    """A human-readable label for an action's target, even when it had no name.

    Returns (label, named) so a caller can format an unnamed target differently --
    "an unnamed Button" beats a blank, but should not be quoted like a real name.
    """
    if action is None or not action.target_id:
        return None
    target = scope.seen.get(action.target_id)
    if target is None:
        return "element", False
    if target.name:
        return target.name, True
    automation_id = target.metadata.get("automation_id")
    return f"{target.role}" + (f" #{automation_id}" if automation_id else ""), False


def _default_on_event(event: StepEvent, scope: WindowScope) -> None:
    if event.result is not None and event.record is not None:
        return  # the runtime re-yields the last step alongside its result; already printed
    decision = event.decision
    what = (decision.kind or decision.terminal).value
    action = event.action
    detail = ""
    target = describe_target(scope, action)
    if event.record and event.record.target_name:
        detail = f" -> {event.record.target_name!r}"
    elif target is not None:
        label, named = target
        detail = f" -> {label!r}" if named else f" -> (unnamed {label})"
    elif action is not None and (action.hotkey or action.key or action.scroll_direction):
        detail = f" {action.hotkey or action.key or action.scroll_direction}"
    confidence = "n/a" if decision.confidence is None else f"{decision.confidence:.2f}"
    operation = decision.raw.get("answers", {}).get("operation", {}) if decision.raw else {}
    ranked = sorted(operation.get("probabilities", {}).items(), key=lambda kv: kv[1], reverse=True)[:3]
    top = ", ".join(f"{name} {p:.2f}" for name, p in ranked if p > 0)
    print(f"step {event.step}: {what}{detail}  confidence={confidence}  [{top}]")


def run_windowed_subtask(
    policy: TypeSafeJevPolicy,
    *,
    subtask: Subtask,
    process: str | None = None,
    window: str | None = None,
    launch: str | None = None,
    confidence_gate: bool = False,
    print_trace: bool = True,
    on_phase: PhaseCallback | None = None,
    on_event: EventCallback | None = None,
) -> ExecutionResult:
    """Bring one window forward and run one bounded Subtask against it with JEV.

    This is the whole "planner" contract in one call: name an app, hand it a goal.
    `on_phase` is called with a short status string before each blocking step that
    precedes the JEV loop (finding the window, bringing it forward), for a caller
    that wants to show progress. `on_event` is called once per StepEvent from the
    runtime; when omitted, `print_trace` controls a plain-text default (used by
    examples/windows_task.py) instead.
    """
    if not (process or window):
        raise ValueError("name the target with process= or window=")

    def phase(message: str) -> None:
        if on_phase is not None:
            on_phase(message)

    backend = WindowsUIABackend()
    phase(f"Finding {process or window}...")
    try:
        # With a launch, this is the window the launch opened -- never a matching
        # window that already existed, which may hold the user's own document.
        hwnd = resolve_window(process_name=process, title_contains=window, launch=launch)
    except LookupError as exc:
        raise TargetWindowError(f"Target window for process={process!r} window={window!r}: {exc}") from exc

    # Something (often a just-launched window) can take the foreground back while
    # this one settles. Check before JEV starts: a run pinned to a window that isn't
    # in front only ever sees an empty screen, and would end BLOCKED doing nothing.
    phase("Bringing the window to the front...")
    for _ in range(2):
        if not activate_window(hwnd):
            continue
        time.sleep(0.5)
        if foreground_window() == hwnd:
            break
    else:
        raise TargetWindowError("The target window would not stay in front (another window kept taking focus)")

    config = RuntimeConfig(
        timeout_s=180,
        confidence_thresholds=SUGGESTED_CONFIDENCE_THRESHOLDS if confidence_gate else None,
    )
    scope = WindowScope(backend, hwnd)
    executor = DesktopExecutor(scope, policy, config=config)
    if on_event is None and print_trace:
        print(f"Jev via {policy.base_url}; hands off the mouse and keyboard until it finishes.\n")

    result: ExecutionResult | None = None
    for event in executor.run_iter(subtask):
        result = event.result or result
        if on_event is not None:
            on_event(event, scope)
        elif print_trace:
            _default_on_event(event, scope)

    assert result is not None
    if result.status != TerminalKind.SUBTASK_COMPLETE and scope.last_observation_hidden:
        # Otherwise this reads as JEV's own judgment, when JEV was shown an empty screen.
        why = "The target window was not in front when JEV last looked, so it saw nothing to act on"
        result = replace(result, reason=f"{why} ({result.reason})" if result.reason else why)
    return result
