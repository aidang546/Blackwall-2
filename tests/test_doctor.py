"""The diagnostic, tested against the broken installs it exists for.

Every check here corresponds to a real finding: doctor used to traceback on a
malformed config, was unreachable on a machine missing its own core
dependencies, aborted the whole report when one native wheel failed to load,
and reported a blocking failure for a correctly configured commands-only
setup. A diagnostic that only works on a healthy machine is worthless.

    python tests/test_doctor.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from erebus import doctor                    # noqa: E402
from erebus.core.config import Config        # noqa: E402

FAILURES = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILURES
    FAILURES += not ok
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")


def state_of(checks, name: str) -> str | None:
    for c in checks:
        if c.name == name:
            return c.state
    return None


def test_config() -> None:
    print("\nCONFIG")
    broken = Config({})
    broken.load_error = "ParserError: bad yaml"
    result = doctor.check_config(broken)
    check("a malformed config is a finding, not a traceback",
          result.state == doctor.FAIL and "ParserError" in result.detail)
    check("and it says what to do", "config.yaml" in result.fix)

    good = Config({"actions": {"apps": {"a": {}, "b": {}}}})
    result = doctor.check_config(good)
    check("a good config passes", result.state == doctor.PASS, result.detail)


def test_python_floor() -> None:
    print("\nPYTHON")
    result = doctor.check_python()
    check("reports the running interpreter", result.state in
          (doctor.PASS, doctor.WARN, doctor.FAIL))
    # 3.11 is the real floor: asyncio.timeout() is used in the run loop.
    with mock.patch.object(sys, "version_info", (3, 10, 6, "final", 0)):
        check("3.10 is refused, since asyncio.timeout needs 3.11",
              doctor.check_python().state == doctor.FAIL)
    with mock.patch.object(sys, "version_info", (3, 12, 4, "final", 0)):
        check("3.12 passes", doctor.check_python().state == doctor.PASS)
    with mock.patch.object(sys, "version_info", (3, 13, 0, "final", 0)):
        check("3.13 warns about audio wheels",
              doctor.check_python().state == doctor.WARN)


def test_brain() -> None:
    print("\nBRAIN")

    # A documented, correct configuration must not be a blocking failure.
    echo = Config({"brain": {"backend": "echo"}})
    result = asyncio.run(doctor.check_brain(echo))
    check("backend 'echo' is a warning, not a FAIL",
          state_of(result, "brain") == doctor.WARN,
          str(state_of(result, "brain")))

    class Response:
        def __init__(self, status, payload=None, raises=False):
            self.status_code = status
            self._payload = payload
            self._raises = raises

        def json(self):
            if self._raises:
                raise ValueError("not json")
            return self._payload

    def with_response(response):
        client = mock.AsyncMock()
        client.get = mock.AsyncMock(return_value=response)
        context = mock.MagicMock()
        context.__aenter__ = mock.AsyncMock(return_value=client)
        context.__aexit__ = mock.AsyncMock(return_value=False)
        return context

    config = Config({"brain": {"backend": "ollama", "model": "llama3.1:8b"}})

    with mock.patch("erebus.pipeline.brain.Brain.load",
                    new=mock.AsyncMock(return_value=False)), \
         mock.patch("erebus.pipeline.brain.Brain.close", new=mock.AsyncMock()):

        with mock.patch("httpx.AsyncClient",
                        return_value=with_response(Response(500))):
            result = asyncio.run(doctor.check_brain(config))
            check("a 5xx is reported as unhealthy, not 'not pulled'",
                  "HTTP 500" in result[0].detail, result[0].detail)

        with mock.patch("httpx.AsyncClient",
                        return_value=with_response(Response(200, raises=True))):
            result = asyncio.run(doctor.check_brain(config))
            check("a non-Ollama listener is called out",
                  "not an Ollama server" in result[0].detail, result[0].detail)

        with mock.patch("httpx.AsyncClient", return_value=with_response(
                Response(200, {"models": [{"name": "other:7b"}]}))):
            result = asyncio.run(doctor.check_brain(config))
            check("a genuinely unpulled model says so, and lists what is there",
                  "not pulled" in result[0].detail and "other:7b" in result[0].detail,
                  result[0].detail)
            check("and the fix is the pull command",
                  result[0].fix == "ollama pull llama3.1:8b")


def test_microphone() -> None:
    print("\nMICROPHONE")
    devices = [
        {"index": 0, "name": "Realtek", "channels": 2, "default_samplerate": 48000},
        {"index": 3, "name": "Blue Yeti", "channels": 1, "default_samplerate": 44100},
    ]
    with mock.patch("erebus.pipeline.audio.AUDIO_AVAILABLE", True), \
         mock.patch("erebus.pipeline.audio.list_devices", return_value=devices):

        result = doctor.check_microphone(Config({}))
        check("counts inputs without claiming which is default",
              "available" in result[0].detail and "default" not in result[0].detail,
              result[0].detail)

        result = doctor.check_microphone(Config({"audio": {"input_device": 3}}))
        check("a valid pinned device is named",
              state_of(result, "input device") == doctor.PASS)

        result = doctor.check_microphone(Config({"audio": {"input_device": 99}}))
        check("a stale pinned device is caught here, not at stream open",
              state_of(result, "input device") == doctor.FAIL)

    with mock.patch("erebus.pipeline.audio.AUDIO_AVAILABLE", False):
        result = doctor.check_microphone(Config({}))
        check("no sounddevice is a clear failure",
              result[0].state == doctor.FAIL and "pip install" in result[0].fix)


def test_isolation() -> None:
    print("\nISOLATION")

    def explode():
        raise OSError("DLL load failed: onnxruntime")

    result = asyncio.run(doctor._guarded("wake word", explode))
    check("a check that raises becomes a finding",
          result[0].state == doctor.FAIL and "OSError" in result[0].detail)
    check("and is attributed to doctor, not blamed on the user",
          "bug in doctor" in result[0].fix)

    async def async_explode():
        raise RuntimeError("boom")

    result = asyncio.run(doctor._guarded("brain", async_explode))
    check("an async check that raises is caught too",
          result[0].state == doctor.FAIL)

    check("a normal check passes through",
          asyncio.run(doctor._guarded("x", lambda: doctor.Check("x", doctor.PASS)))[0]
          .state == doctor.PASS)


def test_full_run() -> None:
    print("\nWHOLE REPORT")
    # The end-to-end guarantee: a completely broken config still produces a
    # report rather than a stack trace.
    broken = Config({})
    broken.load_error = "ParserError: everything is wrong"
    code = asyncio.run(doctor.run(broken))
    check("a broken config still yields a full report", code in (0, 1))
    check("and exits non-zero", code == 1)


def main() -> int:
    test_config()
    test_python_floor()
    test_brain()
    test_microphone()
    test_isolation()
    test_full_run()
    print(f"\n  {'all checks passed' if not FAILURES else f'{FAILURES} failed'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
