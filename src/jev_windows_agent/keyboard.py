"""Keyboard chord syntax shared by callers, policies, and desktop backends."""

from __future__ import annotations

from string import ascii_uppercase, digits

# MOD is Cmd on macOS and Ctrl on Windows. WIN is the Windows key, and has no macOS
# equivalent -- the macOS backend rejects it rather than silently pressing something
# else. A backend rejects any name it cannot map, so a name here is a request, not a
# promise that every platform can press it.
MODIFIERS = frozenset({"MOD", "CTRL", "ALT", "SHIFT", "WIN"})
KEY_NAMES = frozenset({
    *ascii_uppercase, *digits, *(f"F{i}" for i in range(1, 21)),
    "ENTER", "ESCAPE", "TAB", "SPACE", "BACKSPACE", "DELETE",
    "ARROW_UP", "ARROW_DOWN", "ARROW_LEFT", "ARROW_RIGHT",
    "HOME", "END", "PAGE_UP", "PAGE_DOWN", "INSERT",
    "MINUS", "EQUAL", "LEFT_BRACKET", "RIGHT_BRACKET", "BACKSLASH",
    "SEMICOLON", "QUOTE", "COMMA", "PERIOD", "SLASH", "GRAVE",
    # Windows-specific. WIN on its own opens the Start menu; APPS is the context-menu
    # key. Media keys are global: they reach the app that owns playback, which is why
    # they work for a music app that isn't the window in front.
    "WIN", "APPS",
    "MEDIA_PLAY_PAUSE", "MEDIA_NEXT", "MEDIA_PREV", "MEDIA_STOP",
    "VOLUME_UP", "VOLUME_DOWN", "VOLUME_MUTE",
})


# Names only the Windows backend can press. Kept explicit so the split is checkable:
# every *other* name must be encodable by every backend (see tests/test_shortcuts.py).
WINDOWS_ONLY_MODIFIERS = frozenset({"WIN"})
WINDOWS_ONLY_KEYS = frozenset({
    "WIN", "APPS", "INSERT",
    "MEDIA_PLAY_PAUSE", "MEDIA_NEXT", "MEDIA_PREV", "MEDIA_STOP",
    "VOLUME_UP", "VOLUME_DOWN", "VOLUME_MUTE",
})
PORTABLE_KEY_NAMES = KEY_NAMES - WINDOWS_ONLY_KEYS
PORTABLE_MODIFIERS = MODIFIERS - WINDOWS_ONLY_MODIFIERS


def parse_hotkey(hotkey: str) -> tuple[tuple[str, ...], str]:
    """Validate one chord, such as MOD+SHIFT+S; macros and sequences are not accepted."""
    if not isinstance(hotkey, str):
        raise ValueError("A shortcut chord must be a string")
    *modifiers, key = hotkey.split("+")
    if not modifiers or any(modifier not in MODIFIERS for modifier in modifiers):
        raise ValueError("A shortcut requires MOD, CTRL, ALT, SHIFT, or WIN modifiers followed by one key")
    if len(set(modifiers)) != len(modifiers):
        raise ValueError("A shortcut cannot repeat a modifier")
    if key not in KEY_NAMES:
        raise ValueError(f"Unsupported shortcut key: {key!r}; use uppercase key names")
    return tuple(modifiers), key
