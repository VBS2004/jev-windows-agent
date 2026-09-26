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

For a colorful interactive version of this, and for multi-app requests, see the
`jev` CLI (src/jev_windows_agent/cli.py, installed as the `jev` command) instead.
This script is the plain-text original and a thin wrapper over
jev_windows_agent.runner.run_windowed_subtask, the same function the CLI uses.
"""

from __future__ import annotations

import argparse
import json
import sys

from jev_windows_agent import Subtask
from jev_windows_agent.api import result_to_dict
from jev_windows_agent.policies import TypeSafeJevPolicy
from jev_windows_agent.runner import TargetWindowError, load_project_env, run_windowed_subtask


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

    load_project_env()
    policy = TypeSafeJevPolicy()  # fails on a missing key before touching the desktop
    subtask = Subtask(
        goal=args.goal,
        verification=tuple(args.verify),
        constraints=tuple(args.constraint),
        inputs=dict(args.input),
        shortcuts=dict(args.shortcut),
        max_actions=args.max_actions,
    )

    try:
        result = run_windowed_subtask(
            policy,
            subtask=subtask,
            process=args.process,
            window=args.window,
            launch=args.launch,
            confidence_gate=args.confidence_gate,
        )
    except (TargetWindowError, RuntimeError) as exc:
        sys.exit(str(exc))

    print(f"\nstatus: {result.status.value} after {result.actions_taken} actions")
    if result.reason:
        print("reason:", result.reason)
    if args.json:
        summary = result_to_dict(result)
        summary.pop("final_snapshot")  # large; the planner can request it when needed
        print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
