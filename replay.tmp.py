import logging, sys, time
sys.path.insert(0, "examples")
from pathlib import Path
from collections import Counter
from windows_task import WindowScope, load_env_file
load_env_file(Path(".env.local"))
from jev_windows_agent import Subtask
from jev_windows_agent.backends import WindowsUIABackend
from jev_windows_agent.backends import windows_uia as w
from jev_windows_agent.policies import TypeSafeJevPolicy
sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.DEBUG, format="  log %(name)s: %(message)s")
for noisy in ("httpx", "httpcore", "comtypes"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
h = w.find_windows(process_name="spotify")[0]
backend = WindowsUIABackend()
scope = WindowScope(backend, h)
t0 = time.perf_counter()
snap = scope.observe()
print(f"first observe took {(time.perf_counter()-t0)*1000:.0f} ms | hidden={scope.last_observation_hidden} | elements={len(snap.elements)} "
      f"actionable={sum(1 for e in snap.elements if e.actions)}")
print("roles:", Counter(e.role for e in snap.elements).most_common(8))
print("actionable sample:", [(e.role, e.name[:30]) for e in snap.elements if e.actions][:12])
task = Subtask(goal="Open Spotify and start playback of the user's Liked Songs playlist",
               verification=("Spotify window is visible", "The Liked Songs playlist is shown as the current playback context",
                             "The play/pause control shows playback is active"),
               constraints=("Do not change or delete any playlists or saved songs", "Do not modify account settings"))
policy = TypeSafeJevPolicy()
body, maps = policy._fitted_request(task, snap, ())
print(f"request: {len(body['state']['desktop']['elements'])} elements sent; truncation={body['state']['candidate_truncation']}")
d = policy.decide(subtask=task, snapshot=snap, history=())
probs = sorted(d.raw["answers"]["operation"]["probabilities"].items(), key=lambda kv: -kv[1])[:4]
tgt = snap.element(d.target_id).name if d.target_id else None
print(f"JEV would: {(d.kind or d.terminal).value} target={tgt!r} conf={d.confidence:.2f} {probs}  (not executed)")
