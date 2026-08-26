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
