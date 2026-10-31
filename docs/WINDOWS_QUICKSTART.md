# Windows quickstart

Everything you need on the laptop that will run WhatsApp Web. Expect 30–45
minutes, most of it waiting on downloads.

The extension is **not** in the Chrome Web Store. It is unpublished source in
this repository's `extension/` folder, loaded directly into Chrome in
developer mode. That is normal for an unreleased extension and nothing needs
publishing for the demo.

---

## 1. Get the code

Install [Git for Windows](https://git-scm.com/download/win) and
[Python 3.11+](https://www.python.org/downloads/windows/) if you don't have
them. **Tick "Add python.exe to PATH"** in the Python installer.

Open PowerShell:

```powershell
cd $HOME\Documents
```

```powershell
git clone --branch claude/gracious-babbage-85mqc6 https://github.com/shreyascoder2006/decoy_1.git
```

```powershell
cd decoy_1
```

No Git? Download the branch as a ZIP from
`https://github.com/shreyascoder2006/decoy_1/archive/refs/heads/claude/gracious-babbage-85mqc6.zip`
and extract it. The folder you extract is what the rest of this refers to.

---

## 2. Install the backend

```powershell
py -m venv .venv
```

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
```

```powershell
.\.venv\Scripts\python.exe -m pip install --index-url https://download.pytorch.org/whl/cpu torch
```

That one is the big download (a few hundred MB). The CPU index matters: the
default PyPI wheel pulls in several GB of CUDA libraries you will not use.

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-local-model.txt
```

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt
```

Create the config:

```powershell
Copy-Item backend\.env.example backend\.env
```

The defaults need no API key. Leave `.env` as it is unless told otherwise.

---

## 3. Start the backend

First confirm the dependencies actually landed (a cancelled pip install is
easy to miss):

```powershell
.\.venv\Scripts\python.exe -c "import torch, transformers, fastapi, uvicorn; print('deps OK', torch.__version__, transformers.__version__)"
```

If that errors, re-run the install commands from step 2 and let them finish.

### Start the server — this window stays open

```powershell
cd backend
```

```powershell
$env:PYTHONPATH="."; ..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

**This command does not return.** It keeps running and prints logs. That is
correct — the backend is a server. Wait for:

```text
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

Leave this window alone for the whole session. Its log is your evidence trail.
Closing it, or pressing Ctrl+C, stops the backend and everything else will
fail with "could not connect".

### Check it from a SECOND window

Open a **new** PowerShell window (the first one is busy running the server):

```powershell
curl.exe http://127.0.0.1:8000/health
```

You want `"provider":"local"` and `"mock_mode":false`. If `mock_mode` is
`true`, you are about to demo scripted results — fix `.env` first.

> `curl: (7) Failed to connect to 127.0.0.1:8000` means the server is not
> running. Go back to the first window and check it is still showing the
> uvicorn log.

### Pre-download the model

The detection model is **~1.3 GB** and downloads on the **first analysis
session**, not at startup — so the server will appear healthy and then stall
for minutes the first time you actually analyse something. Trigger it now,
not during your demo:

```powershell
cd backend
```

```powershell
$env:PYTHONPATH="."; ..\.venv\Scripts\python.exe scripts\detect_file.py path\to\your-ai-sample.wav --expect fake
```

Convert a sample to the wire format first if needed. Install ffmpeg with:

```powershell
winget install Gyan.FFmpeg
```

Then **close and reopen PowerShell** so the PATH change takes effect, and run:

```powershell
ffmpeg -i input.mp3 -ar 16000 -ac 1 -c:a pcm_s16le output.wav
```

---

## 4. Load the extension into Chrome

1. Open `chrome://extensions` in Chrome.
2. Turn on **Developer mode** — top-right toggle.
3. Click **Load unpacked**.
4. Select the **`extension`** folder inside the repo
   (`...\decoy_1\extension`). Select the folder itself, not a file inside it.
5. The card "Live Call Voice Check (WhatsApp Web)" appears.
6. Click the puzzle-piece icon in the toolbar and **pin** it, so the badge
   stays visible during the call.

Useful while testing: on the extension card, **Service worker** opens its
console, and **Inspect views** reaches the offscreen document. Capture errors
land in those two places, not in the page console.

After any code change, press the **reload** arrow on the extension card.

### If Chrome refuses to load it

| Message | Cause | Fix |
|---|---|---|
| "Manifest file is missing or unreadable" | wrong folder selected | select `extension`, the folder with `manifest.json` in it |
| "Could not load icon" | incomplete clone/extract | re-extract; `extension\icons\` needs 6 PNGs |
| Developer mode disabled by policy | managed/work laptop | use a personal Chrome profile, or a machine you administer |

---

## 5. Virtual audio cable (the caller machine)

This is how the AI voice gets into the call digitally instead of through a
speaker and a microphone. Acoustic injection stacks room noise, WhatsApp's
noise suppression and the Opus codec on top of the signal — and when it then
fails to flag, you have learned nothing about the detector.

1. Install [VB-CABLE](https://vb-audio.com/Cable/). **Run the installer as
   Administrator**, then **reboot**. It does not work reliably otherwise.
2. After rebooting you have two new devices: `CABLE Input` (a playback
   device) and `CABLE Output` (a recording device).
3. In the app playing your AI audio, set the **output** to `CABLE Input`.
   Per-app output: Settings → System → Sound → Volume mixer.
4. In WhatsApp on the **caller** machine, set the **microphone** to
   `CABLE Output`.
5. To hear the sample yourself while it feeds the call: Settings → System →
   Sound → More sound settings → Recording → `CABLE Output` → Properties →
   **Listen** tab → "Listen to this device".

Caller side and receiver side must be **different machines or different
WhatsApp accounts**. Your laptop runs WhatsApp Web plus the extension and
flags the *incoming* voice.

---

## 6. Order of testing

Do these in order. Each isolates one failure, so you are never debugging four
things at once.

| | Step | Proves |
|---|---|---|
| 1 | `detect_file.py` on your AI and human samples | the detector recognises *your* AI engine |
| 2 | Popup → tick **Capture any tab (testing)**, open a tab playing the AI sample, press Start Protection | Chrome capture, playback, PCM and alerting all work |
| 3 | Untick testing mode; real WhatsApp Web call | the whole thing |

**Step 2 is the one that needs no call minutes and no second account**, and it
tests the riskiest untested code: whether you can still hear audio after
capture starts. Chrome stops routing a captured tab's audio to the speakers,
and the extension re-routes it. If the audio goes silent at step 2, stop and
report it — nothing downstream matters until that works.

Full procedure: [LIVE_CALL_MANUAL_TEST.md](LIVE_CALL_MANUAL_TEST.md).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `curl: (7) Failed to connect` / "Could not reach the local backend" | the server is not running. The uvicorn command in step 3 must stay open in its own window; run `curl` from a second window |
| `cd : Cannot find path ...\backend\backend` | you are already in `backend`. Run `cd ..` first, or just skip the `cd` |
| `ffmpeg : The term 'ffmpeg' is not recognized` | ffmpeg is not installed. `winget install Gyan.FFmpeg`, then **open a new PowerShell window** so PATH refreshes. It is only needed to convert samples for offline testing — the extension does not need it |
| "Open https://web.whatsapp.com in the active tab" | WhatsApp Web is not the frontmost tab, or use testing mode |
| "Could not get tab audio permission" | reload the extension, then press Start from a freshly opened popup. If it persists, add `"activeTab"` to the `permissions` array in `extension\manifest.json` and reload |
| Call goes silent on Start | the playback branch failed — check the offscreen console via **Inspect views** |
| `py` is not recognised | Python not on PATH; reinstall with "Add python.exe to PATH" ticked, or use `python` instead of `py` |
| PowerShell blocks the venv | run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or call `.\.venv\Scripts\python.exe` directly as shown above (these commands already avoid activation) |
| Firewall prompt on first run | allow it on **private** networks only; the backend binds to loopback and needs no inbound access |
