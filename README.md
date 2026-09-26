# jev-windows-agent

**A Windows UI Automation extension of [`arc-cua`](https://github.com/shhivv/arc-cua) — the superfast action layer for computer-use agents.**

> Original `arc-cua` project and macOS backend by [shhivv](https://github.com/shhivv), built by [Isle](https://tryisle.com) — managed desktop environments for computer-use agents. This repository extends it with a Windows UI Automation (`WindowsUIABackend`) perception + execution backend, OpenRouter transport support, and optional confidence gating, while keeping the original runtime, validation, and JEV decision policy untouched. See [`ARCHITECTURE.md`](ARCHITECTURE.md) and [`docs/windows-backend-handoff.md`](docs/windows-backend-handoff.md) for what changed and why.

---

`jev-windows-agent` lets a planner or CUA agent hand off bounded desktop subtasks to a fast decision model that executes the UI loop — no frontier model needed for every click.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for how it all fits together, [`docs/walkthrough.md`](docs/walkthrough.md) for a module-by-module code tour, and [`docs/windows-backend-handoff.md`](docs/windows-backend-handoff.md) for the Windows port's implementation notes.

```python
from jev_windows_agent import execute_payload

result = execute_payload(executor, {
    "goal": "Play Get Lucky by Daft Punk in Spotify",
    "inputs": {"search_query": "Get Lucky Daft Punk"},
    "verification": ["Spotify shows Get Lucky as the current track"],
    "constraints": ["Do not modify the user's library"],
    "max_actions": 15,
})

# result: {"status": "SUBTASK_COMPLETE", "actions_taken": 4}
```

Any GPT, Claude, Gemini, local model, or deterministic planner can generate that payload. The planner deliberately lives outside the package.

---

## Why

Computer-use agents should not need a frontier model to reason about every individual click.

A typical CUA loop:

```text
observe → large model → click → observe → large model → type → observe → large model → click
```

`jev-windows-agent` separates high-level reasoning from low-level execution:

```text
planner / LLM
     ↓
bounded subtask
     ↓
jev-windows-agent
     ↓
JEV → action → action → action → action
     ↓
return to planner
```

The optimization target is **fewer expensive reasoning calls per completed task**, not fewer UI actions.

---

## How it works

```text
any planner / CUA
        |
        | Subtask(goal, inputs, verification, constraints)
        v
+-----------------------+
|   jev-windows-agent   |
|                       |
| observe desktop       |
| AX + local OCR        |
|         v             |
| build legal           |
| action space          |
|         v             |
| JEV decision          |<------+
|         v             |       |
| freshness guard       |       |
|         v             |       |
| execute UI            |       |
|         v             |       |
| wait for UI settle    |-------+
+-----------+-----------+
            |
            v
SUBTASK_COMPLETE / BLOCKED / NEEDS_AGENT
            |
            v
         planner
```

### JEV

JEV is the decision backend that powers the action loop. Given structured desktop state (elements, roles, values), it selects the next UI operation from a dynamically built action space — it can only pick targets and operations the current desktop actually exposes.

JEV is accessed through [TypeSafe](https://typesafe.com). One JEV call can resolve the operation and its parameters in parallel.

### The agent owns intent

The upstream agent decides what needs to happen, what literal text may be used, what must not happen, and what counts as success. JEV chooses which element to target and which operation to perform — but never invents arbitrary text. Literal values always originate from the agent via `inputs`.

### Caller-supplied shortcuts

Supply extra keyboard shortcuts for an individual subtask, with descriptions that tell JEV what they do:

```python
from jev_windows_agent import Subtask

task = Subtask(
    goal="Save the current document",
    verification=("The document has no unsaved changes",),
    shortcuts={"MOD+S": "Save the current document in this editor"},
)
```

The same `shortcuts` map is accepted by `execute_payload`. JEV receives these choices alongside the existing default hotkeys and chooses a chord when it selects `HOTKEY`. A supplied description can also clarify a default shortcut's meaning in the current app. The defaults are unchanged, and supplied shortcuts apply only to that subtask.

```python
result = execute_payload(executor, {
    "goal": "Save the current document",
    "verification": ["The document has no unsaved changes"],
    "shortcuts": {"MOD+S": "Save the current document in this editor"},
})
```

Chords use uppercase key names and one or more `MOD`, `CTRL`, `ALT`, or `SHIFT` modifiers, for example `MOD+S`, `CTRL+ALT+7`, or `SHIFT+F12`. `MOD` means Command on macOS and Ctrl on Windows. Supported keys include A-Z, 0-9, F1-F20, navigation keys, and named punctuation keys; see [the keyboard vocabulary](src/jev_windows_agent/keyboard.py). The macOS backend uses US/ANSI physical key positions. Each shortcut is one chord, not a sequence of actions.

Malformed declarations fail when the subtask is created. JEV can choose only offered chords; runtime validation also rejects hotkeys outside the defaults and the current subtask's declarations, including decisions from custom policies.

### Hybrid macOS perception

`jev-windows-agent` combines two local perception sources:

- **Accessibility (AX)** — semantic controls: buttons, fields, menus, roles, values, native actions
- **Apple Vision OCR** — visible screen text with bounding boxes, for apps with incomplete accessibility

Both normalize into `DesktopElement`s that JEV reasons over. JEV receives structured elements and IDs, not screenshots.

### Windows UI Automation perception

On Windows, `WindowsUIABackend` reads the foreground window through UI Automation (UIA) and executes UIA patterns (Invoke, Toggle, SelectionItem, ExpandCollapse, Value, RangeValue), with `SendInput` for keyboard, scroll, and pointer events. It produces the same `DesktopElement`s, so the runtime and JEV policy are unchanged. There is no OCR fallback yet: apps that draw their own UI, and Chromium web content without accessibility enabled, expose little to UIA.

### Runtime-owned settling

After a mutating action, `jev-windows-agent` re-observes the UI until the desktop is structurally stable or a timeout is reached. The decision model decides **what to do**; the runtime decides **when the UI is ready to reason over again**.

### Terminal states

| Status | Meaning |
|---|---|
| `SUBTASK_COMPLETE` | Verification criteria appear satisfied |
| `BLOCKED` | Cannot make progress with available operations |
| `NEEDS_AGENT` | Higher-level reasoning required or action budget reached |

The caller owns overall task completion.

---

## Install

macOS and Windows.

```bash
python3.12 -m venv .venv
source .venv/bin/activate

pip install -e '.[macos]'
```

On Windows (PowerShell):

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1

pip install -e '.[windows]'
```

Set a key for Jev, either TypeSafe's own or an OpenRouter key:

```bash
export TYPESAFE_API_KEY=...      # TypeSafe's endpoint directly
export OPENROUTER_API_KEY=...    # or: Jev via OpenRouter's Decisions API
```

(PowerShell: `$env:OPENROUTER_API_KEY = "..."`.) With only `OPENROUTER_API_KEY` set, `TypeSafeJevPolicy()` routes through OpenRouter (`https://openrouter.ai/api/alpha/decisions`, model `~typesafe/jev-latest`); `TypeSafeJevPolicy.via_openrouter()` does so explicitly. A `TYPESAFE_API_KEY` takes precedence. The request and typed answers are identical on both routes, so every validation applies unchanged.

### macOS permissions

The terminal/editor running Python needs both:

- **Accessibility** — System Settings → Privacy & Security → Accessibility
- **Screen Recording** — System Settings → Privacy & Security → Screen Recording (required for OCR)

Restart the terminal after granting permissions if necessary.

---

## Examples

### Deterministic architecture demo

No API key required:

```bash
python examples/effects_demo.py
```

### macOS probes

```bash
python examples/macos_ax_probe.py   # Inspect frontmost app's AX tree
python examples/ocr_probe.py        # Inspect visible text via Apple Vision
```

### Spotify

Play a track using OCR-heavy workflow:

```bash
python examples/test_spotify.py
```

### System Settings

Change macOS appearance using Accessibility-heavy workflow:

```bash
python examples/test_settings.py
```

### Windows

```powershell
python examples/windows_uia_probe.py --process notepad   # Inspect an app's UIA tree
python examples/windows_notepad_smoke.py                 # End-to-end run, no API key
python examples/test_notepad.py                          # JEV edits and saves a file in Notepad
```

Run your own plain-English subtask against any window (the run is pinned to that window):

```powershell
python examples/windows_task.py --window Settings --launch ms-settings: `
    --goal "Open the Colors page inside Personalization" `
    --verify "Settings is showing the Colors page of Personalization" `
    --constraint "Do not change any setting; only navigate"
```

Text JEV should type goes in `--input name="value"`; JEV picks which input to use but never invents text.

The smoke test and JEV example work on a file they create in a temp directory. Windows 11 Notepad opens files as tabs beside your own documents, so `test_notepad.py` scopes the backend so JEV cannot observe (and so cannot act on) any other tab or window.

### Windows limitations

- Foreground window only, as on macOS. The backend acts on whatever is in front, so avoid using the machine during a run, or scope the backend as `test_notepad.py` does.
- A non-elevated process cannot automate an elevated (administrator) window; `observe()` raises `PermissionError`.
- Hotkeys use US-layout virtual keys. Typed text is layout-independent.
- `SET_VALUE` follows what UIA reports as writable. In File Explorer that includes file items, where setting the value renames the file; the policy can still only use values the planner supplied.

---

## Roadmap

AX + OCR covers native and Electron desktop workflows on macOS; Windows has UI Automation, with an OCR fallback still to come. The next perception frontier is custom graphical interfaces — video timelines, CAD canvases, node graphs, spatial drag targets — which can be added as perception providers while keeping the same `DesktopElement` and execution interfaces.

---

## More documentation

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — how the system fits together: ownership, data flow, safety mechanisms, both backends
- [`docs/walkthrough.md`](docs/walkthrough.md) — a guided, module-by-module tour of the code
- [`docs/internals.md`](docs/internals.md) — concept-level notes on perception, freshness, and OCR stability
- [`docs/windows-backend-handoff.md`](docs/windows-backend-handoff.md) — the Windows UIA backend's spec, implementation status, and known limitations
