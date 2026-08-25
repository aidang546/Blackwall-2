# Training "Erebus" as a wake word

openWakeWord ships pretrained models — `hey_jarvis`, `alexa`, `hey_mycroft` and
a few others. "Erebus" is not among them, so out of the box the config uses
`hey_jarvis` as a stand-in: the pipeline works end to end, but you are saying
the wrong word at it.

Training a real one takes about fifteen minutes, and none of it is manual
recording — the training set is synthetic.

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
