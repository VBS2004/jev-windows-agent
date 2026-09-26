# jev-windows-agent walkthrough

A guided read of how this codebase actually works, module by module, with the
control flow of one subtask end to end.

---

## The mental model

A **planner** (any LLM or deterministic code, outside this package) decides
*what* needs to happen and hands off a bounded `Subtask`. `jev-windows-agent` then owns
the tight observe → decide → execute → settle loop, using **JEV** (via
TypeSafe) as a fast per-step decision model instead of a frontier LLM call per
click. The loop terminates with `SUBTASK_COMPLETE`, `BLOCKED`, or
`NEEDS_AGENT`, and control returns to the planner.

Division of responsibility:

| Owns | Who |
|---|---|
| Intent, literal text/values, constraints, success criteria | External planner (`Subtask`) |
| Perception (turning the OS into structured elements) | `DesktopBackend` |
| Picking the next operation/target from *currently observed* choices | `DecisionPolicy` (JEV in production) |
| Loop control, freshness checks, settling, termination | `DesktopExecutor` (runtime) |

Nothing above the backend layer ever knows about pixels/coordinates — those
are a backend-internal detail (see `macos_hybrid.py`'s OCR click handling).

---

## Core data model — `models.py`

- **`DesktopElement`** — one normalized, currently-observable UI control.
  `id` is only stable for the backend session's lifetime; the model is never
  allowed to invent one. `compact()` produces the JSON-safe view actually sent
  to JEV. `semantic_guard()` hashes the element's meaningful fields (role,
  name, value, enabled, etc.) — used later for freshness/staleness checks.

- **`DesktopSnapshot`** — one full observation: `application`, `window`,
  a content-addressed `revision` fingerprint, all `elements`, and free-form
  `context`. `element(id)` does an indexed lookup (index built lazily).

- **`Subtask`** — the contract the planner hands in: `goal`, `verification`
  (required, planner-authored — the executor never invents these),
  `inputs` (literal values, keyed by name), `constraints`, `max_actions`,
  `shortcuts` (extra keyboard chords with descriptions), `metadata`.
  Validated in `__post_init__` (non-empty goal/verification, valid hotkey
  syntax, non-empty shortcut descriptions).

- **`Decision`** — what the policy chose for one step: either `kind` (an
  `ActionKind`) or `terminal` (a `TerminalKind`), never both. Carries
  whichever of `target_id` / `input_key` / `hotkey` / `scroll_direction` /
  etc. applies, plus `confidence`, `latency_ms`, and the raw provider
  response.

- **`ExecutableAction`** — the *validated*, backend-ready form of a decision
  (produced by `validation.materialize_action`), with target ids resolved to
  actual elements and guards attached, and literal values already resolved
  from `Subtask.inputs`.

- **`ActionRecord`** / **`ExecutionResult`** — the audit trail: one record per
  executed step (before/after revision, whether state changed, latency), and
  the final result the planner receives back.

`ActionKind` enumerates the whole action vocabulary: `CLICK`,
`DOUBLE_CLICK`, `RIGHT_CLICK`, `TYPE_TEXT`, `PRESS_KEY`, `HOTKEY`, `SCROLL`,
`DRAG_TO`, `DRAG_BY` (disabled — see below), `SET_VALUE`, `WAIT`.
`TerminalKind` is `SUBTASK_COMPLETE` / `BLOCKED` / `NEEDS_AGENT`.

---

## The decision policy — `policies/typesafe.py`

This is where TypeSafe/JEV plugs in, and it's a clean instance of the
**speculative fan-out / function-calling** pattern from the TypeSafe docs:
one HTTP request asks several parallel `choice` questions, and only the
branch matching the chosen `operation` is consumed.

### `_build_questions`

For the current snapshot, it groups visible+enabled elements by which
`ActionKind`s they support, then builds:

1. **`operation`** — a `Choice` question whose criteria are every currently
   legal action kind (only offered if some element/global action currently
   supports it) *plus* the three terminal states. Its `instructions` embed
   the subtask and a long `POLICY_RULES` string — this is the actual prompt
   engineering: literal text must come from `subtask.inputs`, only observed
   ids are legal, AX beats OCR when both represent the same control, OCR
   text isn't auto-editable, don't ping-pong between equivalent targets, etc.

2. **One `<kind>_target` choice per targeted kind** (`CLICK`, `DOUBLE_CLICK`,
   `RIGHT_CLICK`, `TYPE_TEXT`, `DRAG_TO`, `SET_VALUE`) — criteria are the
   candidate elements themselves (`element.compact()`).

3. **`drag_to_destination`** — only elements with `accepts_drop=True`; if
   none exist, `DRAG_TO` is pulled back out of the `operation` choices
   entirely (no dead-end branch offered).

4. **`<kind>_input`** for `TYPE_TEXT`/`SET_VALUE` — criteria are the
   planner-supplied `subtask.inputs` keys/values. JEV picks a *key*; the
   runtime resolves it to the literal value later. The model cannot invent
   text this way even in principle — it never sees a free-text slot.

5. **`press_key_value`**, **`hotkey_value`**, **`scroll_direction`** — always
   offered (global keyboard/scroll actions), from `DEFAULT_PRESS_KEYS` /
   `DEFAULT_HOTKEYS` ∪ `subtask.shortcuts` / `SCROLL_DIRECTIONS`.

Every question also gets `max_candidates` truncation (default 240) with the
overflow counted into `candidate_truncation` in the request body — a
transparency signal, not silent dropping.

### `decide`

Posts the whole state + question set once, then:

- Validates the `operation` answer via `_validate_choice` (see below).
- If it's a terminal kind, returns a terminal `Decision` immediately.
- Otherwise looks up only the matching sub-answer for that operation (e.g.
  `click_target`, `type_text_input`, `hotkey_value`) and builds an action
  `Decision`. All other parallel answers are simply discarded — that's the
  "ask broadly, consume narrowly" fan-out trick paying for itself: one round
  trip resolves operation *and* its parameter together instead of a second
  serialized call.

### `_validate_choice` — defense in depth

Before trusting any Choice answer, it checks: `choice` is one of the ids
that were actually offered, `probabilities` keys exactly match the offered
id set, every probability/confidence is a finite number in `[0, 1]`,
probabilities sum to ~1, and the declared `choice` really is the argmax.
Any violation raises `ValueError` and **no action executes**. This is the
guard against a malformed or hallucinated provider response ever reaching
the desktop.

### Transport — TypeSafe directly or via OpenRouter

The same request goes to TypeSafe's `https://api.typesafe.ai/v1/systemone` or to
OpenRouter's Decisions API (`https://openrouter.ai/api/alpha/decisions`, model
`~typesafe/jev-latest`), which returns identically shaped typed answers. With
only `OPENROUTER_API_KEY` set, the default constructor routes through
OpenRouter; `via_openrouter()` is the explicit form. An OpenRouter key is only
ever sent to OpenRouter, never to a caller-supplied `base_url`. Debugging note:
OpenRouter surfaced a request TypeSafe would reject as a malformed request as a
bare `404`.

### Retry/backoff — `_post`

Up to 3 attempts; retries on 429/503/529 with exponential backoff
(`0.5 * 2**attempt`), otherwise raises immediately so a bad decision never
gets silently swallowed.

### Why `DRAG_BY` is excluded

Explicitly documented in-code: a continuous numeric displacement is a bad
fit for a discrete `Choice` primitive. Left as a clean extension point
("a planner can expose named offsets as inputs") rather than forcing it into
the existing question shape.

---

## Turning a decision into a safe action — `validation.py`

`materialize_action` is the last checkpoint before anything touches the OS.
For targeted kinds it re-resolves `target_id` against the *current* snapshot
and re-checks `visible`, `enabled`, and that the chosen `ActionKind` is
actually in that element's `actions` tuple — i.e. it doesn't trust the
policy's own bookkeeping, it re-derives legality from the snapshot. Same
pattern for `DRAG_TO` destinations (`accepts_drop`), `TYPE_TEXT`/`SET_VALUE`
input keys (must exist in `subtask.inputs`), and `HOTKEY` (must be a default
or a subtask-declared shortcut — this check is independent of and in
addition to the one JEV's own request already applied). Any violation raises
`InvalidDecision`, which the runtime treats as fatal (not retried).

---

## The runtime loop — `runtime.py` (`DesktopExecutor`)

`run_iter()` is a generator that yields a `StepEvent` after every decision
cycle (useful for streaming/UI), and `run()` just drains it for the final
`ExecutionResult`.

Per iteration:

1. Check cancellation (`cancel()` sets a `threading.Event`) and wall-clock
   timeout (`RuntimeConfig.timeout_s`) — both exit as `NEEDS_AGENT`.
2. Ask the policy for a `Decision` against the *current* snapshot + history.
3. **Terminal decision** → if it's `SUBTASK_COMPLETE`, optionally run the
   caller-supplied `verify` callback (`RuntimeConfig.verify`); if that
   rejects it, downgrade to `NEEDS_AGENT` with a reason. This is the escape
   hatch for verification that structured state can't establish and JEV's
   own judgment might be optimistic about.
4. **Action decision** → `materialize_action` validates it, then
   `backend.is_fresh(snapshot, action)` checks the target hasn't changed
   since it was observed. If stale, snapshot is re-observed and the loop
   retries (bounded by `stale_retries`, default 8) — **a decision is never
   blindly replayed against changed state.**
5. `backend.execute(...)` runs it. `StaleDesktopState` from the backend is
   treated the same as a freshness-check failure (retry); any other
   exception ends the run as `NEEDS_AGENT`.
6. `_observe_after_action` — runtime-owned settling: polls
   `backend.observe()` until two consecutive structural signatures match (or
   a timeout), with a kind-specific minimum wait (0.65s for `TYPE_TEXT` since
   typed text can take longer to register, 0.18s for everything else that
   mutates state). This is the "runtime decides *when* the UI is ready,
   policy decides *what* to do" split from the README.
7. Record the step. If the last `no_change_limit` (default 3) actions all
   produced no revision change, terminate as `BLOCKED` — a loop-detection
   safety valve independent of JEV's own "don't repeat yourself" prompt rule.
8. Loop again, bounded by `subtask.max_actions`; exhausting it → `NEEDS_AGENT`.

`_structural_signature` is deliberately asymmetric: OCR element identity
ignores recognized text/confidence/geometry noise (Vision's read of the same
pixel can jitter between frames), while AX elements include value/focus/
selection state, since those changes are semantically real.

---

## Contracts and protocols — `protocols.py`, `api.py`

`protocols.py` defines the two extension points as `Protocol`s:
`DesktopBackend` (`observe`/`is_fresh`/`execute`) and `DecisionPolicy`
(`decide`). Anything satisfying these can replace JEV or the OS backend —
this is how `ScriptedPolicy` (deterministic, for tests/examples) and
`StateMachineBackend`/`MacOSHybridBackend` are interchangeable.

`api.py` is the stable, SDK-agnostic JSON boundary a planner actually talks
to: `subtask_from_dict` (strict allow-list of fields), `result_to_dict`
(flattens an `ExecutionResult` to plain JSON), and `execute_payload` which
chains the two around `DesktopExecutor.run`. This is exactly the
`execute_payload(executor, {...})` shown in the README.

---

## Perception backends — `backends/`

- **`memory.py`** (`StateMachineBackend`) — an in-memory fake desktop driven
  by a caller-supplied `transition` function and `snapshot_factory`. Used by
  `examples/effects_demo.py` and `examples/typesafe_demo.py` to validate the
  JEV policy's decision-making *without* touching a real OS — a deterministic
  test harness for the semantic layer.

- **`macos_ax.py`** — wraps the macOS Accessibility API: traverses the AX
  tree, maps roles/actions/values into `DesktopElement`s, executes native AX
  actions.

- **`macos_ocr.py`** — captures the target window and runs local Apple
  Vision OCR, emitting `visible_text` elements with bounding boxes. Only
  elements that look like plausible text-entry regions get `TYPE_TEXT` in
  their `actions` — the rest are click-only. The screenshot never leaves the
  machine or reaches JEV; only structured text + bounds does.

- **`macos_hybrid.py`** (`MacOSHybridBackend`) — the production backend,
  composing both:
  - Detects an active modal (`AXSheet`/`AXDialog`/`AXPopover`/`AXModal`) by
    walking the AX tree from the focused window; if one is active, *only*
    the modal's own AX subtree is exposed (background content is excluded)
    and OCR is clipped to the modal's bounds — so JEV can't "reach through"
    a dialog to the window behind it.
  - Deduplicates overlapping OCR detections by IoU ≥ 0.55, keeping the
    highest-confidence reading (`_dedupe_ocr`/`_iou`) — several noisy Vision
    reads of the same control collapse into one candidate.
  - `revision` is a hash over `(id, source, guard)` for every element plus
    modal state — this is the fingerprint the runtime's settling loop and
    `ActionRecord.state_changed` both key off of.
  - `is_fresh`/`execute` dispatch per-target: AX-sourced elements go through
    native AX execution; OCR-sourced elements execute via raw `Quartz`
    coordinate events (`_click`/`_drag`) against the target's bounds, after
    re-verifying frontmost pid + window id (`is_fresh`) — since OCR bounds
    are meaningless once the wrong window is frontmost.
  - Text entry into an OCR-only field is `click → Cmd+A → per-character
    Unicode CGEvent`, with modifier flags explicitly zeroed on each
    character event so a stray Cmd bit can't turn typed text into a
    shortcut.

- **`windows_uia.py`** (`WindowsUIABackend`) — the Windows counterpart of
  `macos_ax.py`: UI Automation through raw `comtypes` (no OCR fallback yet),
  `SendInput` for keyboard, pointer, and wheel events. `comtypes` is imported
  lazily, so `jev_windows_agent.backends` stays importable on macOS/Linux.
  - `observe()` fetches the foreground window's whole ControlView subtree in
    **one** cross-process `BuildUpdatedCache` call, then walks the cached
    copy — per-property UIA reads are cross-process COM calls and would make
    settling (which calls `observe()` repeatedly) take seconds. Measured
    60–140 ms for Chrome's frame, Notepad, Settings, and Explorer, with every
    id surviving a second observation.
  - Pattern state is only read when the matching `Is*PatternAvailable`
    property is true: `GetCachedPropertyValue` returns *type defaults* for
    unsupported patterns (`ToggleState` 2 = Indeterminate,
    `ExpandCollapseState` 3 = LeafNode), which would otherwise make every
    element look half-checked and expandable.
  - Ids come from `GetRuntimeId()`, UIA's purpose-built identity, with a
    structural fallback for providers that expose none.
  - Menus, combo drop-downs, and XAML flyouts are separate top-level windows
    (Notepad's Edit menu lives under a `PopupHost`), so the app's visible owned
    popups are walked first — otherwise `CLICK` on a menu would open items
    the policy could never see.
  - `CLICK` prefers a pattern — SelectionItem for list/tree/tab items (in
    Explorer, `Invoke` would *open* the file), then Invoke, Toggle,
    ExpandCollapse — and falls back to a pointer click only for clickable
    roles, labelled `click_via: pointer`. Pointer clicks hit-test first
    (`ElementFromPoint` must land on the target or a descendant) and refuse
    covered controls, since synthesized input goes to whatever is on top.
  - `TYPE_TEXT` on `Edit` fields uses `ValuePattern.SetValue`; on `Document`
    surfaces it types real keystrokes (focus → Ctrl+A → text). Windows 11
    Notepad accepts `SetValue` but its tab stays "Unmodified", so a planner
    verifying "saved" would be misled. Keystrokes go one character per
    `SendInput` call: batched `KEYEVENTF_UNICODE` events resolve their
    character late, and a target that stalls (Notepad spell-checks on each
    space) turned `at 08:11:20` into `0000000000`. The result is read back
    and retried once, slower, before failing loudly. Documents that are typed
    into do not also offer `SET_VALUE`: in a live Jev run, the policy picked
    it for Notepad's editor, the tab never showed "Modified", the save that
    followed changed nothing observable, and the policy retried it until the
    runtime reported `BLOCKED`. With typing as the only edit, the same task
    completed in two actions.
  - `is_fresh` requires the same foreground **window**, not just process —
    stricter than the pid check, so a same-app dialog opening invalidates
    targets in the window behind it. A vanished control raises `COMError`,
    which is caught and reported as stale, never propagated (the runtime does
    not guard `is_fresh`).
  - Absolute pointer coordinates are normalized over the *virtual* desktop
    (`MOUSEEVENTF_VIRTUALDESK`) and the process opts into per-monitor DPI
    awareness, so clicks land on scaled and secondary displays. Wheel input
    goes to the window under the pointer, so `SCROLL` first parks the pointer
    over the largest visible scroll region.
  - Values over 1000 characters are truncated for the model, with the guard
    hashing the full text so edits past the cut still register. Password
    fields never surface a value. An elevated foreground window raises
    `PermissionError`: a non-elevated client can neither read it nor send it
    input.

---

## The keyboard vocabulary — `keyboard.py`

Referenced by `Subtask.__post_init__` (`parse_hotkey`) and consumed by the
hybrid backend to encode a chosen chord as modifier flags + physical
keycodes for the current (US/ANSI) layout. A chord is `MOD`/`CTRL`/`ALT`/
`SHIFT` combinations plus one named key; `MOD` is Cmd on macOS and Ctrl on
Windows, where `MOD+CTRL` therefore collapses to a single Ctrl press. The
Windows backend maps the same names to virtual-key codes. This is the
single source of truth both `TypeSafeJevPolicy` (offering choices) and
`validation.materialize_action` (re-checking legality) validate against.

---

## One subtask, end to end

```text
planner
  │  Subtask(goal, inputs, verification, constraints, shortcuts)
  ▼
DesktopExecutor.run(subtask)
  │
  ├─ backend.observe()                         → DesktopSnapshot #1
  │
  ├─ loop (until max_actions):
  │    ├─ policy.decide(subtask, snapshot, history)
  │    │     TypeSafeJevPolicy:
  │    │       build operation + speculative sub-questions
  │    │       → one JEV request
  │    │       → validate operation answer, consume matching sub-answer
  │    │       → Decision(kind=..., target_id=..., confidence=...)
  │    │
  │    ├─ if terminal: optionally verify(), yield result, return
  │    │
  │    ├─ materialize_action(decision, snapshot, subtask)
  │    │     re-validate target/legality/input-key/hotkey against snapshot
  │    │     → ExecutableAction
  │    │
  │    ├─ backend.is_fresh(snapshot, action)?  → no: re-observe, retry
  │    │
  │    ├─ backend.execute(snapshot, action)     → native AX / Quartz events
  │    │
  │    ├─ _observe_after_action(...)            → settle-polling
  │    │                                         → DesktopSnapshot #n+1
  │    │
  │    ├─ record ActionRecord; check no-change streak → maybe BLOCKED
  │    ▼
  └─ SUBTASK_COMPLETE / BLOCKED / NEEDS_AGENT
        │
        ▼
     ExecutionResult → api.result_to_dict() → planner
```

---

## Where TypeSafe/JEV specifically earns its keep here

- **Speculative fan-out**: operation selection and every operation's
  parameter choice happen in one request, so the common case (pick an
  operation and its target) costs one round trip, not two.
- **Choice over free text everywhere it matters**: targets, input *keys*
  (not values), hotkeys, scroll directions are all closed-set `Choice`
  questions — this is what makes "JEV never invents a selector or a literal
  value" structurally true rather than merely prompted.
- **Confidence is surfaced by the model and gated by the runtime**:
  `Decision.confidence` flows into `ActionRecord`/history, and
  `RuntimeConfig.confidence_thresholds` optionally turns it into control flow —
  a per-`ActionKind`/`TerminalKind` minimum, below which the step escalates to
  `NEEDS_AGENT` instead of executing. This is TypeSafe's "verify and escalate"
  pattern: the gate fires *before* `materialize_action`, so nothing touches the
  OS, and it catches a case the `no_change_limit` → `BLOCKED` valve cannot —
  an under-confident `SUBTASK_COMPLETE`, which produces no revision change to
  detect because terminating isn't an action.

  Thresholds are per-kind because cost-of-being-wrong isn't uniform: an
  unwanted `SCROLL` is free to undo, `TYPE_TEXT`/`SET_VALUE` mutate the user's
  data, and a wrong `SUBTASK_COMPLETE` misreports the subtask as done to the
  planner. `SUGGESTED_CONFIDENCE_THRESHOLDS` is a starting point, deliberately
  *not* the default — gating is off unless a caller opts in, and the numbers
  need evaluating against real runs. Two deliberate non-gates: `NEEDS_AGENT`
  (escalating an escalation is a no-op) and any policy that reports no
  confidence at all (`ScriptedPolicy`), since `Decision.confidence` is
  optional. When tuning, note that a flat distribution can also mean several
  targets are equally acceptable rather than that the step is unsafe.
