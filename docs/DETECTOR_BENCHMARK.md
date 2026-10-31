# Detector benchmark — how the default model was chosen

**Date:** 2026-10-31 · **Reproduce:** `python scripts/benchmark_detectors.py --human-dir <dir> --fake-dir <dir>`

## The headline findings

**1. Detector choice does not transfer between TTS engines, and the wrong
model is actively inverted rather than merely weak.**

Of **11 community models** measured against ElevenLabs output and a genuine
WhatsApp-codec human sample, **8 were inverted** — they scored AI speech
*lower* than human speech. The ranking against VITS was almost the reverse of
the ranking against ElevenLabs:

| Model | AUC vs VITS | AUC vs ElevenLabs |
|---|---|---|
| `Gustking/wav2vec2-large-xlsr-...` | 0.394 (inverted) | 1.000 |
| `Hemgg/Deepfake-audio-detection` | **0.889 (best)** | **0.009 (worst)** |

`Hemgg` labelled **4 of 5 windows of genuine human speech** as synthetic and
flagged **0 of 23 ElevenLabs windows**. A demo built on it would have called
the human a deepfake and the deepfake human.

**2. A high AUC does not mean a usable model.**

`WWWxp/wav2vec2_spoof_dection1` scored **AUC 0.922** — but every score, AI and
human alike, fell between 0.479 and 0.492. Perfect ordering, a 0.013-wide
band, and nowhere to put a threshold. AUC measures *ordering*;
what a live detector needs is separated *clusters*.

`scripts/benchmark_detectors.py` therefore reports the **margin** (lowest AI
score minus highest human score), ranks by it rather than by AUC, and flags
anything under 0.2 as `NO-MARGIN`.

## The chosen model

`Shahzaib-Arshad/deepfake_audio_detection` — 378 MB, Apache-licensed
wav2vec2-base fine-tune.

| | your voice (WhatsApp codec) | ElevenLabs |
|---|---|---|
| p_fake range | **0.0006 – 0.0011** | **0.784 – 0.999** |
| @ threshold 0.5 | 0/6 flagged | **24/24 flagged** |

Margin **+0.995**. It was preferred over `Gustking/wav2vec2-large-xlsr-...`,
which scored the same AUC 1.000, because it is **378 MB instead of 1262 MB**
with a wider margin (Gustking: human 0.064–0.121, AI 0.784–0.907).

---

---

## Dataset A — VITS vs LibriSpeech

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

### Dataset B — ElevenLabs vs a real WhatsApp voice note

The decisive test, because it uses the actual engine to be demonstrated and a
human sample that has **already been through WhatsApp's Opus codec**.

| | |
|---|---|
| AI | ElevenLabs ("Viraj – Dramatic Powerful and Epic" preset), 94.6 s → 23 windows |
| Human | WhatsApp voice note (`.opus`, 48 kHz → 16 kHz), 24.0 s → 5–6 windows |

Chosen model, `Gustking/wav2vec2-large-xlsr-deepfake-audio-classification`:

| | p_fake range | mean |
|---|---|---|
| Human voice note | **0.064 – 0.121** | 0.084 |
| ElevenLabs | **0.784 – 0.907** | 0.876 |

| Threshold | AI detected | Human false alarms |
|---|---|---|
| **0.5** | **23/23 (100%)** | **0/5** |
| 0.7 | 23/23 (100%) | 0/5 |
| 0.9 | 5/23 | 0/5 |

**0.5 is the default.** The two clusters are separated by an empty band from
0.121 to 0.784, so 0.5 sits in the middle with large margin on both sides —
room for codec degradation to shift scores without flipping a verdict.

Note this model declares `{0: real, 1: fake}`, the **opposite** order to three
of the others. The label map is read from the model's own `id2label`
(`label_vocab.py`); hardcoding "index 0 means fake" would have made this model
silently backwards.

---

### Threshold sweep on dataset A (for the model that won there)

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
alert policy — via `scripts/detect_file.py` with the chosen model:

| | ElevenLabs (94.6 s) | Your voice note (24.0 s) |
|---|---|---|
| Windows analysed | 24 | 6 |
| Labelled fake | **24/24** | **0/6** |
| p_fake range | 0.784 – 0.999 (mean 0.980) | 0.0006 – 0.0011 |
| **Alerts raised** | **2** (warn + upgrade) | **0** |
| Alert latency from first speech | **8.15 s** | n/a |
| Final label | `fake` | `real` |
| Frames received / dropped | 945 / **0** | 239 / **0** |

Model load 2.2 s once per process, zero dropped frames, windows completing
inside the 4 s cadence on 4 CPU threads.

## Earlier result on dataset A (kept for the transfer comparison)

Against VITS, with the model that won *there* (`Hemgg`) at threshold 0.9:

| | AI call (36.9 s) | Human call (40.0 s) |
|---|---|---|
| Labelled fake | 7/9 | 1/10 |
| Alerts raised | 2 | **0** |
| Alert latency | 8.13 s | n/a |

The human row is instructive: one window *was* falsely labelled fake and the
consecutive-run requirement suppressed it, so no alert reached the user.

## Limitations — read before trusting any of this

1. **The ElevenLabs result rests on one voice and one recording.** 24 AI
   windows from a single preset ("Dramatic Powerful and Epic") against 6
   human windows from one speaker. AUC 1.000 over ~144 pairs is encouraging,
   not a precise accuracy figure, and a dramatic/stylised preset may be easier
   to detect than a neutral conversational clone.
2. **The chosen model's human scores are ~0.001 — suspiciously confident.**
   That usually means the input is far from anything in its training
   distribution, and such models can be brittle on audio that merely
   *resembles* what they were trained on. The wide margin is the reason to
   expect it to survive a codec, not proof that it will.
2. **Only the human side has been through WhatsApp.** The voice note carries
   real Opus compression; the ElevenLabs sample is a clean MP3. A live call
   will degrade the AI side too, and that is the single largest remaining
   unknown. The 0.121→0.784 gap is wide, which is the reason to expect it to
   survive, not evidence that it does.
3. **One human speaker.** 0 false alarms out of 5–6 windows is weak evidence
   for a low false-alarm rate; the upper confidence bound is high. This is why
   `LIVE_CALL_ALERT_MIN_FAKE_WINDOWS` stays at 2. Collect more genuine
   samples, and if false alarms remain at zero, dropping it to 1 halves the
   alert latency to about 4 s.
4. **Nothing here predicts other engines.** PlayHT, Cartesia, OpenAI TTS and
   a targeted voice clone are all untested. Given how completely the ranking
   flipped between VITS and ElevenLabs, assume nothing transfers.
5. **Dataset A's human corpus is not a phone call.** LibriSpeech is clean,
   close-miked audiobook speech.
6. **A stronger model family exists but is not drop-in.** The
   `SpeechAntiSpoofingBenchmarks/*-AntiDeepfake` models (NII Yamagishi Lab,
   [arXiv:2506.21090](https://arxiv.org/abs/2506.21090), reported EER
   0.57–0.92%) are research-grade. They are **CC BY-NC-SA 4.0
   (non-commercial)**, expose no `id2label`, register as
   `Wav2Vec2ForPreTraining` with a custom pooling head whose score is the
   *real* logit, and their authors prohibit evaluation on LibriTTS- and
   MLS-derived corpora. Integrating one needs a separate adapter.

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
