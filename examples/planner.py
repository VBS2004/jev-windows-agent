"""Turn one big natural-language request into an ordered plan, then run it.

This is the "planner" layer the rest of this project deliberately stays outside of
(see ARCHITECTURE.md's ownership table). JEV is a fast per-click decision model, not
a reasoning model -- it can't decide *which app* to open or *how many steps* your
request needs. Something above it has to. Here, that's DeepSeek: given your request,
it proposes an ordered list of steps, each one a single-window Subtask in the exact
shape windows_task.py already runs. You confirm each step before it touches the
desktop; nothing runs without your yes.

Usage:
  pip install -e '.[windows]'
  $env:DEEPSEEK_API_KEY = "sk-..."       # or put DEEPSEEK_API_KEY=... in .env.local
  python examples/planner.py --request "Turn on dark mode, then open Notepad and write today's date"

DeepSeek only plans; it never sees or touches the real desktop. Each confirmed step
runs through the same JEV loop as windows_task.py, scoped to that one step's window
-- JEV still cannot invent a click, a target, or a piece of text that DeepSeek
didn't already put in the step.

For a colorful interactive version of this, see the `jev` CLI
(src/jev_windows_agent/cli.py, installed as the `jev plan` command) instead. This
script is the plain-text original, a thin wrapper over jev_windows_agent.planner
and jev_windows_agent.runner, the same modules the CLI uses.
"""

from __future__ import annotations

import argparse
import sys

from jev_windows_agent import Subtask, TerminalKind
from jev_windows_agent.planner import (
    DEFAULT_DEEPSEEK_MODEL,
    PlannerError,
    describe_step,
    load_deepseek_key,
    plan_request,
)
from jev_windows_agent.policies import TypeSafeJevPolicy
from jev_windows_agent.runner import TargetWindowError, run_windowed_subtask


def continue_after(step: int, total: int) -> bool:
    """After a step that didn't complete: ask whether to go on -- unless it was the last."""
    if step >= total:
        return False
    return confirm(f"Step {step} did not complete. Continue to step {step + 1}/{total} anyway? [y/n]: ") == "y"


def confirm(prompt: str) -> str:
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return "q"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--request", required=True, help="what you want done, in plain English")
    parser.add_argument("--deepseek-model", default=DEFAULT_DEEPSEEK_MODEL)
    parser.add_argument("--max-actions", type=int, default=15, help="per-step JEV action budget")
    parser.add_argument("--confidence-gate", action="store_true",
                        help="hand a step back to you when JEV is less sure than SUGGESTED_CONFIDENCE_THRESHOLDS")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    try:
        deepseek_key = load_deepseek_key()
        policy = TypeSafeJevPolicy()  # fails on a missing JEV key before anything else runs

        print(f"Planning with {args.deepseek_model}...\n")
        steps = plan_request(args.request, api_key=deepseek_key, model=args.deepseek_model)
    except PlannerError as exc:
        sys.exit(str(exc))

    print(f"Proposed plan ({len(steps)} step{'s' if len(steps) != 1 else ''}):\n")
    for i, step in enumerate(steps, 1):
        print(describe_step(i, len(steps), step))
        print()

    outcomes: list[tuple[str, str]] = []
    for i, step in enumerate(steps, 1):
        answer = confirm(f"Run step {i}/{len(steps)}? [y]es / [n]o skip / [q]uit: ")
        if answer == "q":
            print("Stopped.")
            break
        if answer != "y":
            outcomes.append((step["goal"], "skipped"))
            continue

        subtask = Subtask(
            goal=step["goal"],
            verification=tuple(step["verify"]),
            constraints=tuple(step["constraint"]),
            inputs=dict(step["input"]),
            shortcuts=dict(step["shortcut"]),
            max_actions=args.max_actions,
        )
        try:
            result = run_windowed_subtask(
                policy,
                subtask=subtask,
                process=step.get("process"),
                window=step.get("window"),
                launch=step.get("launch"),
                confidence_gate=args.confidence_gate,
            )
        except TargetWindowError as exc:
            print(f"\nstep {i} did not start: {exc}")
            outcomes.append((step["goal"], f"not started: {exc}"))
            if not continue_after(i, len(steps)):
                break
            continue
        except Exception as exc:  # noqa: BLE001 -- report any mid-run failure, then let the user decide
            # The run was underway (it may have acted already), so "before running"
            # would be wrong: say where it stopped and why.
            print(f"\nstep {i} stopped during the run: {type(exc).__name__}: {exc}")
            outcomes.append((step["goal"], f"stopped: {type(exc).__name__}: {exc}"))
            if not continue_after(i, len(steps)):
                break
            continue

        print(f"\nstep {i} result: {result.status.value} after {result.actions_taken} actions")
        if result.reason:
            print("reason:", result.reason)
        print()
        outcomes.append((step["goal"], result.status.value))
        if result.status != TerminalKind.SUBTASK_COMPLETE and not continue_after(i, len(steps)):
            break

    print("\n=== Plan summary ===")
    for goal, status in outcomes:
        print(f"  [{status}] {goal}")


if __name__ == "__main__":
    main()
