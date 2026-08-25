"""The "behind the wall" voice chain.

Piper produces a clean, human, slightly cheerful read. That is exactly wrong for
Erebus, so everything it says is pushed through this chain first: pitched down,
band-limited like a transmission, given a small dark room, and bedded on a thin
noise floor. Each stage is individually switchable from config so you can dial
it back to plain speech if it gets tiring.

Everything here is numpy/scipy - no extra dependency, and fast enough to process
a sentence in a few milliseconds.
"""

from __future__ import annotations

import logging

log = logging.getLogger("erebus.voicefx")

try:  # pragma: no cover
    import numpy as np
    from scipy import signal

    FX_AVAILABLE = True
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]
    signal = None  # type: ignore[assignment]
    FX_AVAILABLE = False


def _ola_timestretch(x, rate: float, frame: int = 1024, overlap: int = 4):
    """Overlap-add time stretch. rate > 1 = longer.

    A phase vocoder would be cleaner, but the phase smearing this produces reads
    as machine-processed rather than as a bug, which suits the persona.
    """
    if abs(rate - 1.0) < 1e-3:
        return x
    hop_in = frame // overlap
    hop_out = int(round(hop_in * rate))
    window = np.hanning(frame).astype(np.float32)

    n_frames = max(1, 1 + (len(x) - frame) // hop_in)
    out = np.zeros(frame + hop_out * n_frames, dtype=np.float32)
    norm = np.zeros_like(out)

    for i in range(n_frames):
        src = i * hop_in
        dst = i * hop_out
        chunk = x[src : src + frame]
        if len(chunk) < frame:
            chunk = np.pad(chunk, (0, frame - len(chunk)))
        out[dst : dst + frame] += chunk * window
        norm[dst : dst + frame] += window

    norm[norm < 1e-6] = 1.0
    return out / norm


def pitch_shift(x, semitones: float):
    """Shift pitch while preserving duration."""
    if abs(semitones) < 0.01:
        return x
    ratio = 2.0 ** (semitones / 12.0)
    # Stretch by the inverse, then resample back: net effect is pitch-only.
    stretched = _ola_timestretch(x, 1.0 / ratio)
    target = len(x)
    idx = np.linspace(0, len(stretched) - 1, target).astype(np.float32)
    lo = np.floor(idx).astype(np.int32)
    hi = np.clip(lo + 1, 0, len(stretched) - 1)
    frac = idx - lo
    return (stretched[lo] * (1 - frac) + stretched[hi] * frac).astype(np.float32)


def formant_shift(x, factor: float, sample_rate: int):
    """Crude formant move by resampling the spectral envelope.

    Below 1.0 this makes the speaker sound physically larger, which is most of
    what sells "this is not a person".
    """
    if abs(factor - 1.0) < 0.01:
        return x
    n = int(len(x) / factor)
    resampled = signal.resample(x, max(1, n))
    return _ola_timestretch(resampled.astype(np.float32), len(x) / max(1, n))[: len(x)]


def bandpass(x, low: float, high: float, sample_rate: int):
    """Trim both ends. Kills the intimacy of a close-miked voice."""
    nyq = sample_rate / 2.0
    low_n = max(1e-4, min(0.99, low / nyq))
    high_n = max(low_n + 1e-3, min(0.99, high / nyq))
    sos = signal.butter(4, [low_n, high_n], btype="band", output="sos")
    return signal.sosfilt(sos, x).astype(np.float32)


def reverb(x, wet: float, sample_rate: int):
    """Schroeder reverb - four damped combs into two allpasses. Small dark room.

    Both filter types are recursive, so the obvious implementation is a Python
    loop over every sample - which costs about 340 ms on a four-second line,
    more than every other stage put together. Written instead as IIR transfer
    functions, `lfilter` runs the same recursion in C for about 3 ms.
    """
    if wet <= 0.001:
        return x

    def feedback_stride(y, delay: int, gain: float):
        """Apply y[n] += gain * y[n-D] in place, vectorised.

        The recursion only ever reaches back D samples, so no two samples
        *within* a D-long block depend on each other. Walking the signal a
        block at a time turns N scalar steps into N/D vector adds - for a 30 ms
        delay that is ~130 numpy operations instead of ~88,000 Python ones.

        (`lfilter` cannot do this: it is O(N x len(a)), and expressing a delay
        line as a denominator makes len(a) the delay length, so it comes out
        slower than the naive loop rather than faster.)
        """
        n = len(y)
        for start in range(delay, n, delay):
            stop = min(start + delay, n)
            y[start:stop] += gain * y[start - delay : stop - delay]
        return y

    def comb(sig, delay_ms: float, feedback: float):
        delay = max(1, int(sample_rate * delay_ms / 1000.0))
        return feedback_stride(sig.astype(np.float32).copy(), delay, feedback)

    def allpass(sig, delay_ms: float, gain: float = 0.5):
        """y[n] = -g x[n] + x[n-D] + g y[n-D]"""
        delay = max(1, int(sample_rate * delay_ms / 1000.0))
        out = (-gain * sig).astype(np.float32)
        out[delay:] += sig[:-delay]
        return feedback_stride(out, delay, gain)

    def damp(sig, amount: float = 0.35):
        """One-pole lowpass - the 'dark' in 'small dark room'.

        Freeverb puts this inside each comb's feedback path. Applied once to
        the summed wet signal instead, it is two filter taps rather than a
        per-comb recursion, and the difference is not audible here.
        """
        return signal.lfilter([1.0 - amount], [1.0, -amount], sig).astype(np.float32)

    # Mutually prime delays, so the combs do not reinforce into a ringing pitch.
    wet_sig = np.zeros_like(x, dtype=np.float32)
    for delay_ms, feedback in ((29.7, 0.78), (37.1, 0.75), (41.1, 0.72), (43.7, 0.70)):
        wet_sig += comb(x, delay_ms, feedback)
    wet_sig /= 4.0
    wet_sig = damp(wet_sig)
    wet_sig = allpass(allpass(wet_sig, 5.0), 1.7)

    # The comb bank adds energy; match it back to the dry level so `wet` stays
    # a mix control rather than doubling as a volume control.
    peak = float(np.abs(wet_sig).max())
    if peak > 1e-6:
        wet_sig *= float(np.abs(x).max()) / peak

    return ((1.0 - wet) * x + wet * wet_sig).astype(np.float32)


def add_static(x, amount: float, sample_rate: int):
    """A thin, filtered noise bed. Sells "transmitted from somewhere else"."""
    if amount <= 0.0001:
        return x
    noise = np.random.normal(0, 1, len(x)).astype(np.float32)
    sos = signal.butter(2, [300 / (sample_rate / 2), 4000 / (sample_rate / 2)],
                        btype="band", output="sos")
    noise = signal.sosfilt(sos, noise).astype(np.float32)
    # Duck the static under speech so it sits behind, not on top.
    envelope = np.abs(signal.sosfilt(
        signal.butter(2, 20 / (sample_rate / 2), btype="low", output="sos"), np.abs(x)
    )).astype(np.float32)
    envelope /= max(1e-6, float(envelope.max()))
    return (x + noise * amount * (0.35 + 0.65 * envelope)).astype(np.float32)


def bitcrush(x, bits: int):
    if not bits or bits <= 0 or bits >= 16:
        return x
    levels = float(2 ** bits)
    return (np.round(x * levels) / levels).astype(np.float32)


def process(audio, sample_rate: int, settings: dict):
    """Run the full chain. Returns float32 in [-1, 1].

    Order matters: pitch and formant first (they are about the source), then
    band-limiting (the channel), then space and noise (the environment).
    """
    if not FX_AVAILABLE or not settings.get("enabled", True):
        return audio

    x = np.asarray(audio, dtype=np.float32)
    if x.size == 0:
        return x

    try:
        x = pitch_shift(x, float(settings.get("pitch_shift", 0.0)))
        x = formant_shift(x, float(settings.get("formant", 1.0)), sample_rate)

        band = settings.get("bandpass") or [None, None]
        if band[0] and band[1]:
            x = bandpass(x, float(band[0]), float(band[1]), sample_rate)

        x = bitcrush(x, int(settings.get("bitcrush", 0) or 0))
        x = reverb(x, float(settings.get("reverb", 0.0)), sample_rate)
        x = add_static(x, float(settings.get("static", 0.0)), sample_rate)
    except Exception as exc:  # noqa: BLE001
        # A broken effect must never cost you the reply itself.
        log.warning("voice fx failed (%s) - passing audio through dry", exc)
        return np.asarray(audio, dtype=np.float32)

    peak = float(np.abs(x).max())
    if peak > 0:
        x = x / peak * 0.89   # leave headroom; clipping sounds cheap
    return x.astype(np.float32)
