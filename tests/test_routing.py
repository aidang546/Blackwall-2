"""Exercise the matcher against the phrasings a real person actually uses."""
import pathlib
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from erebus.core.config import Config
from erebus.actions.registry import Registry

cfg = Config.load()
reg = Registry(cfg, confirm=set(cfg.get("safety.confirm") or []))

CASES = [
    # utterance                      expected action   expected value
    ("gaming mode",                  "gaming_mode",    None),
    ("let's play",                   "gaming_mode",    None),
    ("open spotify",                 "spotify",        None),
    ("launch spotify please",        "spotify",        None),
    ("spotify",                      "spotify",        None),
    ("fire up steam",                "steam",          None),
    ("put on cyberpunk",             "cyberpunk",      None),
    ("volume up",                    "volume_up",      None),
    ("turn it down",                 "volume_down",    None),
    ("set volume to 40",             "volume_set",     "40"),
    ("volume to 12",                 "volume_set",     "12"),
    ("next track",                   "next_track",     None),
    ("lock the computer",            "lock",           None),
    ("shut down",                    "shutdown",       None),
    ("good night",                   "wind_down",      None),
    ("work mode",                    "work_mode",      None),
    ("open the editor",              "vscode",         None),
    # Real Whisper near-misses. These are transcripts the recogniser actually
    # produced for the phrase on the right, so the matcher has to absorb them.
    ("coming mode",                  "gaming_mode",    None),
    ("gaming mold",                  "gaming_mode",    None),
    ("log the computer",             "lock",           None),
    ("next drack",                   "next_track",     None),
    ("work modes",                   "work_mode",      None),

    # These must NOT match a command - they are questions/conversation.
    ("what is the weather like",     None,             None),
    ("how does the display work",    None,             None),   # 'display' vs 'play'
    ("tell me about the blackwall",  None,             None),
    ("who are you",                  None,             None),
    ("set volume",                   None,             None),   # no number yet

    # Fuzzy matching must not become a licence to guess. Each of these is
    # within a few characters of a real phrase but means something else.
    ("what mode is this",            None,             None),
    ("i am going to lock up now",    None,             None),
    ("gaming is fun",                None,             None),
    ("do not shut down",             "shutdown",       None),   # gated on confirm
]

failures = 0
for utterance, want_action, want_value in CASES:
    m = reg.match(utterance)
    got_action = m.action.name if m else None
    got_value = m.value if m else None
    # Mirror the assistant's confidence gate.
    if m and not (m.exact or m.score >= 6):
        got_action, got_value = None, None
    ok = got_action == want_action and got_value == want_value
    if not ok:
        failures += 1
    print(f"  {'ok  ' if ok else 'FAIL'}  {utterance:<32} -> {got_action}"
          f"{'  =' + str(got_value) if got_value else ''}"
          f"{'   (wanted ' + str(want_action) + ')' if not ok else ''}")

print(f"\n  {len(CASES) - failures}/{len(CASES)} passed")
print(f"  confirmation-gated: {sorted(reg.confirm)}")
sys.exit(1 if failures else 0)
