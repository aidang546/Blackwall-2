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


def _wsola(x, rate: float, frame: int = 1024, overlap: int = 4):
    """Waveform-similarity overlap-add time stretch. rate > 1 = longer.

    Plain OLA - which this used to be - lands each frame on an arbitrary phase
    of the previous one. On noise nothing happens; on a voice the overlapping
    harmonics cancel, and measurably so: a one-semitone shift of a 110 Hz
    harmonic tone lost 28% of its total energy and gutted the 500-1000 Hz band
    by 14 dB. WSOLA fixes it by nudging each analysis frame, within half a hop,
    to wherever it best correlates with the tail it is about to be laid over.
    """
    if abs(rate - 1.0) < 1e-3:
        return x
    x = np.asarray(x, dtype=np.float32)
    hop_out = frame // overlap
    hop_in = max(1, int(round(hop_out / rate)))
    search = hop_out // 2
    ahead = frame - hop_out           # the region two adjacent frames share
    window = np.hanning(frame).astype(np.float32)

    n_out = int(len(x) * rate) + frame
    out = np.zeros(n_out, dtype=np.float32)
    norm = np.zeros(n_out, dtype=np.float32)

    natural = None                    # what the previous frame implies comes next
    out_pos = 0
    k = 0
    while True:
        nominal = k * hop_in
        if nominal + frame >= len(x) or out_pos + frame >= n_out:
            break

        pos = nominal
        if natural is not None:
            lo = max(0, nominal - search)
            hi = min(len(x) - frame, nominal + search)
            if hi > lo:
                seg = x[lo : hi + ahead]
                # Normalised cross-correlation: an unnormalised one just picks
                # whichever candidate is loudest.
                corr = np.correlate(seg, natural, "valid")
                energy = np.sqrt(
                    np.convolve(seg.astype(np.float64) ** 2, np.ones(ahead), "valid")
                ) + 1e-9
                pos = lo + int(np.argmax(corr / energy[: len(corr)]))

        out[out_pos : out_pos + frame] += x[pos : pos + frame] * window
        norm[out_pos : out_pos + frame] += window
        natural = x[pos + hop_out : pos + hop_out + ahead]
        if len(natural) < ahead:
            break
        out_pos += hop_out
        k += 1

    norm[norm < 1e-6] = 1.0
    return (out[: out_pos + frame] / norm[: out_pos + frame]).astype(np.float32)


def _resample_to(x, target: int):
    """Linear resample to an exact length. Changes pitch and duration together."""
    if target < 2 or len(x) < 2:
        return np.zeros(max(0, target), dtype=np.float32)
    idx = np.linspace(0, len(x) - 1, target)
    lo = np.floor(idx).astype(np.int32)
    hi = np.clip(lo + 1, 0, len(x) - 1)
    frac = (idx - lo).astype(np.float32)
    return (x[lo] * (1 - frac) + x[hi] * frac).astype(np.float32)


def pitch_shift(x, semitones: float):
    """Shift pitch while preserving duration. Negative = lower.

    Stretch to `ratio` of the length at constant pitch, then resample that back
    to the original length - which multiplies pitch by ratio and restores the
    duration. The stretch factor is the ratio, not its inverse; inverting it
    shifts pitch the wrong way.
    """
    if abs(semitones) < 0.01:
        return x
    ratio = 2.0 ** (semitones / 12.0)
    stretched = _wsola(x, ratio)
    return _resample_to(stretched, len(x))


def formant_shift(x, factor: float, sample_rate: int):
    """Move the spectral envelope without moving pitch. Below 1.0 = larger.

    Resampling moves formants and f0 together, so it is a pitch shift by
    another name. This separates the two per frame: low cepstral coefficients
    are the envelope (the size of the speaker), the rest is fine structure (the
    pitch). Only the envelope is warped, and the original phase is kept, so f0
    comes out where it went in.
    """
    if abs(factor - 1.0) < 0.01:
        return x
    x = np.asarray(x, dtype=np.float32)
    frame, hop = 1024, 256
    if len(x) < frame:
        return x
    window = np.hanning(frame).astype(np.float32)
    n_bins = frame // 2 + 1
    quefrency = 40                       # ~550 Hz envelope resolution at 22 kHz
    bins = np.arange(n_bins, dtype=np.float64)
    warped = np.clip(bins / factor, 0, n_bins - 1)   # where to read the envelope

    out = np.zeros(len(x) + frame, dtype=np.float32)
    norm = np.zeros_like(out)
    for start in range(0, len(x) - frame, hop):
        spec = np.fft.rfft(x[start : start + frame] * window)
        mag = np.abs(spec)
        log_mag = np.log(mag + 1e-9)

        ceps = np.fft.irfft(log_mag, n=frame)
        ceps[quefrency : frame - quefrency + 1] = 0.0
        envelope = np.fft.rfft(ceps, n=frame).real[:n_bins]

        gain = np.exp(np.interp(warped, bins, envelope) - envelope)
        chunk = np.fft.irfft(spec * gain, n=frame).astype(np.float32)
        out[start : start + frame] += chunk * window
        norm[start : start + frame] += window * window

    norm[norm < 1e-6] = 1.0
    return (out[: len(x)] / norm[: len(x)]).astype(np.float32)


def presence(x, gain_db: float, sample_rate: int, freq: float = 3000.0, q: float = 0.9):
    """One peaking bell around 3 kHz.

    Band-limiting a synthesised voice leaves it muffled: measured against a
    real comms mix, a bandpassed Piper render carries about 5% of its energy
    between 2 and 4 kHz where the reference carries 11. That band is what makes
    a transmission read as close and hard rather than distant and soft, so it
    needs putting back deliberately rather than by widening the passband.
    """
    if abs(gain_db) < 0.1:
        return x
    a_ = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * min(freq, sample_rate * 0.45) / sample_rate
    alpha = np.sin(w0) / (2.0 * q)
    b = [1 + alpha * a_, -2 * np.cos(w0), 1 - alpha * a_]
    a = [1 + alpha / a_, -2 * np.cos(w0), 1 - alpha / a_]
    sos = signal.tf2sos(b, a)
    return signal.sosfilt(sos, x).astype(np.float32)


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


def ring_mod(x, freq: float, mix: float, sample_rate: int):
    """Multiply by a sine carrier.

    The oldest robot-voice trick and still the most effective, because it does
    something no human throat can: it creates sum and difference frequencies
    that are not harmonically related to the original. That inharmonicity is
    what the ear reads as "not alive". Low carriers (30-80 Hz) buzz; higher
    ones (200 Hz+) destroy intelligibility, so keep the mix well under 1.
    """
    if mix <= 0.001 or freq <= 0:
        return x
    t = np.arange(len(x), dtype=np.float32) / sample_rate
    carrier = np.sin(2 * np.pi * freq * t).astype(np.float32)
    return ((1.0 - mix) * x + mix * x * carrier).astype(np.float32)


def detune_layers(x, voices: int, cents: float, spread_ms: float,
                  sample_rate: int):
    """Stack slightly detuned, slightly delayed copies of the voice.

    This is the effect people actually mean by "sounds like an AI": several
    near-identical voices speaking as one, never quite in unison. A single
    speaker cannot produce it, and no amount of acting approximates it.

    Small numbers matter here - 8 to 20 cents and 10 to 30 ms. Push either
    further and it stops being one entity and becomes a crowd.
    """
    if voices < 2 or cents <= 0:
        return x

    layers = [x]
    for index in range(1, voices):
        # Alternate sharp and flat so the stack stays centred on the original.
        direction = 1 if index % 2 else -1
        offset = direction * cents * (1 + index // 2) / 100.0
        layer = pitch_shift(x, offset)

        delay = int(sample_rate * (spread_ms / 1000.0) * index)
        if delay:
            layer = np.concatenate([np.zeros(delay, dtype=np.float32), layer])[: len(x)]
        if len(layer) < len(x):
            layer = np.pad(layer, (0, len(x) - len(layer)))
        layers.append(layer[: len(x)])

    stacked = np.sum(layers, axis=0) / len(layers)
    return stacked.astype(np.float32)


def sub_octave(x, mix: float):
    """Mix an octave-down copy underneath.

    Adds weight the source never had. Used sparingly it reads as size; used
    heavily it reads as a different creature entirely.
    """
    if mix <= 0.001:
        return x
    return ((1.0 - mix) * x + mix * pitch_shift(x, -12.0)).astype(np.float32)


def comb_resonance(x, freq: float, feedback: float, sample_rate: int):
    """A short tuned delay, giving a metallic resonant character.

    Distinct from the reverb: that simulates a room, this simulates the voice
    coming out of something with a resonant cavity - a vent, a machine, a
    speaker grille.
    """
    if feedback <= 0.001 or freq <= 0:
        return x
    delay = max(1, int(sample_rate / freq))
    y = x.astype(np.float32).copy()
    for start in range(delay, len(y), delay):
        stop = min(start + delay, len(y))
        y[start:stop] += feedback * y[start - delay : stop - delay]
    peak = float(np.abs(y).max())
    return (y / peak * float(np.abs(x).max())).astype(np.float32) if peak else y


#: Ready-made characters. `preset:` in config picks one; anything set
#: alongside it overrides that key. These exist because the interesting
#: settings interact - detune plus ring mod plus sub is a different creature
#: from any of them alone, and finding that by twiddling one slider at a time
#: takes an evening.
PRESETS = {
    # The current default: a processed human. Cold, but plainly a person.
    "transmitted": dict(
        pitch_shift=-2.5, formant=0.94, bandpass=[180, 6200],
        reverb=0.22, static=0.035, bitcrush=0,
    ),
    # Several voices as one, slightly detuned, with weight underneath and a
    # metallic edge. This is the one that sounds like something behind a wall.
    "blackwall": dict(
        pitch_shift=-3.0, formant=0.90, bandpass=[140, 6800],
        detune_voices=3, detune_cents=11, detune_spread_ms=18,
        sub_octave=0.22, ring_freq=42, ring_mix=0.13,
        comb_freq=180, comb_feedback=0.18,
        reverb=0.30, static=0.045, bitcrush=0,
    ),
    # Classic machine: heavy ring modulation, quantised, band-limited hard.
    "machine": dict(
        pitch_shift=-1.5, formant=0.97, bandpass=[300, 4200],
        ring_freq=95, ring_mix=0.42, bitcrush=9,
        comb_freq=240, comb_feedback=0.25,
        reverb=0.12, static=0.06,
    ),
    # Fitted, not guessed. A reference comms mix was measured for the things a
    # mix can be measured for - octave-band balance, spectral tilt, reverb
    # decay, noise texture - and a grid search found the settings that put a
    # Piper render closest to it, fitted on one line and checked on a held-out
    # one so the numbers are not just tuned to a sentence. Tilt lands at -4.7
    # dB/octave against the reference's -4.7, decay at 551 ms against 511.
    #
    # What is left over is the 60-125 Hz band and the 500-1000 Hz vowel energy,
    # and neither is a mix property: they are where that speaker's fundamental
    # and vowels happen to sit. Matching those would mean modelling the person,
    # not the processing, so the fit stops here on purpose.
    "broadcast": dict(
        pitch_shift=-2.0, formant=0.94, bandpass=[190, 5200],
        presence=6.5, reverb=0.20, static=0.03, bitcrush=0,
    ),
    # Barely processed, for when the persona is doing the work.
    "clean": dict(
        pitch_shift=-1.0, formant=0.98, bandpass=[120, 7600],
        reverb=0.10, static=0.01, bitcrush=0,
    ),
}


def resolve(settings: dict) -> dict:
    """Expand a preset, letting explicit keys override it."""
    name = settings.get("preset")
    if not name:
        return settings
    base = PRESETS.get(name)
    if base is None:
        log.warning("unknown tts preset %r - using settings as given", name)
        return settings
    merged = dict(base)
    for key, value in settings.items():
        if key != "preset":
            merged[key] = value
    return merged


def process(audio, sample_rate: int, settings: dict):
    """Run the full chain. Returns float32 in [-1, 1].

    Order matters: pitch and formant first (they are about the source), then
    band-limiting (the channel), then space and noise (the environment).
    """
    if not FX_AVAILABLE or not settings.get("enabled", True):
        return audio

    settings = resolve(settings)

    x = np.asarray(audio, dtype=np.float32)
    if x.size == 0:
        return x

    try:
        # Order matters. Pitch and formant reshape the speaker; layering then
        # multiplies that speaker; ring modulation and comb act on the stack;
        # band-limiting is the channel; space and noise are the room. Ring
        # modulating before layering would give three differently-detuned
        # carriers, which sounds like a fault rather than a character.
        x = pitch_shift(x, float(settings.get("pitch_shift", 0.0)))
        x = formant_shift(x, float(settings.get("formant", 1.0)), sample_rate)

        x = detune_layers(
            x,
            int(settings.get("detune_voices", 1) or 1),
            float(settings.get("detune_cents", 0.0) or 0.0),
            float(settings.get("detune_spread_ms", 0.0) or 0.0),
            sample_rate,
        )
        x = sub_octave(x, float(settings.get("sub_octave", 0.0) or 0.0))
        x = ring_mod(x, float(settings.get("ring_freq", 0.0) or 0.0),
                     float(settings.get("ring_mix", 0.0) or 0.0), sample_rate)
        x = comb_resonance(x, float(settings.get("comb_freq", 0.0) or 0.0),
                           float(settings.get("comb_feedback", 0.0) or 0.0),
                           sample_rate)

        band = settings.get("bandpass") or [None, None]
        if band[0] and band[1]:
            x = bandpass(x, float(band[0]), float(band[1]), sample_rate)

        x = presence(x, float(settings.get("presence", 0.0) or 0.0), sample_rate)
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
