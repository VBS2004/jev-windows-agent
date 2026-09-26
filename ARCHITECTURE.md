# jev-windows-agent architecture

> This extends [`arc-cua`](https://github.com/shhivv/arc-cua) by shhivv with a Windows backend. The core model below (data model, runtime loop, JEV policy, safety mechanisms) is the original project's design; the Windows-specific sections are new.

A map of how the whole system fits together: what owns what, how one action gets
from "the planner wants this" to "the OS did it," and where each safety check
sits. For a line-by-line tour of the code, see `docs/walkthrough.md`. For the
Windows port's implementation notes and status, see
`docs/windows-backend-handoff.md`.

---

## The one-sentence version

A planner (any LLM, or deterministic code) hands `jev-windows-agent` a bounded `Subtask`
— goal, literal inputs, success criteria — and `jev-windows-agent` runs the
observe → decide → validate → execute → settle loop until the subtask
completes, gets blocked, or needs a human/bigger model, using JEV (via
TypeSafe) as a fast per-step decision model instead of a frontier LLM call per
click.

```
planner ──Subtask──▶ jev-windows-agent ──ExecutionResult──▶ planner
                        │
                        │  observe → decide → validate → execute → settle
                        │  (repeats until terminal)
                        ▼
                  real desktop (macOS AX/OCR, or Windows UIA)
```

---

## Who owns what

This split is the whole design. Every safety property in the system comes
from keeping these four responsibilities in their own layer and never letting
one improvise another's job.

| Layer | Owns | Never does |
|---|---|---|
| **Planner** (external, not in this repo) | Intent, literal text/values, constraints, success criteria (`Subtask`) | Touch the OS directly |
| **Backend** (`DesktopBackend`) | Turning the OS into structured elements; native execution | Decide *what* to do next |
| **Policy** (`DecisionPolicy`, JEV in production) | Picking the next operation/target from *currently observed* choices | Invent an id, a selector, or literal text; touch the OS |
| **Runtime** (`DesktopExecutor`) | Loop control, freshness checks, settling, termination, retries | Decide what a click means, or what "done" means |

Nothing above the backend layer ever sees pixels or coordinates — those are a
backend-internal detail. Nothing below the policy layer ever invents a target
— every id, hotkey, and scroll direction the policy can choose came from the
current snapshot or the subtask itself.

---

## Data flow: one action, end to end

```
┌─────────────────────────────────────────────────────────────────────┐
│ DesktopExecutor.run(subtask)                                        │
│                                                                       │
│  backend.observe() ────────────────────────▶ DesktopSnapshot #1     │
│                                                                       │
│  ┌─ loop (bounded by subtask.max_actions) ───────────────────────┐  │
│  │                                                                 │  │
│  │  policy.decide(subtask, snapshot, history)                     │  │
│  │      TypeSafeJevPolicy: build operation + speculative          │  │
│  │      sub-questions → one JEV request → validate → Decision     │  │
│  │                                                                 │  │
│  │  [optional] confidence gate: below threshold? → NEEDS_AGENT    │  │
│  │                                                                 │  │
│  │  terminal decision? → optionally verify() → yield result, done │  │
│  │                                                                 │  │
│  │  materialize_action(decision, snapshot, subtask)                │  │
│  │      re-validate target/legality/input-key/hotkey against      │  │
│  │      the CURRENT snapshot — never trusts the policy's own      │  │
│  │      bookkeeping → ExecutableAction, or raise InvalidDecision  │  │
│  │                                                                 │  │
│  │  backend.is_fresh(snapshot, action)?                            │  │
│  │      no → re-observe, retry (bounded)                          │  │
│  │                                                                 │  │
│  │  backend.execute(snapshot, action) ── native AX/UIA/SendInput  │  │
│  │                                                                 │  │
│  │  _observe_after_action(...) ── poll until structurally stable  │  │
│  │      or timeout → DesktopSnapshot #n+1                         │  │
│  │                                                                 │  │
│  │  record ActionRecord; N consecutive no-ops? → BLOCKED          │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  → SUBTASK_COMPLETE / BLOCKED / NEEDS_AGENT                          │
└─────────────────────────────────────────────────────────────────────┘
        │
        ▼
   api.result_to_dict() ──▶ planner (plain JSON, no SDK coupling)
```

Every arrow above is a checkpoint, not a formality:

- **Policy → validation**: the policy's own request-time check
  (`_validate_choice`) rejects a malformed/hallucinated provider response
  before it's even turned into a `Decision`. `materialize_action` then
  independently re-derives legality from the snapshot — two checks, from two
  different trust boundaries, before anything can execute.
- **Validation → freshness**: even a *valid* decision can be stale by the time
  it's acted on (the UI moved between observation and execution).
  `is_fresh` re-resolves the actual target and compares a semantic fingerprint
  (`semantic_guard()`), not just an id — an id can survive while what it
  points at silently changed underneath it.
- **Execute → settle**: the runtime, not the policy, decides when the UI is
  "ready to reason over again." A decision model that had to guess its own
  settling time would either race the UI or waste latency on a fixed sleep;
  the runtime instead polls until two consecutive structural signatures match.

---

## Core data model (`models.py`)

```
DesktopElement          one normalized, currently-observable control
  id, role, name, value, actions: tuple[ActionKind, ...]
  enabled, visible, focused, selected, expanded
  source ("macos_ax" | "macos_ocr" | "windows_uia" | ...)
  semantic_guard() ──▶ hash of the fields that matter for freshness

DesktopSnapshot          one full observation
  application, window, revision (content-addressed fingerprint)
  elements: tuple[DesktopElement, ...], context: dict

Subtask                  the planner's contract — never invented by jev-windows-agent
  goal, verification (required), inputs, constraints,
  max_actions, shortcuts, metadata

Decision                 what the policy chose for one step
  kind: ActionKind  XOR  terminal: TerminalKind  (never both)
  target_id, input_key, hotkey, scroll_direction, confidence, ...

ExecutableAction          the VALIDATED, backend-ready form of a Decision
  (produced by validation.materialize_action; ids resolved, literals
   resolved from Subtask.inputs, legality re-derived from the snapshot)

ActionRecord / ExecutionResult    the audit trail the planner gets back
```

`ActionKind`: `CLICK`, `DOUBLE_CLICK`, `RIGHT_CLICK`, `TYPE_TEXT`,
`PRESS_KEY`, `HOTKEY`, `SCROLL`, `DRAG_TO`, `DRAG_BY` (disabled — see below),
`SET_VALUE`, `WAIT`.
`TerminalKind`: `SUBTASK_COMPLETE`, `BLOCKED`, `NEEDS_AGENT`.

---

## The decision policy: JEV via TypeSafe (`policies/typesafe.py`)

One HTTP request per step, using TypeSafe's **Choice** primitive as a
speculative fan-out / function-calling pattern:

1. **`operation`** — a Choice over every currently *legal* action kind (only
   offered if some element/global action actually supports it right now)
   plus the three terminal states.
2. **One `<kind>_target` Choice per targeted kind** — criteria are the
   candidate elements themselves, straight from the current snapshot.
3. **`<kind>_input`** for `TYPE_TEXT`/`SET_VALUE` — JEV picks an *input key*,
   never a literal value. The runtime resolves the key to
   `Subtask.inputs[key]` afterward. The model cannot invent text this way
   even in principle — it never sees a free-text slot.
4. **`press_key_value`, `hotkey_value`, `scroll_direction`** — always
   offered, drawn from defaults plus the subtask's own declared shortcuts.

All of this goes out in **one round trip**; only the branch matching the
chosen `operation` is ever consumed. This is what makes the common case
(pick an operation and its target) cost one request instead of two
serialized ones.

**Why `DRAG_BY` is excluded**: a continuous numeric displacement is a bad fit
for a discrete Choice primitive. Left as a clean extension point rather than
forced into the question shape.

**Defense in depth on every answer** (`_validate_choice`): the chosen id was
actually offered, the probability keys exactly match the offered set, every
number is finite and in `[0, 1]`, probabilities sum to ~1, and the declared
choice really is the argmax. Any violation raises immediately — **no action
executes on a malformed or hallucinated response.**

**Transport**: the same request/response shape reaches TypeSafe's own
`api.typesafe.ai/v1/systemone` endpoint or OpenRouter's Decisions API
(`openrouter.ai/api/alpha/decisions`, model `~typesafe/jev-latest`) — the
constructor picks whichever key (`TYPESAFE_API_KEY` or `OPENROUTER_API_KEY`)
is set, TypeSafe taking precedence. Retries on 429/503/529 with exponential
backoff; anything else raises immediately rather than silently swallowing a
bad decision.

**Confidence is surfaced, and optionally gated** (`runtime.py`,
`RuntimeConfig.confidence_thresholds`): every Choice answer carries a
`confidence` (how concentrated the probability distribution was). By default
nothing is gated on it, but a per-`ActionKind`/`TerminalKind` threshold table
(`SUGGESTED_CONFIDENCE_THRESHOLDS` is a starting point, not a default) turns
an under-confident step into an escalation to `NEEDS_AGENT` **before**
`materialize_action` runs — nothing touches the OS. Thresholds are per-kind
because being wrong isn't equally costly everywhere: an unwanted `SCROLL`
costs nothing, `TYPE_TEXT` mutates the user's data, and a wrong
`SUBTASK_COMPLETE` misreports the task as done to the planner.

---

## Perception + execution backends (`backends/`)

Both real backends implement the same three-method protocol
(`protocols.py`):

```python
class DesktopBackend(Protocol):
    def observe(self) -> DesktopSnapshot: ...
    def is_fresh(self, snapshot, action: ExecutableAction) -> bool: ...
    def execute(self, snapshot, action: ExecutableAction) -> None: ...
```

Structural typing, no inheritance — a new OS backend is purely additive.
Nothing in `runtime.py`, `validation.py`, `models.py`, or
`policies/typesafe.py` needs to change for a new backend; the Windows port
proved this (zero changes to those files).

### macOS — `macos_ax.py` + `macos_ocr.py` + `macos_hybrid.py`

The production backend composes two perception sources:

- **Accessibility (AX)**: semantic controls — buttons, fields, menus, roles,
  values, native actions (`AXUIElementPerformAction`, `AXUIElementSetAttributeValue`).
- **Apple Vision OCR**: for apps with incomplete accessibility. Emits
  `visible_text` elements with bounding boxes; only regions that look like
  plausible text-entry get `TYPE_TEXT`, everything else is click-only. The
  screenshot never leaves the machine or reaches JEV — only structured text
  + bounds does.

`MacOSHybridBackend` merges them: AX beats OCR for the same control, active
modals get their AX subtree isolated (JEV can't reach through a dialog to
the window behind it) with OCR clipped to the modal's bounds, and
overlapping OCR detections are deduplicated by IoU. Execution dispatches
per-target: AX-sourced elements go through native AX calls; OCR-sourced
elements execute via raw `Quartz` coordinate events after re-verifying
frontmost pid + window id (OCR bounds are meaningless once the wrong window
is frontmost). Text entry into an OCR-only field is
`click → Cmd+A → per-character Unicode CGEvent`, with modifier flags zeroed
per character so a stray Cmd bit can't turn typed text into a shortcut.

### Windows — `windows_uia.py`

UI Automation only, no OCR fallback yet (a deliberate v0 cut, mirroring how
macOS started). Same shape as `macos_ax.py`, different native layer:

| macOS | Windows |
|---|---|
| `AXUIElementCreateApplication` + `AXFocusedWindow` | `GetForegroundWindow` + `ElementFromHandle` |
| `AXRole` | `ControlType` → role string |
| `AXUIElementPerformAction(..., "AXPress")` | `InvokePattern.Invoke()` (also Toggle, SelectionItem, ExpandCollapse) |
| `AXUIElementSetAttributeValue(..., "AXValue")` | `ValuePattern.SetValue()` / `RangeValuePattern.SetValue()` |
| `repr(ref)` hash for identity | `GetRuntimeId()` — UIA's purpose-built stable id |
| `CGEventPost` keyboard/mouse events | `SendInput` with `KEYBDINPUT`/`MOUSEINPUT` |

Notable departures from straight AX parity, each found by running on real
apps (Notepad, Settings, Explorer, Chrome) rather than assumed up front:

- **One cached tree read per observation.** UIA property reads are
  cross-process COM calls; a per-property walk would make settling (which
  calls `observe()` repeatedly) take seconds. A single `BuildUpdatedCache`
  over the whole subtree keeps it at 60–140ms.
- **Pattern state is only read when the matching `Is*PatternAvailable`
  property is true.** `GetCachedPropertyValue` returns *type defaults*, not
  a not-supported marker, for patterns an element lacks — every element
  would otherwise look half-toggled and expandable.
- **Owned popup windows are walked too.** Menus and flyouts are separate
  top-level windows (a `PopupHost`); ignoring them makes an opened menu
  invisible to the policy.
- **Documents are typed into with real keystrokes, not `ValuePattern.SetValue`.**
  Windows 11 Notepad accepts `SetValue` but its dirty flag ignores it — a
  planner checking "was it saved?" would be misled. Confirmed live: offering
  both split JEV's confidence and led to a `BLOCKED` retry loop; keystroke-only
  typing completed the same task in 2 actions.
  Keystrokes are paced one `SendInput` call per character with a read-back
  verify, because batched Unicode events resolve their character late and a
  target that stalls (spell-check on each space) corrupts the text.
- **`CLICK` falls back to a synthesized pointer click** for clickable roles
  with no pattern (macOS refuses without `AXPress`), but only after
  `ElementFromPoint` confirms the click would actually land on the target —
  otherwise an occluding window silently eats the input.
- **`is_fresh` compares the foreground *window*, not just the process id** —
  stricter than AX's pid check, so a same-app dialog invalidates targets in
  the window behind it. A vanished element's `COMError` is caught and
  reported as stale, never left to propagate (the runtime doesn't guard
  `is_fresh`).
- **Per-monitor DPI awareness + virtual-desktop-normalized coordinates**, so
  synthesized clicks land correctly on scaled and multi-monitor setups —
  both silent-miss bugs if skipped.

Known limitation, not yet solved: apps that draw their own UI (canvases,
video timelines, most Electron content without accessibility forced on)
expose little to UIA and have no OCR fallback here yet — see
`docs/windows-backend-handoff.md` for what's next.

### `memory.py` — `StateMachineBackend`

An in-memory fake desktop for tests/examples: a caller-supplied `transition`
function and `snapshot_factory`, no real OS involved. Used to validate the
JEV policy's decision-making logic in isolation, and by `ScriptedPolicy` for
deterministic runtime tests.

---

## Safety mechanisms, gathered in one place

| Mechanism | Where | Stops |
|---|---|---|
| Choice options are a closed set (never free text) | `policies/typesafe.py` | The model inventing a selector or literal value |
| `_validate_choice` (argmax/probabilities/membership check) | `policies/typesafe.py` | Acting on a malformed/hallucinated provider response |
| `materialize_action` re-derives legality from the *current* snapshot | `validation.py` | Trusting the policy's own bookkeeping about what's legal |
| `is_fresh` + `semantic_guard()` | backend `is_fresh()` | Executing a decision against UI state that already changed |
| Runtime-owned settling (poll until structurally stable) | `runtime.py` | Racing the UI, or the policy having to guess timing |
| `no_change_limit` consecutive-no-op detection | `runtime.py` | Infinite loops with no structural progress → `BLOCKED` |
| Optional confidence gating | `runtime.py` | Acting on a decision the model itself flagged as unsure |
| Caller-supplied `verify()` callback | `runtime.py` | JEV being optimistic about `SUBTASK_COMPLETE` when structured state can't establish it |
| Hotkey allow-list (defaults + subtask-declared) | `keyboard.py` + `validation.py` | An arbitrary/dangerous keyboard shortcut, even from a custom policy |
| Modal isolation (macOS) / per-window scoping (Windows) | `macos_hybrid.py`, example wrappers | The model "reaching through" a dialog to the window behind it |
| `max_actions` / wall-clock `timeout_s` | `runtime.py` | An unbounded run |

---

## Terminal states

| Status | Meaning | Who can trigger it |
|---|---|---|
| `SUBTASK_COMPLETE` | Verification criteria appear satisfied | Policy, gated by optional `verify()` and confidence |
| `BLOCKED` | No supported operation is making progress | Runtime (no-change-limit), or policy |
| `NEEDS_AGENT` | Needs higher-level reasoning, ran out of budget, timed out, was cancelled, or a decision was too uncertain | Runtime or policy |

The caller always owns what happens next — `jev-windows-agent` never retries a subtask
on its own initiative past these boundaries.

---

## The planner-facing contract (`api.py`)

The stable, SDK-agnostic JSON boundary a planner actually talks to:

```python
subtask_from_dict(payload)   # strict allow-list of fields, no silent extras
result_to_dict(result)       # flattens ExecutionResult to plain JSON
execute_payload(executor, payload)   # chains the two around DesktopExecutor.run
```

No coupling to a specific LLM SDK — the planner can be anything that can
produce this JSON shape and read it back.

---

## Repository map

```
src/jev_windows_agent/
  models.py            data model: DesktopElement, Snapshot, Subtask, Decision, ...
  protocols.py          DesktopBackend / DecisionPolicy interfaces
  runtime.py            DesktopExecutor — the observe/decide/execute/settle loop
  validation.py          materialize_action — last checkpoint before execution
  keyboard.py            shared hotkey syntax (MOD/CTRL/ALT/SHIFT + key)
  errors.py               StaleDesktopState, InvalidDecision, UnsupportedDesktopAction
  api.py                  planner-facing JSON contract
  backends/
    macos_ax.py           macOS Accessibility
    macos_ocr.py           Apple Vision OCR
    macos_hybrid.py        AX + OCR, production macOS backend
    windows_uia.py          Windows UI Automation + SendInput
    memory.py                in-memory fake desktop for tests
  policies/
    typesafe.py             JEV via TypeSafe (or OpenRouter)
    scripted.py               deterministic policy for tests/examples

examples/
  effects_demo.py, typesafe_demo.py     deterministic demos, no API key
  macos_ax_probe.py, ocr_probe.py         macOS perception probes
  test_settings.py, test_spotify.py        macOS end-to-end JEV runs
  windows_uia_probe.py                      Windows perception probe
  windows_notepad_smoke.py                    Windows end-to-end, no API key
  test_notepad.py, windows_task.py             Windows end-to-end JEV runs

docs/
  walkthrough.md                a guided, module-by-module code tour
  internals.md                    concept-level notes (perception, freshness, OCR stability)
  windows-backend-handoff.md        Windows port spec + implementation status
```

Install extras: `pip install -e '.[macos]'` or `pip install -e '.[windows]'`
(`comtypes` on Windows, `pyobjc-framework-*` on macOS) — both are optional;
the core package has no OS-specific dependency.

---

## Where this can generalize next

The `DesktopBackend` protocol is the whole extension point. Anything that can
produce `DesktopElement`s and execute an `ExecutableAction` slots in without
touching the runtime, validation, or policy layers. The two live gaps, both
scoped in `docs/windows-backend-handoff.md`:

- **OCR fallback on Windows**, mirroring `macos_ocr.py`/`macos_hybrid.py`,
  to reach browser content and custom-drawn UIs the way macOS already can.
- **Cross-window / cross-app orchestration** stays outside `jev-windows-agent` by
  design — the planner decides which window to bring forward and splits work
  into one bounded `Subtask` per app; the runtime's job stops at "run this
  one bounded task against whatever's in front."
