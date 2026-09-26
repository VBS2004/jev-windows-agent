"""Pure-logic tests for the Windows UIA backend; they run on every platform.

Everything here is COM-free by design: element mapping works on plain property
dicts, and input encoding produces key/coordinate values before any SendInput call.
The few tests that need a live Windows session are marked and skipped elsewhere.
"""

from __future__ import annotations

import ctypes
import math
import subprocess
import sys
import threading

import pytest

from jev_windows_agent import ActionKind
from jev_windows_agent.backends import windows_uia as w
from jev_windows_agent.errors import UnsupportedDesktopAction
from jev_windows_agent.keyboard import KEY_NAMES
from jev_windows_agent.models import Bounds, DesktopElement, DesktopSnapshot

BUTTON, EDIT, DOCUMENT, LIST_ITEM, PANE, GROUP, TEXT, CHECKBOX, SLIDER, MENU_ITEM = (
    50000, 50004, 50030, 50007, 50033, 50026, 50020, 50002, 50015, 50011,
)


def props(**overrides):
    base = {
        "control_type": BUTTON,
        "name": "",
        "is_enabled": True,
        "is_offscreen": False,
        "is_keyboard_focusable": False,
        "has_keyboard_focus": False,
        "bounds": Bounds(10, 20, 100, 30),
    }
    base.update(overrides)
    return base


def element(**overrides):
    return w._element_from_props(props(**overrides), "el", "parent")


# -- pattern gating ------------------------------------------------------------------


def test_pattern_state_is_ignored_without_the_pattern() -> None:
    # Live finding: GetCachedPropertyValue reports type defaults for unsupported
    # patterns -- ToggleState 2 (Indeterminate), ExpandCollapseState 3 (LeafNode).
    e = element(name="Plain", toggle_state=2, expand_collapse_state=0, selection_item_is_selected=True)
    assert e is not None
    assert e.value is None
    assert e.expanded is None
    assert e.selected is None


@pytest.mark.parametrize(("state", "expanded"), [(0, False), (1, True), (2, True), (3, None)])
def test_expand_collapse_state_mapping(state: int, expanded: bool | None) -> None:
    e = element(control_type=MENU_ITEM, name="File", has_expand_collapse=True, expand_collapse_state=state)
    assert e.expanded is expanded


@pytest.mark.parametrize(("state", "value"), [(0, False), (1, True), (2, "indeterminate")])
def test_toggle_state_becomes_the_value(state: int, value: object) -> None:
    e = element(control_type=CHECKBOX, name="Wrap", has_toggle=True, toggle_state=state)
    assert e.value == value
    assert e.metadata["value_type"] == "toggle"
    assert ActionKind.CLICK in e.actions


# -- click method --------------------------------------------------------------------


def test_list_items_select_on_click_even_when_invokable() -> None:
    # In Explorer, a list item's Invoke opens the file; a click only selects it.
    patterns = frozenset({"selection_item", "invoke"})
    assert w._click_method("ListItem", patterns, has_bounds=True) == "select"


@pytest.mark.parametrize(
    ("role", "patterns", "has_bounds", "method"),
    [
        ("Button", {"invoke"}, True, "invoke"),
        ("CheckBox", {"toggle"}, True, "toggle"),
        ("MenuItem", {"expand_collapse"}, True, "expand"),
        ("Custom", {"selection_item"}, True, "select"),
        ("Button", set(), True, "synthetic"),
        ("Button", set(), False, None),
        ("Text", set(), True, None),
        ("Pane", set(), True, None),
    ],
)
def test_click_method_precedence(role: str, patterns: set[str], has_bounds: bool, method: str | None) -> None:
    assert w._click_method(role, frozenset(patterns), has_bounds=has_bounds) == method


def test_pointer_only_clicks_are_labelled_for_the_model() -> None:
    e = element(name="Legacy button")
    assert e.actions == (ActionKind.CLICK,)
    assert e.metadata["click_via"] == "pointer"
    assert "click_via" not in element(name="Native", has_invoke=True).metadata


# -- typing method ---------------------------------------------------------------------


def writable_value(**extra):
    return {"has_value": True, "value_is_read_only": False, "value_value": "", **extra}


def test_documents_are_typed_with_keystrokes() -> None:
    # Live finding: Notepad accepts ValuePattern.SetValue on its editor, but the tab
    # stays "Unmodified", so a planner verifying "saved" would be misled.
    p = props(control_type=DOCUMENT, is_keyboard_focusable=True, **writable_value())
    assert w._typing_method(p) == "keyboard"
    e = w._element_from_props(p, "doc", None)
    assert e.metadata["text_entry"] == "keyboard"
    # Live JEV run: offered SET_VALUE on the editor, the policy used it, the tab never
    # showed "Modified", and it retried the save until BLOCKED. Typing is the only edit.
    assert e.actions == (ActionKind.TYPE_TEXT,)
    assert "value_settable" not in e.metadata


def test_documents_without_keyboard_typing_keep_set_value() -> None:
    # Without keyboard focus there is no typing path, so the value pattern is the edit.
    p = props(control_type=DOCUMENT, name="Notes", is_keyboard_focusable=False, **writable_value())
    assert w._typing_method(p) == "value"
    e = w._element_from_props(p, "doc", None)
    assert e.actions == (ActionKind.TYPE_TEXT, ActionKind.SET_VALUE)


def test_edit_fields_use_value_pattern() -> None:
    # SetValue on single-line fields runs app logic (Settings search still searched).
    p = props(control_type=EDIT, is_keyboard_focusable=True, **writable_value())
    assert w._typing_method(p) == "value"
    assert "text_entry" not in w._element_from_props(p, "edit", None).metadata


@pytest.mark.parametrize(
    ("overrides", "method"),
    [
        ({"control_type": EDIT, "is_keyboard_focusable": True, "has_text": True}, "keyboard"),
        ({"control_type": EDIT, "is_keyboard_focusable": True}, None),
        ({"control_type": EDIT, "is_keyboard_focusable": True, "has_text": True, "is_enabled": False}, None),
        ({"control_type": EDIT, "is_keyboard_focusable": True, "has_text": True, "is_password": True}, None),
        ({"control_type": BUTTON, **writable_value()}, None),
        ({"control_type": DOCUMENT, "has_text": True}, None),
    ],
)
def test_typing_method_edge_cases(overrides: dict, method: str | None) -> None:
    assert w._typing_method(props(**overrides)) == method


# -- element mapping ---------------------------------------------------------------------


def test_password_values_are_never_surfaced() -> None:
    e = element(control_type=EDIT, name="Password", is_password=True, **writable_value(value_value="hunter2"))
    assert e.value is None
    assert e.metadata["password"] is True
    assert "hunter2" not in repr(e.compact())


def test_long_values_are_truncated_but_edits_past_the_cut_change_the_guard() -> None:
    head = "x" * w._MAX_VALUE_CHARS
    first = element(control_type=DOCUMENT, name="Doc", **writable_value(value_value=head + "tail-A"))
    second = element(control_type=DOCUMENT, name="Doc", **writable_value(value_value=head + "tail-B"))
    assert first.value == head and second.value == head
    assert first.metadata["value_length"] == w._MAX_VALUE_CHARS + 6
    assert first.semantic_guard() != second.semantic_guard()
    assert w._revision([first]) != w._revision([second])


def test_short_values_use_the_standard_guard() -> None:
    e = element(control_type=EDIT, name="Search", **writable_value(value_value="abc"))
    assert e.guard == ""


def test_anonymous_containers_are_dropped_but_structural_ones_kept() -> None:
    assert element(control_type=PANE) is None
    assert element(control_type=GROUP) is not None
    assert element(control_type=TEXT, name="Label") is not None


def test_offscreen_elements_are_not_visible() -> None:
    assert element(name="Hidden", has_invoke=True, is_offscreen=True).visible is False


def test_legacy_name_is_a_fallback_only() -> None:
    assert element(has_legacy_iaccessible=True, legacy_iaccessible_name="Legacy").name == "Legacy"
    named = element(name="Primary", has_legacy_iaccessible=True, legacy_iaccessible_name="Legacy")
    assert named.name == "Primary"
    # (An automation id keeps this unnamed button identifiable, so it survives.)
    assert element(legacy_iaccessible_name="Ignored", has_invoke=True, automation_id="btn").name == ""


def test_range_values() -> None:
    e = element(
        control_type=SLIDER,
        name="Volume",
        has_range_value=True,
        range_value_is_read_only=False,
        range_value_value=40.0,
        range_value_minimum=0.0,
        range_value_maximum=100.0,
    )
    assert e.value == 40.0
    assert e.actions == (ActionKind.SET_VALUE,)
    assert e.metadata["range"] == [0.0, 100.0]


def test_writable_list_items_can_be_renamed_but_not_typed_into() -> None:
    # Explorer file items: CLICK selects, SET_VALUE renames the file.
    e = element(control_type=LIST_ITEM, name="a.txt", has_selection_item=True, **writable_value(value_value="a.txt"))
    assert e.actions == (ActionKind.CLICK, ActionKind.SET_VALUE)


def test_drag_and_drop_come_from_uia_patterns() -> None:
    assert ActionKind.DRAG_TO in element(name="Tab", has_drag=True).actions
    assert ActionKind.DRAG_TO not in element(name="Tab", has_drag=True, bounds=None).actions
    assert element(name="Folder", has_drop_target=True).accepts_drop is True
    assert element(name="Plain").accepts_drop is False


def test_role_names() -> None:
    assert w._role_name(EDIT) == "Edit"
    assert w._role_name(59999) == "ControlType59999"
    assert w._role_name(None) == "Unknown"
    assert w._role_name(True) == "Unknown"


# -- anonymous controls ------------------------------------------------------------------


def test_an_anonymous_button_is_not_offered_at_all() -> None:
    # Live: Spotify's cold Chromium tree was three unnamed buttons -- its own window
    # controls -- and the policy clicked two of them blind.
    assert element(has_invoke=True) is None


@pytest.mark.parametrize("identity", [{"name": "Close"}, {"automation_id": "view_4"}, {"help_text": "Close window"}])
def test_any_identity_keeps_a_button_clickable(identity: dict) -> None:
    assert element(has_invoke=True, **identity).actions == (ActionKind.CLICK,)


def test_a_number_is_not_identity() -> None:
    # A slider at 0.0 could be volume or brightness; setting it blind is a guess.
    slider = element(control_type=SLIDER, has_range_value=True, range_value_is_read_only=False, range_value_value=0.0)
    assert slider is not None and slider.actions == ()


def test_an_anonymous_text_field_keeps_typing_only() -> None:
    # Typing into an editable field has an effect bounded by its role; clicking or
    # setting an unknown control does not.
    field = element(control_type=EDIT, is_keyboard_focusable=True, **writable_value())
    assert field.actions == (ActionKind.TYPE_TEXT,)


def test_non_finite_numbers_are_dropped() -> None:
    # httpx refuses to encode NaN/inf, so one bad slider would crash the whole request.
    assert w._plain(float("nan")) is None
    assert w._plain(float("inf")) is None
    assert w._plain(0.5) == 0.5
    assert w._plain((1.0, float("nan"))) is None
    assert w._plain((1.0, 2.0)) == (1.0, 2.0)


# -- observation robustness -------------------------------------------------------------


def bare_backend(observations: list) -> w.WindowsUIABackend:
    """A backend with no COM behind it, replaying scripted _observe_once outcomes."""
    import threading
    from types import SimpleNamespace

    backend = object.__new__(w.WindowsUIABackend)
    backend._comtypes = SimpleNamespace(COMError=type("COMError", (Exception,), {}))
    backend._owner_thread = threading.get_ident()
    backend._warmed = set()
    outcomes = iter(observations)

    def observe_once():
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    backend._observe_once = observe_once
    return backend


def screen(hwnd: int, count: int) -> DesktopSnapshot:
    elements = tuple(
        DesktopElement(id=f"e{i}", role="Button", name=f"b{i}", source="windows_uia") for i in range(count)
    )
    return DesktopSnapshot(application="App", window="App", revision=str(count), elements=elements,
                           context={"hwnd": hwnd})


def test_a_moment_with_no_foreground_window_is_waited_out() -> None:
    # Live: during a focus change observe() raised and would have ended the run.
    backend = bare_backend([w._NoForegroundWindow(), w._NoForegroundWindow(), screen(1, 30)])
    assert len(backend._observe_settled().elements) == 30


def test_a_cold_chromium_tree_is_waited_on_before_anyone_acts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w, "_window_class", lambda hwnd: w._CHROMIUM_CLASS)
    backend = bare_backend([screen(7, 3), screen(7, 3), screen(7, 158)])
    assert len(backend.observe().elements) == 158
    # Once warm, later observations of that window are not delayed.
    backend._observe_once = lambda: screen(7, 3)
    assert len(backend.observe().elements) == 3


def test_small_non_chromium_windows_are_not_delayed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w, "_window_class", lambda hwnd: "Notepad")
    backend = bare_backend([screen(3, 4)])
    assert len(backend.observe().elements) == 4


# -- identity & revision -------------------------------------------------------------


def test_runtime_ids_are_deterministic_and_distinct() -> None:
    a = w._element_id({"runtime_id": (42, 1001)}, None, 0)
    assert a == w._element_id({"runtime_id": (42, 1001)}, "other-parent", 9)
    assert a.startswith("uia_")
    assert a != w._element_id({"runtime_id": (42, 1002)}, None, 0)


def test_fallback_ids_are_structural() -> None:
    base = {"control_type": BUTTON, "name": "OK", "automation_id": "ok"}
    a = w._element_id(base, "p", 0)
    assert a.startswith("uiax_")
    assert a == w._element_id(dict(base), "p", 0)
    assert a != w._element_id(base, "p", 1)
    assert a != w._element_id(base, "q", 0)


def test_id_collisions_are_suffixed_deterministically() -> None:
    state = w._WalkState()
    assert [state.claim_id("x"), state.claim_id("x"), state.claim_id("x")] == ["x", "x_2", "x_3"]


def test_largest_visible_scroll_region_wins() -> None:
    state = w._WalkState()
    state.note_scroll_region({"has_scroll": True, "bounds": Bounds(0, 0, 10, 10)})
    state.note_scroll_region({"has_scroll": True, "bounds": Bounds(0, 0, 100, 100)})
    state.note_scroll_region({"has_scroll": True, "is_offscreen": True, "bounds": Bounds(0, 0, 999, 999)})
    state.note_scroll_region({"has_scroll": False, "bounds": Bounds(0, 0, 500, 500)})
    assert state.scroll_point == (50.0, 50.0)


def test_revision_tracks_state() -> None:
    a = element(control_type=EDIT, name="Search", **writable_value(value_value="a"))
    b = element(control_type=EDIT, name="Search", **writable_value(value_value="b"))
    assert w._revision([a]) == w._revision([a])
    assert w._revision([a]) != w._revision([b])


# -- VARIANT / geometry helpers ------------------------------------------------------


def test_plain_drops_com_objects() -> None:
    assert w._plain("x") == "x"
    assert w._plain(3) == 3
    assert w._plain((42, 7)) == (42, 7)
    assert w._plain(object()) is None
    assert w._plain(("a", 1)) is None


def test_bounds_from_rect_is_left_top_width_height() -> None:
    assert w._bounds_from_rect((10.0, 20.0, 30.0, 40.0)) == Bounds(10.0, 20.0, 30.0, 40.0)
    assert w._bounds_from_rect((10, 20, 0, 40)) is None
    assert w._bounds_from_rect((10, 20, -5, 40)) is None
    assert w._bounds_from_rect((math.nan, 0, 1, 1)) is None
    assert w._bounds_from_rect(None) is None


def test_range_value_coercion() -> None:
    assert w._coerce_range_value("42.5", 0, 100) == 42.5
    for bad in (True, "abc", math.inf, -1, 101):
        with pytest.raises(UnsupportedDesktopAction):
            w._coerce_range_value(bad, 0, 100)


# -- keyboard encoding ---------------------------------------------------------------


def test_every_shared_key_name_has_a_virtual_key() -> None:
    assert KEY_NAMES <= set(w._VK_CODES)
    assert len(set(w._VK_CODES.values())) == len(w._VK_CODES)


def test_virtual_key_spot_checks() -> None:
    assert (w._VK_CODES["A"], w._VK_CODES["Z"], w._VK_CODES["0"]) == (0x41, 0x5A, 0x30)
    assert (w._VK_CODES["F1"], w._VK_CODES["F12"], w._VK_CODES["F20"]) == (0x70, 0x7B, 0x83)
    assert w._VK_CODES["ENTER"] == 0x0D and w._VK_CODES["ARROW_DOWN"] == 0x28


def test_chord_presses_modifiers_first_and_releases_in_reverse() -> None:
    ctrl, shift, z = w._VK_CONTROL, w._VK_SHIFT, 0x5A
    assert w._chord_events("MOD+SHIFT+Z") == [
        (ctrl, False), (shift, False), (z, False), (z, True), (shift, True), (ctrl, True),
    ]


def test_mod_is_ctrl_and_mod_ctrl_collapses() -> None:
    assert w._chord_events("MOD+S") == w._chord_events("CTRL+S")
    assert w._chord_events("MOD+CTRL+A") == [(w._VK_CONTROL, False), (0x41, False), (0x41, True), (w._VK_CONTROL, True)]


@pytest.mark.parametrize("chord", ["S", "MOD+", "MOD+s", "MOD+MOD+S", "WIN+S"])
def test_invalid_chords_are_rejected(chord: str) -> None:
    with pytest.raises(UnsupportedDesktopAction):
        w._chord_events(chord)


def test_text_units_normalize_newlines_and_split_surrogates() -> None:
    crlf = "a" + chr(13) + chr(10) + "b" + chr(13) + "c" + chr(10)
    assert w._text_units(crlf) == [
        ("unicode", ord("a")), ("vk", w._VK_RETURN),
        ("unicode", ord("b")), ("vk", w._VK_RETURN),
        ("unicode", ord("c")), ("vk", w._VK_RETURN),
    ]
    assert w._text_units("\U0001F600") == [("unicode", 0xD83D), ("unicode", 0xDE00)]


def test_same_text_ignores_line_ending_style_and_final_newline() -> None:
    cr, lf = chr(13), chr(10)
    assert w._same_text("a" + cr + "b" + cr, "a" + lf + "b")
    assert w._same_text("a" + cr + lf + "b", "a" + lf + "b" + lf)
    # The live corruption this guards against: batched VK_PACKET substitution.
    assert not w._same_text("typed 0000000000000", "typed by arc-cua at")


# -- pointer encoding ----------------------------------------------------------------


def test_absolute_coordinates_span_the_virtual_desktop() -> None:
    primary_only = (0, 0, 1920, 1080)
    assert w._normalize_absolute(0, 0, primary_only) == (0, 0)
    assert w._normalize_absolute(1919, 1079, primary_only) == (65535, 65535)
    # A second monitor left of the primary gives the virtual desktop a negative origin.
    two_monitors = (-1920, 0, 3840, 1080)
    assert w._normalize_absolute(-1920, 0, two_monitors) == (0, 0)
    assert w._normalize_absolute(0, 0, two_monitors)[0] == pytest.approx(32776, abs=1)


def test_absolute_coordinates_clamp_and_reject_bad_metrics() -> None:
    assert w._normalize_absolute(-50, 99999, (0, 0, 100, 100)) == (0, 65535)
    with pytest.raises(UnsupportedDesktopAction):
        w._normalize_absolute(0, 0, (0, 0, 0, 0))


@pytest.mark.parametrize(
    ("direction", "flag", "sign"),
    [("UP", w._MOUSEEVENTF_WHEEL, 1), ("DOWN", w._MOUSEEVENTF_WHEEL, -1),
     ("LEFT", w._MOUSEEVENTF_HWHEEL, -1), ("RIGHT", w._MOUSEEVENTF_HWHEEL, 1)],
)
def test_scroll_deltas(direction: str, flag: int, sign: int) -> None:
    got_flag, delta = w._scroll_delta(direction)
    assert got_flag == flag
    assert delta == sign * w._WHEEL_DELTA * w._SCROLL_NOTCHES


def test_unknown_scroll_direction() -> None:
    with pytest.raises(UnsupportedDesktopAction):
        w._scroll_delta("SIDEWAYS")


def test_drag_path_moves_through_intermediate_points_and_ends_exactly() -> None:
    path = w._drag_path((0.0, 0.0), (120.0, -60.0), steps=12)
    assert len(path) == 12
    assert path[-1] == (120.0, -60.0)
    assert all(a[0] < b[0] for a, b in zip(path, path[1:]))


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="winuser.h layout differs on 32-bit")
def test_input_struct_matches_winuser_h() -> None:
    # A wrong union size makes SendInput reject every event with ERROR_INVALID_PARAMETER.
    assert ctypes.sizeof(w._INPUT) == 40


# -- picking the window a task runs in -------------------------------------------------

TERMINAL = 99  # whatever was in front before a launch, e.g. the shell running the planner


def test_launch_targets_the_window_it_opened_not_an_older_match() -> None:
    # Live bug: with older Notepad windows open, the first match was an old window;
    # the launched one then took the foreground and the run saw an empty screen.
    assert w._pick_launched_window({1, 2}, [3, 1, 2], foreground=1, foreground_before=TERMINAL) == 3


def test_launch_keeps_waiting_rather_than_taking_an_older_window() -> None:
    # The launched window isn't up yet; the only match is someone's existing window.
    assert w._pick_launched_window({1}, [1], foreground=TERMINAL, foreground_before=TERMINAL) is None


def test_single_instance_app_is_found_by_coming_to_the_front() -> None:
    # Settings reuses its window: no new window, but the launch brings it forward.
    assert w._pick_launched_window({5}, [5], foreground=5, foreground_before=TERMINAL) == 5


def test_a_window_already_in_front_before_the_launch_is_not_evidence_of_it() -> None:
    assert w._pick_launched_window({5}, [5], foreground=5, foreground_before=5) is None


def test_existing_window_needs_exactly_one_match_or_the_one_in_front() -> None:
    assert w._pick_existing_window([7], foreground=TERMINAL) == 7
    assert w._pick_existing_window([7, 8, 9], foreground=8) == 8
    with pytest.raises(LookupError, match="no visible window"):
        w._pick_existing_window([], foreground=TERMINAL)


def test_several_matches_with_none_in_front_is_refused_not_guessed() -> None:
    # Typing replaces a document's text, so guessing which of the user's windows to
    # target could overwrite their work.
    with pytest.raises(LookupError, match="3 windows match"):
        w._pick_existing_window([7, 8, 9], foreground=TERMINAL)


def test_a_failed_launch_is_a_clear_error_not_a_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    # Live: --launch "...\\Spotify.exe:" (a trailing colon copied from ms-settings:)
    # surfaced as a raw CalledProcessError traceback.
    import subprocess

    def fail(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, output="", stderr="The system cannot find the file")

    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(w, "find_windows", lambda **kw: [])
    monkeypatch.setattr(w, "foreground_window", lambda: TERMINAL)
    with pytest.raises(LookupError, match="could not launch 'Spotify.exe:': The system cannot find the file"):
        w.resolve_window(process_name="spotify", launch="Spotify.exe:")


# -- platform boundaries ---------------------------------------------------------------


def test_importing_backends_does_not_load_comtypes() -> None:
    # The backends package must stay importable on macOS/Linux, where comtypes is absent.
    code = "import sys, jev_windows_agent.backends; print('comtypes' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "False"


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows guard")
def test_backend_refuses_non_windows() -> None:
    with pytest.raises(RuntimeError, match="only available on Windows"):
        w.WindowsUIABackend()


def _uia_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        w._uia()
    except Exception:
        return False
    return True


windows_only = pytest.mark.skipif(not _uia_available(), reason="needs Windows with comtypes/UIAutomationCore")


@windows_only
def test_hardcoded_ids_match_the_uia_typelib() -> None:
    _, UIA = w._uia()
    names = {
        "runtime_id": "RuntimeId", "bounding_rectangle": "BoundingRectangle", "process_id": "ProcessId",
        "control_type": "ControlType", "name": "Name", "has_keyboard_focus": "HasKeyboardFocus",
        "is_keyboard_focusable": "IsKeyboardFocusable", "is_enabled": "IsEnabled", "automation_id": "AutomationId",
        "class_name": "ClassName", "help_text": "HelpText", "is_password": "IsPassword", "is_offscreen": "IsOffscreen",
        "framework_id": "FrameworkId", "value_value": "ValueValue", "value_is_read_only": "ValueIsReadOnly",
        "range_value_value": "RangeValueValue", "range_value_is_read_only": "RangeValueIsReadOnly",
        "range_value_minimum": "RangeValueMinimum", "range_value_maximum": "RangeValueMaximum",
        "expand_collapse_state": "ExpandCollapseExpandCollapseState",
        "selection_item_is_selected": "SelectionItemIsSelected", "toggle_state": "ToggleToggleState",
        "legacy_iaccessible_name": "LegacyIAccessibleName",
    }
    patterns = {
        "invoke": "Invoke", "value": "Value", "range_value": "RangeValue", "scroll": "Scroll",
        "expand_collapse": "ExpandCollapse", "selection_item": "SelectionItem", "text": "Text",
        "toggle": "Toggle", "legacy_iaccessible": "LegacyIAccessible", "drag": "Drag", "drop_target": "DropTarget",
    }
    for pattern, gen in patterns.items():
        names[f"has_{pattern}"] = f"Is{gen}PatternAvailable"
        assert w._PATTERNS[pattern][0] == getattr(UIA, f"UIA_{gen}PatternId")
    for key, gen in names.items():
        assert w._PROPERTY_IDS[key] == getattr(UIA, f"UIA_{gen}PropertyId"), key
    for control_type, name in w._CONTROL_TYPES.items():
        assert getattr(UIA, f"UIA_{name}ControlTypeId") == control_type


@windows_only
def test_backend_is_bound_to_its_creating_thread() -> None:
    backend = w.WindowsUIABackend()
    errors: list[BaseException] = []

    def use_from_other_thread() -> None:
        try:
            backend.observe()
        except BaseException as exc:  # noqa: BLE001 - asserting on the exact failure below
            errors.append(exc)

    thread = threading.Thread(target=use_from_other_thread)
    thread.start()
    thread.join()
    assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
    assert "thread that created it" in str(errors[0])
