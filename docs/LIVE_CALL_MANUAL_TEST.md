# Manual test procedure — live WhatsApp Web call

This is the test that decides whether the feature actually works. Everything
automated in this repository proves plumbing; only this procedure proves
detection on a real call.

Budget 30–40 minutes the first time.

---

## 0. Preconditions

Confirm **all** of these before you start. Each one has caused a failed demo.

- [ ] Two WhatsApp accounts you control. Call them **A** (the caller, a phone)
      and **B** (the laptop, logged in to WhatsApp Web).
- [ ] WhatsApp Web calling is actually available for account B. Open
      `https://web.whatsapp.com`, open a one-to-one chat and confirm a call
      button is present. If it is not, this test cannot run and no amount of
      code will change that.
- [ ] Chrome desktop, version 116 or later (`chrome://version`).
- [ ] A provider API key **with streaming detection access and credits**.
      Check the balance on the provider dashboard, not just that the key exists.
- [ ] Audio samples ready on a second device or in a virtual audio input:
      1. a genuine human voice recording,
      2. an AI/TTS-generated sample,
      3. ideally a voice-cloned sample of a voice **you own or have permission
         to use**.
- [ ] **Consent.** Tell the person on account A that call audio will be
      captured and sent to a third-party detection service. If anyone other
      than you is on the call, get their agreement first.

---

## 1. Start the backend

```bash
cd backend && PYTHONPATH=. ../.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In a second terminal:

```bash
curl -s http://127.0.0.1:8000/health
```

**Required:** `"provider_key_configured": true` and `"mock_mode": false`.

If `mock_mode` is `true` you are about to demonstrate a scripted result. Stop
and fix `.env`. If `provider_key_configured` is `false`, the run will correctly
report `DETECTOR_UNAVAILABLE` and produce no verdict.

Keep this terminal visible — its log is your evidence trail.

---

## 2. Prove the provider path before involving WhatsApp

Do this **first**. It isolates "the detector works" from "Chrome capture
works", and takes 15 seconds.

```bash
cd backend && PYTHONPATH=. ../.venv/bin/python scripts/local_stream_check.py --seconds 15
```

Expected, with a valid key:

```text
status  connecting_to_detector
status  ready                     ← the provider accepted the stream
verdict label=... window=0.0-4.0s ← a real analysis window completed
metrics frames=150 dropped=0 audio=15.0s
RESULT: transport, verdict delivery and latency measurement all worked.
```

Interpretation:

| What you see | What it means | Do next |
|---|---|---|
| `status ready` then a `verdict` | the provider path works end to end | continue to step 3 |
| `ERROR DETECTOR_UNAVAILABLE` | key, entitlement, credits or network | fix before going further; the server log has the reason |
| `ready` but no verdict | every window was skipped | the synthetic tone is not voice-like; this is expected for a tone, so continue |

Note the exact label the provider gave a plain tone. It is useful context for
step 7.

---

## 3. Load the extension

```bash
python3 scripts/validate_extension.py extension
```

Then:

1. `chrome://extensions` → **Developer mode** on → **Load unpacked** →
   select `extension/`.
2. Pin the extension to the toolbar.
3. Click **Service worker** under the extension card to open its console. Keep
   it open; capture errors land there.

---

## 4. Place the call

1. In Chrome, open `https://web.whatsapp.com` and make sure it is the
   **active tab**. The extension verifies the tab host and will refuse
   otherwise.
2. From account **A**, place an audio call to account **B**.
3. Answer it in WhatsApp Web.
4. Confirm you can hear account A normally **before** starting protection.
   This is your baseline.

---

## 5. Start protection

1. Click the extension icon.
2. Confirm the popup shows `Idle` and the disclosure text.
3. If a **Mock detector active** banner is showing, stop — `.env` is wrong.
4. Click **Start Protection**.

Check all four, in order:

- [ ] The popup status moves `Starting` → `Listening`.
- [ ] The badge shows `ON`.
- [ ] **You can still hear the caller.** This is the single most important
      check in the whole procedure. Chrome stops routing a captured tab's audio
      to the speakers automatically; the extension re-routes it through an
      `AudioContext`. If the call goes silent here, the playback branch is
      broken and nothing downstream matters.
- [ ] The backend log shows `created live-call session` and then a `ready`
      status, with no `DETECTOR_UNAVAILABLE`.

### Answers to the likely failures

| Symptom | Cause | Fix |
|---|---|---|
| "Open https://web.whatsapp.com in the active tab" | WhatsApp Web is not the active tab, or the URL is not visible | focus the WhatsApp Web tab and retry |
| "Could not get tab audio permission" | `getMediaStreamId` rejected the invocation | reload the extension, then click the icon and press Start in the same popup session; if it persists, add `"activeTab"` to `manifest.json` permissions |
| "Could not reach the local backend" | uvicorn is not running on 127.0.0.1:8000 | start it (step 1) |
| Call goes silent | playback branch failed | check the offscreen console via `chrome://extensions` → Inspect views |
| Status stays `Listening`, no frames in the log | capture is producing no audio | confirm the call is actually connected and audio is playing |

---

## 6. Confirm audio is actually flowing

In the backend terminal you should see the session progress, and the popup's
**Capture rate** should read something like `48.0 kHz → 16 kHz`. That field
proves the real AudioContext rate was read rather than assumed.

Then, when the session ends, the log line `session ... finished:` reports
`frames_received`, `frames_forwarded`, `frames_dropped` and
`audio_seconds_sent`. For a 60-second call with continuous speech expect
roughly 600 frames and ~60 s of audio, with `frames_dropped: 0`.

If `frames_dropped` is non-zero, the relay could not keep up; note it, because
dropped frames corrupt analysis windows.

---

## 7. Test with a synthetic voice

1. Play the AI/TTS sample into account A's microphone path.
2. Let it run for **at least 15 seconds of continuous speech**. One analysis
   window is ~4 s, and the first window is often skipped.
3. Watch for:
   - [ ] a Chrome **desktop notification**: *Potential AI-generated voice detected*
   - [ ] the badge changing to `AI` in a warning colour
   - [ ] the popup showing the label, the analysis window, and the alert latency
   - [ ] exactly **one** notification, not one per window

Record, honestly:

```text
Sample used:                    ____________________
Injection method:               acoustic / virtual device
Provider label per window:      ____________________
First-speech-to-alert latency:  ________ s   (from the popup)
Backend-reported alert latency: ________ s   (metrics log line)
Notification appeared:          yes / no
Call stayed audible throughout: yes / no
```

### If no alert appears

Diagnose in this order, and do **not** jump to "the detector failed":

1. Did any `verdict` line appear in the backend log at all?
   - **No verdicts** → the provider skipped every window. Not enough
     voice-active audio reached it. Speak longer and louder.
   - **Verdicts with `chunk_label: "skipped"`** → same conclusion.
   - **Verdicts labelled `real`** → the detector analysed the audio and did not
     find a synthetic signal. This is a genuine negative result, and it is a
     legitimate finding — report it rather than hiding it.
2. Was the sample injected acoustically? Room noise, speaker quality and
   WhatsApp's noise suppression and codec all degrade a synthetic sample.
   **A failed acoustic injection is not a detector failure.** Retry with a
   virtual audio input on the caller device if you have one.
3. Check `frames_dropped` — dropped frames corrupt windows.

---

## 8. Test with a genuine human voice

Repeat step 7 with a real person speaking.

- [ ] The popup reads **"No synthetic signal so far"** — it must **never** read
      "verified", "authentic" or "safe".
- [ ] No desktop notification.

Record the label and whether any false positive occurred.

---

## 9. Stop and verify cleanup

1. Click **Stop Protection**.
2. Check:
   - [ ] badge clears
   - [ ] popup returns to `Idle`
   - [ ] the call is **still connected and still audible** — stopping analysis
         must not touch the call
   - [ ] the backend log shows `stop requested` then `session ... finished:`
         with the metrics summary
   - [ ] `chrome://extensions` → Inspect views no longer lists the offscreen
         document
3. Hang up the call normally.

Also test the involuntary paths once:

- [ ] Close the WhatsApp Web tab mid-session → capture stops cleanly, badge clears.
- [ ] Stop uvicorn mid-session → the extension reports an error; **the call is
      unaffected**. This is the fail-open requirement: a detector outage must
      never disturb the call.

---

## 10. Acceptance checklist

Copy this into your notes and fill it in from what you observed, not from what
you expected.

- [ ] WhatsApp Web call works normally with the extension loaded
- [ ] Capture starts only after Start Protection is clicked
- [ ] Captured tab audio continues playing to the user
- [ ] Extension produced mono 16 kHz PCM and the backend received it
- [ ] The provider connection was made from the backend, not the extension
- [ ] A real provider `fake` chunk produced a visible warning on the laptop
- [ ] A `real` result was worded as "no synthetic signal detected so far"
- [ ] Null / skipped / provider-error results showed as inconclusive or unavailable
- [ ] Stop Protection ended capture and cleaned up the backend session
- [ ] First-speech-to-alert latency was measured and recorded: ______ s
- [ ] Whether the **real** provider or the **mock** was used: ____________

If the last line says "mock", the feature has **not** been demonstrated. Say so
plainly rather than describing the system as working end to end.

---

## Appendix — a repeatable test without WhatsApp

For iterating on the alert policy and the UI without burning call minutes or
provider credits, run the backend in mock mode:

```bash
cd backend && LIVE_CALL_MOCK_MODE=true PYTHONPATH=. ../.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The mock scripts the sequence `skipped → fake → fake`, which reproduces the
realistic shape of one skipped window followed by a verdict. The popup will
show a **Mock detector active** banner and every event is tagged
`provider: "mock"`, so a scripted verdict cannot be mistaken for a real one.

This exercises the whole chain except the provider. It proves nothing about
detection.
