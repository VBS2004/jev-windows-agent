"""JEV drives Windows 11 Notepad: replace a file's text with a supplied line and save it.

The Windows counterpart of test_settings.py / test_spotify.py. Needs a key for Jev,
either from OpenRouter (Decisions API) or from TypeSafe directly:

  pip install -e '.[windows]'
  $env:OPENROUTER_API_KEY = "sk-or-v1-..."   # or put OPENROUTER_API_KEY=... in .env.local
  python examples/test_notepad.py
  python examples/test_notepad.py --confidence-gate   # escalate low-confidence steps

TYPESAFE_API_KEY, when set, takes precedence and calls TypeSafe's endpoint directly.

It works on a file it creates in a temp directory. Windows 11 Notepad opens files as
tabs beside your own documents, so the backend is wrapped in NotepadTabScope, which
removes every other tab from what JEV can observe. The policy may only choose ids it
observed, so this makes touching your tabs structurally impossible rather than
merely discouraged by a prompt.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from jev_windows_agent import (
    SUGGESTED_CONFIDENCE_THRESHOLDS,
    ActionKind,
    DesktopExecutor,
    DesktopSnapshot,
    ExecutableAction,
    RuntimeConfig,
    Subtask,
)
from jev_windows_agent.backends import WindowsUIABackend
from jev_windows_agent.backends.windows_uia import _revision, activate_window, find_window
from jev_windows_agent.errors import UnsupportedDesktopAction
from jev_windows_agent.policies import TypeSafeJevPolicy


class NotepadTabScope:
    """A DesktopBackend that exposes exactly one Notepad tab of one window to the policy.

    - Any other foreground window yields an empty snapshot: the backend observes
      whatever is in front, and you may switch windows while this runs.
    - Other tabs, and everything inside them, are dropped from each snapshot.
    - The editor is only exposed while this tab is selected, so TYPE_TEXT cannot
      land in another document.
    - Untargeted input (keys, chords, scrolling) is refused unless this tab is
      selected, since it acts on whatever is in front.
    """

    def __init__(self, inner: WindowsUIABackend, file_name: str, hwnd: int) -> None:
        self.inner = inner
        self.file_name = file_name
        self.hwnd = hwnd

    def observe(self) -> DesktopSnapshot:
        snapshot = self.inner.observe()
        if snapshot.context.get("hwnd") != self.hwnd:
            return replace(snapshot, elements=(), revision=_revision(()))
        hidden: set[str] = set()
        own_tab_selected = False
        for element in snapshot.elements:  # pre-order: parents precede children
            if element.role == "TabItem":
                if self.file_name in element.name:
                    own_tab_selected = bool(element.selected)
                else:
                    hidden.add(element.id)
            elif element.parent_id in hidden:
                hidden.add(element.id)
        kept = tuple(
            e for e in snapshot.elements
            if e.id not in hidden and (own_tab_selected or e.role != "Document")
        )
        return replace(snapshot, elements=kept, revision=_revision(kept))

    def is_fresh(self, snapshot: DesktopSnapshot, action: ExecutableAction) -> bool:
        return self.inner.is_fresh(snapshot, action)

    def execute(self, snapshot: DesktopSnapshot, action: ExecutableAction) -> None:
        if action.kind == ActionKind.WAIT:
            return self.inner.execute(snapshot, action)
        current = self.observe()
        if not current.elements:
            raise UnsupportedDesktopAction("The test Notepad window is no longer in front; refusing to act")
        own_tab_selected = any(e.role == "Document" for e in current.elements)
        if action.target_id is None and not own_tab_selected:
            raise UnsupportedDesktopAction(f"{self.file_name} is not the selected tab; refusing untargeted input")
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--confidence-gate",
        action="store_true",
        help="escalate to NEEDS_AGENT when JEV's confidence is below SUGGESTED_CONFIDENCE_THRESHOLDS",
    )
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    load_env_file(Path(__file__).resolve().parents[1] / ".env.local")
    if not (os.environ.get("TYPESAFE_API_KEY") or os.environ.get("OPENROUTER_API_KEY")):
        sys.exit("Set OPENROUTER_API_KEY or TYPESAFE_API_KEY first; see the module docstring.")
    policy = TypeSafeJevPolicy()  # fail on configuration before touching the desktop
    print(f"Jev via {policy.base_url} (model {policy.model})")

    workdir = Path(tempfile.mkdtemp(prefix="jev-windows-agent-jev-"))
    target = workdir / f"jev_windows_agent_jev_{time.strftime('%H%M%S')}.txt"
    target.write_text("This draft line should be replaced.\n", encoding="utf-8")
    line = "Hello from jev-windows-agent on Windows, driven by JEV."

    backend = WindowsUIABackend()
    print(f"Opening {target} in Notepad...")
    subprocess.Popen(["notepad.exe", str(target)])
    hwnd = None
    for _ in range(40):
        hwnd = find_window(title_contains=target.name)
        if hwnd:
            break
        time.sleep(0.25)
    if not hwnd or not activate_window(hwnd):
        sys.exit("Could not bring the Notepad window to the foreground")
    time.sleep(0.5)

    task = Subtask(
        goal=f"In Notepad, replace all of the text in {target.name} with the supplied line, then save the file.",
        # Literal text the planner decided on. JEV may choose this input key but
        # cannot invent other text.
        inputs={"replacement_line": line},
        # Save is not a default chord; undeclared chords are rejected by validation.
        shortcuts={"MOD+S": "Save the current file"},
        verification=(
            "The Notepad editor contains exactly the supplied replacement line.",
            f"The {target.name} tab reports the file as unmodified, meaning it was saved.",
        ),
        constraints=(
            "Do not close Notepad or any tab.",
            "Do not change Notepad settings, formatting, or zoom.",
        ),
        max_actions=10,
    )
    config = RuntimeConfig(
        timeout_s=120,
        confidence_thresholds=SUGGESTED_CONFIDENCE_THRESHOLDS if args.confidence_gate else None,
    )
    executor = DesktopExecutor(NotepadTabScope(backend, target.name, hwnd), policy, config=config)

    print("Starting jev_windows_agent...\n")
    result = None
    for event in executor.run_iter(task):
        result = event.result or result
        if event.result is not None and event.record is not None:
            continue  # the runtime re-yields the last step alongside its result
        decision = event.decision
        confidence = "n/a" if decision.confidence is None else f"{decision.confidence:.2f}"
        what = (decision.kind or decision.terminal).value
        action = event.action
        detail = ""
        if event.record and event.record.target_name:
            detail = f" -> {event.record.target_name!r}"
        elif action is not None and (action.hotkey or action.key or action.scroll_direction):
            detail = f" {action.hotkey or action.key or action.scroll_direction}"
        # The runner-up operations show when confidence is low because alternatives
        # are equivalent rather than because the step is risky.
        operation = decision.raw.get("answers", {}).get("operation", {}) if decision.raw else {}
        ranked = sorted(operation.get("probabilities", {}).items(), key=lambda kv: kv[1], reverse=True)[:3]
        top = ", ".join(f"{name} {p:.2f}" for name, p in ranked if p > 0)
        print(f"step {event.step}: {what}{detail}  confidence={confidence}  jev={decision.latency_ms}ms  [{top}]")

    assert result is not None
    print("\nstatus:", result.status.value)
    print("actions:", result.actions_taken)
    if result.reason:
        print("reason:", result.reason)
    saved = target.read_text(encoding="utf-8").rstrip("\r\n")
    print(f"file on disk: {saved!r}")
    print("file matches the supplied line:", saved == line)


if __name__ == "__main__":
    main()
