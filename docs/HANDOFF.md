# Handoff — read this first

Context transfer for a new Claude session picking up this project. Written
2026-10-31 by the session that built it.

---

## 1. What this is

A Chrome MV3 extension plus a local FastAPI backend that screen an **active
WhatsApp Web audio call** for AI-generated speech and raise a desktop warning
while the call is still running.

**The user's goal, in their words:** call from one of their phones to another
number logged into WhatsApp Web on their laptop; the caller's voice is
AI-generated; the laptop flags it in real time.

This is a hackathon MVP. Audio only. Advisory warning only — it does not
identify the caller or prove intent.

## 2. Operator environment

| | |
|---|---|
| OS | Windows 11, PowerShell |
| Repo | `C:\Users\Shreyas\Documents\decoy_1` |
| Branch | `claude/gracious-babbage-85mqc6` (all work lives here) |
| venv | `.\.venv\` at repo root — **never activated**, called directly as `.\.venv\Scripts\python.exe` so execution policy cannot block it |
| Backend | runs in a dedicated PowerShell window that stays open |
| AI engine | **ElevenLabs** (this matters enormously — see §5) |

Start the backend (its own window, leave running):

```powershell
cd C:\Users\Shreyas\Documents\decoy_1\backend
$env:PYTHONPATH="."; ..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Check it from a *second* window:

```powershell
curl.exe http://127.0.0.1:8000/health
```

## 3. Current status

**Verified working:**

- Backend relay, session lifecycle, alert policy — 196 tests pass
- Capture maths (downmix, resample, s16le, framing) — 28 Node tests pass
- Detector on the user's real samples: ElevenLabs 24/24 windows flagged,
  alert at 8.15 s; their own WhatsApp voice note 0/6 flagged, no alert
- **Chrome tab capture works on their machine** — the popup showed
  `CAPTURE RATE 48.0 kHz → 16 kHz`, proving the AudioWorklet runs and
  resamples from the real hardware rate

**Not verified:**

- **Whether call audio stays audible during capture.** Chrome mutes a
  captured tab and the extension re-routes it through an `AudioContext`.
  This has never been confirmed by a human. Ask early.
- Any live WhatsApp call
- Behaviour through WhatsApp's Opus codec on the **AI** side
- Any TTS engine other than ElevenLabs and VITS

## 4. THE IMMEDIATE BLOCKER

The user pressed Start Protection with testing mode on. Capture worked, but
the popup sat on **"Waiting for the detector to load"** for 1m34s with
`LATEST LABEL —`. No window was ever analysed.

**Leading hypothesis:** the backend process was still running an older
configuration pointing at `Gustking/wav2vec2-large-xlsr-...` (1.26 GB), whose
cache had been deleted — so it was silently re-downloading. A running Python
process keeps the config it started with; `git pull` does not reach it.

**Diagnose:**

```powershell
curl.exe http://127.0.0.1:8000/health
```

Expect `"model_id":"Shahzaib-Arshad/deepfake_audio_detection"` and
`"model_cached":true`. If the model id is anything else, the server did not
restart after the last pull. If `model_cached` is false, it is downloading.

The backend window is ground truth — it logs
`loading local detector model <id>` and `local detector ready in Xs`
(normally ~2 s).

## 5. Findings that must NOT be undone

These were established by measurement and cost real time. Do not change them
on intuition.

### 5a. Detector choice does not transfer between TTS engines

Of **11 community models measured on the user's own audio, 8 are INVERTED** —
they score AI speech *lower* than human speech. The ranking reversed
completely between engines:

| Model | AUC vs VITS | AUC vs ElevenLabs |
|---|---|---|
| `Gustking/wav2vec2-large-xlsr-...` | 0.394 | 1.000 |
| `Hemgg/Deepfake-audio-detection` | **0.889 (best)** | **0.009 (worst)** |

`Hemgg` was shipped as the default for a while. On the user's data it labelled
**4 of 5 windows of their genuine voice as synthetic** and flagged **0 of 23
ElevenLabs windows**. It would have called the human a deepfake and the
deepfake human.

**Never change `LIVE_CALL_LOCAL_MODEL_ID` without re-running**
`scripts/benchmark_detectors.py` on the user's samples.

### 5b. A high AUC does not mean a usable model

`WWWxp/wav2vec2_spoof_dection1` scored AUC 0.922 with every score — AI and
human — between 0.479 and 0.492. Perfect ordering, nowhere to put a
threshold. The benchmark therefore ranks by **margin** (lowest AI score minus
highest human score), not AUC, and flags anything under 0.2 as `NO-MARGIN`.

### 5c. The alert needs a RUN of consecutive synthetic windows

`LIVE_CALL_ALERT_MIN_FAKE_WINDOWS=2`. A ~10% per-window false-alarm rate is
not a 10% per-call rate: alerting on one window would flag a genuine human
with probability `1 - 0.9^15 ≈ 79%` across a one-minute call. A clean window
resets the run. This was validated — a human test call produced one false
window and zero alerts.

Cost: the earliest possible alert is ~8 s (two 4 s windows). That is the top
of the 5–10 s target band and is a deliberate trade.

### 5d. Label maps come from the model, never from an assumption

`app/live_call/label_vocab.py` reads `id2label` off the loaded model. Models
disagree: `{0: fake, 1: real}`, `{0: real, 1: fake}`, `{0: AIVoice, 1:
HumanVoice}`, `{0: bonafide, 1: spoof}`. A model with uninformative labels
(`LABEL_0`) is **refused** rather than guessed at. Hardcoding "index 0 means
fake" would silently invert some models.

### 5e. Honesty rules, non-negotiable

- Silence is never labelled — windows below the RMS floor report `skipped`
- A `real` verdict is worded "no synthetic signal detected so far", **never**
  "caller verified"
- Provider error or missing credential reports `DETECTOR_UNAVAILABLE`, never
  a real/fake guess
- A missing API key does **not** silently fall back to the mock
- **Never enable `LIVE_CALL_MOCK_MODE` for a demo.** Its verdicts are
  scripted. If a run used the mock, say so plainly.

## 6. The test ladder

Each rung isolates one failure. Do not skip ahead.

| Rung | Command / action | Proves |
|---|---|---|
| 1 | `python scripts/detect_file.py sample.wav --expect fake` | the detector recognises the user's AI engine |
| 2 | `python scripts/benchmark_detectors.py --human-dir h --fake-dir ai` | which model to use, and at what threshold |
| 3 | Extension popup → tick **Capture any tab (testing)** → play the AI sample in a tab → Start Protection | Chrome capture, playback, PCM, alerting — no call minutes |
| 4 | Real two-account WhatsApp call | the whole thing |

Rung 3 is where the user is stuck. Rung 1 passes.

## 7. Known-good numbers (regression baseline)

Full stack, 1× real time, `Shahzaib-Arshad/deepfake_audio_detection` @ 0.5:

```text
ElevenLabs (94.6 s)   24/24 windows fake   p_fake 0.784-0.999   alert 8.15 s   945 frames, 0 dropped
Voice note (24.0 s)    0/6  windows fake   p_fake 0.0006-0.0011  no alert      239 frames, 0 dropped
```

Model loads in ~2.2 s from cache. Windows complete 3.6–4.2 s apart against a
4 s cadence, so inference tracks real time with margin on 4 CPU threads.

## 8. Still unknown — be honest about these

1. **Can the user hear the call while it is being captured?** Never confirmed.
2. ElevenLabs results rest on **one voice, one preset** ("Dramatic Powerful
   and Epic"), 24 windows, against 6 windows from one human speaker.
3. Only the **human** side has been through WhatsApp's codec. The AI sample
   is a clean MP3. A live call compresses the AI side too — the largest
   remaining unknown.
4. The chosen model scores human audio at ~0.001, suspiciously confident.
   Such models can be brittle on audio that merely resembles their training
   distribution.
5. No other TTS engine tested. Assume nothing transfers.

## 9. Remaining work

1. Unblock §4, get rung 3 passing
2. Install VB-CABLE on the **caller** machine (Administrator install, then
   reboot) so AI audio enters the call digitally rather than through a
   speaker. A failed acoustic injection looks exactly like a failed detector.
3. Run the real call per `docs/LIVE_CALL_MANUAL_TEST.md`
4. Record the measured first-speech-to-alert latency honestly

## 10. Where things are

```text
backend/app/live_call/
  router.py              POST /api/live-calls, WS /ws/live-calls/{id}
  session_manager.py     per-call lifecycle, backpressure, cleanup
  local_model_client.py  in-process detector (the default)
  label_vocab.py         normalises each model's own class names
  alert_policy.py        verdict -> alert, with the run requirement
  resemble_client.py     hosted provider adapter + labelled mock
backend/scripts/
  detect_file.py         run a WAV through the whole stack
  local_stream_check.py  transport-only check
scripts/
  benchmark_detectors.py compare models on your own samples
  validate_extension.py  manifest / reference / secret checks
extension/               MV3; offscreen.js holds the capture graph
docs/
  DETECTOR_BENCHMARK.md  the measurements and their limits
  LIVE_CALL_MANUAL_TEST.md
  WINDOWS_QUICKSTART.md
```

Tests: `cd backend && PYTHONPATH=. ..\.venv\Scripts\python.exe -m pytest -q`
(196) and `cd extension && npm test` (28).

## 11. Working style that suited this user

- They are on Windows and paste commands verbatim — give **one command per
  code block**, with full paths, and say which window it belongs in.
- They lost time to a server they had not restarted and to commands run in
  the wrong directory. State both explicitly every time.
- Report negative results plainly. The most valuable finding in this project
  was discovering the shipped default was inverted on their data.
