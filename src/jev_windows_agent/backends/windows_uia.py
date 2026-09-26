from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import math
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from ..errors import StaleDesktopState, UnsupportedDesktopAction
from ..keyboard import parse_hotkey
from ..models import ActionKind, Bounds, DesktopElement, DesktopSnapshot, ExecutableAction

logger = logging.getLogger(__name__)


class WindowsUIABackend:
    """Experimental semantic backend for Windows UI Automation (UIA).

    Mirrors MacOSAXBackend: it automates the foreground window only, executes
    UIA-native patterns where the control exposes one, and uses SendInput for
    keyboard, scroll, and pointer events. There is no OCR fallback; apps with poor
    UIA trees (canvas-drawn UIs, some Electron builds) are out of scope for v0.

    UIA element references are COM objects bound to the thread that created them,
    so one backend instance must be used from the thread that constructed it.

    A non-elevated client cannot see into or send input to an elevated (admin)
    window; observe() raises PermissionError for such targets instead of returning
    a misleadingly empty tree.
    """

    def __init__(self, *, max_elements: int = 1200, max_depth: int = 40) -> None:
        if sys.platform != "win32":
            raise RuntimeError("WindowsUIABackend is only available on Windows")
        self.max_elements = max_elements
        self.max_depth = max_depth
        self._owner_thread = threading.get_ident()
        # Must precede any coordinate work: UIA reports physical pixels, and a
        # DPI-unaware process gets virtualized metrics that miss on scaled displays.
        self.dpi_awareness = _enable_dpi_awareness()
        comtypes, UIA = _uia()
        _ensure_com_initialized(comtypes)
        self._comtypes = comtypes
        self._UIA = UIA
        self._automation = _create_automation(comtypes, UIA)
        self._tree_cache = self._cache_request(subtree=True)
        self._element_cache = self._cache_request(subtree=False)
        self._refs: dict[str, Any] = {}
        self._scroll_point: tuple[float, float] | None = None
        self._own_elevation = _process_elevation(_win32().kernel32.GetCurrentProcessId())

    def register_ref(self, element_id: str, ref: Any) -> None:
        self._refs[element_id] = ref

    def observe(self) -> DesktopSnapshot:
        self._require_owner_thread()
        COMError = self._comtypes.COMError
        last_error: Exception | None = None
        # The foreground window can close or swap mid-walk; that is a race to retry,
        # not a failure to surface.
        for _ in range(3):
            try:
                return self._observe_once()
            except COMError as exc:
                last_error = exc
                logger.debug("uia observe retry after COM error hresult=%s", _hresult(exc))
                time.sleep(0.05)
        raise StaleDesktopState(f"Foreground window kept changing during observation: {last_error}")

    def is_fresh(self, snapshot: DesktopSnapshot, action: ExecutableAction) -> bool:
        self._require_owner_thread()
        hwnd, pid = _foreground()
        if hwnd != snapshot.context.get("hwnd") or pid != snapshot.context.get("pid"):
            return False
        for target_id, guard in (
            (action.target_id, action.target_guard),
            (action.secondary_target_id, action.secondary_target_guard),
        ):
            if target_id and not self._target_is_fresh(snapshot, target_id, guard):
                return False
        return True

    def execute(self, snapshot: DesktopSnapshot, action: ExecutableAction) -> None:
        if not self.is_fresh(snapshot, action):
            raise StaleDesktopState("Windows UIA target changed before execution")

        if action.kind == ActionKind.WAIT:
            time.sleep(0.1)
            return
        if action.kind == ActionKind.PRESS_KEY:
            _press_key(action.key or "")
            return
        if action.kind == ActionKind.HOTKEY:
            _press_hotkey(action.hotkey or "")
            return
        if action.kind == ActionKind.SCROLL:
            _scroll(action.scroll_direction or "DOWN", self._scroll_point)
            return

        if not action.target_id:
            raise UnsupportedDesktopAction(f"{action.kind.value} requires a target on Windows UIA")
        ref = self._refs.get(action.target_id)
        if ref is None:
            raise StaleDesktopState("Target no longer exists")

        COMError = self._comtypes.COMError
        try:
            self._execute_targeted(snapshot, action, ref)
        except COMError as exc:
            if _hresult(exc) in _STALE_HRESULTS:
                raise StaleDesktopState(f"UIA element disappeared during {action.kind.value}") from exc
            raise UnsupportedDesktopAction(
                f"UIA {action.kind.value} failed with HRESULT {_hresult_hex(exc)}"
            ) from exc

    # -- observation -------------------------------------------------------------------

    def _observe_once(self) -> DesktopSnapshot:
        hwnd, pid = _foreground()
        if not hwnd or not pid:
            raise RuntimeError("No foreground Windows window")
        self._require_accessible_process(pid)

        window = self._automation.ElementFromHandle(ctypes.c_void_p(hwnd))
        root = window.BuildUpdatedCache(self._tree_cache)
        root_props = self._read_props(root)
        window_title = str(root_props.get("name") or "")
        app_name = _application_name(pid, window_title)

        refs: dict[str, Any] = {}
        elements: list[DesktopElement] = []
        walk = _WalkState()

        # Win32 menus, combo drop-downs, and XAML flyouts are separate top-level
        # windows owned by the app. They are walked first so a max_elements cap
        # never drops an open menu in favor of the content behind it.
        popups = _popup_windows(hwnd, pid)
        for popup_hwnd in popups:
            try:
                popup = self._automation.ElementFromHandle(ctypes.c_void_p(popup_hwnd))
                popup_root = popup.BuildUpdatedCache(self._tree_cache)
            except self._comtypes.COMError:
                continue  # popups are transient; a closed one is simply not observed
            self._walk(popup_root, elements, refs, walk, parent_id=None, depth=0, sibling_index=0)
        self._walk(root, elements, refs, walk, parent_id=None, depth=0, sibling_index=0)

        self._refs = refs
        self._scroll_point = walk.scroll_point or _bounds_center(root_props.get("bounds"))
        logger.debug("uia observe app=%r popups=%d elements=%d", app_name, len(popups), len(elements))

        return DesktopSnapshot(
            application=app_name,
            window=window_title or app_name,
            revision=_revision(elements),
            elements=tuple(elements),
            context={"pid": pid, "hwnd": hwnd, "backend": "windows_uia"},
            captured_at_ms=round(time.time() * 1000),
        )

    def _walk(
        self,
        element: Any,
        elements: list[DesktopElement],
        refs: dict[str, Any],
        walk: _WalkState,
        *,
        parent_id: str | None,
        depth: int,
        sibling_index: int,
    ) -> None:
        if depth > self.max_depth or len(elements) >= self.max_elements:
            return
        props = self._read_props(element)
        runtime_id = props.get("runtime_id")
        if runtime_id:
            if runtime_id in walk.visited:
                return
            walk.visited.add(runtime_id)

        element_id = walk.claim_id(_element_id(props, parent_id, sibling_index))
        desktop_element = _element_from_props(props, element_id, parent_id)
        next_parent = parent_id
        if desktop_element is not None and desktop_element.visible:
            elements.append(desktop_element)
            refs[element_id] = element
            next_parent = element_id
        # Scroll containers are usually anonymous panes the snapshot filters out, so
        # they are tracked from raw properties rather than emitted elements.
        walk.note_scroll_region(props)

        children = element.GetCachedChildren()
        if not children:
            return
        for index in range(children.Length):
            if len(elements) >= self.max_elements:
                break
            self._walk(
                children.GetElement(index),
                elements,
                refs,
                walk,
                parent_id=next_parent,
                depth=depth + 1,
                sibling_index=index,
            )

    def _read_props(self, element: Any) -> dict[str, Any]:
        """Read cached UIA properties into a plain dict keyed by _PROPERTY_IDS names.

        GetCachedPropertyValue returns a type default rather than a not-supported
        marker for patterns the element lacks (ToggleState 2 = Indeterminate,
        ExpandCollapseState 3 = LeafNode). Pattern state is therefore only read when
        the matching Is*PatternAvailable property says the pattern exists.
        """
        props: dict[str, Any] = {}
        for name in _CORE_PROPERTIES:
            props[name] = _plain(element.GetCachedPropertyValue(_PROPERTY_IDS[name]))
        for pattern, (_, state_properties) in _PATTERNS.items():
            if props.get(f"has_{pattern}"):
                for name in state_properties:
                    props[name] = _plain(element.GetCachedPropertyValue(_PROPERTY_IDS[name]))
        props["bounds"] = _bounds_from_rect(props.pop("bounding_rectangle", None))
        return props

    def _cache_request(self, *, subtree: bool) -> Any:
        UIA = self._UIA
        request = self._automation.CreateCacheRequest()
        for property_id in _PROPERTY_IDS.values():
            request.AddProperty(property_id)
        request.TreeScope = UIA.TreeScope_Subtree if subtree else UIA.TreeScope_Element
        request.TreeFilter = self._automation.ControlViewCondition
        # Full mode keeps live references so actions and freshness checks can
        # re-query the real control instead of trusting cached state.
        request.AutomationElementMode = UIA.AutomationElementMode_Full
        return request

    # -- freshness -----------------------------------------------------------------

    def _target_is_fresh(self, snapshot: DesktopSnapshot, target_id: str, guard: str | None) -> bool:
        ref = self._refs.get(target_id)
        if ref is None:
            return False
        try:
            expected = snapshot.element(target_id)
        except KeyError:
            return False
        # runtime.run_iter does not guard is_fresh, so a vanished control must come
        # back as "stale", never as an exception that ends the whole run.
        try:
            props = self._read_props(ref.BuildUpdatedCache(self._element_cache))
        except (self._comtypes.COMError, ValueError, OSError) as exc:
            logger.debug("uia target %s unavailable: %s", target_id, exc)
            return False
        current = _element_from_props(props, target_id, expected.parent_id)
        return current is not None and current.semantic_guard() == guard

    def _require_accessible_process(self, pid: int) -> None:
        if self._own_elevation is True:
            return
        if _process_elevation(pid) is not False:
            raise PermissionError(
                f"The foreground window (pid {pid}) belongs to an elevated or protected process. "
                "A non-elevated UI Automation client cannot read its UI or send it input; run this "
                "process as administrator to automate elevated apps."
            )

    def _require_owner_thread(self) -> None:
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError(
                "WindowsUIABackend must be used from the thread that created it; "
                "UIA element references are not valid across COM apartments"
            )

    # -- execution -----------------------------------------------------------------

    def _execute_targeted(self, snapshot: DesktopSnapshot, action: ExecutableAction, ref: Any) -> None:
        element = snapshot.element(action.target_id or "")
        props = self._read_props(ref.BuildUpdatedCache(self._element_cache))
        patterns = _patterns_from(props)

        if action.kind == ActionKind.CLICK:
            method = _click_method(element.role, patterns, has_bounds=props.get("bounds") is not None)
            if method is None:
                raise UnsupportedDesktopAction("Target exposes no clickable UIA pattern or clickable geometry")
            self._click_via(ref, method)
            return

        if action.kind in {ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK}:
            x, y = self._hit_testable_center(ref)
            _click_at(
                x,
                y,
                count=2 if action.kind == ActionKind.DOUBLE_CLICK else 1,
                button="right" if action.kind == ActionKind.RIGHT_CLICK else "left",
            )
            return

        if action.kind == ActionKind.DRAG_TO:
            destination = self._refs.get(action.secondary_target_id or "")
            if destination is None:
                raise StaleDesktopState("Drag destination no longer exists")
            start = self._hit_testable_center(ref)
            end = _bounds_center(_current_bounds(destination))
            if end is None:
                raise UnsupportedDesktopAction("Drag destination has no resolvable screen position")
            _drag(start, end)
            return

        if action.kind in {ActionKind.TYPE_TEXT, ActionKind.SET_VALUE}:
            if action.value is None:
                raise UnsupportedDesktopAction(f"{action.kind.value} requires an agent-supplied value")
            self._set_value(ref, props, action)
            return

        raise UnsupportedDesktopAction(f"WindowsUIABackend v0 cannot execute {action.kind.value}")

    def _click_via(self, ref: Any, method: str) -> None:
        UIA = self._UIA
        if method == "select":
            self._pattern(ref, "selection_item", UIA.IUIAutomationSelectionItemPattern).Select()
        elif method == "invoke":
            self._pattern(ref, "invoke", UIA.IUIAutomationInvokePattern).Invoke()
        elif method == "toggle":
            self._pattern(ref, "toggle", UIA.IUIAutomationTogglePattern).Toggle()
        elif method == "expand":
            pattern = self._pattern(ref, "expand_collapse", UIA.IUIAutomationExpandCollapsePattern)
            if pattern.CurrentExpandCollapseState == _EXPAND_COLLAPSED:
                pattern.Expand()
            else:
                pattern.Collapse()
        elif method == "synthetic":
            x, y = self._hit_testable_center(ref)
            _click_at(x, y, count=1, button="left")
        else:  # pragma: no cover - _click_method only returns the names above
            raise UnsupportedDesktopAction(f"Unknown click method {method}")

    def _set_value(self, ref: Any, props: Mapping[str, Any], action: ExecutableAction) -> None:
        UIA = self._UIA
        value = action.value
        if action.kind == ActionKind.TYPE_TEXT and _typing_method(props) == "keyboard":
            self._type_replacing(ref, str(value))
            return
        if props.get("has_value") and props.get("value_is_read_only") is False:
            if isinstance(value, bool):
                text = "true" if value else "false"
            else:
                text = str(value)
            self._pattern(ref, "value", UIA.IUIAutomationValuePattern).SetValue(text)
            return
        if props.get("has_range_value") and props.get("range_value_is_read_only") is False:
            number = _coerce_range_value(value, props.get("range_value_minimum"), props.get("range_value_maximum"))
            self._pattern(ref, "range_value", UIA.IUIAutomationRangeValuePattern).SetValue(number)
            return
        raise UnsupportedDesktopAction("Target exposes no writable UIA value")

    def _type_replacing(self, ref: Any, text: str) -> None:
        """Focus a text control, select all, then type: replace semantics like SetValue.

        Mirrors the macOS OCR path's click -> Cmd+A -> type. See _typing_method for
        when this is chosen over ValuePattern.
        """
        ref.SetFocus()
        time.sleep(0.05)
        focused = self._read_props(ref.BuildUpdatedCache(self._element_cache)).get("has_keyboard_focus")
        if not focused:
            raise UnsupportedDesktopAction("Text target did not accept keyboard focus")
        # Pacing lowers the odds of VK_PACKET substitution (see _type_text) but a long
        # enough stall in the target still corrupts characters, so the result is read
        # back and retried once, slower, before failing loudly.
        actual: str | None = None
        for pause_s in _TYPING_PAUSES_S:
            _press_hotkey("MOD+A")
            _type_text(text, pause_s=pause_s)
            actual = self._await_text(ref, text)
            if actual is None or _same_text(actual, text):
                return
            logger.debug("typed text mismatch at pause %.3fs: %r", pause_s, actual)
        raise UnsupportedDesktopAction(
            f"Typed text did not land intact (control shows {actual[:80]!r}); the target may be "
            "rewriting input (autocorrect, masks) or dropping keystrokes"
        )

    def _await_text(self, ref: Any, expected: str, timeout_s: float = 1.0) -> str | None:
        """Poll the control's value until it matches; None if it has no readable value."""
        deadline = time.perf_counter() + timeout_s
        actual: str | None = None
        while True:
            props = self._read_props(ref.BuildUpdatedCache(self._element_cache))
            if not props.get("has_value") or props.get("is_password"):
                return None
            actual = str(props.get("value_value") or "")
            if _same_text(actual, expected) or time.perf_counter() >= deadline:
                return actual
            time.sleep(0.05)

    def _pattern(self, ref: Any, name: str, interface: Any) -> Any:
        unknown = ref.GetCurrentPattern(_PATTERNS[name][0])
        if not unknown:
            raise UnsupportedDesktopAction(f"Target no longer supports the UIA {name} pattern")
        return unknown.QueryInterface(interface)

    def _hit_testable_center(self, ref: Any) -> tuple[float, float]:
        """Return the target's center only if a pointer event there would reach it.

        Synthesized pointer input goes to whatever is on top at those pixels, so an
        occluding popup or overlapping control must be caught before clicking.
        """
        center = _bounds_center(_current_bounds(ref))
        if center is None:
            raise UnsupportedDesktopAction("Target has no on-screen bounds for a pointer event")
        x, y = center
        hit = self._automation.ElementFromPoint(self._UIA.tagPOINT(int(round(x)), int(round(y))))
        if not self._is_same_or_descendant(hit, ref):
            raise UnsupportedDesktopAction("Target is covered by another element at its center point")
        return x, y

    def _is_same_or_descendant(self, element: Any, ancestor: Any) -> bool:
        walker = self._automation.ControlViewWalker
        current = element
        for _ in range(64):
            if not current:
                return False
            if self._automation.CompareElements(current, ancestor):
                return True
            current = walker.GetParentElement(current)
        return False


class _WalkState:
    def __init__(self) -> None:
        self.visited: set[tuple[int, ...]] = set()
        self.ids: set[str] = set()
        self.scroll_point: tuple[float, float] | None = None
        self._scroll_area = 0.0

    def claim_id(self, element_id: str) -> str:
        # RuntimeId-based ids are unique by construction; this only disambiguates
        # fallback ids, deterministically, so they stay stable across observations.
        candidate = element_id
        suffix = 1
        while candidate in self.ids:
            suffix += 1
            candidate = f"{element_id}_{suffix}"
        self.ids.add(candidate)
        return candidate

    def note_scroll_region(self, props: Mapping[str, Any]) -> None:
        bounds = props.get("bounds")
        if not props.get("has_scroll") or props.get("is_offscreen") or bounds is None:
            return
        area = bounds.width * bounds.height
        if area > self._scroll_area:
            self._scroll_area = area
            self.scroll_point = bounds.center


# -- pure mapping logic (no COM; unit-tested on every platform) -------------------

# UIAutomationClient.h property ids. These are a stable Windows ABI; a win32-only
# test cross-checks them against comtypes' generated typelib.
_PROPERTY_IDS: dict[str, int] = {
    "runtime_id": 30000,
    "bounding_rectangle": 30001,
    "process_id": 30002,
    "control_type": 30003,
    "name": 30005,
    "has_keyboard_focus": 30008,
    "is_keyboard_focusable": 30009,
    "is_enabled": 30010,
    "automation_id": 30011,
    "class_name": 30012,
    "help_text": 30013,
    "is_password": 30019,
    "is_offscreen": 30022,
    "framework_id": 30024,
    "has_expand_collapse": 30028,
    "has_invoke": 30031,
    "has_range_value": 30033,
    "has_scroll": 30034,
    "has_selection_item": 30036,
    "has_text": 30040,
    "has_toggle": 30041,
    "has_value": 30043,
    "value_value": 30045,
    "value_is_read_only": 30046,
    "range_value_value": 30047,
    "range_value_is_read_only": 30048,
    "range_value_minimum": 30049,
    "range_value_maximum": 30050,
    "expand_collapse_state": 30070,
    "selection_item_is_selected": 30079,
    "toggle_state": 30086,
    "has_legacy_iaccessible": 30090,
    "legacy_iaccessible_name": 30092,
    "has_drag": 30137,
    "has_drop_target": 30141,
}

# pattern -> (UIA pattern id, state properties that are only meaningful when present)
_PATTERNS: dict[str, tuple[int, tuple[str, ...]]] = {
    "invoke": (10000, ()),
    "value": (10002, ("value_value", "value_is_read_only")),
    "range_value": (
        10003,
        ("range_value_value", "range_value_is_read_only", "range_value_minimum", "range_value_maximum"),
    ),
    "scroll": (10004, ()),
    "expand_collapse": (10005, ("expand_collapse_state",)),
    "selection_item": (10010, ("selection_item_is_selected",)),
    "text": (10014, ()),
    "toggle": (10015, ("toggle_state",)),
    "legacy_iaccessible": (10018, ("legacy_iaccessible_name",)),
    "drag": (10030, ()),
    "drop_target": (10031, ()),
}

_PATTERN_STATE_PROPERTIES = frozenset(name for _, names in _PATTERNS.values() for name in names)
_CORE_PROPERTIES = tuple(name for name in _PROPERTY_IDS if name not in _PATTERN_STATE_PROPERTIES)

_CONTROL_TYPES: dict[int, str] = {
    50000: "Button",
    50001: "Calendar",
    50002: "CheckBox",
    50003: "ComboBox",
    50004: "Edit",
    50005: "Hyperlink",
    50006: "Image",
    50007: "ListItem",
    50008: "List",
    50009: "Menu",
    50010: "MenuBar",
    50011: "MenuItem",
    50012: "ProgressBar",
    50013: "RadioButton",
    50014: "ScrollBar",
    50015: "Slider",
    50016: "Spinner",
    50017: "StatusBar",
    50018: "Tab",
    50019: "TabItem",
    50020: "Text",
    50021: "ToolBar",
    50022: "ToolTip",
    50023: "Tree",
    50024: "TreeItem",
    50025: "Custom",
    50026: "Group",
    50027: "Thumb",
    50028: "DataGrid",
    50029: "DataItem",
    50030: "Document",
    50031: "SplitButton",
    50032: "Window",
    50033: "Pane",
    50034: "Header",
    50035: "HeaderItem",
    50036: "Table",
    50037: "TitleBar",
    50038: "Separator",
    50039: "SemanticZoom",
    50040: "AppBar",
}

_TEXT_ROLES = frozenset({"Edit", "ComboBox", "Document"})
# Containers kept even when anonymous, mirroring macos_ax's structural roles.
_STRUCTURAL_ROLES = frozenset({"Window", "Group", "ToolBar", "Menu", "MenuBar"})
# Item roles where a click means "select this item"; their InvokePattern, when
# present, is the default *open/activate* action (a double-click in Explorer).
_SELECT_ON_CLICK_ROLES = frozenset({"ListItem", "TreeItem", "DataItem", "TabItem"})
# Roles users click even when the provider exposes no pattern for it.
_POINTER_CLICKABLE_ROLES = frozenset({
    "Button", "SplitButton", "MenuItem", "ListItem", "TreeItem", "DataItem",
    "TabItem", "CheckBox", "RadioButton", "Hyperlink",
})

_EXPAND_COLLAPSED = 0
_EXPAND_LEAF = 3
_TOGGLE_STATES = {0: False, 1: True, 2: "indeterminate"}
_MAX_VALUE_CHARS = 1000


def _plain(value: Any) -> Any:
    """Keep JSON-safe VARIANT payloads; drop COM objects such as the not-supported marker."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, tuple) and all(isinstance(v, (int, float)) for v in value):
        return value
    return None


def _bounds_from_rect(rect: Any) -> Bounds | None:
    # The BoundingRectangle VARIANT is (left, top, width, height), not a RECT.
    if not isinstance(rect, tuple) or len(rect) != 4:
        return None
    x, y, width, height = (float(v) for v in rect)
    if not all(math.isfinite(v) for v in (x, y, width, height)) or width <= 0 or height <= 0:
        return None
    return Bounds(x=x, y=y, width=width, height=height)


def _bounds_center(bounds: Bounds | None) -> tuple[float, float] | None:
    return None if bounds is None else bounds.center


def _role_name(control_type: Any) -> str:
    if isinstance(control_type, int) and not isinstance(control_type, bool):
        return _CONTROL_TYPES.get(control_type, f"ControlType{control_type}")
    return "Unknown"


def _patterns_from(props: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(pattern for pattern in _PATTERNS if props.get(f"has_{pattern}"))


def _click_method(role: str, patterns: frozenset[str], *, has_bounds: bool) -> str | None:
    """Pick how CLICK executes; None means the element is not clickable at all."""
    if "selection_item" in patterns and role in _SELECT_ON_CLICK_ROLES:
        return "select"
    if "invoke" in patterns:
        return "invoke"
    if "toggle" in patterns:
        return "toggle"
    if "selection_item" in patterns:
        return "select"
    if "expand_collapse" in patterns:
        return "expand"
    if role in _POINTER_CLICKABLE_ROLES and has_bounds:
        return "synthetic"
    return None


def _typing_method(props: Mapping[str, Any]) -> str | None:
    """Pick how TYPE_TEXT executes: "keyboard", "value", or None if it cannot.

    Both replace the control's text. Documents (multi-line editor surfaces) are
    typed into with real keystrokes because editors track their dirty flag and undo
    stack from input: Windows 11 Notepad accepts ValuePattern.SetValue, but the tab
    stays "Unmodified", so a planner checking "saved?" would be misled. Single-line
    Edit fields handle SetValue properly (Settings search still runs its query), and
    SetValue is faster and immune to autocomplete rewriting the typed text.
    """
    role = _role_name(props.get("control_type"))
    if role not in _TEXT_ROLES:
        return None
    value_writable = bool(props.get("has_value")) and props.get("value_is_read_only") is False
    typeable = bool(
        props.get("is_keyboard_focusable")
        and props.get("is_enabled") is not False
        and not props.get("is_password")
    )
    if role == "Document" and typeable and (value_writable or props.get("has_text")):
        return "keyboard"
    if value_writable:
        return "value"
    if typeable and props.get("has_text"):
        return "keyboard"
    return None


def _capabilities(
    role: str,
    patterns: frozenset[str],
    *,
    value_writable: bool,
    range_writable: bool,
    typing: str | None,
    has_bounds: bool,
) -> tuple[ActionKind, ...]:
    capabilities: list[ActionKind] = []
    if _click_method(role, patterns, has_bounds=has_bounds) is not None:
        capabilities.append(ActionKind.CLICK)
    if typing is not None:
        capabilities.append(ActionKind.TYPE_TEXT)
    # A document is edited by typing. Writing its text through ValuePattern skips the
    # editor's dirty flag: in a live JEV run on Notepad, SET_VALUE left the tab
    # "Unmodified", so the save that followed changed nothing observable and the
    # policy retried it until the runtime reported BLOCKED. Offering two equivalent
    # edit operations also splits the policy's probability between them.
    document_typed = role == "Document" and typing == "keyboard"
    if (value_writable or range_writable) and not document_typed:
        capabilities.append(ActionKind.SET_VALUE)
    if "drag" in patterns and has_bounds:
        capabilities.append(ActionKind.DRAG_TO)
    return tuple(capabilities)


def _element_from_props(props: Mapping[str, Any], element_id: str, parent_id: str | None) -> DesktopElement | None:
    role = _role_name(props.get("control_type"))
    patterns = _patterns_from(props)
    bounds = props.get("bounds")
    password = bool(props.get("is_password"))

    name = str(props.get("name") or "")
    if not name and "legacy_iaccessible" in patterns:
        name = str(props.get("legacy_iaccessible_name") or "")

    metadata: dict[str, Any] = {}
    value: str | int | float | bool | None = None
    value_writable = "value" in patterns and props.get("value_is_read_only") is False
    range_writable = "range_value" in patterns and props.get("range_value_is_read_only") is False
    full_value: Any = None
    if password:
        # Never surface secret text to the policy, even if a provider leaks it.
        metadata["password"] = True
    elif "value" in patterns:
        full_value = props.get("value_value")
        if isinstance(full_value, str) and len(full_value) > _MAX_VALUE_CHARS:
            value = full_value[:_MAX_VALUE_CHARS]
            metadata["value_length"] = len(full_value)
        else:
            value = full_value
        metadata["value_type"] = "text"
    elif "range_value" in patterns:
        value = props.get("range_value_value")
        metadata["value_type"] = "number"
        minimum, maximum = props.get("range_value_minimum"), props.get("range_value_maximum")
        if isinstance(minimum, (int, float)) and isinstance(maximum, (int, float)):
            metadata["range"] = [minimum, maximum]
    elif "toggle" in patterns:
        value = _TOGGLE_STATES.get(props.get("toggle_state"))
        metadata["value_type"] = "toggle"

    typing = _typing_method(props)
    capabilities = _capabilities(
        role,
        patterns,
        value_writable=value_writable,
        range_writable=range_writable,
        typing=typing,
        has_bounds=bounds is not None,
    )

    semantic = bool(name or value not in (None, "") or capabilities or role in _STRUCTURAL_ROLES)
    if not semantic:
        return None

    if ActionKind.SET_VALUE in capabilities:
        metadata["value_settable"] = True
    if typing == "keyboard":
        metadata["text_entry"] = "keyboard"
    if ActionKind.CLICK in capabilities and _click_method(role, patterns, has_bounds=bounds is not None) == "synthetic":
        metadata["click_via"] = "pointer"
    automation_id = props.get("automation_id")
    if automation_id:
        metadata["automation_id"] = str(automation_id)
    help_text = props.get("help_text")
    if help_text and str(help_text) != name:
        metadata["help"] = str(help_text)[:300]

    selected = props.get("selection_item_is_selected") if "selection_item" in patterns else None
    expanded = None
    if "expand_collapse" in patterns:
        state = props.get("expand_collapse_state")
        expanded = None if state in (None, _EXPAND_LEAF) else state != _EXPAND_COLLAPSED

    element = DesktopElement(
        id=element_id,
        role=role,
        name=name,
        value=value,
        actions=capabilities,
        enabled=props.get("is_enabled") is not False,
        visible=not props.get("is_offscreen"),
        focused=bool(props.get("has_keyboard_focus")),
        selected=None if selected is None else bool(selected),
        expanded=expanded,
        parent_id=parent_id,
        bounds=bounds,
        source="windows_uia",
        accepts_drop="drop_target" in patterns,
        metadata=metadata,
    )
    if "value_length" in metadata:
        # The model sees a truncated value, but freshness and change detection must
        # still notice edits past the cut, so the guard hashes the full text.
        element = _with_full_value_guard(element, full_value)
    return element


def _with_full_value_guard(element: DesktopElement, full_value: Any) -> DesktopElement:
    digest = hashlib.sha256(str(full_value).encode("utf-8", "surrogatepass")).hexdigest()
    base = replace(element, value=f"sha256:{digest}").semantic_guard()
    return replace(element, guard=base)


def _element_id(props: Mapping[str, Any], parent_id: str | None, sibling_index: int) -> str:
    runtime_id = props.get("runtime_id")
    if runtime_id:
        # GetRuntimeId is UIA's purpose-built identity, unique while the element lives.
        return "uia_" + hashlib.sha1(",".join(str(v) for v in runtime_id).encode()).hexdigest()[:14]
    # Some providers expose no RuntimeId; derive a structural id that stays stable
    # while the element keeps its place in the tree.
    payload = json.dumps(
        [
            props.get("control_type"),
            props.get("automation_id"),
            props.get("class_name"),
            props.get("name"),
            parent_id,
            sibling_index,
        ],
        default=str,
    )
    return "uiax_" + hashlib.sha1(payload.encode()).hexdigest()[:14]


def _revision(elements: list[DesktopElement] | tuple[DesktopElement, ...]) -> str:
    payload = [
        {
            "id": e.id,
            "role": e.role,
            "name": e.name,
            "value": e.value,
            "enabled": e.enabled,
            "focused": e.focused,
            "selected": e.selected,
            "expanded": e.expanded,
            "parent_id": e.parent_id,
            # Captures edits beyond a truncated value; empty for everything else.
            "guard": e.guard,
        }
        for e in elements
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _coerce_range_value(supplied: Any, minimum: Any, maximum: Any) -> float:
    if isinstance(supplied, bool):
        raise UnsupportedDesktopAction("A range value requires a number, not a boolean")
    try:
        number = float(supplied)
    except (TypeError, ValueError) as exc:
        raise UnsupportedDesktopAction(f"Could not parse numeric value: {supplied!r}") from exc
    if not math.isfinite(number):
        raise UnsupportedDesktopAction(f"Range value must be finite: {supplied!r}")
    if isinstance(minimum, (int, float)) and number < minimum:
        raise UnsupportedDesktopAction(f"Value {number} is below the control minimum {minimum}")
    if isinstance(maximum, (int, float)) and number > maximum:
        raise UnsupportedDesktopAction(f"Value {number} is above the control maximum {maximum}")
    return number


# -- keyboard / pointer encoding (pure) --------------------------------------------

# Windows virtual-key codes (winuser.h) for the shared keyboard.KEY_NAMES vocabulary.
# OEM punctuation codes assume a US layout, matching the macOS backend's ANSI table.
_VK_CODES: dict[str, int] = {
    "ENTER": 0x0D,
    "ESCAPE": 0x1B,
    "TAB": 0x09,
    "SPACE": 0x20,
    "BACKSPACE": 0x08,
    "DELETE": 0x2E,
    "ARROW_LEFT": 0x25,
    "ARROW_UP": 0x26,
    "ARROW_RIGHT": 0x27,
    "ARROW_DOWN": 0x28,
    "HOME": 0x24,
    "END": 0x23,
    "PAGE_UP": 0x21,
    "PAGE_DOWN": 0x22,
    **{chr(c): c for c in range(ord("A"), ord("Z") + 1)},
    **{chr(c): c for c in range(ord("0"), ord("9") + 1)},
    **{f"F{i}": 0x6F + i for i in range(1, 21)},
    "MINUS": 0xBD,
    "EQUAL": 0xBB,
    "LEFT_BRACKET": 0xDB,
    "RIGHT_BRACKET": 0xDD,
    "BACKSLASH": 0xDC,
    "SEMICOLON": 0xBA,
    "QUOTE": 0xDE,
    "COMMA": 0xBC,
    "PERIOD": 0xBE,
    "SLASH": 0xBF,
    "GRAVE": 0xC0,
}

_VK_SHIFT = 0x10
_VK_CONTROL = 0x11
_VK_MENU = 0x12
_VK_LWIN = 0x5B
_VK_RWIN = 0x5C
_VK_RETURN = 0x0D

# MOD is Ctrl on Windows, so MOD+CTRL collapses into one Ctrl press.
_MODIFIER_VKS = {"MOD": _VK_CONTROL, "CTRL": _VK_CONTROL, "ALT": _VK_MENU, "SHIFT": _VK_SHIFT}

# Keys whose scan codes need KEYEVENTF_EXTENDEDKEY, or apps read them as numpad keys.
_EXTENDED_VKS = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E})

_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004

_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_HWHEEL = 0x1000
_MOUSEEVENTF_VIRTUALDESK = 0x4000
_MOUSEEVENTF_ABSOLUTE = 0x8000

_WHEEL_DELTA = 120
_SCROLL_NOTCHES = 3


def _chord_events(hotkey: str) -> list[tuple[int, bool]]:
    """Encode a chord as (virtual key, key_up) pairs: modifiers down, key tap, modifiers up reversed."""
    try:
        modifiers, key = parse_hotkey(hotkey)
    except ValueError as exc:
        raise UnsupportedDesktopAction(str(exc)) from exc
    vk = _VK_CODES.get(key)
    if vk is None:
        raise UnsupportedDesktopAction(f"Unsupported Windows hotkey key: {key}")
    modifier_vks: list[int] = []
    for modifier in modifiers:
        modifier_vk = _MODIFIER_VKS.get(modifier)
        if modifier_vk is None:
            raise UnsupportedDesktopAction(f"Unsupported Windows modifier: {modifier}")
        if modifier_vk not in modifier_vks:
            modifier_vks.append(modifier_vk)
    return (
        [(m, False) for m in modifier_vks]
        + [(vk, False), (vk, True)]
        + [(m, True) for m in reversed(modifier_vks)]
    )


def _same_text(actual: str, expected: str) -> bool:
    """Compare typed text to a control's value, ignoring line-ending style and a final newline.

    Rich-edit documents report paragraph breaks as CR, and Ctrl+A leaves a
    document's final line break in place, as it would for a person typing.
    """
    def norm(value: str) -> str:
        return value.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")

    return norm(actual) == norm(expected)


def _text_units(text: str) -> list[tuple[str, int]]:
    """Split text into ("unicode", UTF-16 code unit) or ("vk", VK_RETURN) steps.

    Newlines become real Enter presses since many controls ignore a Unicode LF.
    Characters outside the BMP are sent as their UTF-16 surrogate pair.
    """
    units: list[tuple[str, int]] = []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for char in normalized:
        if char == "\n":
            units.append(("vk", _VK_RETURN))
            continue
        encoded = char.encode("utf-16-le", "surrogatepass")
        for i in range(0, len(encoded), 2):
            units.append(("unicode", int.from_bytes(encoded[i : i + 2], "little")))
    return units


def _normalize_absolute(x: float, y: float, virtual: tuple[int, int, int, int]) -> tuple[int, int]:
    """Map a physical screen point to SendInput's 0..65535 virtual-desktop space.

    Without MOUSEEVENTF_VIRTUALDESK and virtual-screen metrics, absolute input is
    normalized against the primary monitor only and misses on any other display.
    """
    left, top, width, height = virtual
    if width <= 1 or height <= 1:
        raise UnsupportedDesktopAction("Virtual screen metrics are unavailable")
    nx = round((x - left) * 65535 / (width - 1))
    ny = round((y - top) * 65535 / (height - 1))
    return min(max(nx, 0), 65535), min(max(ny, 0), 65535)


def _scroll_delta(direction: str) -> tuple[int, int]:
    """Return (wheel flag, signed delta). Positive WHEEL scrolls up, positive HWHEEL right."""
    amount = _WHEEL_DELTA * _SCROLL_NOTCHES
    if direction == "UP":
        return _MOUSEEVENTF_WHEEL, amount
    if direction == "DOWN":
        return _MOUSEEVENTF_WHEEL, -amount
    if direction == "LEFT":
        return _MOUSEEVENTF_HWHEEL, -amount
    if direction == "RIGHT":
        return _MOUSEEVENTF_HWHEEL, amount
    raise UnsupportedDesktopAction(f"Unknown scroll direction: {direction}")


def _drag_path(start: tuple[float, float], end: tuple[float, float], steps: int = 12) -> list[tuple[float, float]]:
    # Intermediate positions matter: many drop targets only register on movement.
    return [
        (start[0] + (end[0] - start[0]) * i / steps, start[1] + (end[1] - start[1]) * i / steps)
        for i in range(1, steps + 1)
    ]


# -- SendInput structures -----------------------------------------------------------
# Fixed-width types keep the layout identical to winuser.h on every 64-bit host, so
# the struct-size test is meaningful off Windows too. sizeof(INPUT) must be 40 on
# x64; a wrong union makes SendInput reject every event with ERROR_INVALID_PARAMETER.

class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_int32),
        ("dy", ctypes.c_int32),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_uint16),
        ("wScan", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_uint32), ("wParamL", ctypes.c_uint16), ("wParamH", ctypes.c_uint16)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("union", _INPUTUNION)]


_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1


def _key_input(vk: int, *, up: bool) -> _INPUT:
    flags = _KEYEVENTF_KEYUP if up else 0
    if vk in _EXTENDED_VKS:
        flags |= _KEYEVENTF_EXTENDEDKEY
    scan = _win32().user32.MapVirtualKeyW(vk, 0) & 0xFFFF
    event = _INPUT(type=_INPUT_KEYBOARD)
    event.union.ki = _KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return event


def _unicode_input(code_unit: int, *, up: bool) -> _INPUT:
    flags = _KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if up else 0)
    event = _INPUT(type=_INPUT_KEYBOARD)
    event.union.ki = _KEYBDINPUT(wVk=0, wScan=code_unit, dwFlags=flags, time=0, dwExtraInfo=0)
    return event


def _mouse_input(flags: int, *, dx: int = 0, dy: int = 0, data: int = 0) -> _INPUT:
    event = _INPUT(type=_INPUT_MOUSE)
    event.union.mi = _MOUSEINPUT(
        dx=dx, dy=dy, mouseData=data & 0xFFFFFFFF, dwFlags=flags, time=0, dwExtraInfo=0
    )
    return event


def _send(events: list[_INPUT]) -> None:
    if not events:
        return
    array = (_INPUT * len(events))(*events)
    sent = _win32().user32.SendInput(len(events), array, ctypes.sizeof(_INPUT))
    if sent != len(events):
        error = ctypes.get_last_error()
        raise UnsupportedDesktopAction(
            f"SendInput injected {sent}/{len(events)} events (Win32 error {error}); input may be "
            "blocked by a secure desktop, a locked session, or an elevated foreground window"
        )


def _require_no_held_modifiers() -> None:
    # The Windows analogue of zeroing CGEvent flags on macOS: typed characters must
    # not combine with a physically held Ctrl/Alt/Win into shortcuts.
    held = [
        name
        for name, vk in (("Ctrl", _VK_CONTROL), ("Alt", _VK_MENU), ("Win", _VK_LWIN), ("Win", _VK_RWIN))
        if _win32().user32.GetAsyncKeyState(vk) & 0x8000
    ]
    if held:
        raise UnsupportedDesktopAction(f"Refusing to type while {'/'.join(sorted(set(held)))} is held down")


def _press_key(key: str) -> None:
    vk = _VK_CODES.get(key)
    if vk is None:
        raise UnsupportedDesktopAction(f"Unsupported Windows key: {key}")
    _send([_key_input(vk, up=False), _key_input(vk, up=True)])


def _press_hotkey(hotkey: str) -> None:
    sequence = _chord_events(hotkey)
    try:
        # One SendInput call keeps the chord atomic against interleaved user input.
        _send([_key_input(vk, up=up) for vk, up in sequence])
    except UnsupportedDesktopAction:
        # A partial injection can strand a synthetic modifier in the down state.
        modifiers = [vk for vk, up in sequence if up and vk in _MODIFIER_VKS.values()]
        try:
            _send([_key_input(vk, up=True) for vk in modifiers])
        except UnsupportedDesktopAction:
            pass
        raise


# Per-character pauses for keystroke typing: the first pass, then a slower retry.
_TYPING_PAUSES_S = (0.01, 0.04)


def _type_text(text: str, *, pause_s: float = 0.01) -> None:
    """Type text as keystrokes, one character per SendInput call.

    KEYEVENTF_UNICODE arrives as VK_PACKET, whose character is resolved when the
    target *translates* the message, not when it was injected. If the target stalls
    (Notepad spell-checks on every space), batched packets queue up and all resolve
    to the newest character: "at 08:11:20" typed as "00000000000". Sending one
    character at a time with a pause keeps the queue drained.
    """
    _require_no_held_modifiers()
    units = _text_units(text)
    index = 0
    while index < len(units):
        kind, code = units[index]
        if kind == "vk":
            events = [_key_input(code, up=False), _key_input(code, up=True)]
            index += 1
        elif 0xD800 <= code <= 0xDBFF and index + 1 < len(units):
            # A surrogate pair is one character; its halves must arrive together.
            low = units[index + 1][1]
            events = [
                _unicode_input(code, up=False), _unicode_input(code, up=True),
                _unicode_input(low, up=False), _unicode_input(low, up=True),
            ]
            index += 2
        else:
            events = [_unicode_input(code, up=False), _unicode_input(code, up=True)]
            index += 1
        _send(events)
        time.sleep(pause_s)


def _virtual_screen() -> tuple[int, int, int, int]:
    metrics = _win32().user32.GetSystemMetrics
    # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN
    return metrics(76), metrics(77), metrics(78), metrics(79)


def _move_to(x: float, y: float) -> _INPUT:
    nx, ny = _normalize_absolute(x, y, _virtual_screen())
    return _mouse_input(_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK, dx=nx, dy=ny)


def _click_at(x: float, y: float, *, count: int, button: str) -> None:
    if button == "right":
        down, up = _MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP
    else:
        down, up = _MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP
    _send([_move_to(x, y)])
    for i in range(count):
        _send([_mouse_input(down), _mouse_input(up)])
        if i + 1 < count:
            time.sleep(0.06)


def _drag(start: tuple[float, float], end: tuple[float, float]) -> None:
    _send([_move_to(*start), _mouse_input(_MOUSEEVENTF_LEFTDOWN)])
    try:
        for point in _drag_path(start, end):
            time.sleep(0.015)
            _send([_move_to(*point)])
        time.sleep(0.05)
    finally:
        _send([_mouse_input(_MOUSEEVENTF_LEFTUP)])


def _scroll(direction: str, point: tuple[float, float] | None) -> None:
    flag, delta = _scroll_delta(direction)
    # Windows routes wheel input to the window under the pointer, not the focused
    # one, so the pointer is parked over the largest scrollable region first.
    events = [_move_to(*point)] if point is not None else []
    events.append(_mouse_input(flag, data=delta))
    _send(events)


# -- Win32 / COM plumbing ------------------------------------------------------------

_STALE_HRESULTS = frozenset({
    0x80040201,  # UIA_E_ELEMENTNOTAVAILABLE
    0x80040200,  # UIA_E_ELEMENTNOTENABLED
    0x80010108,  # RPC_E_DISCONNECTED
    0x800706BA,  # RPC_S_SERVER_UNAVAILABLE
    0x80070578,  # ERROR_INVALID_WINDOW_HANDLE
})


def _hresult(exc: Exception) -> int | None:
    code = getattr(exc, "hresult", None)
    if code is None and exc.args and isinstance(exc.args[0], int):
        code = exc.args[0]
    return None if code is None else code & 0xFFFFFFFF


def _hresult_hex(exc: Exception) -> str:
    code = _hresult(exc)
    return "unknown" if code is None else f"0x{code:08X}"


def _uia() -> tuple[Any, Any]:
    try:
        import comtypes  # type: ignore
        import comtypes.client  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Install the Windows extra: pip install 'jev-windows-agent[windows]'") from exc
    comtypes.client.GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient as UIA  # type: ignore

    return comtypes, UIA


def _ensure_com_initialized(comtypes: Any) -> None:
    # comtypes initializes COM for the thread that first imports it. A backend built
    # on another thread needs its own apartment; an existing one of a different
    # model (RPC_E_CHANGED_MODE) is fine to reuse.
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
    except OSError as exc:
        if (getattr(exc, "winerror", 0) or 0) & 0xFFFFFFFF != 0x80010106:
            raise


def _create_automation(comtypes: Any, UIA: Any) -> Any:
    import comtypes.client  # type: ignore

    try:
        automation = comtypes.client.CreateObject(UIA.CUIAutomation8, interface=UIA.IUIAutomation)
    except (OSError, comtypes.COMError, AttributeError):
        automation = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
    try:
        # A hung target otherwise blocks observe() for the 20s default transaction.
        tuned = automation.QueryInterface(UIA.IUIAutomation2)
        tuned.ConnectionTimeout = 2000
        tuned.TransactionTimeout = 5000
    except (OSError, comtypes.COMError, AttributeError):
        logger.debug("IUIAutomation2 timeouts unavailable; using UIA defaults")
    return automation


class _Win32:
    def __init__(self) -> None:
        from ctypes import wintypes

        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self.dwmapi = ctypes.WinDLL("dwmapi")
        u, k, a = self.user32, self.kernel32, self.advapi32

        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
        u.SendInput.restype = wintypes.UINT
        u.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        u.MapVirtualKeyW.restype = wintypes.UINT
        u.GetAsyncKeyState.argtypes = [ctypes.c_int]
        u.GetAsyncKeyState.restype = ctypes.c_short
        u.GetSystemMetrics.argtypes = [ctypes.c_int]
        u.GetSystemMetrics.restype = ctypes.c_int
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsWindowVisible.restype = wintypes.BOOL
        u.IsIconic.argtypes = [wintypes.HWND]
        u.IsIconic.restype = wintypes.BOOL
        u.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        u.GetWindow.restype = wintypes.HWND
        u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        u.GetAncestor.restype = wintypes.HWND
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.restype = ctypes.c_int
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.restype = ctypes.c_int
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetWindowRect.restype = wintypes.BOOL
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.ShowWindow.restype = wintypes.BOOL
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.SetForegroundWindow.restype = wintypes.BOOL
        u.BringWindowToTop.argtypes = [wintypes.HWND]
        u.BringWindowToTop.restype = wintypes.BOOL
        u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        u.AttachThreadInput.restype = wintypes.BOOL
        self.enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u.EnumWindows.argtypes = [self.enum_proc, wintypes.LPARAM]
        u.EnumWindows.restype = wintypes.BOOL

        k.GetCurrentProcessId.restype = wintypes.DWORD
        k.GetCurrentThreadId.restype = wintypes.DWORD
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        k.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        k.QueryFullProcessImageNameW.restype = wintypes.BOOL

        a.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        a.OpenProcessToken.restype = wintypes.BOOL
        a.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        a.GetTokenInformation.restype = wintypes.BOOL

        self.dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        self.dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long


_WIN32: _Win32 | None = None


def _win32() -> _Win32:
    global _WIN32
    if _WIN32 is None:
        _WIN32 = _Win32()
    return _WIN32


def _enable_dpi_awareness() -> int | None:
    """Opt into per-monitor-v2 DPI awareness and report the effective awareness.

    Returns 2 for per-monitor aware. Anything else means the host already fixed a
    lower awareness (it can only be set once per process), and pointer events may
    land off target on scaled displays.
    """
    user32 = _win32().user32
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_int
        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
    except AttributeError:
        logger.warning("Per-monitor DPI APIs are unavailable (pre-Windows 10 1703)")
        return None
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    awareness = user32.GetAwarenessFromDpiAwarenessContext(user32.GetThreadDpiAwarenessContext())
    if awareness != 2:
        logger.warning(
            "Process DPI awareness is %s, not per-monitor; synthesized clicks may miss on scaled displays",
            awareness,
        )
    return awareness


def _foreground() -> tuple[int | None, int | None]:
    from ctypes import wintypes

    user32 = _win32().user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None, None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(hwnd), int(pid.value) or None


def _window_class(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    _win32().user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def _is_cloaked(hwnd: int) -> bool:
    cloaked = ctypes.c_uint32(0)
    result = _win32().dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    return result == 0 and cloaked.value != 0


def _popup_windows(foreground: int, pid: int) -> list[int]:
    """Visible top-level popups (menus, drop-downs, flyouts) belonging to the foreground app."""
    from ctypes import wintypes

    w = _win32()
    found: list[int] = []

    def visit(hwnd: int, _: int) -> bool:
        hwnd = int(hwnd or 0)
        if not hwnd or hwnd == foreground or not w.user32.IsWindowVisible(hwnd) or _is_cloaked(hwnd):
            return True
        owner_pid = wintypes.DWORD()
        w.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        if owner_pid.value != pid:
            return True
        rect = wintypes.RECT()
        if not w.user32.GetWindowRect(hwnd, ctypes.byref(rect)) or rect.right <= rect.left or rect.bottom <= rect.top:
            return True
        cls = _window_class(hwnd)
        owner = int(w.user32.GetWindow(hwnd, 4) or 0)  # GW_OWNER
        owner_root = int(w.user32.GetAncestor(owner, 3) or 0) if owner else 0  # GA_ROOTOWNER
        if cls == "#32768" or owner == foreground or owner_root == foreground:
            found.append(hwnd)
        return True

    callback = w.enum_proc(visit)
    w.user32.EnumWindows(callback, 0)
    return found


def _process_image_name(pid: int) -> str | None:
    from ctypes import wintypes

    w = _win32()
    handle = w.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not w.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return buffer.value
    finally:
        w.kernel32.CloseHandle(handle)


def _application_name(pid: int, window_title: str) -> str:
    path = _process_image_name(pid)
    if not path:
        return window_title or f"pid:{pid}"
    stem = path.replace("/", "\\").rsplit("\\", 1)[-1]
    stem = stem[:-4] if stem.lower().endswith(".exe") else stem
    # UWP/WinUI apps render inside ApplicationFrameHost; its window title is the
    # recognizable app name ("Settings", "Calculator").
    if stem.lower() == "applicationframehost" and window_title:
        return window_title
    return stem


def _process_elevation(pid: int) -> bool | None:
    """True if elevated (or its token is inaccessible), False if not, None if unknown."""
    from ctypes import wintypes

    w = _win32()
    handle = w.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return True if ctypes.get_last_error() == 5 else None
    try:
        token = wintypes.HANDLE()
        if not w.advapi32.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            # Access denied to a token means a higher integrity level than ours.
            return True if ctypes.get_last_error() == 5 else None
        try:
            elevation = wintypes.DWORD()
            size = wintypes.DWORD()
            ok = w.advapi32.GetTokenInformation(
                token, 20, ctypes.byref(elevation), ctypes.sizeof(elevation), ctypes.byref(size)  # TokenElevation
            )
            return bool(elevation.value) if ok else None
        finally:
            w.kernel32.CloseHandle(token)
    finally:
        w.kernel32.CloseHandle(handle)


def _current_bounds(ref: Any) -> Bounds | None:
    rect = ref.CurrentBoundingRectangle  # a RECT: left, top, right, bottom
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    return Bounds(x=float(rect.left), y=float(rect.top), width=float(width), height=float(height))


def find_window(*, process_name: str | None = None, title_contains: str | None = None) -> int | None:
    """Return the first visible, uncloaked top-level window matching the filters.

    A convenience for examples and tests that need to put a known app in front; the
    backend itself only ever observes the foreground window.
    """
    from ctypes import wintypes

    w = _win32()
    matches: list[int] = []
    wanted_process = process_name.lower().removesuffix(".exe") if process_name else None
    wanted_title = title_contains.lower() if title_contains else None

    def visit(hwnd: int, _: int) -> bool:
        hwnd = int(hwnd or 0)
        if not hwnd or not w.user32.IsWindowVisible(hwnd) or _is_cloaked(hwnd):
            return True
        if int(w.user32.GetWindow(hwnd, 4) or 0):  # skip owned popups
            return True
        buffer = ctypes.create_unicode_buffer(512)
        w.user32.GetWindowTextW(hwnd, buffer, 512)
        if not buffer.value:
            return True
        if wanted_title and wanted_title not in buffer.value.lower():
            return True
        if wanted_process:
            pid = wintypes.DWORD()
            w.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            path = _process_image_name(pid.value) or ""
            stem = path.replace("/", "\\").rsplit("\\", 1)[-1].lower().removesuffix(".exe")
            if stem != wanted_process:
                return True
        matches.append(hwnd)
        return False

    w.user32.EnumWindows(w.enum_proc(visit), 0)
    return matches[0] if matches else None


def activate_window(hwnd: int, *, timeout_s: float = 3.0) -> bool:
    """Bring a window to the foreground; returns whether it actually got there.

    Windows only lets the foreground owner hand off focus, so this briefly attaches
    to the foreground thread's input queue, the documented route. That is refused
    intermittently (the foreground lock), so it retries, and between attempts
    injects a zero-distance mouse move, which renews this process's claim to the
    last input event without the side effects of the usual Alt-tap trick (Alt
    alone opens menus/access keys in whichever app is in front).
    """
    w = _win32()
    if w.user32.IsIconic(hwnd):
        w.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    deadline = time.perf_counter() + timeout_s
    attempt = 0
    while time.perf_counter() < deadline:
        if attempt:
            try:
                _send([_mouse_input(_MOUSEEVENTF_MOVE)])
            except UnsupportedDesktopAction:
                pass
        attempt += 1
        foreground = w.user32.GetForegroundWindow()
        foreground_thread = w.user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        own_thread = w.kernel32.GetCurrentThreadId()
        attached = bool(
            foreground_thread
            and foreground_thread != own_thread
            and w.user32.AttachThreadInput(own_thread, foreground_thread, True)
        )
        try:
            w.user32.BringWindowToTop(hwnd)
            w.user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                w.user32.AttachThreadInput(own_thread, foreground_thread, False)
        settle = time.perf_counter() + 0.5
        while time.perf_counter() < min(settle, deadline):
            if _foreground()[0] == hwnd:
                return True
            time.sleep(0.05)
    return False
