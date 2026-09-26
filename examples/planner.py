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
(see run_windowed_subtask / WindowScope there) -- JEV still cannot invent a click,
a target, or a piece of text that DeepSeek didn't already put in the step.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
from windows_task import load_env_file, run_windowed_subtask

from jev_windows_agent import TerminalKind
from jev_windows_agent.models import Subtask
from jev_windows_agent.policies import TypeSafeJevPolicy

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"  # DeepSeek-V3: the cheap, fast one -- not deepseek-reasoner

PLANNER_SYSTEM_PROMPT = """You turn one user request into an ordered plan of desktop
automation steps for a Windows execution engine. Output strict JSON only, no prose,
matching exactly this shape:

{"steps": [
  {
    "process": "notepad" | null,
    "window": "Settings" | null,
    "launch": "notepad.exe" | "ms-settings:" | null,
    "goal": "Replace the document text with the supplied line and save it",
    "verify": ["The editor shows the supplied line", "The tab reports the file as unmodified"],
    "constraint": ["Do not close the window"],
    "input": {"line": "some literal text"},
    "shortcut": {"MOD+S": "Save the current file"}
  }
]}

Rules:
- Each step targets exactly ONE application window. A request touching several apps
  becomes several steps, in the order they must run.
- "process" identifies a running app by executable name (e.g. "notepad", "explorer").
  "window" is a substring of the window title (e.g. "Settings"). Give at least one;
  give both if you know both. "launch" is a command/URI to start or open the app
  first (e.g. "notepad.exe", "ms-settings:", "explorer.exe C:\\path") -- null if it's
  probably already open and you're targeting it by process/window alone.
- "goal" is what a downstream execution model should accomplish in that one window.
  It does not click for you -- describe the outcome, not individual clicks.
- "verify" is a list of ways to observe success from on-screen state. Never invent a
  criterion that can't actually be checked by looking at the screen.
- "constraint" is a list of things not to do. Use it for anything destructive or out
  of scope for the step (closing windows, changing unrelated settings, deleting data).
- "input" holds literal text/values the execution model may need to type or set,
  keyed by a short name your goal/verify text can refer to. Only put here what the
  step's goal actually needs entered -- never place secrets or credentials here.
- Input values are typed exactly as written. Each must be the final text itself
  ("2026-09-26"), never a description or placeholder ("today's date", "<date>",
  "the current time"). Work out dates and times from the current date and time you
  are given; if a value can't be known, refuse instead of guessing.
- Typing replaces all of the text in the target field or document. To write new
  content, launch the app so a new window opens (e.g. "notepad.exe") rather than
  targeting a document that may already hold the user's work; never target an
  existing document the user didn't mention.
- "shortcut" adds any keyboard chord beyond the defaults (Enter/Escape/Tab/arrows,
  Ctrl+A/C/V/Z/Shift+Z/F) that a step's goal requires, as {"MOD+X": "what it does"}.
  MOD means Ctrl on Windows. Omit if the defaults suffice.
- If the request is unsafe, destructive beyond what was asked, or requires
  information you don't have (e.g. real credentials), return {"steps": []} and put
  the reason in a top-level "refusal" string instead.
- When "constraint", "input", or "shortcut" has nothing to add, use [] or {} for it
  -- never null. Only "process", "window", and "launch" may be null.
"""


def load_deepseek_key() -> str:
    load_env_file(Path(__file__).resolve().parent.parent / ".env.local")
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        sys.exit("Set DEEPSEEK_API_KEY first (or put it in .env.local); see the module docstring.")
    return key


def current_context(now: datetime | None = None) -> str:
    """Facts the planner can't know on its own: the local date, time, and time zone."""
    now = (now or datetime.now()).astimezone()
    offset = now.strftime("%z")
    return (
        f"Current local date and time: {now:%A}, {now:%Y-%m-%d} {now:%H:%M} "
        f"(UTC{offset[:3]}:{offset[3:]}). The desktop is Windows."
    )


def call_deepseek(request: str, *, api_key: str, model: str) -> dict:
    """One DeepSeek chat-completions call, retried like TypeSafeJevPolicy._post."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            # The model has no clock: without this, "today's date" came back as that
            # literal placeholder string.
            {"role": "system", "content": current_context()},
            {"role": "user", "content": request},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }
    with httpx.Client(timeout=60) as client:
        for attempt in range(3):
            response = client.post(DEEPSEEK_URL, json=body, headers={"Authorization": f"Bearer {api_key}"})
            if response.status_code in {429, 503} and attempt < 2:
                time.sleep(1.0 * (2**attempt))
                continue
            if response.is_error:
                sys.exit(f"DeepSeek returned HTTP {response.status_code}: {response.text[:300]}")
            break
    content = response.json()["choices"][0]["message"]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        sys.exit(f"DeepSeek's plan wasn't valid JSON: {exc}\n---\n{content[:1000]}")


def parse_steps(plan: dict) -> list[dict]:
    """Validate DeepSeek's plan and normalize each step's optional fields.

    A JSON schema in a prompt is a request, not a guarantee: a model can (and here,
    did) write an *explicit* null for an optional field instead of omitting it or
    using {}/[]. dict.get(key, default) only falls back to default when the key is
    *absent* -- a present null still comes back as None -- so every optional field
    is normalized here, once, rather than trusted at each of its several call sites.
    """
    if plan.get("refusal"):
        sys.exit(f"DeepSeek declined to plan this request: {plan['refusal']}")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        sys.exit(f"DeepSeek's plan had no usable steps: {json.dumps(plan)[:500]}")
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or not step.get("goal") or not step.get("verify"):
            sys.exit(f"Step {i + 1} is missing goal/verify: {json.dumps(step)[:300]}")
        if not (step.get("process") or step.get("window")):
            sys.exit(f"Step {i + 1} names neither a process nor a window: {json.dumps(step)[:300]}")
        for key, expected in (("constraint", list), ("verify", list), ("input", dict), ("shortcut", dict)):
            value = step.get(key)
            if value is not None and not isinstance(value, expected):
                sys.exit(f"Step {i + 1}'s {key!r} must be a {expected.__name__} or null: {json.dumps(step)[:300]}")
            step[key] = value if value is not None else expected()
    return steps


def describe_step(i: int, total: int, step: dict) -> str:
    target = " / ".join(filter(None, [step.get("process"), step.get("window")]))
    lines = [f"[{i}/{total}] target: {target}" + (f"  (launch: {step['launch']})" if step.get("launch") else "")]
    lines.append(f"      goal: {step['goal']}")
    for v in step["verify"]:
        lines.append(f"      verify: {v}")
    for c in step["constraint"]:
        lines.append(f"      constraint: {c}")
    if step["input"]:
        lines.append(f"      input: {step['input']}")
    if step["shortcut"]:
        lines.append(f"      shortcut: {step['shortcut']}")
    return "\n".join(lines)


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

    deepseek_key = load_deepseek_key()
    policy = TypeSafeJevPolicy()  # fails on a missing JEV key before anything else runs

    print(f"Planning with {args.deepseek_model}...\n")
    plan = call_deepseek(args.request, api_key=deepseek_key, model=args.deepseek_model)
    steps = parse_steps(plan)

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
        except RuntimeError as exc:
            print(f"\nstep {i} failed before running: {exc}")
            outcomes.append((step["goal"], f"error: {exc}"))
            if confirm("Continue to the next step anyway? [y/n]: ") != "y":
                break
            continue

        print(f"\nstep {i} result: {result.status.value} after {result.actions_taken} actions")
        if result.reason:
            print("reason:", result.reason)
        print()
        outcomes.append((step["goal"], result.status.value))
        if result.status != TerminalKind.SUBTASK_COMPLETE:
            if confirm("That step did not complete. Continue to the next step anyway? [y/n]: ") != "y":
                break

    print("\n=== Plan summary ===")
    for goal, status in outcomes:
        print(f"  [{status}] {goal}")


if __name__ == "__main__":
    main()
