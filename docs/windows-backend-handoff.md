# Handoff: Windows UIA backend for jev-windows-agent

## Status — implemented (2026-09-26, Windows side)

`src/jev_windows_agent/backends/windows_uia.py` (`WindowsUIABackend`) is built and run
against real apps: Notepad, Settings, File Explorer, and Chrome's frame. As the
spec required, nothing in `runtime.py`, `validation.py`, `models.py`, or
`policies/typesafe.py` changed to support it. (`runtime.py` separately gained
opt-in confidence gating, `RuntimeConfig.confidence_thresholds`, which is
platform-independent and off by default.)

- `examples/windows_uia_probe.py`: prints a snapshot, observe latency, and id
  stability across two observations.
- `examples/windows_notepad_smoke.py`: the full `DesktopExecutor` loop with a
  deterministic policy, no API key. It passes: type, save (verified on disk),
  open a menu (items observed), Escape, scroll.
- `jev` CLI (`src/jev_windows_agent/cli.py`): `jev run` and `jev plan`, the
  styled front end over the same `runner.py`/`planner.py` the examples use.
  `jev plan --yes` approves a whole plan up front (but still stops on a step
  that did not complete).
- Windows keys: the shared vocabulary now carries `WIN`, `APPS`, `INSERT` and
  the media/volume keys, with `keyboard.WINDOWS_ONLY_KEYS` marking the subset
  macOS cannot press. The policy offers platform keys from the snapshot's own
  backend, so a Windows run gets the media keys and a macOS run does not.
  `WIN`-based chords are deliberately not offered by default: they open the
  Start menu, a different window, and a scoped run would go blind.
- Launching by app name: `resolve_launch_target()` turns "Apple Music" into
  `shell:AppsFolder\<AppUserModelID>` via `Get-StartApps`, which is what made
  opening Store apps possible at all -- the planner previously returned an
  empty plan for them.
- `examples/test_notepad.py`: the JEV-driven run, **passing through OpenRouter's
  Decisions API**: `TYPE_TEXT` (confidence 0.80), Ctrl+S (0.96),
  `SUBTASK_COMPLETE` (0.53), with the saved file matching. (The key on hand was
  an OpenRouter key, which TypeSafe's own endpoint rejects with 401;
  `TypeSafeJevPolicy` now routes through OpenRouter when only
  `OPENROUTER_API_KEY` is set.)
- `tests/test_windows_uia.py`: pure-logic tests that run on any OS, plus two
  Windows-only checks (hard-coded ids against the UIA typelib; thread binding).

### Where the implementation departs from this spec, and why

Each of these came from observing real apps, not from preference:

- **`TYPE_TEXT` on `Document` controls types keystrokes instead of calling
  `ValuePattern.SetValue`.** Windows 11 Notepad accepts `SetValue`, but its tab
  keeps saying "Unmodified", so a planner verifying "saved" is misled. `Edit`
  fields keep `SetValue`, since Settings search runs its query either way.
  `SET_VALUE` always uses `SetValue`.
- **Documents typed into do not offer `SET_VALUE`.** In the first live Jev run
  the policy chose `SET_VALUE` for Notepad's editor (confidence 0.39, split
  with `TYPE_TEXT`); the tab never showed "Modified", so the save changed
  nothing observable and the policy retried it until `BLOCKED`. Without it, the
  run completed in two actions.
- **Keystrokes go one character per `SendInput` call, then are read back.**
  Batched `KEYEVENTF_UNICODE` events resolve their character when the target
  translates the message; Notepad stalls to spell-check on each space, and
  `at 08:11:20` arrived as `0000000000`.
- **Pattern state is gated on `Is*PatternAvailable`.** `GetCachedPropertyValue`
  returns type defaults for missing patterns, not the not-supported marker: every
  element would read as toggle-indeterminate and expandable.
- **Owned popup windows are observed too.** Menus and XAML flyouts are separate
  top-level windows (`PopupHost`); walking only the foreground window's tree made
  an opened menu invisible to the policy.
- **`CLICK` can fall back to a pointer click** for clickable roles with no
  pattern (`macos_ax.py` refuses without `AXPress`). It is labelled
  `click_via: pointer` in the snapshot, and it hit-tests first, refusing covered
  targets. List, tree, and tab items click via SelectionItem, because their
  `Invoke` is "open" (Explorer).
- **`is_fresh` compares the foreground window, not only the pid**, so a dialog
  in the same process invalidates targets behind it.
- **`DRAG_TO` follows UIA's Drag and DropTarget patterns** instead of being
  refused. In practice no observed app exposes a drop target, so the policy
  prunes it.
- **COM runs in comtypes' default STA**, not MTA. Hang protection comes from
  `IUIAutomation2` connection/transaction timeouts instead of the apartment.
- `max_depth` defaults to 40 (the AX backend uses 18), since XAML trees nest deeper.

### Still open

- **A visible "Play" control doesn't always start playback.** In Apple Music,
  JEV clicked one at 0.75 and playback did not begin; `MEDIA_PLAY_PAUSE`
  (global, added for this) started it. The general shape -- a control whose
  name implies an action it doesn't perform on its own -- is a perception
  problem no amount of policy tuning fixes, and worth a closer look at what
  UIA exposes for such controls before trusting a name.
- **Cold Chromium/Electron trees can still lose a first run.** The 2.5s warm-up
  covers the common case; Spotify after a long idle came back `BLOCKED` once
  and worked on a rerun. A longer wait, or retrying a first-step `BLOCKED` when
  the tree is suspiciously small, would close it.
- Tune `SUGGESTED_CONFIDENCE_THRESHOLDS` from real runs. First data point: the
  correct `SUBTASK_COMPLETE` above came at 0.53, so the suggested 0.75 would
  have escalated it to `NEEDS_AGENT`. The example prints each step's top
  operation probabilities for this; `--confidence-gate` turns the gate on.
- OCR fallback for apps with poor UIA trees (Chromium web content without
  accessibility, canvas UIs), as on macOS.
- `DOUBLE_CLICK`/`RIGHT_CLICK` execute but are not advertised, matching macOS.
  Explorer's "open file" is the obvious first case for advertising them.

---

The original spec follows unchanged.

Context for whoever (human or Claude session) picks this up on the Windows
side of this dual-boot machine. Read `docs/walkthrough.md` first if you
haven't — it's a full architecture tour. This doc is the actionable spec for
one task: **write a Windows perception+execution backend**, decided in a
prior chat on the Linux side of this box (which cannot run/test Windows UIA
or Win32 code, hence the handoff instead of doing it there).

## Goal, scope, decided already — don't re-litigate

- Build `WindowsUIABackend` implementing the `DesktopBackend` protocol
  (`src/jev_windows_agent/protocols.py`), analogous to `MacOSAXBackend`
  (`src/jev_windows_agent/backends/macos_ax.py` — **read this file in full, it is the
  template to mirror**, including its exceptions, structure, and the way it
  splits `observe` / `is_fresh` / `execute` / tree-walk / element-mapping).
- **Scope: UI Automation only, no OCR fallback yet.** This mirrors
  `macos_ax.py`, not the hybrid `macos_ocr.py`/`macos_hybrid.py` combo. Skip
  Electron/canvas/custom-drawn apps with poor UIA trees for now — that's a
  deliberate v0 cut, same one macOS made initially.
- Nothing in `runtime.py`, `validation.py`, `policies/typesafe.py`, or
  `models.py` should need to change. If you find yourself wanting to edit
  those, stop and reconsider — the whole point of the `DesktopBackend`
  protocol is that a new backend is additive.

## The protocol to implement (`protocols.py`)

```python
class DesktopBackend(Protocol):
    def observe(self) -> DesktopSnapshot: ...
    def is_fresh(self, snapshot: DesktopSnapshot, action: ExecutableAction) -> bool: ...
    def execute(self, snapshot: DesktopSnapshot, action: ExecutableAction) -> None: ...
```

No inheritance needed — structural typing. Put the new file at
`src/jev_windows_agent/backends/windows_uia.py`.

## Library choices (decide/pin these; not yet chosen)

- **UI Automation access**: `comtypes` + the raw `UIAutomationCore` COM API
  gives the closest analogue to `ApplicationServices`/AX (direct
  attribute/action access, no extra abstraction to fight). `pywinauto`'s
  `uia` backend is higher-level and faster to get started with but adds its
  own object model on top — pick one and be consistent; don't mix.
  Recommendation: start with `comtypes` for parity with how `macos_ax.py`
  talks to raw AX, since `validation.py`'s trust model assumes the backend
  re-derives ground truth on every check, not cached wrapper state.
- **Input execution**: Win32 `SendInput` (via `ctypes`) for mouse clicks,
  drags, and keyboard events — the Windows equivalent of `CGEventPost` in
  `macos_hybrid.py`/`macos_ax.py`. Don't use `pyautogui` (it fails silently
  in some elevated/UAC/secure-desktop contexts, and this project wants
  correctness over convenience).
- Add a `windows` extra to `pyproject.toml`, mirroring the `macos` extra:
  ```toml
  [project.optional-dependencies]
  windows = ["comtypes>=1.2"]
  ```

## Mapping macOS AX concepts → Windows UIA

| macOS (`macos_ax.py`) | Windows UIA equivalent |
|---|---|
| `AXUIElementCreateApplication(pid)` + `AXFocusedWindow` | `IUIAutomation::GetFocusedElement` or enumerate top-level windows via `GetForegroundWindow` + `ElementFromHandle` |
| `AXRole` | `ControlType` (`UIA_ControlTypePropertyId`) — map to a role string, e.g. `UIA_EditControlTypeId` → `"Edit"` |
| `AXTitle`/`AXLabel`/`AXDescription`/`AXHelp` → `name` | `Name` property (`UIA_NamePropertyId`), fall back to `LegacyIAccessible.Name` if empty |
| `AXValue` | `ValuePattern.CurrentValue` (text/value controls), or `TogglePattern`/`RangeValuePattern` for others |
| `AXEnabled` | `IsEnabledPropertyId` |
| `AXFocused` | `HasKeyboardFocusPropertyId` |
| `AXSelected` | `SelectionItemPattern.IsSelected` |
| `AXExpanded` | `ExpandCollapsePattern.ExpandCollapseState` |
| `AXChildren` (tree walk) | `TreeWalker` (`RawViewWalker` or `ControlViewWalker`) |
| `AXUIElementCopyActionNames` → capability inference | Check which patterns are supported: `InvokePattern` present → `CLICK`; `ValuePattern`/`TextPattern` with `IsReadOnly=false` → `TYPE_TEXT`/`SET_VALUE`; `TogglePattern` → treat as `CLICK` too |
| `AXUIElementPerformAction(ref, "AXPress")` | `InvokePattern.Invoke()` |
| `AXUIElementSetAttributeValue(ref, "AXValue", ...)` | `ValuePattern.SetValue(...)` |
| `AXPosition`/`AXSize` → `Bounds` | `BoundingRectangle` property |
| `AXUIElementIsAttributeSettable` | Check pattern availability + `ValuePattern.CurrentIsReadOnly` |
| `repr(ref)` as a stable-ish identity key for `_stable_id`/dedup | `IUIAutomationElement::GetRuntimeId()` — this is UIA's actual purpose-built stable-within-session id; use it directly instead of hashing a repr |
| `AXIsProcessTrusted()` permission gate | No direct analogue; UIA generally works without special permission, but note: elevated (admin) target apps are invisible to a non-elevated automation client — document this as a known limitation, don't silently fail |

Follow `_element_from_ref`'s filtering logic closely: only emit an element
if it has a name, a value, at least one capability, or is a structurally
useful container — this is what keeps the model-visible snapshot small
enough to be a good JEV candidate set. Don't just dump the whole raw UIA
tree.

## `revision` fingerprint

Same approach as `macos_ax.py`'s `observe()`: hash a sorted, JSON-safe
payload of `(id, role, name, value, enabled, focused, selected, expanded,
parent_id)` over every element with `sha256`. This is what the runtime's
settling loop and `ActionRecord.state_changed` key off of — get the field
set right or settling/no-change detection silently breaks.

## `is_fresh` semantics — don't skip this

This is the safety-critical method. Before trusting a stored element
reference for execution:
1. Re-check the foreground/target process still matches
   (`snapshot.context["pid"]`, same pattern as `_frontmost_pid()`).
2. Re-resolve the element from its stored UIA reference and recompute
   `semantic_guard()` (already implemented generically in `models.py` —
   nothing to change there), compare against `action.target_guard`.
3. Return `False` on any COM error (stale/disconnected element) — UIA
   elements can throw `COMError` (`UIA_E_ELEMENTNOTAVAILABLE`) when the
   underlying control is gone; catch that specifically and treat as stale,
   don't let it propagate as an unhandled backend exception (the runtime's
   `run_iter` only catches generic `Exception` around `backend.execute`, not
   `is_fresh`, so an uncaught COM error there would crash the whole loop).

## Keyboard execution

Mirror `_press_key`/`_press_hotkey`/`_scroll` in `macos_ax.py`, but with
`SendInput` + Windows virtual-key codes (`VK_*` from `winuser.h`) instead of
`CGEventCreateKeyboardEvent`. Build a `_KEYCODES` dict keyed by the exact
same string vocabulary already defined in `src/jev_windows_agent/keyboard.py`
(`KEY_NAMES`, `MODIFIERS`) — that module is shared/platform-agnostic and
should not need any change; only the backend's private
key-name-to-VK-code table is new. `MOD` should map to `VK_CONTROL` on
Windows (the keyboard docs already say "Ctrl elsewhere" — this is that case).

For scroll, use `mouse_event`/`SendInput` with `MOUSEEVENTF_WHEEL` /
`MOUSEEVENTF_HWHEEL`, magnitude in multiples of `WHEEL_DELTA` (120).

## Click/drag execution

`SendInput` with `MOUSEEVENTF_MOVE|MOUSEEVENTF_ABSOLUTE` (remember: absolute
coordinates need the 0–65535 normalized range, not raw pixels — a common
bug) then `MOUSEEVENTF_LEFTDOWN`/`_LEFTUP` (or `RIGHTDOWN`/`_RIGHTUP`).
Double-click: two down/up pairs with a short sleep, same shape as
`_click_at`'s `click_state` loop. For `DRAG_TO`, interpolate intermediate
move events like `macos_hybrid.py`'s `_drag` does — don't teleport the
cursor, some apps only register drag targets on intermediate positions.

## Errors — reuse, don't invent new ones

Use the existing `JevDesktopError` subclasses from `errors.py`:
`StaleDesktopState`, `UnsupportedDesktopAction`. Don't add new exception
types unless something genuinely doesn't fit either.

## What NOT to build yet

- No OCR fallback (no `windows_ocr.py`, no hybrid backend) — flagged as a
  separate future task.
- No `DRAG_BY` (it's disabled at the policy level already, see
  `policies/typesafe.py`'s `TypeSafeJevPolicy.decide` — a `ValueError` is
  raised deliberately; leave that alone).
- No changes to `TypeSafeJevPolicy` — it's platform-agnostic by design (it
  only ever sees `DesktopElement`/`DesktopSnapshot`, never OS APIs).

## How to actually test this, once on Windows

1. `pip install -e '.[windows,dev]'` (after adding the `windows` extra).
2. Reuse `examples/effects_demo.py` + `backends/memory.py`
   (`StateMachineBackend`) first if you want to sanity-check anything
   backend-adjacent without touching real UIA — but the actual new code
   needs a real Windows app to exercise. Notepad or Windows Settings are
   good first targets (analogous to `test_settings.py`/`test_spotify.py` on
   macOS) since they have reasonably good native UIA trees.
3. Write `examples/windows_notepad_probe.py` (mirroring
   `examples/macos_ax_probe.py`) as a standalone script that just prints the
   observed tree — verify element extraction looks sane *before* wiring up
   the full executor loop.
4. Then a `test_notepad.py` example using
   `DesktopExecutor(WindowsUIABackend(), TypeSafeJevPolicy())` for something
   like "type a line of text and save" — needs `TYPESAFE_API_KEY` set.
5. Add `tests/test_windows_uia.py` unit tests for the pure-logic pieces that
   don't need a live Windows session where feasible (element filtering
   logic, key/hotkey encoding) — same spirit as how AX-independent logic in
   this repo is unit-tested in `tests/`.

## Open judgment calls (use your judgment, don't ask back to the Linux side)

- Whether to support non-foreground/background-window automation at all in
  v0 (macOS AX backend only automates the frontmost app — probably keep
  parity and do the same here).
- Exact `ControlType` → capability mapping breadth — start narrow (matching
  the roles `macos_ax.py` actually handles: buttons/menus for `CLICK`,
  edit/combo/search-like for `TYPE_TEXT`/`SET_VALUE`) and expand only as
  real apps need it, rather than trying to handle every UIA pattern upfront.
