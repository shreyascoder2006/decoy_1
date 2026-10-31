# Detector benchmark — how the default model was chosen

**Date:** 2026-10-31 · **Reproduce:** `python scripts/benchmark_detectors.py --human-dir <dir> --fake-dir <dir>`

The short version: **four of the five most-downloaded community audio-deepfake
models are inverted on neural TTS.** They score AI-generated speech *lower*
than genuine human speech. Picking by download count would have produced a
demo that confidently labels humans as AI and AI as human.

---

## Method

| | |
|---|---|
| Genuine human speech | 40 clips, LibriSpeech `dev-clean` (CC-BY 4.0), 16 kHz mono, each ≥ 4.2 s |
| AI-generated speech | 20 clips, `facebook/mms-tts-eng` (VITS neural TTS), 16 kHz mono, 5–7 s |
| Scored window | first 4.0 s of each clip, matching the live analysis window |
| Metric | AUC — probability a random AI clip scores above a random human clip. 1.0 perfect, 0.5 coin flip, below 0.5 inverted |
| Comparisons | 40 × 20 = 800 pairs |

Clip texts deliberately mixed scam-style scripts ("this is the fraud
department calling") with neutral prose, so the result is not an artefact of
one phrasing style.

---

## Results

| Model | AUC | @0.9 detections | @0.9 false alarms | Verdict |
|---|---|---|---|---|
| `Hemgg/Deepfake-audio-detection` | **0.889** | 15/20 (75%) | 4/40 (10%) | **usable** |
| `Gustking/wav2vec2-large-xlsr-deepfake-audio-classification` | 0.394 | 0/20 | 0/40 | inverted |
| `Om-Parab/distilhubert-finetuned-audio-deepfake-in-the-wild` | 0.264 | 0/20 | 2/40 | inverted |
| `mo-thecreator/Deepfake-audio-detection` | 0.121 | 0/20 | 4/40 | inverted |
| `MelodyMachine/Deepfake-audio-detection-V2` | 0.007 | 0/20 | 3/40 | inverted |

`mo-thecreator` is the most downloaded of the five (~19k) and was this
project's first default. At AUC 0.121 it is worse than a coin flip — it was
replaced on the strength of this measurement, not on reputation.

### Threshold sweep for the chosen model

| Threshold | Detections | False alarms | Per-window FA rate |
|---|---|---|---|
| 0.5 | 19/20 (95%) | 21/40 | 52% |
| 0.7 | 18/20 (90%) | 11/40 | 27% |
| **0.9** | **15/20 (75%)** | **4/40** | **10%** |
| 0.95 | 9/20 (45%) | 1/40 | 2.5% |

**0.9 is the default.** 0.5 catches almost everything but flags half of all
genuine human windows, which is useless.

---

## Why the alert policy needs a run of two windows

A 10% false-alarm rate *per window* is not a 10% false-alarm rate *per call*.
Windows are ~4 s, so a one-minute call is about 15 windows. Alerting on the
first fake window would flag a genuine human caller with probability

```
1 - 0.90^15  ≈  79%
```

That is not a detector you can demo. Requiring **two consecutive** fake
windows drops it to roughly 13%, because a genuine false positive is usually
isolated while a true positive recurs window after window. A clean window
resets the run, so scattered noise cannot accumulate across a long call.

This is `LIVE_CALL_ALERT_MIN_FAKE_WINDOWS`, default 2. The cost is latency:
two 4 s windows means the earliest possible alert is ~8 s. That is the top of
the 5–10 s target band and is a deliberate trade for not crying wolf. Lower
`LIVE_CALL_LOCAL_WINDOW_SECONDS` to 3.0 for a ~6 s floor if you accept
shorter, noisier windows.

---

## End-to-end verification

Full stack at 1× real time — HTTP session, WebSocket, local model, windowing,
alert policy — via `scripts/detect_file.py`:

| | AI call (36.9 s) | Human call (40.0 s) |
|---|---|---|
| Windows analysed | 9 | 10 |
| Labelled fake | 7/9 | 1/10 |
| **Alerts raised** | **2** (warn + upgrade) | **0** |
| Alert latency from first speech | **8.13 s** | n/a |
| Final label | `fake` | `real` |
| Frames dropped | 0 | 0 |

The human call is the important row. One window *was* falsely labelled fake,
and the consecutive-run requirement suppressed it — no alert reached the user.
Without that rule this run would have produced a false "Potential
AI-generated voice detected" on genuine human speech.

Model load takes ~2.1 s once per process. Inference keeps up comfortably at
real time on 4 CPU threads (0 dropped frames in both runs).

---

## Limitations — read before trusting any of this

1. **One TTS family.** All AI samples came from VITS (`mms-tts-eng`). Nothing
   here predicts behaviour on ElevenLabs, PlayHT, Cartesia, OpenAI TTS, or a
   voice clone of a specific person. Those are what a real attacker uses.
   **Benchmark your own samples before relying on a verdict.**
2. **One human corpus.** LibriSpeech is clean read speech from audiobooks:
   quiet, close-miked, native-accented. Real calls are noisy, compressed and
   accented. The 10% false-alarm rate may be optimistic or pessimistic.
3. **No WhatsApp codec in the loop.** These are clean 16 kHz WAVs. A live call
   adds Opus compression, noise suppression and packet loss. Re-measure
   through the real call path before quoting any number.
4. **40 + 20 is a small sample.** AUC 0.889 over 800 pairs has a wide
   confidence interval. Treat it as "this model has signal and the others do
   not", not as a precise accuracy figure.
5. **Concatenation artefacts.** The end-to-end samples were built by
   concatenating 5–7 s clips, so some windows straddle a join. This may
   account for a few of the misses.

---

## Re-running this with your own samples

```bash
mkdir -p mysamples/human mysamples/ai
```

Put genuine recordings in `mysamples/human` and your AI-generated samples in
`mysamples/ai`, converting each to the wire format:

```bash
ffmpeg -i input.mp3 -ar 16000 -ac 1 -c:a pcm_s16le mysamples/ai/out.wav
```

```bash
python scripts/benchmark_detectors.py --human-dir mysamples/human --fake-dir mysamples/ai --thresholds 0.5,0.7,0.9,0.95
```

Then validate the winner through the whole stack:

```bash
cd backend && PYTHONPATH=. python scripts/detect_file.py ../mysamples/ai/out.wav --expect fake
```

The benchmark exits non-zero if no model separates your data (AUC < 0.7),
which is a signal to find different samples or a different model family
rather than to ship a verdict.
