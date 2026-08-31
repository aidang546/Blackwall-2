"""The effects chain, measured rather than listened to.

Two bugs in here survived a long time because the chain was only ever judged
by ear, and by ear "processed" and "broken" sound alike:

  * `pitch_shift` moved a 110 Hz tone to 105 Hz when asked for 92, and threw
    away 63% of the signal's energy doing it. The overlap-add stretch underneath
    it laid frames down at arbitrary phase, so harmonics cancelled - invisible
    on noise, devastating on a voice.
  * `formant_shift` resampled the whole signal, which moves f0 along with the
    formants. That is a pitch shift wearing another name.
  * The stretch laid frames a whole hop apart, so the output length - and with
    it the pitch ratio, which is that length over the input's - could only land
    on multiples of ~6.7 cents. That is coarser than the entire useful range of
    the detune control: 5 and 8 cents both came out as 10.8, and 11 and 15 both
    as 17.5, so two thirds of the dial did nothing.

Both are now asserted numerically, because both are the kind of failure a
listener writes off as character.

Runs standalone: python tests/test_voicefx.py
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from erebus.pipeline import voicefx as V   # noqa: E402

if not V.FX_AVAILABLE:
    print("numpy/scipy not installed - skipping")
    raise SystemExit(0)

import numpy as np              # noqa: E402
from scipy import signal as sg  # noqa: E402

SR = 22050
PASSED = FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  ok    {label:<44} {detail}")
    else:
        FAILED += 1
        print(f"  FAIL  {label:<44} {detail}")


def tone(f0: float = 110.0, seconds: float = 2.0, partials: int = 25):
    """A harmonic stack - a voice's periodicity without a voice's variability."""
    t = np.arange(int(SR * seconds)) / SR
    x = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, partials))
    return (x * 0.1).astype(np.float32)


def precise_f0(x, low: float = 30.0, high: float = 260.0) -> float:
    """Parabolic-interpolated FFT peak - accurate to a fraction of a cent.

    The autocorrelation estimate below is quantised to a whole lag, which is
    about 40 cents at 110 Hz: far too coarse to see a detune-sized error.
    """
    y = np.asarray(x, dtype=np.float64) * np.hanning(len(x))
    spec = np.abs(np.fft.rfft(y))
    freqs = np.fft.rfftfreq(len(y), 1 / SR)
    band = (freqs > low) & (freqs < high)
    i = int(np.where(band)[0][np.argmax(spec[band])])
    a, b, c = (np.log(spec[j] + 1e-12) for j in (i - 1, i, i + 1))
    return (i + 0.5 * (a - c) / (a - 2 * b + c + 1e-12)) * SR / len(y)


def cents_between(x, y) -> float:
    return 1200.0 * np.log2(precise_f0(y) / precise_f0(x))


def measured_f0(x) -> float:
    y = np.asarray(x, dtype=np.float64)
    y = y - y.mean()
    acf = np.correlate(y, y, "full")[len(y) - 1 :]
    acf /= acf[0] + 1e-12
    lo, hi = int(SR / 400), int(SR / 50)
    return SR / (lo + int(np.argmax(acf[lo:hi])))


def band_energy(x, low: float, high: float) -> float:
    f, p = sg.welch(np.asarray(x, dtype=np.float64), SR, nperseg=4096)
    return float(p[(f >= low) & (f < high)].sum())


def centroid(x) -> float:
    f, p = sg.welch(np.asarray(x, dtype=np.float64), SR, nperseg=4096)
    m = (f > 60) & (f < 7000)
    return float((f[m] * p[m]).sum() / p[m].sum())


print("\npitch shift")
src = tone()
for semitones in (-4.0, -2.0, 3.0):
    out = V.pitch_shift(src, semitones)
    want = 110.0 * 2 ** (semitones / 12.0)
    got = measured_f0(out)
    check(
        f"{semitones:+.0f} semitones lands on pitch",
        abs(got - want) / want < 0.02,
        f"{got:.1f} Hz, wanted {want:.1f}",
    )
    ratio = band_energy(out, 60, 7000) / band_energy(src, 60, 7000)
    check(
        f"{semitones:+.0f} semitones keeps its energy",
        ratio > 0.85,
        f"{ratio * 100:.0f}% retained",
    )
    check(
        f"{semitones:+.0f} semitones keeps its duration",
        len(out) == len(src),
        f"{len(out)} samples",
    )

check("a zero shift is a no-op", V.pitch_shift(src, 0.0) is src)

print("\npitch shift, at detune resolution")
fine = tone(seconds=3.0)
for cents in (5.0, 8.0, 11.0, 15.0, 22.0):
    got = cents_between(fine, V.pitch_shift(fine, cents / 100.0))
    check(
        f"{cents:.0f} cents means {cents:.0f} cents",
        abs(got - cents) < 1.5,
        f"got {got:.1f}",
    )
distinct = {round(cents_between(fine, V.pitch_shift(fine, c / 100.0)))
            for c in (5.0, 8.0, 11.0, 15.0, 22.0)}
check(
    "and five settings give five different results",
    len(distinct) == 5,
    f"{sorted(distinct)}",
)

print("\nformant shift")
for factor in (0.85, 0.92, 1.15):
    out = V.formant_shift(src, factor, SR)
    got = measured_f0(out)
    check(
        f"factor {factor:.2f} leaves the pitch alone",
        abs(got - 110.0) / 110.0 < 0.02,
        f"f0 {got:.1f} Hz",
    )
    moved = centroid(out) / centroid(src)
    check(
        f"factor {factor:.2f} moves the envelope the right way",
        (moved < 0.97) if factor < 1 else (moved > 1.03),
        f"centroid x{moved:.2f}",
    )

print("\npresence")
flat = np.random.default_rng(0).normal(0, 0.1, SR * 2).astype(np.float32)
lifted = V.presence(flat, 6.0, SR)
gain = band_energy(lifted, 2000, 4000) / band_energy(flat, 2000, 4000)
check("+6 dB lifts the 2-4 kHz band", 1.5 < gain < 4.0, f"x{gain:.2f}")
edges = band_energy(lifted, 200, 800) / band_energy(flat, 200, 800)
check("and leaves the rest roughly alone", 0.8 < edges < 1.25, f"x{edges:.2f}")
check("zero gain is a no-op", V.presence(flat, 0.0, SR) is flat)

print("\npresets")
speechlike = (tone(105.0) * (0.4 + 0.6 * np.abs(np.sin(
    2 * np.pi * 3.5 * np.arange(SR * 2) / SR)))).astype(np.float32)
for name in sorted(V.PRESETS):
    out = V.process(speechlike, SR, {"preset": name, "enabled": True})
    peak = float(np.abs(out).max())
    check(
        f"{name}: processes and stays in range",
        np.isfinite(out).all() and 0.1 < peak <= 1.0,
        f"peak {peak:.2f}",
    )
    kept = band_energy(out, 300, 3000) / band_energy(speechlike, 300, 3000)
    check(f"{name}: keeps its mid band", kept > 0.05, f"x{kept:.2f}")

check(
    "an unknown preset falls through to the raw settings",
    V.resolve({"preset": "nope", "reverb": 0.5}) == {"preset": "nope", "reverb": 0.5},
)
check(
    "explicit keys beat the preset",
    V.resolve({"preset": "clean", "reverb": 0.99})["reverb"] == 0.99,
)

print("\nfailure is not silence")
broken = V.process(speechlike, SR, {"enabled": True, "bandpass": ["x", "y"]})
check(
    "a bad setting returns the dry audio, not an exception",
    len(broken) == len(speechlike),
)
check("disabled is a passthrough", V.process(speechlike, SR, {"enabled": False}) is speechlike)
check("empty input survives", len(V.process(np.zeros(0, np.float32), SR, {"enabled": True})) == 0)

print(f"\n  {PASSED}/{PASSED + FAILED} passed")
raise SystemExit(1 if FAILED else 0)
