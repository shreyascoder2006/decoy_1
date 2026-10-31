# Live Call Voice Check — real-time AI-voice screening for WhatsApp Web

A Chrome Manifest V3 extension plus a local FastAPI backend that screen the
audio of an **active WhatsApp Web call** for synthetic-speech signals and raise
an advisory desktop warning while the call is still in progress.

> **What this does and does not claim.** The system reports that the audio it
> analysed carries signals associated with synthetic speech. It does **not**
> identify the caller, prove intent, or guarantee that every AI-generated voice
> is detected. Treat every result as advisory.

---

## Status of this build

| Area | State |
|---|---|
| Backend relay, session lifecycle, alert policy | Implemented, 105 automated tests passing |
| PCM capture maths (downmix, resample, s16le, framing) | Implemented, 28 automated tests passing |
| Real-time streaming over a live WebSocket | **Verified** against a running uvicorn (see below) |
| Chrome extension code (capture, playback, UI, notifications) | Implemented; **not executed** — no Chrome or display in the build environment |
| Real provider detection verdict | **Not verified** — no API key available. The endpoint was reached and returned HTTP 401 for a dummy key, so the URL, the `extra_params` encoding and the Bearer auth path are exercised; only a valid credential is missing |
| First-speech-to-alert latency on a real call | **Not measured** — requires two WhatsApp accounts and Chrome |

Nothing in this repository fabricates a detection verdict. With no credential
the system reports `DETECTOR_UNAVAILABLE`; it never falls back to a guess.

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
  resemble_client.py  authenticated upstream stream   ← the API key lives ONLY here
  alert_policy.py     provider label → user-facing alert
        │
        ▼
Streaming deepfake-detection provider  (~4 s analysis windows)
        │
        ▼
JSON events back to the extension → desktop notification + badge + popup
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
      resemble_client.py        real provider adapter + labelled mock
      alert_policy.py           verdict → alert mapping
      metrics.py                latency timestamps and counters
  tests/                        105 tests
  scripts/local_stream_check.py real-time streaming integration check
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
docs/LIVE_CALL_MANUAL_TEST.md   the real two-account test procedure
```

---

## Install and run

### 1. Backend

```bash
cd backend
python3 -m venv ../.venv
../.venv/bin/python -m pip install -r requirements-dev.txt
```

```bash
cd backend && cp .env.example .env
```

Then edit `.env` and set `RESEMBLE_API_KEY` to a key with streaming detection
access. Leave `LIVE_CALL_MOCK_MODE=false`.

```bash
cd backend && PYTHONPATH=. ../.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Confirm it is up and sees your credential:

```bash
curl -s http://127.0.0.1:8000/health
```

`provider_key_configured` must be `true` for real detection. If it is `false`,
every session will report `DETECTOR_UNAVAILABLE` — by design.

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
| `LIVE_CALL_PROVIDER` | `resemble` | `resemble` or `mock` |
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

Real-time streaming check against a running backend (use `LIVE_CALL_MOCK_MODE=true`
for a plumbing-only run):

```bash
cd backend && PYTHONPATH=. ../.venv/bin/python scripts/local_stream_check.py --seconds 13
```

Its exit codes: `0` verdict delivered, `1` backend unreachable,
`2` detector unavailable, `3` no verdict (too little audio).

---

## How a verdict becomes an alert

The policy uses the provider's categorical `label` and never invents a numeric
threshold for a score whose calibration we do not control.

| Provider result | What the user sees |
|---|---|
| first `fake` on a voice-active window | **Potential AI-generated voice detected** — desktop notification, `AI` badge, marked *provisional* |
| a second `fake` window | **Stronger synthetic-audio evidence** — upgraded once, then silent |
| further `fake` windows | state updates silently; no repeated toast |
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

Measured on the live streaming check in mock mode, where the first window is
deliberately scripted as `skipped`:

```text
first-verdict latency  3.95 s   (first window completed, skipped)
first-alert latency    8.00 s   (second window returned fake)
```

That shape — one skipped window then a verdict — is the realistic case, and it
lands inside the 5–10 s band. **It is not evidence about detection accuracy,**
only about the pipeline's timing.

---

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
3. **Provider verdicts are unverified.** The transport is proven; the detection
   is not.
4. **WhatsApp's own noise suppression and codec** may degrade an acoustically
   injected synthetic sample enough to change the verdict. Prefer a virtual
   audio device on the caller side for repeatable tests.
5. **Linear-interpolation resampling** has no anti-aliasing filter, so content
   above 8 kHz folds back. Acceptable for 16 kHz speech from a compressed call,
   but it is a quality compromise worth knowing about.
6. **The client-side speech gate is a fixed RMS threshold** (0.01 over 0.3 s).
   It only moves the latency origin; it never gates audio or affects a verdict.
7. **Single concurrent call per backend** is what has been exercised, though the
   registry allows up to 32 sessions.
