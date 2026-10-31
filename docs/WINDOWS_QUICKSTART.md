# Setup guide: run everything locally

How to go from an empty machine to a working copy of every feature, with a
check after each step. Written for Windows 11 and PowerShell; macOS and Linux
differences are at the end. Allow 30-45 minutes, most of it downloads.

**What you will have at the end**

| Feature | Where it appears | Needs |
|---|---|---|
| **Gmail phishing check** | A result card above each email you open | backend + extension + the text model |
| **WhatsApp chat scam check** | A chip under each received message; a card for risky ones | backend + extension + the text model |
| **WhatsApp voice-note check** | A card with a score when you press play on a voice note | backend + extension + the audio model |
| **Live-call check** | A card and notification during a call (or a captured tab) | backend + extension + the audio model |

Everything runs on your own machine. Message text and audio go only to a small
server on `127.0.0.1`; nothing is sent to a cloud service and no account or API
key is needed. See [Privacy and limits](#privacy-and-limits) before using it on
real conversations.

The extension is **not** in the Chrome Web Store. It is source code in the
`extension/` folder, loaded into Chrome in developer mode.

Throughout, **Window A** is the PowerShell window that runs the backend and stays
open. **Window B** is any other PowerShell window, for everything else.

---

## 1. Prerequisites

| Need | Why | Check |
|---|---|---|
| [Git for Windows](https://git-scm.com/download/win) | get the code | `git --version` |
| [Python 3.11 or newer](https://www.python.org/downloads/windows/) | the backend. **Tick "Add python.exe to PATH"** in the installer | `py --version` |
| Google Chrome 116 or newer | the extension | `chrome://version` |
| About 2 GB free disk, plus internet for the first run | the Python packages and two models (about 620 MB) | |

You do not need Node.js, ffmpeg or an API key to run the system. (Node is only
for the extension's unit tests; ffmpeg only for converting audio samples.)

---

## 2. Get the code

**Window B:**

```powershell
cd $HOME\Documents
```

```powershell
git clone https://github.com/shreyascoder2006/decoy_1.git
```

```powershell
cd decoy_1
```

No Git? Download the repository as a ZIP from its GitHub page and extract it.
The folder you extract is "the repo" below.

**Check:** `dir` shows `backend`, `extension`, `docs` and `scripts`.

---

## 3. Install the backend

Run these in the repo folder. The virtual environment is called directly
(`.\.venv\Scripts\python.exe`) and never activated, so PowerShell's script
policy cannot block it.

```powershell
py -m venv .venv
```

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
```

Install the **CPU-only** build of PyTorch first. The default download pulls in
several GB of GPU libraries you will not use:

```powershell
.\.venv\Scripts\python.exe -m pip install --index-url https://download.pytorch.org/whl/cpu torch
```

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-local-model.txt
```

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt
```

**Check** that everything landed (a cancelled install is easy to miss):

```powershell
.\.venv\Scripts\python.exe -c "import torch, transformers, fastapi, uvicorn; print('deps OK', torch.__version__, transformers.__version__)"
```

If that errors, re-run the install commands above and let each finish.

---

## 4. Configure

```powershell
Copy-Item backend\.env.example backend\.env
```

`backend\.env` is private to your machine and is never committed. The defaults
work with no key. The settings that matter:

| Setting | Default | What it does |
|---|---|---|
| `LIVE_CALL_PROVIDER` | `local` | Use the model on this machine. Leave as is |
| `LIVE_CALL_MOCK_MODE` | `false` | **Never set to `true` for a real run.** It returns scripted verdicts, not detection |
| `LIVE_CALL_LOCAL_MODEL_ID` | `Shahzaib-Arshad/deepfake_audio_detection` | The audio model (calls and voice notes) |
| `EMAIL_MODEL_ID` | `ptouch/phishing-distilbert-cyber207` | The Gmail text model. Empty = rules only |
| `CHAT_MODEL_ID` | `ptouch/phishing-distilbert-cyber207` | The WhatsApp chat text model. Empty = rules only |
| `EMAIL_MODEL_PHISHING_INDEXES`, `CHAT_MODEL_PHISHING_INDEXES` | `1` | Which model output means "phishing". This model has generic labels, so it is stated here explicitly |
| `CHAT_MODEL_THRESHOLD` | `0.95` | How sure the chat model must be before it counts |

**Do not change a model ID without re-measuring it.** On this project, 8 of 11
audio models were *inverted* on real samples, and which model is best flipped
between voice engines. See [DETECTOR_BENCHMARK.md](DETECTOR_BENCHMARK.md),
[EMAIL_MODEL_BENCHMARK.md](EMAIL_MODEL_BENCHMARK.md) and
[MODELS.md](MODELS.md).

---

## 5. Download the models (once)

The repository stores no model weights. This fetches the two models named in
your `.env` into the Hugging Face cache (about 361 MB + 256 MB):

```powershell
.\.venv\Scripts\python.exe scripts\download_models.py
```

**Check:** it prints `ready` for the audio model and the text model, then
`All models ready.` After this the models load from disk in a couple of seconds.

If a download fails with "couldn't connect", you may have an offline flag set in
that PowerShell session. Clear it and retry:

```powershell
Remove-Item Env:HF_HUB_OFFLINE -ErrorAction SilentlyContinue
```

**Licence note:** the text model's Hugging Face page declares no licence. Fine
for a local demo; check with its author before any other use. Details in
[MODELS.md](MODELS.md).

---

## 6. Start the backend

**Window A** (stays open for the whole session):

```powershell
cd $HOME\Documents\decoy_1\backend
```

```powershell
$env:PYTHONPATH="."; ..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

This command does not return. That is correct: it is a server, and its log is
your evidence trail. Wait for `Uvicorn running on http://127.0.0.1:8000`.
Closing the window or pressing Ctrl+C stops everything.

**Check, from Window B:**

```powershell
curl.exe http://127.0.0.1:8000/health
```

You want `"provider":"local"`, `"mock_mode":false`, and the audio `model_id`
with `"model_cached":true`.

Confirm all the routes exist:

```powershell
(Invoke-RestMethod http://127.0.0.1:8000/openapi.json).paths.PSObject.Properties.Name | Where-Object { $_ -match 'email|chat|voice|live' }
```

You should see `/api/email/analyze`, `/api/chat/analyze`, `/api/voice/analyze`
and the `/api/live-calls` routes. If a route is missing, the backend is running
old code: see "Stale backend" under Troubleshooting.

A firewall prompt on first run: allow **private** networks only. The server
binds to loopback and needs no inbound access.

---

## 7. Load the extension into Chrome

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top-right).
3. Click **Load unpacked** and choose the **`extension`** folder (the folder
   containing `manifest.json`, not a file inside it).
4. A card "Live Call Voice Check (WhatsApp Web)" appears. Note its **Version**.
5. Click the puzzle-piece icon and **pin** the extension so its badge stays visible.

**Check:** the card shows no red **Errors** button.

| Chrome says | Cause | Fix |
|---|---|---|
| "Manifest file is missing or unreadable" | wrong folder | choose `extension`, the one with `manifest.json` |
| "Could not load icon" | incomplete clone or extract | re-clone or re-extract; `extension\icons\` needs 6 PNGs |
| Developer mode disabled by policy | managed or work laptop | use a personal Chrome profile or a machine you administer |

**After any code change, do both:** press the **reload** arrow on the extension
card, **and press F5 on every open Gmail and WhatsApp tab.** Chrome does not
add a new script to a tab that was already open, and this is the most common
reason a change appears to have no effect.

Useful for debugging: on the extension card, **service worker** opens the
worker's console. In a Gmail or WhatsApp tab, press F12 and filter the console
for `decoy` to see the extension's own log lines.

---

## 8. Try each feature

Do these in order. Each proves one thing, so a failure points at one cause. In
every case, **Window A's log is the ground truth** for whether the backend
received and scored the request.

### 8.1 Gmail

1. In Chrome, open `file:///` followed by the path to
   `docs/test-emails/phishing-test.html` in your repo.
2. Select everything **below the horizontal line** and copy it (Ctrl+C).
3. In Gmail, click **Compose**, address it **to yourself**, use the subject
   `Urgent: verify your account immediately`, paste the text into the body, and send.
4. Open the received email. Press F5 first if you loaded the extension after
   opening Gmail.

**Expected:** a red **HIGH RISK** card above the message with a score and
signals including "A link reads paypal.com but goes to example.net" (the
pasted link really points to a reserved test domain). The first email takes
about 12 seconds while the text model loads. Window A shows
`email checked: level=high ... model=used`.

A normal email should show a green **LOW RISK** card that says no signals were
found and explicitly does **not** call the message genuine.

### 8.2 WhatsApp chat

You need a second WhatsApp account or phone, because your own messages are
outgoing and are not scanned.

1. Open web.whatsapp.com in Chrome, sign in, and press F5.
2. Open the chat with the other account.
3. From the other account, send:

```text
Hi Mum, this is my new number. You have won the lottery, claim your prize at http://198.51.100.9/x and verify your account immediately
```

**Expected:** a compact **HIGH RISK** chip under the message and a card at the
top-right with the score and signals (a **Details** button reopens the card).
Messages with no scam signals get one quiet line, "Checked: no scam signals
found". Window A shows `chat message checked: level=high ... model=used`.

### 8.3 WhatsApp voice note

1. From the other account, send a **voice note** or an audio message.
2. On the laptop, open the chat and **press play**. The check only starts when
   you play it.

**Expected:** a card "Voice note analysis" with a score out of 100, a strip of
blocks (one per ~4 second window: red synthetic, green human-like, grey too
quiet), and evidence. A real human voice should come out **LOW RISK**. Window A
shows `voice note checked: level=... score=...`.

### 8.4 Live call (the audio path without a real call)

This tests Chrome tab capture and the live detector with no call minutes.

1. Open a Chrome tab playing some speech. Any audio page works; to try an AI
   voice, generate a clip with a text-to-speech tool and open the file in a tab.
2. With that tab in front, open the extension popup, tick **Capture any tab
   (testing)**, and press **Start Protection**. The popup should show a capture
   rate such as `48.0 kHz -> 16 kHz`.
3. Play the audio.

**Expected:** you can still **hear** the audio; the popup's `LATEST LABEL` fills
in within about 4 seconds; and for synthetic speech an alert follows after
roughly 8-10 seconds (two analysis windows are required, deliberately, to avoid
false alarms). Window A shows `window ... -> fake` and `alert
POTENTIAL_SYNTHETIC_VOICE`. If the audio goes **silent** when you press Start,
stop and report it: nothing downstream matters until that works.

To see the alert card inside WhatsApp, capture the WhatsApp tab itself (testing
mode) and play an audio message there.

**A real call.** The WhatsApp Web version you use may not take calls at all (it
may say to use the app). Check by calling the laptop's number from a phone. If
Web does not ring, use the tab-capture test above. Caller and receiver must be
different machines or accounts. The capture works only on a **browser tab**, not
on the WhatsApp desktop app.

---

## 9. Optional: measure the models on your own data

The models were chosen on small, partly synthetic sample sets, and their results
are weak and unproven on real traffic. To measure them on your own mail:

1. Export your mail with Google Takeout (Mail, labels Inbox and Spam) and
   extract the `.mbox` files somewhere outside the repo.
2. Convert them locally. This prints counts only, never message text:

```powershell
.\.venv\Scripts\python.exe scripts\mbox_to_samples.py "C:\path\to\Inbox.mbox" "C:\path\to\Spam.mbox" --per-class 200
```

3. Benchmark:

```powershell
.\.venv\Scripts\python.exe scripts\benchmark_email_models.py --samples-dir mysamples\email --show-errors
```

Your Spam folder includes ordinary junk as well as phishing, so expect some
label noise. `mysamples/`, `Takeout/` and `*.mbox` are gitignored. For audio,
`scripts\benchmark_detectors.py` does the same job with your own `.wav` samples.

---

## 10. Optional: a virtual audio cable for repeatable call tests

If you test with a real call, this routes an AI voice into the call digitally,
not through a speaker and microphone, which would add room noise and WhatsApp's
noise suppression and make a miss uninformative.

1. Install [VB-CABLE](https://vb-audio.com/Cable/). **Run the installer as
   Administrator**, then **reboot**.
2. You now have `CABLE Input` (playback) and `CABLE Output` (recording).
3. In the app playing your AI audio, set its **output** to `CABLE Input`
   (Settings, System, Sound, Volume mixer).
4. In WhatsApp on the **caller** machine, set the **microphone** to `CABLE Output`.
5. To hear it yourself: Sound settings, More sound settings, Recording,
   `CABLE Output`, Properties, Listen tab, "Listen to this device".

---

## 11. Updating later

After pulling new code, restart the backend and reload the extension:

```powershell
cd $HOME\Documents\decoy_1
```

```powershell
git pull
```

Then, in order: press **Ctrl+C** in Window A and start the backend again
(section 6), reload the extension on `chrome://extensions` (check the version
number changed), and press **F5** on your Gmail and WhatsApp tabs. If
`requirements` changed, re-run the install commands in section 3.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `curl: (7) Failed to connect` | The backend is not running. The server in Window A must stay open; run `curl` from Window B |
| **A change has no effect** | You reloaded the extension but did not press **F5** on the Gmail or WhatsApp tab, or did not restart the backend after a Python change. Do all three |
| **Stale backend** | A running server keeps the code it started with, so `git pull` does not reach it. Ctrl+C and start it again. The route check in section 6 shows what it really has |
| Port 8000 already in use | Another copy is running. Find it with `Get-NetTCPConnection -LocalPort 8000 -State Listen` and stop that process |
| No card appears in Gmail | Press F5 after loading the extension. Then F12, filter the console for `decoy`: you should see `Gmail email check loaded`. If not, the extension card may show an **Errors** button. Gmail's markup is undocumented; the selectors at the top of `extension/gmail-content.js` are the first thing to check |
| No chip in WhatsApp | Same F5 rule. The console should say `received messages in view: N`; `0` while a received message is on screen means WhatsApp changed its markup (selectors at the top of `extension/whatsapp-content.js`). Your own messages are never scanned |
| Voice note does nothing | You must **press play**. The console shows `voice: play reported by the page hook` when it is caught. If `window.__decoyAudioHookInstalled` is `undefined` in the console, the extension was not reloaded or the page not refreshed |
| First check takes about 12-15 seconds | The model loads on first use. Later checks take well under a second. `download_models.py` makes the first load fast |
| Card says "Email check unavailable" or "Not checked" | The backend is down or unreachable; start it and reopen the message |
| `model=unavailable` in Window A | The text model could not load (not downloaded, or a bad `EMAIL_MODEL_ID`). Run `download_models.py`. The rules still work and the card says so |
| No desktop notification | The extension asked Chrome for one but Windows hid it. Settings, System, Notifications: turn on **Google Chrome**, and turn off **Do not disturb**. Test from the service-worker console with `chrome.notifications.create('t',{type:'basic',iconUrl:chrome.runtime.getURL('icons/alert-128.png'),title:'Test',message:'hi'})` |
| Audio goes silent when Start Protection is pressed | The playback branch failed; open the offscreen console via the extension card's **Inspect views** and report it |
| "Could not get tab audio permission" | Reload the extension, then press Start from a freshly opened popup |
| Popup says "Open https://web.whatsapp.com in the active tab" | WhatsApp Web is not the front tab; tick **Capture any tab (testing)** or switch tabs |
| `py` is not recognised | Python is not on PATH; reinstall with "Add python.exe to PATH" ticked, or use `python` |
| PowerShell blocks the venv | The commands here avoid activation. If you want to activate: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| Red errors in the page console | Most are WhatsApp's or other extensions' own (CSP, `removeChild`). Only lines mentioning `decoy` or `whatsapp-content.js`/`gmail-content.js` are ours |

---

## Privacy and limits

**Privacy**

- Message text, links and voice audio go only to `127.0.0.1`, through the
  extension's service worker. Nothing is stored; the backend log carries counts,
  levels and rule codes, never message text or audio.
- Capture is user-initiated and shows a badge. There is no silent recording.
- **The extension reads every Gmail email you open and every received WhatsApp
  message**, and the audio of voice notes you play. There are no on/off switches
  yet. It does not use private WhatsApp APIs and does not touch the WhatsApp
  desktop app.
- **Consent is your responsibility.** Tell the other person if you capture a
  call, and follow your local law.

**Limits: read before relying on any result**

- Results are **advisory triage, not verdicts.** A low score never proves a
  message or voice is genuine, and the cards say so.
- The text models are **weak**: on synthetic samples they caught roughly half of
  phishing with some false alarms on tricky legitimate mail, and are unproven on
  real traffic. Their points are capped below the "high" bar, so the model alone
  cannot turn a result red.
- The audio model was chosen on one ElevenLabs voice and one human speaker.
  Detector choice did **not** transfer between voice engines in testing, and
  audio through a real WhatsApp call's codec has not been measured.
- Gmail's and WhatsApp's page layouts are undocumented and change; if cards
  stop appearing, the selectors are what to check.
- Chrome on desktop only; Gmail only (no Outlook); English text only, most likely.

More detail: [README.md](../README.md), [HANDOFF.md](HANDOFF.md),
[LIVE_CALL_MANUAL_TEST.md](LIVE_CALL_MANUAL_TEST.md).

---

## macOS and Linux

The same code runs; only the shell changes. From the repo folder:

```bash
python3 -m venv .venv
```

```bash
.venv/bin/python -m pip install --index-url https://download.pytorch.org/whl/cpu torch
```

```bash
.venv/bin/python -m pip install -r backend/requirements-local-model.txt -r backend/requirements-dev.txt
```

```bash
cp backend/.env.example backend/.env
```

```bash
.venv/bin/python scripts/download_models.py
```

Start the backend (keep this terminal open):

```bash
cd backend && PYTHONPATH=. ../.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Check it with `curl http://127.0.0.1:8000/health`. Loading the extension, the
feature checks, updating and troubleshooting are identical, except that the
notification setting is in your OS's notification preferences and VB-CABLE is a
Windows tool (use BlackHole on macOS or a PulseAudio null sink on Linux).
