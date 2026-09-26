"""Print a compact semantic snapshot of the foreground Windows app.

Usage:
  pip install -e '.[windows]'
  python examples/windows_uia_probe.py                     # whatever is in front after the delay
  python examples/windows_uia_probe.py --process notepad   # bring a running app forward first
  python examples/windows_uia_probe.py --title "Settings"

The probe observes twice and reports how many element ids survived. Ids must be
stable between observations of an unchanged window, or the runtime's settling and
freshness checks cannot work against that app.
"""

import argparse
import sys
import time

from jev_windows_agent.backends import WindowsUIABackend
from jev_windows_agent.backends.windows_uia import activate_window, find_window

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--process", help="executable name of a running app to bring forward, e.g. notepad")
parser.add_argument("--title", help="substring of a window title to bring forward")
parser.add_argument("--delay", type=float, default=0.0, help="seconds to wait before observing")
parser.add_argument("--limit", type=int, default=150, help="max elements to print")
args = parser.parse_args()

# Window titles routinely contain characters the legacy console code page lacks.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

backend = WindowsUIABackend()

if args.process or args.title:
    hwnd = find_window(process_name=args.process, title_contains=args.title)
    if hwnd is None:
        sys.exit(f"No visible window matched process={args.process!r} title={args.title!r}")
    if not activate_window(hwnd):
        sys.exit("Found the window but Windows refused to bring it to the foreground")
if args.delay:
    time.sleep(args.delay)

started = time.perf_counter()
snapshot = backend.observe()
first_ms = (time.perf_counter() - started) * 1000
started = time.perf_counter()
again = backend.observe()
second_ms = (time.perf_counter() - started) * 1000

print(f"{snapshot.application} — {snapshot.window} — {len(snapshot.elements)} elements")
print(f"observe: {first_ms:.0f} ms, then {second_ms:.0f} ms; dpi_awareness={backend.dpi_awareness}")
first_ids = {e.id for e in snapshot.elements}
second_ids = {e.id for e in again.elements}
print(
    f"id stability: {len(first_ids & second_ids)}/{len(first_ids)} ids survived a second observe; "
    f"revision {'stable' if snapshot.revision == again.revision else 'CHANGED'}"
)
actionable = [e for e in snapshot.elements if e.actions]
print(f"actionable: {len(actionable)}")
print()
for element in snapshot.elements[: args.limit]:
    print(element.compact())
