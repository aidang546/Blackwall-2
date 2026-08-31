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
| **Briefings** | "brief me" — where you stand, in a register that does not flatter you |
| **Talks back fast** | streams as it thinks; talk over it and it stops |
| **Investigation** | capture and attest to a page, read EXIF, research a domain |
| **OPSEC** | tamper-evident audit chain, encrypted notes, "stand down" |

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

# 2. The voice pipeline, and a voice to speak with.
pip install -r requirements-voice.txt
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12   # GPU Whisper; see note below
python -m erebus fetch-voice en_GB-alan-medium

# 3. The brain.
winget install Ollama.Ollama
ollama pull llama3.1:8b

python -m erebus
```

Stuck at any point, run this instead of guessing:

```powershell
python -m erebus doctor
```

It checks Python, every dependency, the microphone, CUDA, the voice files,
Ollama, and your config — and prints the exact command to fix whatever is
broken. Nothing about a first run needs to be diagnosed one error at a time.

Whisper here runs on CTranslate2, **not torch** — installing torch does nothing
for it. What it wants on an NVIDIA card is cuBLAS and cuDNN. Without them it
logs the CUDA failure and falls back to CPU rather than refusing to start, so
check the startup line to see which you got.

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
| **talk over it** | it stops, the way a person would |
| **esc** | cut it off from the keyboard |
| type in the console | same routing as speech, useful when muted |

### Responsiveness

Replies are streamed. The model generates, the first sentence is synthesised
and spoken while the rest is still being written, and synthesis of the next
sentence overlaps playback of the current one — so after the first chunk there
is no gap between sentences.

Measured on one reply, comparing the same generation both ways:

```
  generation, start to finish        7.20s
  synthesising the whole reply       1.10s
  BEFORE  first word after           8.30s   generate all, then synthesise all
  AFTER   first word after           2.27s   first chunk only
```

73% of the dead air, gone. Those absolute numbers are a slow CPU box; on a GPU
both shrink and the ratio holds.

The chunker cuts at sentence ends, and at clause boundaries once a chunk is
long enough to be worth speaking alone. The **first** cut is allowed to be much
shorter than later ones — it is the only one you experience as latency, since
every cut after it happens while audio is already playing.

### Barge-in

Talking over Erebus stops it. The awkward part is that its own voice comes back
through the microphone, so the gate is deliberately high and has to be
sustained — a single loud frame is a cough or an echo, not an interruption:

```yaml
audio:
  barge_in:
    enabled: true
    threshold_multiplier: 3.5   # raise if it interrupts itself
    sustain: 0.35               # seconds the level must hold
```

On headphones there is no echo path and the defaults are comfortable. On
speakers, raise `threshold_multiplier` if it cuts itself off.

Barge-in **stops** it; it does not then start listening. That is deliberate —
auto-listening on an echo-triggered barge-in would have it transcribe its own
tail and act on the result. Say the wake word, or hit space.

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

Since it holds notes, sources and health data and can run programs, it is
hardened in its own right: a hash-chained audit log of every action and every
connection, per-address lockout on failed auth, machine-bound encryption at
rest, and a real microphone kill switch. Full detail, including what the
encryption does *not* protect against: **[docs/OPSEC.md](docs/OPSEC.md)**.

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

### It remembers

Two kinds of memory, arriving two different ways, both surviving a restart.

**It learns your wording by being corrected.** Every question it asks is a
labelled example - you answer once, and that phrasing is yours from then on:

```
you   open the music thing
it    Spotify?
you   yes
      -> opens Spotify, and never asks about that wording again
```

A bare "open" is deliberately never learned. Answering it once does not mean
"open" is Spotify forever; it means you answered that question that time.

**It holds what you tell it.**

```
you   remember that I train on Tuesdays and Thursdays
it    Held.
you   what do you know about me
it    1. I train on Tuesdays and Thursdays...
you   forget about Tuesdays
it    Dropped 1.
```

Facts go into the system prompt rather than the chat history, so it knows them
the way it knows its persona - they survive clearing the conversation, and it
does not have to be reminded. Your words are stored as you said them, casing
and all.

Both live in `memory.local.jsonl`, append-only and gitignored. Forgetting is
recorded as an event rather than by deleting a line: a store you can quietly
revise cannot be trusted to tell you what it thinks it knows. And it can always
be asked - a wrong belief is findable and removable out loud.

### It asks rather than guessing

You do not have to know the vocabulary. An unfinished request gets a question:

```
you   open
it    Which. Spotify, browser, vs code, explorer, terminal, or steam.
you   the second one
      -> launches the browser
```

Half a request works too — "open the music thing" gets *"Spotify?"*, and a yes
runs it. So does "start the game", and "open something to write code".

Answers can be the name, an alias it was read out under, a position ("the
second one", "three", "last"), or "never mind". Saying something else entirely
is treated as a new command rather than a wrong answer, and an unanswered
question expires after 45 seconds so a stray word later does not launch
anything.

The important part is what this does *not* change: the choices only ever come
from your registry. Answering a question cannot reach an action you have not
configured, exactly as the model cannot compose one. And it stays out of
ordinary conversation — "open the music thing" gets a question, "what is the
weather" gets an answer.

### Reaching it

`ctrl+alt+space` from any window: tap, speak, done. `ctrl+alt+x` stops it
talking. Both are registered with Windows itself, so they work from inside a
game or an editor — unlike the wall's space bar, which needs its window
focused.

The wake word is **not** "Erebus". openWakeWord ships no model for it, and
measured against all six stock models the word scores 0.000 — see
[docs/WAKEWORD.md](docs/WAKEWORD.md) for the numbers and for training a real
one. Until then, use the hotkey.

### Installing

```
python install.py
```

Does the whole setup and stops at the first thing that needs a human, with the
command to type. Safe to re-run; `--check` changes nothing. Standard library
only, since it runs before anything is installed.

### Proving it works

```
python -m erebus selftest
```

`doctor` checks that things are present; this runs them. It moves the volume
and puts it back, registers and releases the hotkeys, opens the microphone,
seals and unseals a vault value, then synthesises a command, transcribes it and
routes it — with the clock running on each stage. `shutdown`, `restart`,
`sleep` and `lock` are reported as wired but deliberately not executed.

### Calibration

```
python -m erebus calibrate
```

Three settings are properties of your room rather than preferences: the noise
floor, your speaking level, and how much of Erebus's own voice returns through
the microphone. This measures all three — including talking to itself to find
the echo path — and writes `config.local.yaml`. It prints what it measured
next to what it concluded, so a wrong number is visible rather than mysterious.

`brain.persona` is the system prompt — cold, terse, no filler. Rewrite it to
taste.

`tts.effects` carries more of the character than the voice model does. Game AI
voices are mostly processing applied to an ordinary human take — the inhuman
quality comes from detuned layering, ring modulation and resonance, not from a
special performance. Five presets:

| preset | what it is | intelligible |
|---|---|---|
| `clean` | barely processed; the persona does the work | 93% |
| `machine` | heavy ring modulation and quantisation. Classic robot. | 75% |
| `broadcast` | fitted to a real comms mix. Band-limited, mid-forward, hard. | 71% |
| `transmitted` | a processed human. Cold, but plainly a person. **Default.** | 67% |
| `blackwall` | weight underneath, metallic edge, resonant cavity | 37% |

The last column is how much of a sentence survives being spoken through the
preset and transcribed back by Whisper — five sentences, two runs each, against
96% for the unprocessed voice. Treat gaps under about ten points as a tie; the
measurement is that noisy. `blackwall` at 37% is not noise, though, and is
worth knowing before you pick it for anything you need to *hear* rather than
admire. `broadcast` and `transmitted` are a coin toss on clarity, so pick
between them on character.

```yaml
tts:
  effects:
    preset: blackwall
    reverb: 0.4        # any key set alongside a preset overrides just that key
```

`blackwall` used to be built around layering — three copies of the voice a few
cents apart, speaking as one. That is the effect people mean by "sounds like an
AI", and it had to go: measured, it cost 20-25 points of word accuracy however
it was built, as pitch-shifted copies or as a proper chorus, at no delay spread
or at 28ms, and the preset as a whole was transcribing at **0%**. What is left
is the part a voice survives — an octave-down layer for weight, a 42Hz ring
modulator for the metallic edge, and a tuned comb for the resonant-cavity
quality. Set `detune_voices: 3` to put the layering back if you want it and can
live with the cost.

`broadcast` was not chosen by ear. A reference comms mix was measured for the
properties a *mix* has — octave-band balance, spectral tilt, reverb decay time,
noise texture — and a grid search found the chain settings that put a Piper
render closest to those numbers, fitted on one sentence and checked against a
held-out one. It lands on -4.7 dB/octave of tilt against the reference's -4.7,
and 551ms of decay against 511.

The fit deliberately stops short of matching two things: the energy below
125Hz, and the vowel energy around 500-1000Hz. Those are not properties of the
processing — they are where that particular speaker's fundamental and vowels
happen to sit. Chasing them would mean modelling a person rather than a
channel, which is a different thing and not one this does.

Audition without a full run:

```
python -m erebus say "The wall holds."
python -m erebus say "The wall holds." --out wall.wav
```

Set `tts.effects.enabled: false` for an unprocessed read.

---

## Mobile

The phone client is the same page. `python -m erebus pair` prints a URL with
the token in the fragment; open it on your phone on the same Wi-Fi and you get
the same wall, the same console, and push-to-talk.

It ships a web app manifest, so "Add to Home Screen" installs it as a
fullscreen app with its own icon and no browser chrome. The pairing token is
kept in `localStorage`, which survives relaunching from the home screen.

On Android the browser handles speech recognition locally. On iOS there is no
background microphone, so it is tap-to-talk — which is what the HOLD button is
for. A native wrapper for always-on phone listening is the obvious next step
and nothing in the architecture is in its way; the daemon already treats every
client as a remote.

---

## Briefings

Say **"brief me"** and it tells you where you stand against what you said you
would do — reading your commitments, your journal and your wearable export, and
never inventing a figure it was not given.

```
Two videos in thirty days against a target of one a week. You are four behind
and the gap is not closing. Your resting heart rate has climbed while your
variability has fallen and you are sleeping five and a half hours, so the two
sessions you managed this week were not discipline, they were what was left
after you spent yourself on nothing.
```

It fires only when asked. Setup is one file:

```powershell
copy profile.example.yaml profile.local.yaml   # then fill it in
python -m erebus brief
```

Recovery trends are computed in Python before the model sees them, so it cannot
get your numbers backwards — and when the data shows you are under-recovered it
lowers the training demand rather than raising it.

Full guide: **[docs/BRIEFING.md](docs/BRIEFING.md)**. Getting your wearable data
in, and where it goes: **[docs/HEALTH.md](docs/HEALTH.md)**.

## Auditioning the voice

You do not need a sound card to work on the voice — render it to a file:

```
python -m erebus fetch-voice en_GB-alan-medium
python -m erebus say "The wall holds." --out wall.wav
python -m erebus say "The wall holds." --out dry.wav --dry     # no effects
python -m erebus voices                                        # what's installed
```

`--voice NAME` overrides the configured voice for one render, which makes
comparing candidates a one-liner.

## Running without a microphone

`--fake-mic` feeds a WAV file through the real capture path — same frames, same
silence detection, same recogniser, same matcher — takes one turn, and exits:

```
python -m erebus say "gaming mode" --dry --out cmd.wav
python -m erebus --fake-mic cmd.wav
```

```
  replaying...
    heard    'coming mode.'
    action   gaming_mode
    said     'Reallocating. Good hunting.'
```

Besides making the loop testable on a machine with no audio hardware, this
makes a misheard command reproducible: capture the audio once, then replay it
while you fix the phrasing.

## Tests

```
python tests/test_routing.py         # the matcher, against real phrasings
python tests/test_e2e.py             # boots the daemon, drives it over the websocket
python tests/test_brain.py           # the LLM layer, against a scripted Ollama
python tests/test_briefing.py        # profile, journal, wearable parsing, prompt
python tests/test_streaming.py       # sentence chunking and barge-in
python tests/test_opsec.py           # audit chain, vault, lockout, EXIF
python tests/test_doctor.py          # the diagnostic, against broken installs
python tests/test_voice_roundtrip.py # speaks commands, transcribes them, routes them
python tests/test_wake.py            # speaks the wake word at the detector
```

The first seven need no GPU, model, or microphone. The last two need the voice
extras and one Piper voice, and skip cleanly without them.

`test_brain.py` is the one that guards the security claim: it feeds the router
a hallucinated action name and a shell command dressed up as one, and asserts
both are refused. It also covers the shapes a real model actually produces —
JSON buried in prose, truncated JSON, an empty reply — plus a dead server, a
500, and a model that was never pulled.

---

## Status

Desktop (Windows) is the built target. Known gaps:

- The wake word ships as a stock openWakeWord model standing in for "erebus"
  until you train a real one — [docs/WAKEWORD.md](docs/WAKEWORD.md), about
  fifteen minutes.
- macOS and Linux run everything except `actions/system.py`, which is Windows
  native and no-ops elsewhere.
- Mobile is browser-based, so iOS is tap-to-talk only.
