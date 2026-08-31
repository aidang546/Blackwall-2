# Training "Erebus" as a wake word

openWakeWord ships pretrained models — `hey_jarvis`, `alexa`, `hey_mycroft` and
a few others. "Erebus" is not among them, so out of the box the config uses
`hey_jarvis` as a stand-in: the pipeline works end to end, but you are saying
the wrong word at it.

Those pretrained models are a separate ~10 MB download. Erebus fetches them
automatically the first time it loads a wake model, so there is nothing to do
for the stand-in to work.

Training a real one takes about fifteen minutes, and none of it is manual
recording — the training set is synthetic.

---

## What "stand-in" actually costs, measured

It is worse than a compromise. Every stock model was run against "Erebus"
spoken by four different synthetic voices at three speaking rates:

| model | scores on "Erebus" | scores on other speech |
|---|---|---|
| `hey_jarvis` | **0.000** | 0.000 |
| `hey_mycroft` | 0.000 | 0.000 |
| `alexa` | 0.000 | 1.000 (on "Alexa, play some music") |
| `hey_rhasspy` | 0.004 | 0.003 |

Not "less reliable" — zero. Saying "Erebus" at it does nothing at all. Until a
real model exists the wake word is literally `hey jarvis`, and `doctor` now
says so rather than reporting that models are installed.

**Use the hotkey instead.** `ctrl+alt+space` reaches Erebus from inside any
window, with no false alarms and no training. It is configured under `hotkey:`
and is the recommended way to run until you have trained a model.

### Why not train one from here

It was attempted, and it failed in a way worth recording. Roughly 1,200
positives were synthesised from four Piper voices, augmented across pitch,
formant, band-limiting, reverb and noise, against 1,100 negatives that included
deliberate near-misses — *Cerberus*, *aerobics*, *her office*, *every bus*.

Held out one voice at a time, so the model was scored on a speaker it had never
trained on:

```
held out alan      AUC 0.911
held out northern  AUC 0.888
held out lessac    AUC 0.914
held out jenny     AUC 0.817
```

AUC 0.9 sounds respectable and is nowhere near enough. A wake word runs about
45,000 windows an hour, so it needs a false-alarm rate near one in 10⁵ — the
useful figure is recall at zero false alarms, and that came out at 0–12%.
Aligning the phrase to the window edge did not move it. Neither did handing the
model 40 clips of the held-out voice, as though the user had recorded
themselves at setup.

The limit is voice diversity, not the recipe. Four TTS voices cannot stand in
for the range of people and rooms a wake word has to survive. The notebook
below uses hundreds, which is exactly why it works and this did not.

---

## The short version

openWakeWord's own notebook does the whole thing: it synthesises a few thousand
utterances of your phrase with a text-to-speech model across many voices and
accents, mixes them against noise and negative speech, and trains a small
classifier.

1. Open the [automatic model training
   notebook](https://github.com/dscripka/openWakeWord/blob/main/notebooks/automatic_model_training.ipynb)
   in Google Colab. A free GPU runtime is enough.
2. Set the target phrase:
   ```python
   target_word = "erebus"
   ```
3. Run every cell. Roughly 10-15 minutes.
4. Download the resulting `erebus.onnx`.

Then, in this repo:

```
models/erebus.onnx
```

```yaml
# config.local.yaml
wake:
  model: models/erebus.onnx
  threshold: 0.55
```

Export the **ONNX** model, not the tflite one. Erebus loads openWakeWord with
`inference_framework="onnx"` on purpose: the `tflite-runtime` wheel is compiled
against NumPy 1.x and fails under NumPy 2 with a bare `AttributeError:
_ARRAY_API not found` that names neither package. onnxruntime is already
required by Piper, so nothing extra is needed.

Verify it before trusting it — `tests/test_wake.py` speaks the wake word and a
set of ordinary commands at the detector and reports the scores. Point it at
your model by editing `WAKE_MODEL` and `WAKE_PHRASE` at the top:

```
python tests/test_wake.py
```

```
  ok    'Hey Jarvis' fires  peak=0.997
  ok    'Gaming mode' does not fire  peak=0.000
  ok    separation is wide enough to be safe  0.000 .. 0.997
```

What you want is that separation. The stock model scores 0.997 on its phrase
and a flat 0.000 on everything else, which is why a 0.55 threshold is safe. If
your trained model puts the negatives up around 0.3-0.4, the margin is thin and
it will false-trigger in conversation — retrain with more negative examples
rather than papering over it with a higher threshold.

---

## Tuning the threshold

Run with debug logging to watch the live detection scores:

```powershell
python -m erebus -v
```

Say the word ten times, say unrelated things for a few minutes, and look at
where the scores land.

- **Missing you when you say it** → lower the threshold, 0.45 or 0.4.
- **Firing at random** → raise it, 0.65 or 0.7.
- **Firing at one specific thing you say often** → that phrase is close to your
  wake word in the model's feature space. Retrain with it added as a negative
  example, or pick a different wake word.

`wake.refractory` (default 2.0s) is how long it ignores further detections
after one fires. Raise it if a single "erebus" triggers twice.

---

## If you would rather not train one

Two alternatives:

**Use a stock model as the real wake word.** Set `wake.model: hey_jarvis` and
just say "hey jarvis". Works perfectly; it just isn't the name you chose.

**Turn the wake word off.**

```yaml
wake:
  enabled: false
```

Push-to-talk only — space bar on the desktop, the HOLD button on the phone.
This uses no CPU at idle and cannot false-trigger, which some people prefer for
an assistant that can run programs.
