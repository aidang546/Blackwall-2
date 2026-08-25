# EREBUS

A local-first voice assistant that lives behind the Blackwall.

Wake word, speech recognition, reasoning and speech synthesis all run on your
own machine. No API keys, no per-use cost, no audio leaving the computer. The
interface is a WebGL rendering of the Blackwall: a single stationary crimson
line at rest, which tears open into a field of vertical data-rain and a torn
horizontal seam when it is listening or speaking.

```
  say "erebus"  ->  it wakes  ->  you talk  ->  it acts, or it answers
```

---

## What it does

| | |
|---|---|
| **Launch things** | "open spotify", "fire up steam", "put on cyberpunk" |
| **System control** | volume, media keys, lock, sleep, shutdown |
| **Macros** | "gaming mode" — sets volume, opens Steam and Discord in one word |
| **Conversation** | anything that isn't a command goes to a local LLM |

Everything it can do lives in `config.yaml`. Adding a command is three lines of
YAML; see [Adding your own commands](#adding-your-own-commands).

---

## Quick start (Windows)

```powershell
git clone <this repo> Blackwall-2
cd Blackwall-2
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 1. The UI and server. Enough to see the wall.
pip install -r requirements.txt
python -m erebus --no-voice

# 2. The voice pipeline. NVIDIA users: CUDA torch first, or you get the
#    CPU wheel and everything runs ten times slower.
pip install torch --index-url https://download.pytorch.org/whl/cu124
pip install -r requirements-voice.txt

# 3. The brain.
winget install Ollama.Ollama
ollama pull llama3.1:8b

python -m erebus
```

Full step-by-step, including voice downloads and autostart:
**[docs/SETUP.md](docs/SETUP.md)**.

### Just want to look at it?

```
python -m erebus --no-voice
```
Then open <http://127.0.0.1:8848/?demo> — the wall cycles through every state
with synthetic audio. No models, no GPU, nothing to download.

---

## Controls

| | |
|---|---|
| say **"erebus"** | wake it |
| **space** | push to talk (skips the wake word) |
| **esc** | cut it off mid-sentence |
| type in the console | same routing as speech, useful when muted |

---

## How it fits together

```
  microphone ─► wake word ─► record ─► whisper ─► ROUTER ─┬─► registry ─► your PC
   (always on)  openWakeWord          faster-whisper      │
                                                          └─► ollama ──► piper ─► voice fx ─► speakers
                                                              (LLM)              (the Blackwall voice)
        every stage publishes to ─► EventBus ─► websocket ─► the wall
```

Nine files carry the whole thing:

| file | what it owns |
|---|---|
| `erebus/core/assistant.py` | the loop — the only file that knows all the pieces |
| `erebus/core/bus.py` | async pub/sub; the reason the parts don't know about each other |
| `erebus/actions/registry.py` | everything it can do, and the only path by which it can do it |
| `erebus/pipeline/wake.py` `stt.py` `brain.py` `tts.py` | one stage each, all swappable |
| `erebus/pipeline/voicefx.py` | the "behind the wall" voice chain |
| `erebus/server/static/blackwall.js` | the visualiser — one WebGL fragment shader |

Every stage degrades instead of refusing to start. No GPU? Whisper falls back
to CPU. Ollama not running? Every command still works, it just can't chat.
Nothing installed at all? The UI and text console still run.

---

## Adding your own commands

```yaml
actions:
  apps:
    obsidian:
      phrases: ["obsidian", "my notes"]
      run: "start obsidian://"

  macros:
    stream_mode:
      phrases: ["stream mode", "going live"]
      say: "You are live."
      steps:
        - do: volume_set
          value: 30
        - run: "start obs64.exe"
        - wait: 2
        - run: "start chrome https://dashboard.twitch.tv"
```

`python -m erebus actions` prints everything currently registered.

Put your own edits in **`config.local.yaml`** — it deep-merges over
`config.yaml` and is gitignored, so your machine-specific paths and private
macros stay out of version control.

---

## Security

The assistant runs programs on your computer, so this part is deliberate rather
than incidental:

- **Shell commands come only from your config.** Speech and the LLM can select
  an action *by name*; neither can compose, extend, or parameterise a command.
  The worst a misheard sentence can do is run something you already wrote down.
- **Destructive actions need spoken confirmation.** `shutdown`, `restart` and
  `sleep` by default — `safety.confirm` in the config.
- **The server binds to loopback.** Nothing else on your network can reach it
  until you change `server.host`.
- **Remote clients need a token *and* a private-network address.** Both, not
  either: a leaked token still shouldn't be usable from the open internet. The
  token is generated on first run into `.erebus_token` (gitignored).

`python -m erebus pair` prints the URL to open on your phone.

---

## Tuning the look

`ui:` in the config covers intensity, film grain and scanlines. The shape of
the wall itself is the `ENERGY` table at the top of `blackwall.js` — one number
per state, and that table is the entire look design.

The visualiser is exposed as `__wall` in the browser console, so you can try
things live without restarting anything:

```js
__wall.setState('speaking');
__wall.setLevel(0.8);
__wall.ping();          // the travelling pulse that fires on an action
```

---

## Voice and persona

`brain.persona` is the system prompt — cold, terse, no filler. Rewrite it to
taste.

`tts.effects` is the voice chain: pitch down, formant shift, band-limiting,
a small dark room, and a thin static bed. Test changes without a full run:

```
python -m erebus say "The wall holds."
```

Set `tts.effects.enabled: false` for a clean read.

---

## Mobile

The phone client is the same page. `python -m erebus pair` prints a URL with
the token in the fragment; open it on your phone on the same Wi-Fi and you get
the same wall, the same console, and push-to-talk.

On Android the browser handles speech recognition locally. On iOS there is no
background microphone, so it is tap-to-talk — which is what the HOLD button is
for. A native wrapper for always-on phone listening is the obvious next step
and nothing in the architecture is in its way; the daemon already treats every
client as a remote.

---

## Tests

```
python tests/test_routing.py    # the matcher, against real phrasings
python tests/test_e2e.py        # boots the daemon, drives it over the websocket
```

Neither needs a GPU, a model, or a microphone.

---

## Status

Desktop (Windows) is the built target. Known gaps:

- The wake word ships as a stock openWakeWord model standing in for "erebus"
  until you train a real one — [docs/WAKEWORD.md](docs/WAKEWORD.md), about
  fifteen minutes.
- macOS and Linux run everything except `actions/system.py`, which is Windows
  native and no-ops elsewhere.
- Mobile is browser-based, so iOS is tap-to-talk only.
