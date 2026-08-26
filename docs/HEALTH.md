# Getting health data into Erebus

## Where it goes: nowhere

Worth stating before anything else, because "share my health data" usually
means handing it to someone.

Your data is read from a local file, parsed on your machine, and summarised
into a prompt sent to `127.0.0.1:11434` — Ollama, on your own PC. The reply is
spoken by Piper, locally. There are no outbound endpoints in the runtime path.
Nothing is uploaded, nothing is stored off-machine, and no account is involved.

The journal records *that* a briefing happened and whether health data was
present. It does not store your numbers.

Two things to know:

- **`brain.host` is the one line that would change this.** It is `127.0.0.1`
  and there is no voice command to alter it. Point it at a cloud endpoint and
  your sleep, HRV and resting heart rate travel with every briefing.
- **`health.local.jsonl`, `profile.local.yaml` and `journal.local.jsonl` are
  gitignored**, as are the common export filenames (`export.xml`,
  `*cycles*.csv`, `*.fit`, and others) in case one lands in the repo folder.

---

## Three ways in

| | when to use it |
|---|---|
| **`source: push`** | the phone POSTs to Erebus. **Best route for iPhone and for Nothing/CMF.** |
| **`source: csv`** | you export a CSV periodically (Whoop, Garmin, Oura, Fitbit) |
| **`source: apple`** | a one-off Apple Health `export.xml` dump |

---

## iPhone / Apple Watch

Apple Health has no cloud API, and its built-in export is a manual dump of a
several-hundred-megabyte XML file. That is fine once — `source: apple` reads it
— but useless as a daily feed.

The working route is an iOS app that reads HealthKit and POSTs on a schedule.
**[Health Auto Export][hae-app]** does exactly this: it has a REST API
automation that sends JSON to any URL with configurable headers, which is
precisely what `/api/health` accepts. The REST feature is a paid tier.

### Setup

On the PC:

```yaml
# config.local.yaml
server:
  host: 0.0.0.0
briefing:
  health:
    source: push
```

`python -m erebus pair` prints your token.

In Health Auto Export → Automations → new automation:

| | |
|---|---|
| Type | REST API |
| URL | `http://192.168.1.20:8848/api/health` — your PC's LAN address |
| Format | JSON |
| Header | `X-Erebus-Token: <your token>` |
| Schedule | daily, or hourly if you want the briefing current |
| Metrics | resting heart rate, HRV, sleep analysis, step count, active energy, body mass, exercise time, workouts |

Only those metrics are read. The app exports 150+ and the rest are ignored —
select fewer and the payload stays small.

### What Erebus does with it

Its payload is **metric-major** (a list of metrics, each with samples) rather
than one record per day, and several metrics have their own shape — sleep uses
`totalSleep`, heart rate uses `Min`/`Avg`/`Max`. So it gets its own adapter
rather than the generic alias path:

- Metrics are inverted into one record per day.
- Steps and calories are **summed** across a day; resting heart rate and HRV
  take the reading, because adding two resting heart rates together would be
  nonsense.
- Sleep is read from `totalSleep`, converted from minutes when the `units`
  field says so.
- Workouts attach to the day they started.
- 20+ minutes of Apple exercise time counts as a session when no workout was
  logged; a few incidental minutes do not.

### Does your Nothing watch reach Apple Health?

Only if the CMF app writes to HealthKit. Health Connect syncing is
Android-only, so on iOS the watch data may stop at the CMF app. Check whether
Health → Sharing → Apps lists it. If it does not, your iPhone still supplies
steps and workouts on its own, and the watch's sleep and HRV would need the
Android route instead.

---

## Push — the endpoint itself

Nothing's CMF watches, and anything else that only reaches **Android Health
Connect**, have no public cloud API to pull from. So the phone pushes instead,
which suits the architecture anyway: Erebus already runs a token-authed server
your phone can reach.

```
  watch ──► Nothing X app ──► Health Connect ──► pusher ──► POST /api/health
                                                              (your PC, LAN)
```

### Turn it on

```yaml
# config.local.yaml
server:
  host: 0.0.0.0          # so the phone can reach it
briefing:
  health:
    source: push
```

`python -m erebus pair` prints your token.

### The endpoint

`POST /api/health`, token in `?token=` or an `X-Erebus-Token:` header. Same
auth as everything else: loopback is trusted, anything else needs the token
**and** a private-network address.

It is deliberately tolerant, because the sender might be Tasker, a third-party
exporter, or curl, and none of them agree on field names. A bare record, a
list, or an envelope all work:

```bash
curl -X POST "http://192.168.1.20:8848/api/health?token=YOUR_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"date":"2026-08-26","sleep_hours":6.4,"resting_hr":57,"hrv":52,"strain":11.2}'
```

Aliases are matched after stripping case and separators, so `restingHeartRate`,
`resting_heart_rate` and `Resting Heart Rate (bpm)` all land on the same field.
Recognised: `date`, `sleep_hours`, `sleep_score`, `resting_hr`, `hrv`, `steps`,
`calories`, `weight_kg`, `readiness`, `strain`, `workouts`. Anything else is
dropped rather than rejected.

Sleep reported as a number above 20 is taken as minutes and converted — no
one sleeps twenty hours, and every second exporter reports minutes.

Only `date` is required. Re-sending a day is expected: a later push overwrites
field by field, so a morning push followed by an evening one carrying the
workout merges correctly rather than duplicating the day.

### Getting Android Health Connect to send it

Three options, easiest first:

**1. Health Connect's own scheduled export** (Android 14+). Health Connect can
export daily/weekly/monthly as a zip to a cloud folder. Sync that folder to the
PC (Syncthing, Drive) and point `source: csv` at the file. No extra software,
but it is a scheduled export rather than a live push.

**2. Tasker + the Health Connect plugin.** [TaskerHealthConnect][thc] reads
Health Connect as JSON; Tasker's HTTP Request action POSTs it. A daily profile
at, say, 07:00 gives you a genuine automated push. Most control, needs Tasker.

**3. A purpose-built exporter.** [HealthConnectExports][hce] reads Health
Connect and pings an HTTP server with the data. Closest to plug-and-play; its
payload shape is undocumented, which is exactly why the endpoint above accepts
almost anything.

Note that the Health Connect API only supports on-demand reads — it cannot
notify anything when new data arrives — so every route here is a scheduled
pull on the phone's side, not a live stream. Erebus warns when the most recent
record is stale rather than reasoning from old numbers.

---

## CSV — Whoop, Garmin, Oura, Fitbit

You name the columns and the unit conversion; no per-vendor adapter needed.

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

---

## Apple Health — the one-off dump

For backfilling history, not for daily use. Health app → your photo → Export
All Health Data.

```yaml
briefing:
  health:
    source: apple
    path: "C:/.../apple_health_export/export.xml"
```

The file is routinely hundreds of megabytes, so it is streamed rather than
loaded. For an ongoing feed use the push route above instead.

---

## What Erebus does with it

Deltas, trends and the recovery verdict are computed in Python before the model
sees anything, because models are unreliable at arithmetic and a briefing that
reports your resting heart rate backwards is worse than one that omits it:

```
WHAT THIS MEANS (already worked out - do not recompute):
  - UNDER-RECOVERED: resting heart rate is climbing while HRV falls.
    The required action is sleep and food, NOT more training.
  - TRAINING HAS COLLAPSED: 2 sessions in 7 days.
```

Under-recovery is rising resting HR **together with** falling HRV — either
alone is noise. When it fires, the briefing lowers the training demand rather
than raising it. Erebus is instructed never to counsel training through pain,
skipping rest, or eating less.

[hae-app]: https://apps.apple.com/app/health-auto-export-json-csv/id1115567069
[hae-docs]: https://github.com/Lybron/health-auto-export/wiki/API-Export---JSON-Format
[thc]: https://github.com/RafhaanShah/TaskerHealthConnect
[hce]: https://github.com/angeloanan/HealthConnectExports
