"""Deterministic end-to-end smoke test of WindowsUIABackend on Notepad. No API key.

Drives the real DesktopExecutor loop -- freshness checks, native execution, settling
-- with a step policy instead of JEV, so a failure here is a backend bug rather
than a policy judgment. It only touches a file it creates in a temp directory:

  1. TYPE_TEXT  replace the editor's text via UIA ValuePattern
  2. HOTKEY     MOD+S (Ctrl+S) to save; the file already exists, so no dialog
  3. CLICK      the Edit menu, which opens a popup outside the main window's tree
  4. PRESS_KEY  ESCAPE to close it
  5. SCROLL     DOWN over the editor

then checks the saved file on disk and closes its own (now saved) tab.

Usage:
  pip install -e '.[windows]'
  python examples/windows_notepad_smoke.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Sequence

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
from jev_windows_agent.backends import WindowsUIABackend
from jev_windows_agent.backends.windows_uia import activate_window, find_window
from jev_windows_agent.models import ActionRecord

Step = Callable[[DesktopSnapshot], Decision]


class StepPolicy:
    """Resolves each step against the live snapshot, since popup ids appear only after a click."""

    def __init__(self, steps: Sequence[Step]) -> None:
        self._steps = list(steps)
        self.snapshots: list[DesktopSnapshot] = []

    def decide(self, *, subtask: Subtask, snapshot: DesktopSnapshot, history: Sequence[ActionRecord]) -> Decision:
        del subtask
        self.snapshots.append(snapshot)
        return self._steps[len(history)](snapshot)


def find(snapshot: DesktopSnapshot, *, role: str, name: str, exact: bool = True) -> DesktopElement:
    for element in snapshot.elements:
        if element.role == role and (element.name == name if exact else name in element.name):
            return element
    raise SystemExit(f"No {role} named {name!r} in the current snapshot")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    workdir = Path(tempfile.mkdtemp(prefix="jev-windows-agent-smoke-"))
    # A unique name per run: Notepad tabs are matched by file name, and a tab left
    # over from an earlier run must never be mistaken for this run's tab.
    target = workdir / f"jev_windows_agent_smoke_{time.strftime('%H%M%S')}.txt"
    target.write_text("original text\n", encoding="utf-8")
    line = f"typed by jev-windows-agent at {time.strftime('%H:%M:%S')}"

    backend = WindowsUIABackend()
    subprocess.Popen(["notepad.exe", str(target)])
    hwnd = None
    for _ in range(40):
        hwnd = find_window(title_contains=target.name)
        if hwnd:
            break
        time.sleep(0.25)
    if not hwnd or not activate_window(hwnd):
        raise SystemExit("Could not bring the smoke-test Notepad window to the foreground")
    time.sleep(0.5)

    def our_tab_selected(snapshot: DesktopSnapshot) -> None:
        # Windows 11 Notepad opens files as tabs beside the user's own documents.
        # Typing replaces the editor's text, so never proceed on anyone else's tab.
        tab = find(snapshot, role="TabItem", name=target.name, exact=False)
        if not tab.selected:
            raise SystemExit(f"Refusing to type: the selected Notepad tab is not {target.name}")

    def type_line(snapshot: DesktopSnapshot) -> Decision:
        our_tab_selected(snapshot)
        editor = find(snapshot, role="Document", name="Text editor")
        return Decision(kind=ActionKind.TYPE_TEXT, target_id=editor.id, input_key="line")

    def save(snapshot: DesktopSnapshot) -> Decision:
        our_tab_selected(snapshot)
        return Decision(kind=ActionKind.HOTKEY, hotkey="MOD+S")

    def open_edit_menu(snapshot: DesktopSnapshot) -> Decision:
        return Decision(kind=ActionKind.CLICK, target_id=find(snapshot, role="MenuItem", name="Edit").id)

    def close_menu(snapshot: DesktopSnapshot) -> Decision:
        return Decision(kind=ActionKind.PRESS_KEY, key="ESCAPE")

    def scroll(snapshot: DesktopSnapshot) -> Decision:
        return Decision(kind=ActionKind.SCROLL, scroll_direction="DOWN")

    def done(snapshot: DesktopSnapshot) -> Decision:
        return Decision(terminal=TerminalKind.SUBTASK_COMPLETE)

    policy = StepPolicy([type_line, save, open_edit_menu, close_menu, scroll, done])
    subtask = Subtask(
        goal="Replace the smoke-test file's text with the supplied line and save it",
        verification=("The editor shows the supplied line", "The tab reports the file as unmodified"),
        inputs={"line": line},
        # Save is not in the default hotkey set; validation rejects undeclared chords.
        shortcuts={"MOD+S": "Save the current file"},
        max_actions=10,
    )
    executor = DesktopExecutor(backend, policy, config=RuntimeConfig(timeout_s=60))

    for event in executor.run_iter(subtask):
        if event.record is not None:
            r = event.record
            print(
                f"step {r.step}: {r.action.kind.value:<9} target={r.target_name!r:<18} "
                f"state_changed={r.state_changed} elapsed={r.elapsed_ms}ms"
            )
        if event.result is not None:
            result = event.result
            print(f"result: {result.status.value} after {result.actions_taken} actions; reason={result.reason}")

    after_menu = policy.snapshots[3]
    menu_bar = {"File", "Edit", "View"}
    menu_items = [e.name for e in after_menu.elements if e.role == "MenuItem" and e.name not in menu_bar]
    print(f"menu items visible after CLICK Edit: {menu_items[:8]}")
    saved = target.read_text(encoding="utf-8")
    print(f"file on disk: {saved!r}")
    typed_tab = find(policy.snapshots[1], role="TabItem", name=target.name, exact=False).name
    saved_tab = find(policy.snapshots[2], role="TabItem", name=target.name, exact=False).name
    print(f"tab after typing: {typed_tab!r}; after saving: {saved_tab!r}")

    failures = []
    if result.status != TerminalKind.SUBTASK_COMPLETE:
        failures.append(f"status {result.status.value}")
    if saved.rstrip("\r\n") != line:
        failures.append("saved file does not contain the typed line")
    if not menu_items:
        failures.append("Edit menu items were not observed after opening it")
    # Typing must register as a real edit, or a planner reading "Unmodified" as
    # "saved" would be misled (UIA SetValue on Notepad's editor skips the dirty flag).
    if "Unmodified" in typed_tab or "Unmodified" not in saved_tab:
        failures.append("the tab's modified/saved state did not track the edit")
    close_own_tab(backend, target.name)
    if failures:
        raise SystemExit("SMOKE TEST FAILED: " + "; ".join(failures))
    print("SMOKE TEST PASSED")


def close_own_tab(backend: WindowsUIABackend, file_name: str) -> None:
    """Close the smoke-test tab via its own Close Tab button; saved, so no prompt."""
    from jev_windows_agent.validation import materialize_action

    snapshot = backend.observe()
    tab = find(snapshot, role="TabItem", name=file_name, exact=False)
    if "Unmodified" not in tab.name:
        print(f"leaving {file_name} open: it has unsaved changes")
        return
    close = next(
        (e for e in snapshot.elements if e.parent_id == tab.id and e.metadata.get("automation_id") == "CloseButton"),
        None,
    )
    if close is None:
        print(f"leaving {file_name} open: no Close Tab button observed")
        return
    decision = Decision(kind=ActionKind.CLICK, target_id=close.id)
    subtask = Subtask(goal="close the smoke-test tab", verification=("tab closed",))
    backend.execute(snapshot, materialize_action(decision, snapshot, subtask))
    print(f"closed tab {file_name}")


if __name__ == "__main__":
    main()
