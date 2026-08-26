# Setup — Windows

Target: an NVIDIA RTX machine. Roughly 20 minutes, most of it downloads.

---

## 1. Python

Python 3.11 or 3.12. **Not 3.13** — several of the audio wheels have no build
for it yet.

```powershell
winget install Python.Python.3.12
```

```powershell
cd Blackwall-2
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If activation is blocked:
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

---

## 2. The wall, with nothing else installed

```powershell
pip install -r requirements.txt
python -m erebus --no-voice
```

A window opens with the stationary red line. Type `volume up` in the console —
it will say the audio interface is unavailable, because `pycaw` isn't installed
yet. That's the expected result at this stage; the routing works.

Add `?demo` to the URL to watch it cycle through every state.

---

## 3. Voice pipeline

```powershell
pip install -r requirements-voice.txt
```

### GPU acceleration for Whisper

faster-whisper runs on **CTranslate2, not torch**. Installing torch does
nothing for it — what CTranslate2 needs is cuBLAS and cuDNN 9:

```powershell
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

You do not have to get this right to proceed. `stt.py` tries CUDA, and on
failure logs the reason and re-loads on CPU rather than refusing to start. The
startup log tells you which one you ended up on:

```
whisper small.en ready on cuda in 2.3s      <- good
GPU load failed (...); retrying on CPU      <- cuBLAS/cuDNN missing
```

On CPU, drop to `stt.model: base.en` or `tiny.en` in `config.local.yaml` to
keep short commands responsive.

---

## 4. Microphone

Check the microphone is visible:

```powershell
python -m erebus devices
```

Pin one if the default is wrong — create `config.local.yaml`:

```yaml
audio:
  input_device: 3        # the index from the list above
```

---

## 5. A voice for it

`en_GB-alan-medium` is a low, flat British read that suits the persona:

```powershell
python -m erebus fetch-voice en_GB-alan-medium
```

That lands in `models/` and is picked up by name — no path needed. The download
is ~63 MB and resumes if the connection drops. It is verified against the
server's declared length, because a short file is not an error at download
time; it surfaces much later as an opaque protobuf failure when the model
loads.

Audition it, with and without the effects chain:

```powershell
python -m erebus say "The wall holds."
python -m erebus say "The wall holds." --out wall.wav          # render to a file
python -m erebus say "The wall holds." --out dry.wav --dry     # bypass effects
```

Other voices worth trying — `--voice` auditions one without changing config:

| voice | character |
|---|---|
| `en_GB-alan-medium` | low, flat, British. The default. |
| `en_GB-northern_english_male-medium` | rougher, more weathered |
| `en_US-lessac-medium` | neutral American |
| `en_GB-jenny_dioco-medium` | British female |

Full catalogue: <https://rhasspy.github.io/piper-samples/>. `python -m erebus
voices` lists what you have downloaded and marks the active one.

Tune the processing under `tts.effects` — `pitch_shift` and `reverb` do most of
the work. The whole chain costs about 40 ms per reply, so it is not worth
disabling for speed.

---

## 6. The brain

```powershell
winget install Ollama.Ollama
ollama pull llama3.1:8b
```

On 8GB VRAM or less, use `llama3.2:3b` instead and set it in
`config.local.yaml`:

```yaml
brain:
  model: llama3.2:3b
```

Ollama runs as a background service after install. Confirm:

```powershell
curl.exe http://127.0.0.1:11434/api/tags
```

---

## 7. Run it

```powershell
python -m erebus
```

The startup log prints a capability line. All four should be true:

```
capabilities: {'wake': True, 'stt': True, 'tts': True, 'brain': True, 'audio': True}
```

Any that are false are also shown in the bottom-left of the wall, greyed out.

---

## 8. Autostart

Task Scheduler, so it survives reboots and starts before you log in to
anything else.

```powershell
$action  = New-ScheduledTaskAction -Execute "$PWD\.venv\Scripts\pythonw.exe" `
                                   -Argument "-m erebus" -WorkingDirectory $PWD
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                                         -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "Erebus" -Action $action -Trigger $trigger `
                       -Settings $settings
```

`pythonw.exe` rather than `python.exe` keeps the console window from appearing.

---

## 9. Your phone

```powershell
python -m erebus pair
```

Set the server to listen beyond loopback first, in `config.local.yaml`:

```yaml
server:
  host: 0.0.0.0
```

Then allow it through the firewall, scoped to private networks only:

```powershell
New-NetFirewallRule -DisplayName "Erebus" -Direction Inbound -LocalPort 8848 `
                    -Protocol TCP -Action Allow -Profile Private
```

Open the printed URL on your phone, then use "Add to Home Screen" — it installs
as a fullscreen app with its own icon, and stays paired across relaunches.

The token in that URL is a password. Anyone holding it, on your network, can
run anything in your registry.

---

## Check everything at once

```powershell
python -m erebus doctor
```

```
  ok    python           3.12.4
  ok    config           8 apps configured
  FAIL  microphone       sounddevice not available
                           -> pip install -r requirements-voice.txt
  warn  speech in        faster-whisper installed, no CUDA device
                           -> It will run on CPU. For GPU: pip install
                              nvidia-cublas-cu12 nvidia-cudnn-cu12
  FAIL  brain            ollama not reachable at http://127.0.0.1:11434
                           -> Start it: ollama serve
```

`FAIL` is blocking for that feature; `warn` is optional. Erebus starts either
way — every stage degrades rather than refusing to run — so a failure means
that one capability is switched off, not that nothing works.

Run it first when anything misbehaves. It is faster than reading a log.

---

## Troubleshooting

**It wakes up at random.** Raise `wake.threshold` toward 0.7. The stock model
is standing in for "erebus" until you train one — see
[WAKEWORD.md](WAKEWORD.md).

**It cuts me off mid-sentence.** Raise `audio.silence_timeout` to 1.5-2.0.

**It never stops listening.** Your noise floor is above the gate. Raise
`audio.silence_threshold` to 0.02-0.03.

**Whisper is slow.** Look for `retrying on CPU` in the startup log — that means
cuBLAS/cuDNN are missing (step 3). If it says `ready on cuda` and is still
slow, drop to `stt.model: base.en`.

**cuDNN errors on startup.** `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`,
or set `stt.device: cpu` and accept CPU speed — `base.en` is usable for short
commands.

**`AttributeError: _ARRAY_API not found` on startup.** Something loaded
`tflite-runtime`, which is built for NumPy 1.x. Erebus loads the wake model
through onnxruntime specifically to avoid this, so if you see it, something
else in your environment pulled tflite in — `pip uninstall tflite-runtime` is
safe here, nothing in this project uses it.

**The voice sounds broken.** Set `tts.effects.enabled: false` to hear the dry
signal. If dry is fine, the chain is over-driven — start by halving `reverb`
and `static`.

**Nothing launches.** Run `python -m erebus actions` to see what's registered,
then test the raw command in PowerShell. A `start` command that fails there
will fail here too.

**It mishears one particular command.** Capture it and replay it while you
adjust, instead of saying it over and over:

```powershell
python -m erebus say "the phrase" --dry --out cmd.wav
python -m erebus --fake-mic cmd.wav
```

The matcher already absorbs close misses (Whisper hearing "coming mode" for
"gaming mode" still routes correctly), so if something consistently fails, add
the way it actually gets transcribed as another entry under `phrases:`.

**A voice model fails to load with a protobuf error.** The download was
truncated. Delete it from `models/` and re-run `fetch-voice`, which verifies
the length and resumes.
