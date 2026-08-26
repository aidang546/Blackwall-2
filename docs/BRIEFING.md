# Briefings

> "Two videos in thirty days against a target of one a week. You are four
> behind and the gap is not closing."

Say **"brief me"** (or "status report", "where am I", "assess me") and Erebus
tells you where you stand. It does not fire on a schedule — nothing interrupts
you.

---

## Why it isn't generic

A briefing that only generates motivational text is noise by the third day. The
only thing that makes one land is that it *knows things*: what you said you
would do, what your wearable says you actually did, and what you have been
avoiding. So this is built around remembering, not around prose.

Three inputs:

| | |
|---|---|
| **`profile.local.yaml`** | who you are, what you're building, what you committed to. Hand-edited. Gitignored. |
| **`journal.local.jsonl`** | append-only record of what has happened, so it can quote you |
| **wearable export** | sleep, resting HR, HRV, training load — normalised across devices |

Erebus is told, in the prompt, never to state a number it was not given. If
something is not recorded it says so and holds you responsible for not
measuring it, rather than inventing a figure.

---

## Setup

```powershell
copy profile.example.yaml profile.local.yaml
notepad profile.local.yaml
python -m erebus brief
```

The profile is the whole game. Everything in it is optional, but a thin profile
produces a thin briefing — and an empty one produces a briefing that says so
and tells you to go and fill it in.

Be honest in `known_weaknesses`. It is the sharpest input there is, because it
lets Erebus name the avoidance instead of guessing at it:

```yaml
known_weaknesses:
  - "I rewrite drafts instead of shipping them."
  - "I train hard for two weeks then miss a week."
  - "I do the work that feels productive instead of the work that sells."
```

`standards` get quoted back at you verbatim when you break one, so write them
in your own words.

---

## Wearable data

`source: none` is fine — the briefing just has less to work with.

### Any device that exports CSV

Whoop, Garmin, Oura, Fitbit and most others. Rather than a separate adapter per
vendor, you name the columns and the unit conversion in config:

```yaml
briefing:
  health:
    source: csv
    path: "C:/Users/you/Downloads/physiological_cycles.csv"
    columns:
      date:        "Cycle start time"
      sleep_hours: "Asleep duration (min)"
      resting_hr:  "Resting heart rate (bpm)"
      hrv:         "Heart rate variability (ms)"
      strain:      "Day Strain"
    scale:
      sleep_hours: 0.01666667      # minutes -> hours
```

Recognised fields: `sleep_hours`, `sleep_score`, `resting_hr`, `hrv`, `steps`,
`calories`, `weight_kg`, `readiness`, `strain`, `workouts`. Map only the ones
your export has.

### Apple Health

```yaml
briefing:
  health:
    source: apple
    path: "C:/Users/you/Documents/apple_health_export/export.xml"
```

That file is routinely hundreds of megabytes, so it is streamed rather than
loaded.

---

## What it works out for itself

Deltas, trends and the recovery verdict are computed in Python before the model
sees anything. Models are unreliable at arithmetic, and a briefing that gets
your resting heart rate backwards is worse than one that omits it. The model
receives conclusions:

```
WHAT THIS MEANS (already worked out - do not recompute):
  - UNDER-RECOVERED: resting heart rate is climbing while HRV falls.
    The required action is sleep and food, NOT more training.
  - SHORT SLEEP: averaging 5.7 hours over the last three nights.
  - TRAINING HAS COLLAPSED: 2 sessions in 7 days.
```

Under-recovery is detected as rising resting HR *together with* falling HRV —
either alone is noise. When it fires, the briefing lowers the training demand
instead of raising it.

---

## The register, and its limits

Cold, serious, contemptuous. No jokes, no encouragement, no praise. Approval is
at most the withholding of criticism.

Three boundaries are written into the prompt. They are not softeners — they are
what keeps it usable in a year rather than something you mute in a week:

- **It attacks your output, your consistency and your excuses.** Never your
  body, your appearance, your intelligence, or your worth.
- **It never invents a figure.** Missing data is reported as missing.
- **It never tells you to train through pain, skip sleep, or eat less.**
  Under-recovery is treated as a performance failure to fix, not to override.

An assistant that fabricates your numbers, or talks you into training on an
injury, isn't hard. It's just unreliable.

To retune the voice, edit `PERSONA` in `erebus/briefing/compose.py`. The worked
example near the bottom does most of the work — the model matches its rhythm
and density, so changing that example changes the output more than changing the
adjectives above it.

---

## Model size matters here

The briefing asks for more than command routing does: hold a dozen facts, avoid
contradicting yourself, and sustain a register. `llama3.1:8b` handles it.
A 3B model produces the right shape but contradicts itself and copies phrasing
out of the example. If the briefings read as repetitive, that is the first
thing to check.

---

## Testing it

```powershell
python -m erebus brief                    # print it
python -m erebus brief --out brief.wav    # and render the speech
python tests/test_briefing.py             # no model, no network
```

---

## The webcam, later

`build_prompt()` already takes an `observation` argument, and `brief` already
takes `--observe` to stand in for it:

```powershell
python -m erebus brief --observe "He is at the desk. It is 01:40."
```

When vision lands it passes a plain-language description through that seam and
nothing else changes. Worth being realistic about what a camera actually buys:
presence and timing are reliable and genuinely useful ("still at the desk at
two in the morning"), and that alone is a real input to a briefing. Form
checking and reading mood from a face are much harder and mostly gimmick.
