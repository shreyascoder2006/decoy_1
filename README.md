# Live Call Voice Check — real-time AI-voice screening for WhatsApp Web

A Chrome Manifest V3 extension plus a local FastAPI backend that screen the
audio of an **active WhatsApp Web call** for synthetic-speech signals and raise
an advisory desktop warning while the call is still in progress.

> **What this does and does not claim.** The system reports that the audio it
> analysed carries signals associated with synthetic speech. It does **not**
> identify the caller, prove intent, or guarantee that every AI-generated voice
> is detected. Treat every result as advisory.

Detection runs **locally by default** — an open-source model in the backend
process, so there is no API key, no credits and no network call at detection
time. A hosted provider (Resemble streaming) and an explicitly-labelled mock
are available as alternatives behind the same interface.

---

## Status of this build

| Area | State |
|---|---|
| Backend relay, session lifecycle, alert policy | Implemented, 194 automated tests passing |
| PCM capture maths (downmix, resample, s16le, framing) | Implemented, 28 automated tests passing |
| Real-time streaming over a live WebSocket | **Verified** against a running uvicorn |
| **Local detector flagging real ElevenLabs audio** | **Verified end to end.** 24/24 AI windows flagged, alert in 8.15 s; a real WhatsApp voice note → 0/6 flagged, no alert |
| Detector model selection | **11 models measured against ElevenLabs; 8 were inverted.** The ranking *reversed* between TTS engines — see [docs/DETECTOR_BENCHMARK.md](docs/DETECTOR_BENCHMARK.md) |
| Chrome extension code (capture, playback, UI, notifications) | Implemented; **not executed** — no Chrome or display in the build environment |
| Hosted provider (Resemble) verdict | **Not verified** — no API key. The endpoint was reached and returned HTTP 401 for a dummy key, so the URL, `extra_params` encoding and Bearer auth path are exercised; only a credential is missing |
| Latency on a real WhatsApp call | **Not measured** — needs two accounts and Chrome |
| Accuracy on other engines (PlayHT, Cartesia, OpenAI TTS, targeted clones) | **Not measured.** Assume nothing transfers |
| AI audio *through* a live WhatsApp call | **Not measured** — the human side has real Opus codec, the AI side does not |

Nothing in this repository fabricates a detection verdict. With no working
detector the system reports `DETECTOR_UNAVAILABLE`; it never falls back to a
guess, and mock mode must be enabled explicitly.

---

## Architecture

```text
WhatsApp Web call in Chrome
        │
        │  user clicks "Start Protection"  (the only way capture begins)
        ▼
Chrome MV3 extension
  service-worker.js   verifies the tab host, gets a tabCapture stream ID
  offscreen.js        holds the MediaStream + AudioContext
  audio-worklet.js    mono downmix → resample to 16 kHz → s16le → 100 ms frames
        │
        │  binary WebSocket, ~3200 bytes per frame, loopback only
        ▼
FastAPI backend  (backend/app/live_call/)
  router.py           session creation + WebSocket endpoint
  session_manager.py  3-task relay with a bounded audio queue
  alert_policy.py     label → alert, needs a RUN of synthetic windows
        │
        ├── local_model_client.py   in-process model (DEFAULT, no credential)
        ├── resemble_client.py      hosted stream  ← the API key lives ONLY here
        └── resemble_client.py      MockDetectorClient (scripted, opt-in only)
        │
        ▼
~4 s analysis windows → per-window label + P(fake)
        │
        ▼
JSON events back to the extension → desktop notification + badge + popup

All three detectors implement one `DetectorClient` interface, so the alert
policy, metrics, WebSocket protocol and extension are identical whichever you
run.
```

The audio branch and the playback branch are separate, so the call stays
audible while it is being analysed:

```text
tab MediaStream ──┬── playbackGain(1.0) ── destination    ← you still hear the call
                  └── workletNode ── muteGain(0.0) ── destination
```

Chrome stops routing a captured tab's audio to the speakers automatically, so
the playback branch is **mandatory**, not an optimisation.

---

## Repository layout

```text
backend/
  app/
    main.py                     FastAPI app factory + /health
    config.py                   env-driven settings (holds the credential)
    logging_config.py           redacting log filter
    live_call/
      router.py                 POST /api/live-calls, WS /ws/live-calls/{id}
      schemas.py                typed protocol models (all three boundaries)
      session_manager.py        per-call lifecycle, backpressure, cleanup
      audio_format.py           streaming WAV header + PCM frame checks
      resemble_client.py        hosted provider adapter + labelled mock
      local_model_client.py     in-process open-source detector (default)
      label_vocab.py            normalises each model's own class names
      alert_policy.py           verdict → alert, with the run requirement
      metrics.py                latency timestamps and counters
  tests/                        194 tests
  scripts/local_stream_check.py real-time streaming integration check
  scripts/detect_file.py        run a WAV through the whole stack
  requirements-local-model.txt  torch + transformers
  requirements.txt              runtime deps
  requirements-dev.txt          + test deps
  .env.example                  copy to .env
extension/
  manifest.json                 MV3, 4 permissions, 2 narrow host permissions
  service-worker.js             coordinator: gesture, capture ID, notifications
  offscreen.html / offscreen.js capture graph + backend WebSocket
  audio-dsp.js                  pure audio maths (shared with Node tests)
  audio-worklet.js              render-thread processor
  popup.html / popup.js / styles.css
  icons/                        generated PNGs
  tests/audio-dsp.test.js       28 tests
scripts/validate_extension.py   static manifest / reference / secret checks
scripts/benchmark_detectors.py  compare detector models on your own samples
docs/LIVE_CALL_MANUAL_TEST.md   the real two-account test procedure
docs/DETECTOR_BENCHMARK.md      how the default model was chosen, and its limits
docs/WINDOWS_QUICKSTART.md      Windows setup, VB-CABLE, loading the extension
docs/HANDOFF.md                 context transfer for a new session: state, blockers, findings
```

---

## Install and run

**On Windows, follow [docs/WINDOWS_QUICKSTART.md](docs/WINDOWS_QUICKSTART.md)
instead** — it has the PowerShell equivalents, the VB-CABLE setup and the
Chrome loading steps in one place.

### 1. Backend

```bash
cd backend
python3 -m venv ../.venv
../.venv/bin/python -m pip install -r requirements-dev.txt
```

For the default local detector, install the CPU-only torch build (the default
PyPI wheel drags in several GB of unused CUDA libraries):

```bash
../.venv/bin/python -m pip install --index-url https://download.pytorch.org/whl/cpu torch
```

```bash
cd backend && ../.venv/bin/python -m pip install -r requirements-local-model.txt
```

```bash
cd backend && cp .env.example .env
```

The defaults work with no credential. The model (~378 MB) downloads on first
use and loads in about 2 s thereafter. To use the hosted provider instead, set
`LIVE_CALL_PROVIDER=resemble` and `RESEMBLE_API_KEY`.

```bash
cd backend && PYTHONPATH=. ../.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Confirm it is up and sees your credential:

```bash
curl -s http://127.0.0.1:8000/health
```

With the local detector, `provider` reads `local` and
`provider_key_configured` is irrelevant. With `resemble`,
`provider_key_configured` must be `true` or every session reports
`DETECTOR_UNAVAILABLE` — by design.

### 2. Chrome extension

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and select the `extension/` directory.
4. Pin the extension so its badge is visible during the call.

Check the manifest first if you like:

```bash
python3 scripts/validate_extension.py extension
```

---

## Required environment variables

All of these live in `backend/.env`. Never commit it; `.gitignore` excludes it.

| Variable | Default | Purpose |
|---|---|---|
| `RESEMBLE_API_KEY` | *(empty)* | Provider credential. **Server-side only.** |
| `RESEMBLE_STREAM_URL` | `wss://stream.resemble.ai/api/v1/detect/audio` | Upstream streaming endpoint |
| `LIVE_CALL_PROVIDER` | `local` | `local`, `resemble` or `mock` |
| `LIVE_CALL_LOCAL_MODEL_ID` | `Shahzaib-Arshad/deepfake_audio_detection` | 378 MB. Chosen by measurement against ElevenLabs — **re-measure for your engine** |
| `LIVE_CALL_LOCAL_FAKE_THRESHOLD` | `0.5` | Decision threshold on P(fake). Sits in the empty band between measured clusters |
| `LIVE_CALL_LOCAL_WINDOW_SECONDS` | `4.0` | Analysis window. Lower for faster alerts, noisier windows |
| `LIVE_CALL_LOCAL_MIN_RMS` | `0.005` | Voice-activity floor; quieter windows are skipped, never labelled |
| `LIVE_CALL_LOCAL_DEVICE` | `cpu` | `cpu` or `cuda` |
| `LIVE_CALL_LOCAL_LABEL_OVERRIDE` | *(empty)* | Only for a model with uninformative labels, e.g. `0=fake,1=real` |
| `LIVE_CALL_ALERT_MIN_FAKE_WINDOWS` | `2` | Consecutive synthetic windows required to warn |
| `LIVE_CALL_MOCK_MODE` | `false` | Explicit opt-in to the scripted detector |
| `LIVE_CALL_HOST` / `LIVE_CALL_PORT` | `127.0.0.1` / `8000` | Bind address |
| `LIVE_CALL_PUBLIC_BASE_URL` | *(derived)* | Override the WS URL handed to the extension |
| `LIVE_CALL_MAX_FRAME_BYTES` | `16384` | Per-frame size limit |
| `LIVE_CALL_SESSION_TIMEOUT_SECONDS` | `3600` | In-memory session TTL |
| `LIVE_CALL_MAX_QUEUED_FRAMES` | `64` | Audio queue depth before frames are dropped |
| `LIVE_CALL_MAX_DROPPED_FRAMES` | `200` | Drop budget before the session is failed |
| `LIVE_CALL_CONNECT_TIMEOUT_SECONDS` | `10` | Upstream connect timeout |
| `LIVE_CALL_READY_TIMEOUT_SECONDS` | `15` | Wait for the provider `ready` frame |
| `LIVE_CALL_FINAL_TIMEOUT_SECONDS` | `10` | Wait for the `final` result after `{"type":"end"}` |
| `LIVE_CALL_ALLOWED_ORIGINS` | `chrome-extension://*,http://127.0.0.1,http://localhost` | WebSocket origin allowlist |

The wildcard origin default exists because an unpacked extension gets a new ID
on every load. For a tighter setup, replace it with your actual
`chrome-extension://<id>`.

---

## Running the tests

```bash
cd backend && PYTHONPATH=. ../.venv/bin/python -m pytest -q
```

```bash
cd extension && npm test
```

```bash
python3 scripts/validate_extension.py extension
```

### The test ladder

Each rung isolates one failure, so you never debug four things at once.

**1. Does the detector flag your AI samples at all?** No Chrome, no WhatsApp:

```bash
cd backend && PYTHONPATH=. ../.venv/bin/python scripts/detect_file.py my-ai-sample.wav --expect fake
```

```bash
cd backend && PYTHONPATH=. ../.venv/bin/python scripts/detect_file.py my-human-sample.wav --expect real
```

If it cannot flag a clean file, it will not flag the same voice after
WhatsApp's noise suppression and Opus codec. Fix this rung first.

**2. Which model is best on your samples?**

```bash
python3 scripts/benchmark_detectors.py --human-dir mysamples/human --fake-dir mysamples/ai
```

**3. Does Chrome capture, playback and alerting work?** Tick **Capture any
tab (testing)** in the popup, open a tab playing your AI sample, and press
Start Protection. This validates the whole browser chain without spending
call minutes.

**4. The real call.** See [docs/LIVE_CALL_MANUAL_TEST.md](docs/LIVE_CALL_MANUAL_TEST.md).

### Transport-only check

```bash
cd backend && PYTHONPATH=. ../.venv/bin/python scripts/local_stream_check.py --seconds 13
```

Exit codes for both scripts: `0` verdict delivered, `1` backend unreachable,
`2` detector unavailable, `3` nothing analysable, `4` verdict did not match
`--expect`.

---

## How a verdict becomes an alert

The policy uses the provider's categorical `label` and never invents a numeric
threshold for a score whose calibration we do not control.

| Provider result | What the user sees |
|---|---|
| a `fake` window, below the run threshold | "A synthetic-speech signal appeared in 1 of the 2 windows needed to warn" — no notification |
| **2 consecutive** `fake` windows | **Potential AI-generated voice detected** — desktop notification, `AI` badge, marked *provisional* |
| a run of 4 | **Stronger synthetic-audio evidence** — upgraded once, then silent |
| further `fake` windows | state updates silently; no repeated toast |
| a `real` window | breaks the run, so scattered false positives cannot accumulate |
| `real` | "No synthetic signal detected in analysed audio so far" — **never** "caller verified" |
| `label: null` or `chunk_label: "skipped"` | "Insufficient speech — continuing analysis" |
| provider error, or no credential | "Detection unavailable" — **never** a real/fake verdict |

---

## Latency

A first verdict cannot exist before a provider analysis window (~4 s) has
completed over voice-active audio. On top of that sit capture start-up,
resampling, network transfer, inference and alert delivery.

The latency clock starts at **first detected speech** (a client-side RMS gate
sends a `speech_start` mark), not at call connection, so the number reported is
the one that matters. 5–10 s is the engineering target to test, not a
guarantee: a caller who speaks little, pauses, or has poor audio will take
longer, and some windows will be skipped entirely.

Because the policy needs two consecutive synthetic windows, the earliest
possible alert is about two window lengths — roughly **8 s** at the default
4 s window. That is the top of the 5–10 s target band, and it is a deliberate
trade: alerting on one window would false-alarm on most genuine calls (see
[docs/DETECTOR_BENCHMARK.md](docs/DETECTOR_BENCHMARK.md)). Set
`LIVE_CALL_LOCAL_WINDOW_SECONDS=3.0` for a ~6 s floor if you accept shorter,
noisier windows.

Measured end to end at 1× real time with the real samples:

```text
ElevenLabs AI    first verdict 4.25 s   first alert 8.15 s   upgrade 16.3 s
Human voice note first verdict 4.13 s   no alert raised
```

Both runs dropped zero frames, and analysis windows complete 3.6–4.2 s apart
against a 4 s cadence, so inference tracks real time with margin on 4 CPU
threads. **This is pipeline timing on one ElevenLabs preset, not an accuracy
claim about every AI voice.**

## Security and privacy

- Capture is user-initiated, never automatic, and the badge shows while active.
- A **Stop Protection** control is always available; there is no silent recording.
- The provider credential stays in `backend/.env`. It never crosses the
  backend-to-extension boundary, and `scripts/validate_extension.py` fails the
  build if a credential-shaped literal appears anywhere in the extension.
- Logs carry counts, durations and provider labels only — never PCM, never
  headers, never the key. A redacting log filter backs this up.
- No call audio is written to disk. Session state is in memory and swept on a
  timeout.
- The backend binds to loopback. The WebSocket origin allowlist rejects
  prefix lookalikes such as `http://127.0.0.1.evil.test`.
- Frames are size-limited and alignment-checked; the audio queue is bounded
  with a documented drop policy rather than unbounded buffering.
- Detector failure never interferes with the call. It cannot mute, hang up or
  block anything — it only stops reporting.

**Consent is the operator's responsibility.** Tell the other caller that audio
is being analysed, and obtain consent where your local law or institutional
rules require it. The popup carries this disclosure.

---

## What this build deliberately does not do

- No WhatsApp DOM scraping, no private WhatsApp JS APIs, no WebRTC internals,
  no encryption bypass, no call automation. Only the documented
  `chrome.tabCapture` API.
- No live-call media through Baileys — that is not something Baileys exposes.
- No video analysis, no mobile interception, no automatic answering, rejecting,
  recording or blocking.
- No speaker-identity verification.
- No transcript or social-engineering analysis.
- No database, history, accounts or cloud deployment.
- Chrome desktop only.

---

## Known limitations

1. **Tab audio is not provably caller-only.** It is whatever the WhatsApp Web
   tab renders, which can include your own side-tone or other tab sounds. The
   UI calls its input "WhatsApp Web tab audio" rather than "the caller". Verify
   experimentally before trusting a demo.
2. **Extension code is unexecuted.** It is written to the documented
   `tabCapture` + `offscreen` contract but has not run in a browser here. The
   first real run is step 6 of `docs/LIVE_CALL_MANUAL_TEST.md`.
3. **Model choice does not transfer between TTS engines.** The five models'
   ranking *reversed* between VITS and ElevenLabs; the best on one was the
   worst on the other, and inverted rather than merely weak. The shipped
   default is tuned for ElevenLabs. **For any other engine, re-run
   `scripts/benchmark_detectors.py` before trusting a verdict.**
4. **The ElevenLabs result rests on one voice and one recording** (23 AI
   windows, one preset) against 5–6 windows from one human speaker. Wide
   separation, small sample.
5. **Only the human side has been through WhatsApp's codec.** The AI sample is
   a clean MP3. A live call compresses the AI side too, and that is the
   largest remaining unknown.
6. **The hosted provider path is unverified.** Reached, authenticated-rejected
   with a dummy key, never run with a real one.
7. **WhatsApp's own noise suppression and codec** may degrade an acoustically
   injected synthetic sample enough to change the verdict. Prefer a virtual
   audio device on the caller side for repeatable tests.
8. **Linear-interpolation resampling** has no anti-aliasing filter, so content
   above 8 kHz folds back. Acceptable for 16 kHz speech from a compressed call,
   but it is a quality compromise worth knowing about.
9. **The client-side speech gate is a fixed RMS threshold** (0.01 over 0.3 s).
   It only moves the latency origin; it never gates audio or affects a verdict.
10. **Single concurrent call per backend** is what has been exercised, though the
   registry allows up to 32 sessions.
