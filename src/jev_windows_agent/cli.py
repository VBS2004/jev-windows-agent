"""`jev`: a colorful terminal front end for driving Windows apps with JEV.

Two subcommands, both bounded to a real app window and neither able to make JEV
invent a click, a target, or a piece of text:

  jev run  --process notepad --goal "..." --verify "..." [--input name=value]
      One subtask, one window you name. The direct, scriptable form -- the
      plain-text original is examples/windows_task.py; this is the same call
      into jev_windows_agent.runner.run_windowed_subtask, styled.

  jev plan --request "Turn on dark mode, then open Notepad and write today's date"
      One big request, several apps. DeepSeek proposes an ordered plan; you
      confirm each step before it touches anything. The plain-text original is
      examples/planner.py; this is the same call into jev_windows_agent.planner,
      styled.

Installed as the `jev` command by `pip install -e .`; also runnable as
`python -m jev_windows_agent.cli`.
"""

from __future__ import annotations

import argparse
import json
import sys

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from . import ExecutionResult, StepEvent, Subtask, TerminalKind
from .api import result_to_dict
from .planner import DEFAULT_DEEPSEEK_MODEL, PlannerError, load_deepseek_key, plan_request
from .policies import TypeSafeJevPolicy
from .runner import TargetWindowError, WindowScope, describe_target, load_project_env, run_windowed_subtask

THEME = Theme({
    "jev.brand": "bold magenta",
    "jev.dim": "grey58",
    "jev.accent": "cyan",
    "jev.complete": "bold green",
    "jev.blocked": "bold yellow",
    "jev.needs_agent": "bold yellow",
    "jev.error": "bold red",
    "jev.conf.high": "bold green",
    "jev.conf.mid": "yellow",
    "jev.conf.low": "bold red",
})

_BANNER_LINES = (
    r"     ██╗███████╗██╗   ██╗",
    r"     ██║██╔════╝██║   ██║",
    r"     ██║█████╗  ██║   ██║",
    r"██   ██║██╔══╝  ╚██╗ ██╔╝",
    r"╚█████╔╝███████╗ ╚████╔╝ ",
    r" ╚════╝ ╚══════╝  ╚═══╝  ",
)
_BANNER_GRADIENT = ("bright_magenta", "magenta", "medium_purple", "medium_purple", "blue_violet", "blue_violet")

_ACTION_ICON = {
    "CLICK": "[cyan]◉[/]",  # ◉
    "DOUBLE_CLICK": "[cyan]◉◉[/]",
    "RIGHT_CLICK": "[cyan]◉→[/]",
    "TYPE_TEXT": "[cyan]⌨[/]",  # ⌨
    "PRESS_KEY": "[cyan]⌨[/]",
    "HOTKEY": "[cyan]⌨[/]",
    "SCROLL": "[cyan]↕[/]",  # ↕
    "DRAG_TO": "[cyan]✋[/]",  # ✋
    "DRAG_BY": "[cyan]✋[/]",
    "SET_VALUE": "[cyan]⚙[/]",  # ⚙
    "WAIT": "[jev.dim]⏳[/]",  # ⏳
    "SUBTASK_COMPLETE": "[jev.complete]✓[/]",  # ✓
    "BLOCKED": "[jev.blocked]⛔[/]",  # ⛔
    "NEEDS_AGENT": "[jev.needs_agent]?[/]",
}

_STATUS_STYLE = {
    TerminalKind.SUBTASK_COMPLETE: "jev.complete",
    TerminalKind.BLOCKED: "jev.blocked",
    TerminalKind.NEEDS_AGENT: "jev.needs_agent",
}


def make_console() -> Console:
    return Console(theme=THEME, highlight=False)


def print_banner(console: Console) -> None:
    console.print()
    for line, style in zip(_BANNER_LINES, _BANNER_GRADIENT):
        console.print(Text(line, style=f"bold {style}"))
    console.print("  [jev.dim]a fast decision loop for Windows desktop agents, powered by JEV[/]\n")


def action_icon(name: str) -> str:
    return _ACTION_ICON.get(name, "•")


def confidence_style(confidence: float | None) -> str:
    if confidence is None:
        return "jev.dim"
    if confidence >= 0.8:
        return "jev.conf.high"
    if confidence >= 0.5:
        return "jev.conf.mid"
    return "jev.conf.low"


def confidence_text(confidence: float | None) -> Text:
    label = "n/a" if confidence is None else f"{confidence:.0%}"
    return Text(label, style=confidence_style(confidence))


def new_step_table() -> Table:
    table = Table(show_header=True, header_style="bold", expand=False, border_style="grey35")
    table.add_column("#", justify="right", style="jev.dim", width=3)
    table.add_column("action", width=14)
    table.add_column("target", overflow="ellipsis", max_width=42)
    table.add_column("confidence", justify="right", width=10)
    table.add_column("top candidates", style="jev.dim", overflow="ellipsis", max_width=40)
    table.add_column("ms", justify="right", style="jev.dim", width=6)
    return table


def event_row(event: StepEvent, scope: WindowScope) -> tuple[Text | str, ...] | None:
    """One table row for a StepEvent, or None for the trailing terminal re-yield."""
    if event.result is not None and event.record is not None:
        return None  # the runtime re-yields the last step alongside its result
    decision = event.decision
    what = (decision.kind or decision.terminal).value

    target_label = ""
    if event.record and event.record.target_name:
        target_label = event.record.target_name
    else:
        described = describe_target(scope, event.action)
        if described is not None:
            label, named = described
            target_label = label if named else f"(unnamed {label})"
        elif event.action is not None:
            target_label = event.action.hotkey or event.action.key or event.action.scroll_direction or ""

    operation = decision.raw.get("answers", {}).get("operation", {}) if decision.raw else {}
    ranked = sorted(operation.get("probabilities", {}).items(), key=lambda kv: kv[1], reverse=True)[:3]
    top = ", ".join(f"{name} {p:.0%}" for name, p in ranked if p > 0)

    action_cell = Text.from_markup(f"{action_icon(what)} {what}")
    latency = "" if decision.latency_ms is None else str(decision.latency_ms)
    return (str(event.step), action_cell, target_label, confidence_text(decision.confidence), top, latency)


def result_panel(result: ExecutionResult, *, title: str = "Result") -> Panel:
    style = _STATUS_STYLE.get(result.status, "jev.dim")
    icon = action_icon(result.status.value)
    body = Text.from_markup(f"{icon} [{style}]{result.status.value}[/{style}] after {result.actions_taken} action(s)")
    if result.reason:
        body.append(f"\n{result.reason}", style="jev.dim")
    return Panel(body, title=title, border_style=style, expand=False)


def run_one(
    console: Console,
    policy: TypeSafeJevPolicy,
    *,
    subtask: Subtask,
    process: str | None,
    window: str | None,
    launch: str | None,
    confidence_gate: bool,
) -> ExecutionResult:
    """The colorized equivalent of run_windowed_subtask's default plain-text trace.

    A spinner covers the pre-loop phases (finding the window, bringing it forward),
    then hands off to a live-growing table of JEV's decisions once the loop starts.
    """
    table = new_step_table()
    status = console.status("[jev.accent]Starting...[/]", spinner="dots")
    status.start()
    live: Live | None = None

    def on_phase(message: str) -> None:
        status.update(f"[jev.accent]{message}[/]")

    def on_event(event: StepEvent, scope: WindowScope) -> None:
        nonlocal live
        row = event_row(event, scope)
        if not console.is_terminal:
            # Redirected to a file or a pipe: a Live display can't repaint in place,
            # so it would append the whole table again on every step. One line each.
            status.stop()
            if row is not None:
                console.print(f"step {row[0]}: {row[1]} {row[2]}  confidence={row[3].plain}")
            return
        if live is None:
            status.stop()
            live = Live(table, console=console, refresh_per_second=8, transient=False)
            live.start()
        if row is not None:
            table.add_row(*row)
        live.refresh()

    try:
        result = run_windowed_subtask(
            policy,
            subtask=subtask,
            process=process,
            window=window,
            launch=launch,
            confidence_gate=confidence_gate,
            on_phase=on_phase,
            on_event=on_event,
        )
    finally:
        status.stop()
        if live is not None:
            live.stop()
    console.print(result_panel(result))
    return result


def add_task_arguments(parser: argparse.ArgumentParser) -> None:
    def key_value(text: str) -> tuple[str, str]:
        if "=" not in text:
            raise argparse.ArgumentTypeError(f"expected NAME=VALUE, got {text!r}")
        name, value = text.split("=", 1)
        return name.strip(), value

    parser.add_argument("--input", action="append", type=key_value, default=[], dest="input",
                        help="literal text JEV may type, as NAME=VALUE (repeatable)")
    parser.add_argument("--shortcut", action="append", type=key_value, default=[],
                        help='extra chord JEV may press, as "MOD+S=what it does" (repeatable)')
    parser.add_argument("--max-actions", type=int, default=15)
    parser.add_argument("--confidence-gate", action="store_true",
                        help="hand back to you when JEV is less sure than SUGGESTED_CONFIDENCE_THRESHOLDS")


def cmd_run(args: argparse.Namespace, console: Console) -> int:
    if not (args.window or args.process):
        console.print("[jev.error]Name the target with --window or --process.[/]")
        return 2
    load_project_env()
    try:
        policy = TypeSafeJevPolicy()
    except ValueError as exc:
        console.print(f"[jev.error]{exc}[/]")
        return 1
    console.print(f"[jev.dim]JEV via[/] {policy.base_url}\n")

    subtask = Subtask(
        goal=args.goal,
        verification=tuple(args.verify),
        constraints=tuple(args.constraint),
        inputs=dict(args.input),
        shortcuts=dict(args.shortcut),
        max_actions=args.max_actions,
    )
    try:
        result = run_one(
            console, policy, subtask=subtask, process=args.process, window=args.window,
            launch=args.launch, confidence_gate=args.confidence_gate,
        )
    except (TargetWindowError, RuntimeError) as exc:
        console.print(f"[jev.error]{exc}[/]")
        return 1

    if args.json:
        summary = result_to_dict(result)
        summary.pop("final_snapshot")
        console.print_json(json.dumps(summary, default=str))
    return 0 if result.status == TerminalKind.SUBTASK_COMPLETE else 1


def plan_table(steps: list[dict]) -> Table:
    table = Table(show_header=True, header_style="bold", border_style="grey35")
    table.add_column("#", justify="right", style="jev.dim", width=3)
    table.add_column("target")
    table.add_column("goal")
    table.add_column("verify / constraint", style="jev.dim")
    for i, step in enumerate(steps, 1):
        target = " / ".join(filter(None, [step.get("process"), step.get("window")]))
        if step.get("launch"):
            target += f"\n[jev.dim](launch: {step['launch']})[/]"
        criteria = "\n".join([f"✓ {v}" for v in step["verify"]] + [f"✗ {c}" for c in step["constraint"]])
        table.add_row(str(i), target, step["goal"], criteria)
    return table


def confirm_step(console: Console, i: int, total: int, *, assume_yes: bool = False) -> str:
    if assume_yes:
        console.print(f"[jev.dim]Running step {i}/{total} (--yes)[/]")
        return "y"
    while True:
        answer = console.input(f"[jev.accent]Run step {i}/{total}?[/] [bold]\\[y][/]es / [bold]\\[n][/]o skip / "
                                f"[bold]\\[q][/]uit: ").strip().lower()
        if answer in ("y", "n", "q", ""):
            return answer or "y"
        console.print("[jev.dim]Please answer y, n, or q.[/]")


def continue_after(console: Console, step: int, total: int, *, assume_yes: bool = False) -> bool:
    if step >= total:
        return False
    if assume_yes:
        # --yes approves the plan, it does not ignore a step that failed: carrying on
        # from a step that did not do what it claimed is how a run compounds a mistake.
        console.print("[jev.blocked]Stopping: a step did not complete (--yes does not skip past that).[/]")
        return False
    answer = console.input(
        f"[jev.blocked]Step {step} did not complete.[/] Continue to step {step + 1}/{total} anyway? [y/N]: "
    ).strip().lower()
    return answer == "y"


def cmd_plan(args: argparse.Namespace, console: Console) -> int:
    try:
        deepseek_key = load_deepseek_key()
        policy = TypeSafeJevPolicy()
    except (PlannerError, ValueError) as exc:
        console.print(f"[jev.error]{exc}[/]")
        return 1
    console.print(f"[jev.dim]JEV via[/] {policy.base_url}  [jev.dim]|  planning with[/] {args.deepseek_model}\n")

    with console.status(f"[jev.accent]Planning with {args.deepseek_model}...[/]", spinner="dots"):
        try:
            steps = plan_request(args.request, api_key=deepseek_key, model=args.deepseek_model)
        except PlannerError as exc:
            console.print(f"[jev.error]{exc}[/]")
            return 1

    console.print(f"[bold]Proposed plan[/] ({len(steps)} step{'s' if len(steps) != 1 else ''}):\n")
    console.print(plan_table(steps))
    console.print()

    outcomes: list[tuple[str, str]] = []
    for i, step in enumerate(steps, 1):
        answer = confirm_step(console, i, len(steps), assume_yes=args.yes)
        if answer == "q":
            console.print("[jev.dim]Stopped.[/]")
            break
        if answer != "y":
            outcomes.append((step["goal"], "skipped"))
            continue

        subtask = Subtask(
            goal=step["goal"], verification=tuple(step["verify"]), constraints=tuple(step["constraint"]),
            inputs=dict(step["input"]), shortcuts=dict(step["shortcut"]), max_actions=args.max_actions,
        )
        try:
            result = run_one(
                console, policy, subtask=subtask, process=step.get("process"), window=step.get("window"),
                launch=step.get("launch"), confidence_gate=args.confidence_gate,
            )
        except TargetWindowError as exc:
            console.print(f"[jev.error]Step {i} did not start:[/] {exc}")
            outcomes.append((step["goal"], f"not started: {exc}"))
            if not continue_after(console, i, len(steps), assume_yes=args.yes):
                break
            continue
        except Exception as exc:  # noqa: BLE001 -- report any mid-run failure, then let the user decide
            console.print(f"[jev.error]Step {i} stopped during the run:[/] {type(exc).__name__}: {exc}")
            outcomes.append((step["goal"], f"stopped: {type(exc).__name__}: {exc}"))
            if not continue_after(console, i, len(steps), assume_yes=args.yes):
                break
            continue

        outcomes.append((step["goal"], result.status.value))
        if result.status != TerminalKind.SUBTASK_COMPLETE and not continue_after(
            console, i, len(steps), assume_yes=args.yes
        ):
            break

    console.print("\n[bold]Plan summary[/]")
    for goal, status in outcomes:
        style = "jev.complete" if status == "SUBTASK_COMPLETE" else "jev.blocked" if status == "skipped" else "jev.error"  # noqa: E501
        console.print(f"  [{style}]•[/] {goal}  [jev.dim]({status})[/]")
    return 0 if outcomes and all(s == "SUBTASK_COMPLETE" for _, s in outcomes) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jev", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="run one subtask against one named window")
    where = run_parser.add_argument_group("target window (one of)")
    where.add_argument("--window", help="substring of the window title")
    where.add_argument("--process", help="executable name, e.g. notepad or explorer")
    run_parser.add_argument("--launch", help="command to start the app first, e.g. ms-settings: or notepad.exe")
    run_parser.add_argument("--goal", required=True, help="what to accomplish, in plain English")
    run_parser.add_argument("--verify", action="append", required=True,
                            help="observable success criterion (repeatable)")
    run_parser.add_argument("--constraint", action="append", default=[], help="something not to do (repeatable)")
    add_task_arguments(run_parser)
    run_parser.add_argument("--json", action="store_true", help="print the planner-facing result as JSON")
    run_parser.set_defaults(func=cmd_run)

    plan_parser = sub.add_parser("plan", help="plan and run a multi-step request across several apps")
    plan_parser.add_argument("--request", required=True, help="what you want done, in plain English")
    plan_parser.add_argument("--deepseek-model", default=DEFAULT_DEEPSEEK_MODEL)
    plan_parser.add_argument("--max-actions", type=int, default=15, help="per-step JEV action budget")
    plan_parser.add_argument("--confidence-gate", action="store_true",
                             help="hand a step back to you when JEV is less sure than SUGGESTED_CONFIDENCE_THRESHOLDS")
    plan_parser.add_argument("-y", "--yes", action="store_true",
                             help="approve every step up front and run the whole plan without stopping "
                                  "(a step that does not complete still stops the run)")
    plan_parser.set_defaults(func=cmd_plan)

    return parser


def main(argv: list[str] | None = None) -> int:
    console = make_console()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        print_banner(console)
        parser.print_help()
        return 0

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print_banner(console)
    try:
        return args.func(args, console)
    except KeyboardInterrupt:
        console.print("\n[jev.dim]Interrupted.[/]")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
