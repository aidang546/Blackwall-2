"""Config loading with a local-override layer.

`config.yaml` is committed and is the documented default. `config.local.yaml`
is gitignored and deep-merges on top, so machine-specific paths, your token and
your private macros never end up in version control.
"""

from __future__ import annotations

import copy
import os
import secrets
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config.yaml"
LOCAL_CONFIG = ROOT / "config.local.yaml"
TOKEN_FILE = ROOT / ".erebus_token"


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    """Dotted-path access over the merged YAML: `cfg.get("stt.model")`."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        base_path = path or DEFAULT_CONFIG
        with open(base_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if path is None and LOCAL_CONFIG.exists():
            with open(LOCAL_CONFIG, "r", encoding="utf-8") as fh:
                data = _deep_merge(data, yaml.safe_load(fh) or {})
        # Environment always wins - handy for one-off runs and for CI.
        for env_key, dotted in (
            ("EREBUS_STT_DEVICE", "stt.device"),
            ("EREBUS_BRAIN_MODEL", "brain.model"),
            ("EREBUS_PORT", "server.port"),
            ("EREBUS_TOKEN", "server.token"),
        ):
            if env_key in os.environ:
                cls._assign(data, dotted, os.environ[env_key])
        return cls(data)

    @staticmethod
    def _assign(data: dict, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        value = self.get(name, {})
        return value if isinstance(value, dict) else {}

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    # -- auth ---------------------------------------------------------------

    def resolve_token(self) -> str:
        """Return the shared secret, generating and persisting one if needed.

        Any client that is not on the loopback interface must present this. It
        is written to a gitignored file with owner-only permissions so that
        pairing a phone is a copy-paste rather than a password you invent.
        """
        token = self.get("server.token")
        if token:
            return str(token)
        if TOKEN_FILE.exists():
            existing = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        token = secrets.token_urlsafe(24)
        TOKEN_FILE.write_text(token, encoding="utf-8")
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except OSError:
            pass  # Windows ACLs; the gitignore is the real protection there.
        return token
