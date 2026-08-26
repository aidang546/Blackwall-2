"""The briefing: profile, journal, wearable parsing, and the prompt it builds.

The prompt assertions matter more than they look. The first working version of
this told the model that a *target* ("publish 1 per week") was an achievement,
and it duly congratulated the operator for work he had not done. A briefing
that fabricates progress is worse than no briefing, so the labelling is pinned
here.

No model and no network. `python tests/test_briefing.py`
"""

from __future__ import annotations

import csv
import json
import pathlib
import sys
import tempfile
from datetime import date, datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from erebus.briefing import health as health_mod        # noqa: E402
from erebus.briefing.compose import PERSONA, build_prompt   # noqa: E402
from erebus.briefing.journal import Journal             # noqa: E402
from erebus.briefing.profile import Profile             # noqa: E402

FAILURES = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILURES
    FAILURES += not ok
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")


def write_yaml(tmp: pathlib.Path, text: str) -> pathlib.Path:
    path = tmp / "profile.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_profile(tmp: pathlib.Path) -> None:
    print("\nPROFILE")
    empty = Profile()
    check("an absent profile is not 'configured'", not empty.configured)

    path = write_yaml(tmp, """
name: Test
business: "Content-led."
commitments:
  publish: "3 per week"
metrics:
  subscribers: 4200
training:
  goal: "squat 110"
standards:
  - "I publish on schedule."
known_weaknesses:
  - "I rewrite instead of shipping."
""")
    profile = Profile.load(path)
    check("loads", profile.name == "Test" and profile.configured)

    rendered = profile.as_prompt()
    # The bug that caused fabricated progress.
    check("commitments are labelled TARGETS, not achievements",
          "TARGETS" in rendered and "PROMISED" in rendered)
    check("metrics are labelled as actuals", "RECORDED FACTS" in rendered)
    check("standards are carried through", "I publish on schedule." in rendered)
    check("weaknesses are carried through", "rewrite instead of shipping" in rendered)

    empty_render = Profile(name="x").as_prompt()
    check("empty sections are omitted, not padded",
          "TARGETS" not in empty_render and "Training" not in empty_render)

    bad = write_yaml(tmp, "name: Test\nnonsense_key: 1\n")
    check("an unknown key does not crash the load", Profile.load(bad).name == "Test")

    broken = write_yaml(tmp, "name: [unclosed\n")
    check("invalid YAML degrades to an empty profile",
          not Profile.load(broken).configured)


def test_journal(tmp: pathlib.Path) -> None:
    print("\nJOURNAL")
    path = tmp / "journal.jsonl"
    journal = Journal(path)

    check("no history is not an error", journal.days_since("briefing") is None)
    check("empty journal reads as first briefing",
          "first briefing" in journal.as_prompt())

    journal.append("briefing", words=140)
    journal.append("trained", session="legs")
    check("entries come back", len(list(journal.entries())) == 2)
    check("days_since is zero for today", journal.days_since("briefing") == 0)
    check("last() finds the right kind",
          journal.last("trained").data["session"] == "legs")

    # Backdated entries, written directly, to exercise streaks.
    with open(path, "a", encoding="utf-8") as fh:
        for days_ago in (1, 2, 3, 5):
            ts = datetime.now() - timedelta(days=days_ago)
            fh.write(json.dumps({"ts": ts.isoformat(), "kind": "published"}) + "\n")
    check("streak counts the consecutive run ending yesterday",
          journal.streak("published") == 3, f"{journal.streak('published')}")
    check("a gap ends the streak when nothing is tolerated",
          journal.streak("published", allow_gap=0) == 3)
    check("allow_gap bridges the missing day and picks up the one beyond it",
          journal.streak("published", allow_gap=1) == 4,
          f"{journal.streak('published', allow_gap=1)}")

    with open(path, "a", encoding="utf-8") as fh:
        fh.write("this is not json\n")
    check("one corrupt line does not lose the history",
          len(list(journal.entries())) == 6)

    check("history renders for the prompt", "published" in journal.as_prompt())


def test_health_csv(tmp: pathlib.Path) -> None:
    print("\nHEALTH (csv)")
    path = tmp / "whoop.csv"
    today = date.today()
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Cycle start time", "Asleep duration (min)",
                         "Resting heart rate (bpm)", "HRV", "Day Strain"])
        for ago, sleep, rhr, hrv, strain in [
            (5, 480, 50, 80, 14.0), (4, 470, 50, 78, 13.0),
            (3, 400, 55, 60, 5.0),  (2, 360, 58, 52, 4.0),
            (1, 340, 60, 45, 3.0),
        ]:
            writer.writerow([(today - timedelta(days=ago)).isoformat(),
                             sleep, rhr, hrv, strain])

    source = health_mod.build({
        "source": "csv", "path": str(path),
        "columns": {"date": "Cycle start time",
                    "sleep_hours": "Asleep duration (min)",
                    "resting_hr": "Resting heart rate (bpm)",
                    "hrv": "HRV", "strain": "Day Strain"},
        "scale": {"sleep_hours": 1 / 60},
    })
    snaps = source.snapshots(14)
    check("reads every row", len(snaps) == 5, f"{len(snaps)}")
    check("newest is last", snaps[-1].day > snaps[0].day)
    check("unit scaling is applied", abs(snaps[0].sleep_hours - 8.0) < 0.01,
          f"{snaps[0].sleep_hours:.2f}h from 480min")

    check("high strain counts as a training day", snaps[0].trained)
    check("low strain does not", not snaps[-1].trained)

    verdict = health_mod.interpret(snaps)
    check("spots under-recovery", "UNDER-RECOVERED" in verdict)
    check("spots short sleep", "SHORT SLEEP" in verdict)
    check("under-recovery forbids pushing harder",
          "NOT more training" in verdict)

    summary = health_mod.summarise(snaps)
    check("summary computes deltas rather than leaving them to the model",
          "average" in summary and "WHAT THIS MEANS" in summary)

    missing = health_mod.build({"source": "csv", "path": str(tmp / "nope.csv"),
                                "columns": {"date": "d"}})
    check("a missing export yields nothing rather than raising",
          missing.snapshots() == [])
    check("no data is stated, never invented",
          health_mod.summarise([]) == "No wearable data available.")

    check("an unknown source falls back to none",
          isinstance(health_mod.build({"source": "nonsense"}),
                     health_mod.NoHealthSource))
    check("a source missing its path falls back to none",
          isinstance(health_mod.build({"source": "csv"}),
                     health_mod.NoHealthSource))


def test_health_apple(tmp: pathlib.Path) -> None:
    print("\nHEALTH (apple)")
    path = tmp / "export.xml"
    today = date.today()
    rows = []
    for ago in range(1, 4):
        day = (today - timedelta(days=ago)).isoformat()
        rows.append(
            f'<Record type="HKQuantityTypeIdentifierRestingHeartRate" '
            f'startDate="{day} 08:00:00 +0000" value="55"/>'
        )
        rows.append(
            f'<Record type="HKQuantityTypeIdentifierStepCount" '
            f'startDate="{day} 09:00:00 +0000" value="3000"/>'
        )
        rows.append(
            f'<Record type="HKQuantityTypeIdentifierStepCount" '
            f'startDate="{day} 18:00:00 +0000" value="4000"/>'
        )
        rows.append(
            f'<Workout workoutActivityType="HKWorkoutActivityTypeTraditionalStrengthTraining" '
            f'startDate="{day} 17:00:00 +0000"/>'
        )
    path.write_text("<HealthData>" + "".join(rows) + "</HealthData>", encoding="utf-8")

    snaps = health_mod.build({"source": "apple", "path": str(path)}).snapshots(14)
    check("parses records", len(snaps) == 3, f"{len(snaps)}")
    check("single readings are taken as-is", snaps[-1].resting_hr == 55)
    check("cumulative measures are summed across the day",
          snaps[-1].steps == 7000, f"{snaps[-1].steps}")
    check("workouts make it a training day", snaps[-1].trained)


def test_health_push(tmp: pathlib.Path) -> None:
    print("\nHEALTH (push)")
    today = date.today()

    # The canonical shape.
    records = health_mod.normalise_pushed(
        {"days": [{"date": today.isoformat(), "sleep_hours": 6.4,
                   "resting_hr": 57, "hrv": 52, "workouts": ["strength"]}]}
    )
    check("accepts the documented envelope", len(records) == 1)
    check("keeps workouts as a list", records[0]["workouts"] == ["strength"])

    # What a real sender is more likely to produce.
    camel = health_mod.normalise_pushed(
        [{"startTime": f"{today}T00:00:00Z", "sleepDuration": 388,
          "restingHeartRate": 59, "heartRateVariability": 47,
          "stepCount": 9100, "deviceModel": "CMF Watch Pro 2"}]
    )
    check("accepts a bare list", len(camel) == 1)
    check("maps camelCase aliases", camel[0]["resting_hr"] == 59)
    check("converts sleep reported in minutes",
          abs(camel[0]["sleep_hours"] - 6.47) < 0.05,
          f"{camel[0]['sleep_hours']:.2f}h from 388min")
    check("drops fields it does not understand", "deviceModel" not in camel[0])

    single = health_mod.normalise_pushed({"day": str(today), "rhr": 61})
    check("accepts a single bare record", len(single) == 1)
    check("maps short aliases", single[0]["resting_hr"] == 61)

    # Sleep already in hours must not be mangled.
    hours = health_mod.normalise_pushed({"date": str(today), "sleep_hours": 7.5})
    check("leaves plausible hour values alone", hours[0]["sleep_hours"] == 7.5)

    check("a record with no date is refused",
          health_mod.normalise_pushed({"resting_hr": 60}) == [])
    check("non-JSON-object input yields nothing",
          health_mod.normalise_pushed("nonsense") == [])
    check("an empty list yields nothing", health_mod.normalise_pushed([]) == [])

    # Round trip through the store.
    store = tmp / "health.jsonl"
    health_mod.store_pushed(records, store)
    health_mod.store_pushed(camel, store)
    source = health_mod.PushedHealthSource(store)
    snaps = source.snapshots(14)
    check("stored records come back", len(snaps) == 1, f"{len(snaps)} day(s)")

    # Two pushes for the same day: later wins field by field, workouts union.
    merged = snaps[0]
    check("a later push overwrites an earlier field",
          merged.resting_hr == 59, f"rhr={merged.resting_hr}")
    check("fields only the first push had survive",
          merged.hrv == 47 and merged.workouts == ["strength"])
    check("a re-push is not a duplicate day", len(snaps) == 1)

    with open(store, "a", encoding="utf-8") as fh:
        fh.write("not json\n")
    check("a corrupt line does not lose the store",
          len(health_mod.PushedHealthSource(store).snapshots(14)) == 1)

    check("push source is selectable from config",
          isinstance(health_mod.build({"source": "push", "path": str(store)}),
                     health_mod.PushedHealthSource))
    check("an empty store is not an error",
          health_mod.PushedHealthSource(tmp / "nothing.jsonl").snapshots() == [])


def test_health_apple_push(tmp: pathlib.Path) -> None:
    """Health Auto Export's payload, in the shape its docs specify.

    Metric-major rather than day-major, with several metrics carrying their own
    field names, so it cannot go through the generic alias path.
    """
    print("\nHEALTH (Health Auto Export / iOS)")
    payload = {"data": {
        "metrics": [
            {"name": "resting_heart_rate", "units": "count/min", "data": [
                {"qty": 57, "date": "2026-08-25 08:00:00 +0000"},
                {"qty": 61, "date": "2026-08-26 08:00:00 +0000"}]},
            {"name": "heart_rate_variability", "units": "ms", "data": [
                {"qty": 52.4, "date": "2026-08-25 08:00:00 +0000"}]},
            {"name": "sleep_analysis", "units": "hr", "data": [
                {"totalSleep": 6.4, "deep": 1.1,
                 "date": "2026-08-25 06:30:00 +0000"}]},
            {"name": "step_count", "units": "count", "data": [
                {"qty": 4200, "date": "2026-08-26 12:00:00 +0000"},
                {"qty": 5100, "date": "2026-08-26 20:00:00 +0000"}]},
            {"name": "heart_rate", "units": "count/min", "data": [
                {"Min": 52, "Avg": 74, "Max": 161,
                 "date": "2026-08-26 12:00:00 +0000"}]},
            {"name": "vo2_max", "units": "ml/kg/min", "data": [
                {"qty": 44.1, "date": "2026-08-26 08:00:00 +0000"}]},
        ],
        "workouts": [
            {"id": "abc", "name": "Traditional Strength Training",
             "start": "2026-08-25 17:00:00 +0000", "duration": 3900},
        ],
    }}

    check("the shape is recognised", health_mod.looks_like_hae(payload))
    records = health_mod.normalise_pushed(payload)
    check("flattens to one record per day", len(records) == 2, f"{len(records)}")

    first, second = records
    check("metric-major is inverted to day-major",
          first["date"] == "2026-08-25" and first["resting_hr"] == 57)
    check("sleep is read from totalSleep", first["sleep_hours"] == 6.4)
    check("workouts land on the right day",
          first["workouts"] == ["Traditional Strength Training"]
          and "workouts" not in second)
    check("same-day samples are summed where that is correct",
          second["steps"] == 9300, f"steps={second.get('steps')}")
    check("readings are NOT summed where that would be wrong",
          second["resting_hr"] == 61, f"rhr={second.get('resting_hr')}")
    check("unmapped metrics are ignored",
          "vo2_max" not in second and "heart_rate" not in second)

    # Sleep in minutes, which the app emits depending on its settings.
    minutes = health_mod.normalise_pushed({"data": {"metrics": [
        {"name": "sleep_analysis", "units": "min", "data": [
            {"totalSleep": 384, "date": "2026-08-26 06:30:00 +0000"}]}]}})
    check("sleep in minutes is converted using the units field",
          abs(minutes[0]["sleep_hours"] - 6.4) < 0.01,
          f"{minutes[0]['sleep_hours']:.2f}h from 384min")

    # Exercise minutes are evidence of training when no workout was logged.
    exercise = health_mod.normalise_pushed({"data": {"metrics": [
        {"name": "apple_exercise_time", "units": "min", "data": [
            {"qty": 45, "date": "2026-08-26 18:00:00 +0000"}]}]}})
    check("exercise time counts as a session when nothing else does",
          exercise[0].get("workouts") == ["exercise"])
    check("the helper field does not leak into the record",
          "exercise_minutes" not in exercise[0])

    brief_sleep = health_mod.normalise_pushed({"data": {"metrics": [
        {"name": "apple_exercise_time", "units": "min", "data": [
            {"qty": 4, "date": "2026-08-26 18:00:00 +0000"}]}]}})
    check("a few incidental minutes do not count as a session",
          not brief_sleep[0].get("workouts"))

    check("an empty export is not an error",
          health_mod.normalise_pushed({"data": {"metrics": []}}) == [])
    check("undated samples are dropped, not guessed at",
          health_mod.normalise_pushed({"data": {"metrics": [
              {"name": "step_count", "data": [{"qty": 100}]}]}}) == [])

    # Round trip through the store, as the endpoint would.
    store = tmp / "hae.jsonl"
    health_mod.store_pushed(records, store)
    snaps = health_mod.PushedHealthSource(store).snapshots(3650)
    check("stored and read back", len(snaps) == 2)
    check("the workout day is a training day", snaps[0].trained)
    check("the untrained day is not", not snaps[1].trained)


def test_prompt(tmp: pathlib.Path) -> None:
    print("\nPROMPT & PERSONA")
    profile = Profile.load(write_yaml(tmp, """
name: Test
business: "Content-led."
commitments:
  publish: "1 per week"
metrics:
  videos_last_30_days: 2
"""))
    journal = Journal(tmp / "empty.jsonl")
    prompt = build_prompt(profile, journal, [])

    check("prompt states the data is absent rather than omitting it",
          "No wearable data available." in prompt)
    check("prompt marks a first briefing", "never requested" in prompt)
    check("prompt forbids inventing missing facts",
          "not recorded" in prompt.lower())
    check("targets and actuals are distinguished in the prompt",
          "TARGETS" in prompt and "RECORDED FACTS" in prompt)

    seen = build_prompt(profile, journal, [], observation="He is not at the desk.")
    check("the vision seam injects cleanly", "not at the desk" in seen)

    # The three guarantees the persona is responsible for.
    check("persona forbids inventing numbers", "NEVER invent a number" in PERSONA)
    check("persona keeps the attack on output, not the person",
          "NEVER his body" in PERSONA)
    check("persona forbids training through pain",
          "train through pain" in PERSONA)
    check("persona forbids markdown, since it is spoken",
          "NO headings" in PERSONA)


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        tmp = pathlib.Path(raw)
        test_profile(tmp)
        test_journal(tmp)
        test_health_csv(tmp)
        test_health_apple(tmp)
        test_health_push(tmp)
        test_health_apple_push(tmp)
        test_prompt(tmp)
    print(f"\n  {'all checks passed' if not FAILURES else f'{FAILURES} failed'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
