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
- [ ] A working detector. With the default local model this needs no
      credential, but the model must be downloaded and **validated on your own
      samples first** (step 2 below). With `LIVE_CALL_PROVIDER=resemble` you
      need a key with streaming access and credits — check the balance on the
      dashboard, not just that the key exists.
- [ ] Audio samples ready:
      1. a genuine human voice recording,
      2. an AI/TTS sample from the engine you actually care about,
      3. ideally a voice-cloned sample of a voice **you own or have permission
         to use**.
      Convert each to the wire format: `ffmpeg -i in.mp3 -ar 16000 -ac 1 -c:a pcm_s16le out.wav`
- [ ] **A virtual audio device on the caller machine** (see the next section).
      This is the single biggest determinant of whether the demo works.
- [ ] **Consent.** Tell the person on account A that call audio will be
      captured and sent to a third-party detection service. If anyone other
      than you is on the call, get their agreement first.

---

## 0b. Getting the AI voice into the call

This is where most attempts fail, and the failure looks exactly like a broken
detector.

**Do not hold a speaker up to the phone.** Acoustic injection stacks room
noise, speaker colouration, WhatsApp's noise suppression and the Opus codec on
top of a signal the detector needs reasonably clean. If it then fails to flag,
you have learned nothing about the detector.

**Make the caller side a computer with a virtual audio device.** The AI audio
then enters the call digitally.

| OS | Install | Then |
|---|---|---|
| macOS | [BlackHole](https://existential.audio/blackhole/) (2ch) | System Settings → Sound. Play the sample through BlackHole as output; select BlackHole as WhatsApp's microphone |
| Windows | [VB-CABLE](https://vb-audio.com/Cable/) | Play the sample to "CABLE Input"; set "CABLE Output" as WhatsApp's microphone |
| Linux | PipeWire/PulseAudio (built in) | `pactl load-module module-null-sink sink_name=aivoice` then select the `aivoice` monitor as the input |

Caller side runs WhatsApp Web or WhatsApp Desktop on that machine; receiver
side is your laptop with the extension. Your laptop still flags an *incoming*
AI voice, which is exactly the scenario being demonstrated.

To hear the sample yourself while it feeds the call, create a multi-output
device (macOS: Audio MIDI Setup) or use VB-CABLE's "Listen to this device"
option on Windows.

---

## 1. Start the backend

```bash
cd backend && PYTHONPATH=. ../.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In a second terminal:

```bash
curl -s http://127.0.0.1:8000/health
```

**Required:** `"mock_mode": false`. If it is `true` you are about to
demonstrate a scripted result — stop and fix `.env`.

With `"provider": "local"` no key is needed. With `"provider": "resemble"`,
`"provider_key_configured"` must also be `true`, or every session will
correctly report `DETECTOR_UNAVAILABLE` and produce no verdict.

The local model downloads (~378 MB) on the first session and then loads in
about 2 s. Do that once before the demo, not during it.

Keep this terminal visible — its log is your evidence trail.

---

## 2. Prove the detector flags YOUR samples, before involving WhatsApp

Do this **first**. It isolates "the detector works" from "Chrome capture
works", and it is the step most likely to reveal a showstopper.

```bash
cd backend && PYTHONPATH=. ../.venv/bin/python scripts/detect_file.py ../mysamples/ai.wav --expect fake
```

```bash
cd backend && PYTHONPATH=. ../.venv/bin/python scripts/detect_file.py ../mysamples/human.wav --expect real
```

Use samples of at least 15 seconds: the policy needs two consecutive
synthetic windows, so the earliest possible alert is ~8 s.

| What you see | What it means | Do next |
|---|---|---|
| AI → `PASS`, human → `PASS` | the detector works on your material | continue to step 3 |
| AI → `FAIL (got real)` | **the model does not recognise your TTS engine** | run the benchmark below; do not proceed until fixed |
| human → `FAIL (got fake)` | false-alarm rate too high at this threshold | raise `LIVE_CALL_LOCAL_FAKE_THRESHOLD` |
| `ERROR DETECTOR_UNAVAILABLE` | model failed to load, or missing provider key | the server log has the reason |
| `nothing analysable` | too quiet or too short | check the sample is speech and ≥ 8 s |

If the AI sample is not flagged, find a better model for your data rather than
hoping the live call goes differently:

```bash
python3 scripts/benchmark_detectors.py --human-dir mysamples/human --fake-dir mysamples/ai --thresholds 0.5,0.7,0.9,0.95
```

This matters more than it sounds. Of the five most-downloaded community
models, **four scored AI speech lower than human speech** — see
[DETECTOR_BENCHMARK.md](DETECTOR_BENCHMARK.md). Set the winner with
`LIVE_CALL_LOCAL_MODEL_ID` and its best threshold with
`LIVE_CALL_LOCAL_FAKE_THRESHOLD`.

If no model separates your samples, say so and demo the recorded-audio path
rather than staging a live call you expect to fail.

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

## 3b. Prove the browser chain without a call

Before spending call minutes, validate capture, playback, PCM and alerting
against a tab you control.

1. Open the extension popup and tick **Capture any tab (testing)**.
2. Open a new tab and start playing your AI sample (a local file in the
   browser, or any page that plays it).
3. With that tab in front, press **Start Protection**.

Check:

- [ ] A **Testing mode** banner appears in the popup.
- [ ] You can still hear the sample playing.
- [ ] The popup **Capture rate** shows something like `48.0 kHz → 16 kHz`.
- [ ] Within ~10 s of speech, the desktop warning appears.

If this works, the browser half of the system is proven and anything that
fails later is specific to the WhatsApp call path.

**Untick Capture any tab before the real demo**, so the host check is active
again.

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

1. Did step 2 pass on this same sample? If not, that is your answer — the
   detector never recognised this voice, and the call path is irrelevant.
2. Did any `verdict` line appear in the backend log at all?
   - **No verdicts** → every window was skipped for insufficient voice
     activity. Speak longer and louder.
   - **Verdicts with `chunk_label: "skipped"`** → same conclusion.
   - **Verdicts labelled `real`** → the detector analysed the audio and did not
     find a synthetic signal. A genuine negative result; report it rather than
     hiding it.
   - **Isolated `fake` windows but no alert** → working as designed. The
     policy needs two *consecutive* synthetic windows, because a single
     window false-alarms about 10% of the time. Keep the AI voice talking
     continuously for 10–15 s.
3. Was the sample injected acoustically? Room noise, speaker quality and
   WhatsApp's noise suppression and codec all degrade a synthetic sample.
   **A failed acoustic injection is not a detector failure.** Switch to the
   virtual audio device from section 0b.
4. Check `frames_dropped` — dropped frames corrupt windows.

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
      (expect ≥ 8 s: two consecutive 4 s windows are required)
- [ ] Which detector was used: **local model** / **hosted provider** / **mock**
- [ ] Model id and threshold: ______________________ / ______
- [ ] The genuine human call raised **no** alert: yes / no
- [ ] Step 2 had already passed on these same samples: yes / no

If the detector line says "mock", the feature has **not** been demonstrated.
Say so plainly rather than describing the system as working end to end.

If the human call *did* raise an alert, report the false-positive rate you
observed. A detector that flags everyone is not a detector, and a demo that
hides this is worse than no demo.

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

This exercises the whole chain except the detector. It proves nothing about
detection.

For a repeatable test that *does* exercise the real detector, use
`scripts/detect_file.py` (step 2) rather than the mock. It runs the actual
model over a fixed file, so results are comparable run to run.
