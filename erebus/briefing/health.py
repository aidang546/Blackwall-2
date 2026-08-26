"""Wearable data, normalised.

Every device exports something different, and none of them agree on units, so
this reduces all of them to one `HealthSnapshot` per day and lets the briefing
reason about that instead of about vendors.

Two adapters cover almost everything:

    csv     Whoop, Garmin, Oura, Fitbit and most others export CSV. Rather
            than writing one adapter per vendor, the column names and unit
            conversions are declared in config - see `columns` and `scale`.
    apple   Apple Health exports a single very large XML file, which needs its
            own streaming parser.

Missing data is normal and is never faked. A field that is None is reported as
unknown; the briefing is told to say so rather than to invent a number, because
a briefing that invents your resting heart rate is worse than one that admits
it does not know it.
"""

from __future__ import annotations

import csv as csv_mod
import json
import logging
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("erebus.briefing.health")


@dataclass
class HealthSnapshot:
    """One day. All measures optional; None means "not reported"."""
    day: date
    sleep_hours: float | None = None
    sleep_score: float | None = None
    resting_hr: float | None = None
    hrv: float | None = None
    steps: float | None = None
    calories: float | None = None
    weight_kg: float | None = None
    readiness: float | None = None
    strain: float | None = None
    workouts: list[str] = field(default_factory=list)

    @property
    def trained(self) -> bool:
        """Whether this counts as a training day.

        Strain and workout entries are direct evidence. Steps are not - a busy
        day on your feet is not a session, and letting it count would make the
        streak a lie.
        """
        if self.workouts:
            return True
        return self.strain is not None and self.strain >= TRAINING_STRAIN

    def known(self) -> dict[str, Any]:
        out = {}
        for f in fields(self):
            if f.name in ("day", "workouts"):
                continue
            value = getattr(self, f.name)
            if value is not None:
                out[f.name] = value
        if self.workouts:
            out["workouts"] = ", ".join(self.workouts)
        return out


#: Above this, a day counts as trained even with no named workout. Whoop-style
#: 0-21 strain; override in config if your device scales differently.
TRAINING_STRAIN = 8.0


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"na", "n/a", "null", "-", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


class HealthSource:
    """Base: returns the most recent days, newest last."""

    def snapshots(self, days: int = 14) -> list[HealthSnapshot]:
        raise NotImplementedError

    @property
    def describe(self) -> str:
        return type(self).__name__


class NoHealthSource(HealthSource):
    """No device configured. The briefing simply has less to work with."""

    def snapshots(self, days: int = 14) -> list[HealthSnapshot]:
        return []

    @property
    def describe(self) -> str:
        return "none"


class CsvHealthSource(HealthSource):
    """A CSV export, with columns and units declared in config.

    Example (Whoop):

        health:
          source: csv
          path: "C:/Users/you/Downloads/physiological_cycles.csv"
          columns:
            date: "Cycle start time"
            sleep_hours: "Asleep duration (min)"
            resting_hr: "Resting heart rate (bpm)"
            hrv: "Heart rate variability (ms)"
            strain: "Day Strain"
          scale:
            sleep_hours: 0.01666667      # minutes -> hours
    """

    def __init__(self, path: str, columns: dict, scale: dict | None = None,
                 date_format: str | None = None) -> None:
        self.path = Path(path)
        self.columns = columns or {}
        self.scale = scale or {}
        self.date_format = date_format

    @property
    def describe(self) -> str:
        return f"csv:{self.path.name}"

    def _parse_date(self, text: str) -> date | None:
        text = (text or "").strip()
        if not text:
            return None
        if self.date_format:
            try:
                return datetime.strptime(text, self.date_format).date()
            except ValueError:
                return None
        # Exports vary; try ISO first, then the common ambiguous forms. Day-first
        # before month-first, because these are UK/EU devices more often than not.
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S",
                    "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        return None

    def snapshots(self, days: int = 14) -> list[HealthSnapshot]:
        if not self.path.exists():
            log.warning("health export not found: %s", self.path)
            return []

        date_column = self.columns.get("date")
        if not date_column:
            log.error("health.columns.date is not set - cannot read the export")
            return []

        cutoff = date.today() - timedelta(days=days)
        by_day: dict[date, HealthSnapshot] = {}

        try:
            with open(self.path, "r", encoding="utf-8-sig", newline="") as fh:
                for row in csv_mod.DictReader(fh):
                    day = self._parse_date(row.get(date_column, ""))
                    if day is None or day < cutoff:
                        continue
                    snap = by_day.setdefault(day, HealthSnapshot(day=day))
                    for name, column in self.columns.items():
                        if name == "date":
                            continue
                        if name == "workouts":
                            value = (row.get(column) or "").strip()
                            if value:
                                snap.workouts.append(value)
                            continue
                        number = _to_float(row.get(column))
                        if number is None:
                            continue
                        number *= float(self.scale.get(name, 1.0))
                        if hasattr(snap, name):
                            setattr(snap, name, number)
                        else:
                            log.warning("health.columns: unknown field %r", name)
        except OSError as exc:
            log.error("could not read %s: %s", self.path, exc)
            return []

        return [by_day[d] for d in sorted(by_day)]


class AppleHealthSource(HealthSource):
    """Apple Health's export.xml.

    The file is routinely hundreds of megabytes, so it is streamed with
    iterparse and each element dropped once read. Loading it whole would use
    more memory than the rest of the assistant combined.
    """

    TYPES = {
        "HKCategoryTypeIdentifierSleepAnalysis": "sleep_hours",
        "HKQuantityTypeIdentifierRestingHeartRate": "resting_hr",
        "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "hrv",
        "HKQuantityTypeIdentifierStepCount": "steps",
        "HKQuantityTypeIdentifierActiveEnergyBurned": "calories",
        "HKQuantityTypeIdentifierBodyMass": "weight_kg",
    }
    #: Fields that accumulate across a day rather than being a single reading.
    SUMMED = {"steps", "calories", "sleep_hours"}

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    @property
    def describe(self) -> str:
        return f"apple:{self.path.name}"

    def snapshots(self, days: int = 14) -> list[HealthSnapshot]:
        if not self.path.exists():
            log.warning("Apple Health export not found: %s", self.path)
            return []

        import xml.etree.ElementTree as ET

        cutoff = date.today() - timedelta(days=days)
        by_day: dict[date, HealthSnapshot] = {}

        def day_of(text: str) -> date | None:
            try:
                return datetime.strptime(text[:10], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                return None

        try:
            for _event, element in ET.iterparse(self.path, events=("end",)):
                tag = element.tag
                if tag == "Record":
                    field_name = self.TYPES.get(element.get("type", ""))
                    day = day_of(element.get("startDate", ""))
                    if field_name and day and day >= cutoff:
                        snap = by_day.setdefault(day, HealthSnapshot(day=day))
                        if field_name == "sleep_hours":
                            start = element.get("startDate", "")
                            end = element.get("endDate", "")
                            hours = _apple_duration_hours(start, end)
                            if hours:
                                snap.sleep_hours = (snap.sleep_hours or 0) + hours
                        else:
                            value = _to_float(element.get("value"))
                            if value is not None:
                                if field_name in self.SUMMED:
                                    prior = getattr(snap, field_name) or 0
                                    setattr(snap, field_name, prior + value)
                                else:
                                    setattr(snap, field_name, value)
                elif tag == "Workout":
                    day = day_of(element.get("startDate", ""))
                    if day and day >= cutoff:
                        snap = by_day.setdefault(day, HealthSnapshot(day=day))
                        name = (element.get("workoutActivityType", "")
                                .replace("HKWorkoutActivityType", ""))
                        if name:
                            snap.workouts.append(name)
                else:
                    continue
                element.clear()
        except (OSError, ET.ParseError) as exc:
            log.error("could not parse %s: %s", self.path, exc)
            return []

        return [by_day[d] for d in sorted(by_day)]


def _apple_duration_hours(start: str, end: str) -> float | None:
    fmt = "%Y-%m-%d %H:%M:%S %z"
    try:
        return (datetime.strptime(end, fmt) - datetime.strptime(start, fmt)).seconds / 3600
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
#  Pushed data
#
#  Some devices have no cloud API to pull from - Nothing's CMF watches among
#  them - so the phone pushes instead. Erebus already runs a token-authed
#  server the phone can reach, which makes push the natural direction here
#  rather than a workaround.
#
#  The sender may be a Tasker task, a purpose-built exporter, or a curl
#  one-liner. None of them agree on field names, and at least one is
#  undocumented, so the parser is deliberately tolerant: it accepts a bare
#  record, a list, or an envelope, and maps a wide set of aliases onto the
#  canonical fields. Being strict here would mean rejecting real data over a
#  spelling.
# ---------------------------------------------------------------------------

STORE_PATH = Path(__file__).resolve().parents[2] / "health.local.jsonl"

#: Alias -> canonical field. Compared after lowercasing and stripping
#: separators, so "restingHeartRate", "resting_heart_rate" and "Resting Heart
#: Rate (bpm)" all collapse to the same key.
ALIASES = {
    "date": "date", "day": "date", "starttime": "date", "startdate": "date",
    "timestamp": "date", "cyclestarttime": "date", "recordeddate": "date",

    "sleephours": "sleep_hours", "sleep": "sleep_hours",
    "sleepduration": "sleep_hours", "totalsleep": "sleep_hours",
    "asleepduration": "sleep_hours", "sleeptime": "sleep_hours",

    "sleepscore": "sleep_score", "sleepquality": "sleep_score",

    "restingheartrate": "resting_hr", "restinghr": "resting_hr",
    "rhr": "resting_hr", "restingheartratebpm": "resting_hr",

    "hrv": "hrv", "heartratevariability": "hrv", "hrvmillis": "hrv",
    "heartratevariabilitysdnn": "hrv", "sdnn": "hrv",

    "steps": "steps", "stepcount": "steps", "totalsteps": "steps",

    "calories": "calories", "activecalories": "calories",
    "activeenergyburned": "calories", "caloriesburned": "calories",

    "weight": "weight_kg", "weightkg": "weight_kg", "bodymass": "weight_kg",

    "readiness": "readiness", "recovery": "readiness",
    "readinessscore": "readiness", "recoveryscore": "readiness",

    "strain": "strain", "daystrain": "strain", "load": "strain",
    "trainingload": "strain", "exertion": "strain",

    "workouts": "workouts", "workout": "workouts", "activity": "workouts",
    "activities": "workouts", "exercisetype": "workouts",
}

#: Values arriving in units we can convert without being told.
_SLEEP_MINUTES_ABOVE = 20.0     # nobody sleeps 20 hours; that many is minutes


def _canonical(key: str) -> str | None:
    flat = "".join(ch for ch in str(key).lower() if ch.isalnum())
    return ALIASES.get(flat)


# ---------------------------------------------------------------------------
#  Health Auto Export (iOS)
#
#  Apple Health has no cloud API, and its built-in export is a manual dump of a
#  several-hundred-megabyte XML file - fine once, useless as a daily feed. The
#  practical route is an iOS app that reads HealthKit and POSTs on a schedule.
#
#  Its payload is metric-major rather than day-major, and several metrics have
#  their own shape, so it cannot go through the generic alias path. This
#  flattens it into the same day records everything else produces.
# ---------------------------------------------------------------------------

#: Health Auto Export metric name -> canonical field. Names not listed are
#: ignored; the app exports 150+ metrics and almost none of them belong in a
#: briefing.
HAE_METRICS = {
    "resting_heart_rate": "resting_hr",
    "heart_rate_variability": "hrv",
    "sleep_analysis": "sleep_hours",
    "step_count": "steps",
    "active_energy": "calories",
    "weight_body_mass": "weight_kg",
    "apple_exercise_time": "exercise_minutes",
}

#: Fields that accumulate over a day. Everything else takes the latest reading -
#: two resting heart rates on one day should not be added together.
HAE_SUMMED = {"steps", "calories", "sleep_hours", "exercise_minutes"}


def _hae_date(text: str) -> str | None:
    """Health Auto Export stamps are 'yyyy-MM-dd HH:mm:ss Z'. We want the day."""
    if not text:
        return None
    day = str(text)[:10]
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return None
    return day


def _hae_value(sample: dict, field_name: str) -> float | None:
    """Pull the number out of one sample, allowing for the per-metric shapes."""
    if field_name == "sleep_hours":
        # Aggregated sleep reports totalSleep; asleep is the fallback when the
        # app is configured for unaggregated output.
        for key in ("totalSleep", "asleep"):
            value = _to_float(sample.get(key))
            if value is not None:
                return value
        return None
    if "qty" in sample:
        return _to_float(sample["qty"])
    # Heart-rate style samples carry Min/Avg/Max instead of qty.
    for key in ("Avg", "avg"):
        if key in sample:
            return _to_float(sample[key])
    return None


def looks_like_hae(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("data"), dict)
        and ("metrics" in payload["data"] or "workouts" in payload["data"])
    )


def flatten_hae(payload: dict) -> list[dict]:
    """Metric-major Health Auto Export JSON -> one record per day."""
    data = payload.get("data") or {}
    by_day: dict[str, dict] = {}

    for metric in data.get("metrics") or []:
        if not isinstance(metric, dict):
            continue
        field_name = HAE_METRICS.get(str(metric.get("name", "")).lower())
        if field_name is None:
            continue
        units = str(metric.get("units", "")).lower()
        for sample in metric.get("data") or []:
            if not isinstance(sample, dict):
                continue
            day = _hae_date(sample.get("date") or sample.get("sleepEnd")
                            or sample.get("startDate", ""))
            value = _hae_value(sample, field_name)
            if day is None or value is None:
                continue
            # Sleep is reported in hours or minutes depending on the app's
            # settings; the units string says which.
            if field_name == "sleep_hours" and units.startswith("min"):
                value /= 60.0
            record = by_day.setdefault(day, {"date": day})
            if field_name in HAE_SUMMED:
                record[field_name] = record.get(field_name, 0.0) + value
            else:
                record[field_name] = value

    for workout in data.get("workouts") or []:
        if not isinstance(workout, dict):
            continue
        day = _hae_date(workout.get("start") or workout.get("date", ""))
        name = str(workout.get("name") or "").strip()
        if not day or not name:
            continue
        record = by_day.setdefault(day, {"date": day})
        record.setdefault("workouts", [])
        if name not in record["workouts"]:
            record["workouts"].append(name)

    # exercise_minutes is not a HealthSnapshot field; it is only useful as
    # evidence that a day was trained, so fold it in and drop it.
    for record in by_day.values():
        minutes = record.pop("exercise_minutes", None)
        if minutes and minutes >= 20 and not record.get("workouts"):
            record.setdefault("workouts", []).append("exercise")

    return [by_day[d] for d in sorted(by_day)]


def normalise_pushed(payload: Any) -> list[dict]:
    """Turn whatever arrived into a list of canonical day records.

    Accepts a single record, a bare list, or an envelope under any of the
    common wrapper keys. Unknown fields are dropped rather than rejected - a
    sender that includes its own metadata should not fail the whole upload.
    """
    # Health Auto Export is metric-major and needs its own flattening before
    # the generic alias path can do anything with it.
    if looks_like_hae(payload):
        return flatten_hae(payload)

    if isinstance(payload, dict):
        for wrapper in ("days", "data", "records", "entries", "results", "items"):
            if isinstance(payload.get(wrapper), list):
                payload = payload[wrapper]
                break
        else:
            payload = [payload]
    if not isinstance(payload, list):
        return []

    out: list[dict] = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        record: dict = {}
        for key, value in raw.items():
            field_name = _canonical(key)
            if field_name is None or value is None:
                continue
            if field_name == "date":
                record["date"] = str(value)[:10]
            elif field_name == "workouts":
                if isinstance(value, list):
                    record.setdefault("workouts", []).extend(str(v) for v in value)
                elif str(value).strip():
                    record.setdefault("workouts", []).append(str(value).strip())
            else:
                number = _to_float(value)
                if number is not None:
                    record[field_name] = number

        if "date" not in record:
            continue    # undateable data cannot be reasoned about

        # Sleep is the one field senders routinely report in minutes. A value
        # that could only be minutes is converted; anything ambiguous is left
        # alone rather than silently mangled.
        sleep = record.get("sleep_hours")
        if sleep is not None and sleep > _SLEEP_MINUTES_ABOVE:
            record["sleep_hours"] = sleep / 60.0

        out.append(record)
    return out


def store_pushed(records: list[dict], path: Path | None = None) -> int:
    """Append records to the local store. Returns how many were written."""
    path = path or STORE_PATH
    if not records:
        return 0
    with open(path, "a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    log.info("stored %d pushed health record(s)", len(records))
    return len(records)


class PushedHealthSource(HealthSource):
    """Days pushed from the phone, newest wins.

    The store is append-only, so re-sending a day is expected rather than an
    error - a later record for the same date replaces the earlier one field by
    field, which is what you want when a morning push is followed by an evening
    one carrying the day's workout.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else STORE_PATH

    @property
    def describe(self) -> str:
        return f"push:{self.path.name}"

    def snapshots(self, days: int = 14) -> list[HealthSnapshot]:
        if not self.path.exists():
            return []
        cutoff = date.today() - timedelta(days=days)
        merged: dict[date, dict] = {}

        with open(self.path, "r", encoding="utf-8") as fh:
            for number, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    day = datetime.strptime(record["date"][:10], "%Y-%m-%d").date()
                except (json.JSONDecodeError, KeyError, ValueError):
                    log.warning("health store line %d unreadable, skipping", number)
                    continue
                if day < cutoff:
                    continue
                existing = merged.setdefault(day, {})
                for key, value in record.items():
                    if key == "date":
                        continue
                    if key == "workouts":
                        existing.setdefault("workouts", [])
                        for w in value:
                            if w not in existing["workouts"]:
                                existing["workouts"].append(w)
                    else:
                        existing[key] = value

        snapshots = []
        for day in sorted(merged):
            snap = HealthSnapshot(day=day)
            for key, value in merged[day].items():
                if hasattr(snap, key):
                    setattr(snap, key, value)
            snapshots.append(snap)
        return snapshots

    def latest_day(self) -> date | None:
        snaps = self.snapshots(3650)
        return snaps[-1].day if snaps else None


def build(config: dict) -> HealthSource:
    """Construct the configured source. Never raises - falls back to none."""
    source = (config or {}).get("source", "none")
    try:
        if source == "csv":
            return CsvHealthSource(
                path=config["path"],
                columns=config.get("columns", {}),
                scale=config.get("scale", {}),
                date_format=config.get("date_format"),
            )
        if source in ("apple", "apple_health"):
            return AppleHealthSource(path=config["path"])
        if source == "push":
            return PushedHealthSource(config.get("path"))
    except KeyError as exc:
        log.error("health source %r is missing %s - disabling it", source, exc)
    except Exception as exc:  # noqa: BLE001
        log.error("could not build health source %r: %s", source, exc)
    return NoHealthSource()


def summarise(snapshots: list[HealthSnapshot]) -> str:
    """Render for the LLM, with the deltas already worked out.

    The comparison is done here rather than left to the model, because models
    are unreliable at arithmetic and a briefing that gets your numbers backwards
    is worse than one that omits them.
    """
    if not snapshots:
        return "No wearable data available."

    latest = snapshots[-1]
    lines = [f"Most recent day on record: {latest.day:%a %d %b}"]
    for key, value in latest.known().items():
        lines.append(f"  {key}: {value if isinstance(value, str) else round(value, 1)}")

    window = snapshots[-7:]
    trained = [s for s in window if s.trained]
    lines.append(f"  training days in the last {len(window)} recorded: {len(trained)}")

    for measure in ("sleep_hours", "resting_hr", "hrv", "strain"):
        values = [getattr(s, measure) for s in window if getattr(s, measure) is not None]
        if len(values) < 3:
            continue
        average = sum(values) / len(values)
        current = getattr(latest, measure)
        if current is None:
            lines.append(f"  {measure}: {len(values)}-day average {average:.1f}")
            continue
        delta = current - average
        direction = "above" if delta > 0 else "below"
        lines.append(
            f"  {measure}: {current:.1f} vs {average:.1f} average "
            f"({abs(delta):.1f} {direction})"
        )

    verdict = interpret(snapshots)
    if verdict:
        lines.append("")
        lines.append(verdict)

    return "\n".join(lines)


def interpret(snapshots: list[HealthSnapshot]) -> str:
    """State plainly what the numbers mean, worked out here rather than by the model.

    Small models list measurements instead of drawing the conclusion, and a
    briefing that reads out four figures without saying "you are under-recovered
    and you have stopped training" has done nothing. Recovery and training load
    are also the one place where getting the reading backwards matters: telling
    someone to push harder while their resting heart rate climbs and their HRV
    falls is how people get hurt. So it is computed, not inferred.
    """
    window = snapshots[-7:]
    if len(window) < 4:
        return ""

    def trend(measure: str) -> float | None:
        """Recent half minus earlier half. Positive means rising."""
        values = [getattr(s, measure) for s in window if getattr(s, measure) is not None]
        if len(values) < 4:
            return None
        half = len(values) // 2
        earlier = sum(values[:half]) / half
        recent = sum(values[half:]) / (len(values) - half)
        return recent - earlier

    findings: list[str] = []

    rhr_trend = trend("resting_hr")
    hrv_trend = trend("hrv")
    sleep_recent = [s.sleep_hours for s in window[-3:] if s.sleep_hours is not None]

    # Rising resting heart rate together with falling HRV is the classic
    # under-recovery signature. Either alone is noise.
    under_recovered = (
        rhr_trend is not None and hrv_trend is not None
        and rhr_trend > 1.0 and hrv_trend < -3.0
    )
    if under_recovered:
        findings.append(
            "UNDER-RECOVERED: resting heart rate is climbing while HRV falls. "
            "This is a recovery failure, not a motivation failure. The required "
            "action is sleep and food, NOT more training. Do not tell him to "
            "push harder through this."
        )
    if sleep_recent and sum(sleep_recent) / len(sleep_recent) < 6.5:
        findings.append(
            f"SHORT SLEEP: averaging {sum(sleep_recent)/len(sleep_recent):.1f} "
            "hours over the last three nights. This is the constraint on "
            "everything else he is failing at."
        )

    trained = sum(1 for s in window if s.trained)
    if trained == 0:
        findings.append("HAS NOT TRAINED AT ALL in the recorded window.")
    elif trained <= 2 and len(window) >= 6:
        findings.append(
            f"TRAINING HAS COLLAPSED: {trained} sessions in {len(window)} days."
        )

    strain_trend = trend("strain")
    if strain_trend is not None and strain_trend < -2.0 and not under_recovered:
        findings.append("Training load is falling week on week. He is coasting.")

    if not findings:
        findings.append(
            "Nothing in the recovery data excuses anything. He is fine and "
            "whatever he did not do, he chose not to do."
        )

    return "WHAT THIS MEANS (already worked out - do not recompute):\n" + "\n".join(
        f"  - {f}" for f in findings
    )
