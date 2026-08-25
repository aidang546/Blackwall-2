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

## 3. CUDA PyTorch — do this before the voice requirements

Out of order, pip resolves the CPU wheel and Whisper runs about ten times
slower with no warning that anything is wrong.

```powershell
pip install torch --index-url https://download.pytorch.org/whl/cu124
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

Must print `True` and your card. If it prints `False`, update your NVIDIA
driver and try again before continuing.

---

## 4. Voice pipeline

```powershell
pip install -r requirements-voice.txt
```

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

Piper voices are two files, downloaded from Hugging Face. `en_GB-alan-medium`
is a low, flat British read that suits the persona:

```powershell
mkdir models
curl.exe -L -o models/en_GB-alan-medium.onnx `
  https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/en_GB-alan-medium.onnx
curl.exe -L -o models/en_GB-alan-medium.onnx.json `
  https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/en_GB-alan-medium.onnx.json
```

Point at it in `config.local.yaml`:

```yaml
tts:
  voice: models/en_GB-alan-medium.onnx
```

Test the voice and its effects chain without booting the whole thing:

```powershell
python -m erebus say "The wall holds."
```

Other voices worth trying: `en_GB-northern_english_male-medium` (rougher),
`en_US-lessac-medium` (neutral), `en_GB-jenny_dioco-medium` (female). Browse at
<https://rhasspy.github.io/piper-samples/>. Tune the processing under
`tts.effects` — `pitch_shift` and `reverb` do most of the work.

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

Open the printed URL on your phone. On Android, add it to the home screen and
it runs full-screen with no browser chrome.

The token in that URL is a password. Anyone holding it, on your network, can
run anything in your registry.

---

## Troubleshooting

**It wakes up at random.** Raise `wake.threshold` toward 0.7. The stock model
is standing in for "erebus" until you train one — see
[WAKEWORD.md](WAKEWORD.md).

**It cuts me off mid-sentence.** Raise `audio.silence_timeout` to 1.5-2.0.

**It never stops listening.** Your noise floor is above the gate. Raise
`audio.silence_threshold` to 0.02-0.03.

**Whisper is slow.** Check step 3. If `torch.cuda.is_available()` is False you
are on CPU. If it's True and still slow, drop to `stt.model: base.en`.

**cuDNN errors on startup.** Install the NVIDIA cuDNN runtime, or set
`stt.device: cpu` — `base.en` on CPU is usable for short commands.

**The voice sounds broken.** Set `tts.effects.enabled: false` to hear the dry
signal. If dry is fine, the chain is over-driven — start by halving `reverb`
and `static`.

**Nothing launches.** Run `python -m erebus actions` to see what's registered,
then test the raw command in PowerShell. A `start` command that fails there
will fail here too.
