"""Run one natural-language subtask against a Windows app with JEV.

The goal, verification criteria, and constraints are plain English. Any literal text
JEV should type goes in --input; JEV can choose which input to use but can never
invent text of its own.

  python examples/windows_task.py --window Settings --launch ms-settings: ^
      --goal "Search Settings for dark mode" ^
      --verify "The Settings search box contains the supplied search text" ^
      --input query="dark mode"

  python examples/windows_task.py --process notepad ^
      --goal "Replace the document text with the supplied line and save it" ^
      --verify "The editor shows the supplied line" --verify "The tab reports the file as unmodified" ^
      --input line="hello" --shortcut "MOD+S=Save the current file"

The run is pinned to the window you name: if anything else comes to the front, JEV
observes nothing and every action is refused. Keys come from OPENROUTER_API_KEY or
TYPESAFE_API_KEY, read from the environment or from .env.local.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from arc_cua import (
    SUGGESTED_CONFIDENCE_THRESHOLDS,
    ActionKind,
    DesktopExecutor,
    DesktopSnapshot,
    ExecutableAction,
    RuntimeConfig,
    Subtask,
)
from arc_cua.api import result_to_dict
from arc_cua.backends import WindowsUIABackend
from arc_cua.backends.windows_uia import _revision, activate_window, find_window
from arc_cua.errors import UnsupportedDesktopAction
from arc_cua.policies import TypeSafeJevPolicy


class WindowScope:
    """A DesktopBackend limited to one top-level window.

    The backend observes whatever window is in front. This wrapper returns an empty
    snapshot for any other window, so the policy has nothing to choose there, and
    refuses to execute anything while the pinned window is not in front.
    """

    def __init__(self, inner: WindowsUIABackend, hwnd: int) -> None:
        self.inner = inner
        self.hwnd = hwnd

    def observe(self) -> DesktopSnapshot:
        snapshot = self.inner.observe()
        if snapshot.context.get("hwnd") != self.hwnd:
            return replace(snapshot, elements=(), revision=_revision(()))
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


def key_value(text: str) -> tuple[str, str]:
    if "=" not in text:
        raise argparse.ArgumentTypeError(f"expected NAME=VALUE, got {text!r}")
    name, value = text.split("=", 1)
    return name.strip(), value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    where = parser.add_argument_group("target window (one of)")
    where.add_argument("--window", help="substring of the window title")
    where.add_argument("--process", help="executable name, e.g. notepad or explorer")
    parser.add_argument("--launch", help="command to start the app first, e.g. ms-settings: or notepad.exe")
    parser.add_argument("--goal", required=True, help="what to accomplish, in plain English")
    parser.add_argument("--verify", action="append", required=True, help="observable success criterion (repeatable)")
    parser.add_argument("--constraint", action="append", default=[], help="something not to do (repeatable)")
    parser.add_argument("--input", action="append", type=key_value, default=[],
                        help="literal text JEV may type, as NAME=VALUE (repeatable)")
    parser.add_argument("--shortcut", action="append", type=key_value, default=[],
                        help='extra chord JEV may press, as "MOD+S=what it does" (repeatable)')
    parser.add_argument("--max-actions", type=int, default=15)
    parser.add_argument("--confidence-gate", action="store_true",
                        help="hand back to you when JEV is less sure than SUGGESTED_CONFIDENCE_THRESHOLDS")
    parser.add_argument("--json", action="store_true", help="print the planner-facing result as JSON")
    args = parser.parse_args()
    if not (args.window or args.process):
        parser.error("name the target with --window or --process")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    load_env_file(Path(__file__).resolve().parents[1] / ".env.local")
    policy = TypeSafeJevPolicy()  # fails on a missing key before touching the desktop
    subtask = Subtask(
        goal=args.goal,
        verification=tuple(args.verify),
        constraints=tuple(args.constraint),
        inputs=dict(args.input),
        shortcuts=dict(args.shortcut),
        max_actions=args.max_actions,
    )

    backend = WindowsUIABackend()
    if args.launch:
        # "start" resolves URIs such as ms-settings: as well as executables.
        subprocess.run(["cmd", "/c", "start", "", args.launch], check=True)
    hwnd = None
    for _ in range(40):
        hwnd = find_window(process_name=args.process, title_contains=args.window)
        if hwnd:
            break
        time.sleep(0.25)
    if not hwnd:
        sys.exit(f"No visible window matched process={args.process!r} window={args.window!r}")
    if not activate_window(hwnd):
        sys.exit("Found the window but Windows refused to bring it to the foreground")
    time.sleep(0.5)

    config = RuntimeConfig(
        timeout_s=180,
        confidence_thresholds=SUGGESTED_CONFIDENCE_THRESHOLDS if args.confidence_gate else None,
    )
    executor = DesktopExecutor(WindowScope(backend, hwnd), policy, config=config)
    print(f"Jev via {policy.base_url}; hands off the mouse and keyboard until it finishes.\n")

    result = None
    for event in executor.run_iter(subtask):
        result = event.result or result
        if event.result is not None and event.record is not None:
            continue  # the runtime re-yields the last step alongside its result
        decision = event.decision
        what = (decision.kind or decision.terminal).value
        action = event.action
        detail = ""
        if event.record and event.record.target_name:
            detail = f" -> {event.record.target_name!r}"
        elif action is not None and (action.hotkey or action.key or action.scroll_direction):
            detail = f" {action.hotkey or action.key or action.scroll_direction}"
        confidence = "n/a" if decision.confidence is None else f"{decision.confidence:.2f}"
        operation = decision.raw.get("answers", {}).get("operation", {}) if decision.raw else {}
        ranked = sorted(operation.get("probabilities", {}).items(), key=lambda kv: kv[1], reverse=True)[:3]
        top = ", ".join(f"{name} {p:.2f}" for name, p in ranked if p > 0)
        print(f"step {event.step}: {what}{detail}  confidence={confidence}  [{top}]")

    assert result is not None
    print(f"\nstatus: {result.status.value} after {result.actions_taken} actions")
    if result.reason:
        print("reason:", result.reason)
    if args.json:
        summary = result_to_dict(result)
        summary.pop("final_snapshot")  # large; the planner can request it when needed
        print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
