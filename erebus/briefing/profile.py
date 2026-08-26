"""The operator's own facts.

Lives in `profile.local.yaml`, which is gitignored - it holds your revenue,
your lifts and your goals, and none of that belongs in a repository. See
`profile.example.yaml` for the shape.

Everything here is optional. A profile with nothing but a name still produces a
briefing; it just produces a vaguer one, and the briefing will say so rather
than inventing detail it does not have.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("erebus.briefing.profile")

ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = ROOT / "profile.local.yaml"
EXAMPLE_PATH = ROOT / "profile.example.yaml"


@dataclass
class Profile:
    name: str = "operator"
    #: Free text: what the business is and how it makes money.
    business: str = ""
    #: Named targets with a cadence, e.g. {"publish": "3 per week"}.
    commitments: dict[str, str] = field(default_factory=dict)
    #: The numbers that matter, and where they stand.
    metrics: dict[str, Any] = field(default_factory=dict)
    #: Training split, current lifts, targets.
    training: dict[str, Any] = field(default_factory=dict)
    #: Non-negotiables. Quoted back at you verbatim when you miss one.
    standards: list[str] = field(default_factory=list)
    #: Things you have already admitted you avoid. The briefing uses these.
    known_weaknesses: list[str] = field(default_factory=list)
    #: Anything else worth knowing.
    notes: str = ""

    @property
    def configured(self) -> bool:
        """False when this is an empty stub, which the briefing should admit."""
        return bool(self.business or self.commitments or self.training)

    @classmethod
    def load(cls, path: Path | None = None) -> "Profile":
        path = path or PROFILE_PATH
        if not path.exists():
            log.warning(
                "no profile at %s - briefings will be generic. "
                "Copy profile.example.yaml to profile.local.yaml and fill it in.",
                path.name,
            )
            return cls()
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            log.error("profile is not valid YAML (%s) - ignoring it", exc)
            return cls()

        known = {f.name for f in cls.__dataclass_fields__.values()}
        unknown = set(data) - known
        if unknown:
            # Silently dropping a mistyped key would mean quietly ignoring
            # something the operator meant to be acted on.
            log.warning("profile: ignoring unknown keys %s", ", ".join(sorted(unknown)))
        return cls(**{k: v for k, v in data.items() if k in known})

    def as_prompt(self) -> str:
        """Render for the LLM. Omits empty sections rather than padding them."""
        blocks: list[str] = [f"Operator: {self.name}"]
        if self.business:
            blocks.append(f"Business:\n{self.business.strip()}")
        if self.commitments:
            lines = "\n".join(f"  - {k}: {v}" for k, v in self.commitments.items())
            # Labelled emphatically. A model that reads a target as an
            # achievement will congratulate him for work he has not done, which
            # is the single worst failure this thing can have.
            blocks.append(
                "TARGETS he set for himself. These are what he PROMISED, NOT "
                "what he has DONE. There is no record of whether he hit any of "
                "them unless it appears under RECORDED FACTS below:\n" + lines
            )
        if self.metrics:
            lines = "\n".join(f"  - {k}: {v}" for k, v in self.metrics.items())
            blocks.append(
                "RECORDED FACTS - figures he last entered himself. These are "
                "actuals:\n" + lines
            )
        if self.training:
            lines = "\n".join(f"  - {k}: {v}" for k, v in self.training.items())
            blocks.append(f"Training:\n{lines}")
        if self.standards:
            lines = "\n".join(f"  - {s}" for s in self.standards)
            blocks.append(f"Standards he set for himself:\n{lines}")
        if self.known_weaknesses:
            lines = "\n".join(f"  - {w}" for w in self.known_weaknesses)
            blocks.append(f"Patterns he has already admitted to:\n{lines}")
        if self.notes:
            blocks.append(f"Other context:\n{self.notes.strip()}")
        return "\n\n".join(blocks)
