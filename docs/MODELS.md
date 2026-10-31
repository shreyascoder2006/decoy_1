# Models used

Every pretrained model this project loads or measured, and what it is for.
**No model weights are stored in this repository.** GitHub rejects files over
100 MB and the runtime models are 256-361 MB each, so the repo records only each
model's Hugging Face ID. Weights download on first use into the Hugging Face
cache (`~/.cache/huggingface/hub`), or all at once with
`python scripts/download_models.py`.

## Runtime models (loaded by the backend)

| Role | Model ID | Architecture | Size on disk | Licence | Setting |
|---|---|---|---|---|---|
| **Live-call audio and voice notes**: real vs AI-generated speech | [`Shahzaib-Arshad/deepfake_audio_detection`](https://huggingface.co/Shahzaib-Arshad/deepfake_audio_detection) | wav2vec2-base fine-tune (base: `facebook/wav2vec2-base`) | 361 MB | Apache-2.0 | `LIVE_CALL_LOCAL_MODEL_ID` |
| **Gmail**: phishing wording | [`ptouch/phishing-distilbert-cyber207`](https://huggingface.co/ptouch/phishing-distilbert-cyber207) | DistilBERT, 2 classes | 256 MB | **not declared on the model card** | `EMAIL_MODEL_ID` |
| **WhatsApp chat**: scam wording | the same `ptouch/...` model, separate settings and threshold | | (shared) | **not declared on the model card** | `CHAT_MODEL_ID` |

The two text roles share one model but are configured and measured separately:
email uses threshold 0.5, chat uses 0.95. `ptouch/...` has generic labels
(`LABEL_0`/`LABEL_1`), so `EMAIL_MODEL_PHISHING_INDEXES=1` and
`CHAT_MODEL_PHISHING_INDEXES=1` state, explicitly, that index 1 means
phishing/scam. That choice was fitted on synthetic samples; see
[EMAIL_MODEL_BENCHMARK.md](EMAIL_MODEL_BENCHMARK.md).

**Licence note.** The `ptouch` model card declares no licence. Check with its
author before any use beyond a local prototype.

## Rule-based, not model-based

Email and chat findings that are *not* the model finding come from fixed rules
(`backend/app/email_check/scorer.py`): sender/brand mismatch, link text versus
destination, IP / shortened / look-alike / non-HTTPS links, risky attachments,
urgency, credential, payment and chat-scam wording. The model adds one more
finding (`MODEL_PHISHING_TEXT`), capped at 40 points, below the 55-point "high"
bar.

## Benchmarked but not used

Measured on this project's samples and rejected or set aside. Kept so the
decision can be reproduced.

**Audio** (`scripts/benchmark_detectors.py`, measured on real ElevenLabs and
WhatsApp voice-note samples; 8 of 11 models were *inverted* on that data):

| Model ID | Licence | Outcome |
|---|---|---|
| `Gustking/wav2vec2-large-xlsr-deepfake-audio-classification` (1.2 GB) | Apache-2.0 | AUC 1.000 vs ElevenLabs but 1.2 GB; replaced by the smaller, wider-margin model above |
| `Hemgg/Deepfake-audio-detection` | Apache-2.0 | Best vs VITS (0.889), **worst vs ElevenLabs (0.009)**: flagged real speech as AI |
| `mo-thecreator/Deepfake-audio-detection` | Apache-2.0 | Inverted (AUC 0.121 vs VITS) |
| `MelodyMachine/Deepfake-audio-detection-V2` | Apache-2.0 | Inverted (AUC 0.007 vs VITS) |
| `Om-Parab/distilhubert-finetuned-audio-deepfake-in-the-wild` | not declared | Inverted (AUC 0.264 vs VITS) |
| `WWWxp/wav2vec2_spoof_dection1` | Apache-2.0 | AUC 0.922 but every score in a 0.013-wide band: no usable threshold |

**Text** (`scripts/benchmark_email_models.py`, on 60 synthetic emails and 40
synthetic chat messages written for this project):

| Model ID | Licence | Outcome |
|---|---|---|
| `cybersectony/phishing-email-detection-distilbert_v2.4.1` | Apache-2.0 | 4 generic labels. Email: 11/30 false alarms; chat: 9/20 false alarms. Rejected as too noisy |
| `ealvaradob/bert-finetuned-phishing` | Apache-2.0 | Failed to load here (needs `pytorch_model.bin`); not measured |
| `mrm8488/bert-tiny-finetuned-sms-spam-detection` | not declared | Chat AUC 0.752 |
| `mariagrandury/distilbert-base-uncased-finetuned-sms-spam-detection` | Apache-2.0 | Chat AUC 0.757 |
| `niru-nny/SMS_Spam_Detection` | MIT | Chat AUC 0.790 |
| `Goodmotion/spam-mail-classifier` | Apache-2.0 | Chat AUC 0.800 |

Licences above come from each model's Hugging Face metadata on 2026-11-01.

## What these results do and do not show

- Detector choice does **not** transfer between data sources: re-measure with
  `scripts/benchmark_detectors.py` (audio) or `scripts/benchmark_email_models.py`
  (text) before trusting a model on new data or a new voice engine.
- The text samples are synthetic, written by the assistant. Run
  `scripts/mbox_to_samples.py` on your own Gmail export for a real measurement.
- Model scores are the model's own, often saturating near 0 or 1. They are not
  calibrated probabilities.
