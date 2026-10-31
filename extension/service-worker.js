/*
 * service-worker.js -- extension coordinator.
 *
 * Responsibilities:
 *   - verify the active tab really is WhatsApp Web (by URL host, never by
 *     reaching into the page's DOM or its private JavaScript)
 *   - obtain a tabCapture stream ID off the user's click gesture
 *   - create the offscreen document and hand the stream ID over
 *   - create the backend session over loopback HTTP
 *   - own the user-visible state: badge, desktop notification, popup state
 *
 * A Manifest V3 service worker can be terminated at any time, so all durable
 * state lives in chrome.storage.session, and nothing here holds a
 * MediaStream.
 */
'use strict';

import { createStateStore } from './state-store.js';

const BACKEND_ORIGIN = 'http://127.0.0.1:8000';
const OFFSCREEN_PATH = 'offscreen.html';
const WHATSAPP_HOSTS = ['web.whatsapp.com'];
const ALERT_NOTIFICATION_ID = 'live-call-synthetic-voice';
const STATE_KEY = 'liveCallState';
const TESTING_MODE_KEY = 'allowAnyTab';

const STATUS = {
  IDLE: 'Idle',
  STARTING: 'Starting',
  LISTENING: 'Listening',
  ANALYSING: 'Analysing',
  ALERT: 'Potential AI voice',
  INCONCLUSIVE: 'Inconclusive',
  ERROR: 'Error'
};

const DEFAULT_STATE = {
  status: STATUS.IDLE,
  sessionId: null,
  provider: null,
  mockMode: false,
  startedAt: null,
  speechAt: null,
  alertAt: null,
  alertLatencySeconds: null,
  firstVerdictLatencySeconds: null,
  lastLabel: null,
  lastScore: null,
  lastConsistency: null,
  lastWindow: null,
  provisional: null,
  alertCode: null,
  alertTitle: null,
  alertMessage: null,
  alertRiskScore: null,
  alertReasons: [],
  detail: null,
  errorCode: null,
  errorMessage: null,
  framesSent: 0,
  inputSampleRate: null,
  tabId: null,
  testingMode: false,
  capturedUrl: null
};

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

// Updates are serialised (see state-store.js). Backend events, the popup and the
// Start button all update this state at nearly the same moment, and an update
// that read the state a moment earlier used to overwrite a newer one, which
// erased the session id and left Stop Protection greyed out mid-call.
const stateStore = createStateStore({
  storage: chrome.storage.session,
  key: STATE_KEY,
  defaults: DEFAULT_STATE,
  // The popup may be closed; a failed broadcast is normal.
  onChange: (state) => {
    chrome.runtime.sendMessage({ target: 'popup', type: 'STATE', state }).catch(() => {});
  }
});

const readState = () => stateStore.read();
const writeState = (patch) => stateStore.write(patch);
const resetState = () => stateStore.reset();

// ---------------------------------------------------------------------------
// Badge and notifications
// ---------------------------------------------------------------------------

async function setBadge(text, colour) {
  try {
    await chrome.action.setBadgeText({ text });
    if (colour) {
      await chrome.action.setBadgeBackgroundColor({ color: colour });
    }
  } catch (error) {
    /* the action may be unavailable mid-teardown */
  }
}

async function showAlertNotification(alert) {
  try {
    await chrome.notifications.create(ALERT_NOTIFICATION_ID, {
      type: 'basic',
      iconUrl: chrome.runtime.getURL('icons/alert-128.png'),
      title: alert.title || 'Potential AI-generated voice detected',
      message:
        alert.message ||
        'The analysed audio contains synthetic-speech signals. Independently verify the caller.',
      priority: 2,
      requireInteraction: true
    });
  } catch (error) {
    console.warn('[sw] notification failed', error && error.message);
  }
}

async function clearAlertNotification() {
  try {
    await chrome.notifications.clear(ALERT_NOTIFICATION_ID);
  } catch (error) {
    /* nothing to clear */
  }
}

// ---------------------------------------------------------------------------
// Email check (Gmail)
// ---------------------------------------------------------------------------

// One relay for every page-based check. Each route names the only host allowed
// to use it, the backend path, the request timeout and the notification text.
const RELAY_ROUTES = {
  EMAIL_ANALYZE: {
    host: 'mail.google.com',
    path: '/api/email/analyze',
    timeoutMs: 8000,
    notificationId: 'decoy-email-alert',
    title: 'Possible phishing email'
  },
  CHAT_ANALYZE: {
    host: 'web.whatsapp.com',
    path: '/api/chat/analyze',
    timeoutMs: 8000,
    notificationId: 'decoy-chat-alert',
    title: 'Possible scam message on WhatsApp'
  },
  VOICE_ANALYZE: {
    host: 'web.whatsapp.com',
    path: '/api/voice/analyze',
    // The first request may load the model, and a long note has many windows.
    timeoutMs: 120000,
    notificationId: 'decoy-voice-alert',
    title: 'Possible AI-generated voice note'
  }
};
const notifiedKeys = new Set();

function base64ToBytes(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

// The content script cannot call the loopback backend itself (the page's CORS
// rules apply to it), so the worker relays the request. Each route accepts
// messages only from its own host; anything else is ignored.
async function relayAnalysis(message, sender) {
  const route = RELAY_ROUTES[message.type];
  let senderHost = '';
  try {
    senderHost = new URL(sender && sender.url ? sender.url : '').hostname;
  } catch (error) {
    /* fall through to the rejection below */
  }
  if (!route || senderHost !== route.host) {
    return { ok: false, error: `rejected: not a ${route ? route.host : 'known'} tab` };
  }

  const isVoice = message.type === 'VOICE_ANALYZE';
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), route.timeoutMs);
  try {
    const response = await fetch(`${BACKEND_ORIGIN}${route.path}`, {
      method: 'POST',
      headers: { 'Content-Type': isVoice ? 'application/octet-stream' : 'application/json' },
      body: isVoice ? base64ToBytes(message.pcmBase64) : JSON.stringify(message.payload),
      signal: controller.signal
    });
    if (!response.ok) {
      let detail = '';
      try {
        const body = await response.json();
        detail = typeof body.detail === 'string' ? `: ${body.detail}` : '';
      } catch (error) {
        /* no JSON body */
      }
      return { ok: false, error: `backend returned HTTP ${response.status}${detail}` };
    }
    const result = await response.json();
    if (result.level === 'high' && message.key && !notifiedKeys.has(message.key)) {
      notifiedKeys.add(message.key);
      await showRelayNotification(route, result);
    }
    return { ok: true, result };
  } catch (error) {
    return {
      ok: false,
      error:
        error && error.name === 'AbortError'
          ? 'the backend did not answer in time'
          : 'the backend is not reachable on 127.0.0.1:8000'
    };
  } finally {
    clearTimeout(timer);
  }
}

async function showRelayNotification(route, result) {
  // Email and chat results carry findings; voice results carry reasons.
  const main = result.findings
    ? result.findings.length
      ? result.findings[0].message
      : result.summary
    : result.reasons && result.reasons.length
      ? result.reasons[0]
      : result.summary;
  try {
    await chrome.notifications.create(route.notificationId, {
      type: 'basic',
      iconUrl: chrome.runtime.getURL('icons/alert-128.png'),
      title: route.title,
      message: `Risk score ${result.score}/100. Main reason: ${main}`,
      priority: 2
    });
  } catch (error) {
    console.warn('[sw] notification failed', error && error.message);
  }
}

// Show a live-call alert on the page itself. Best effort: in testing mode the
// captured tab may not be WhatsApp, and then it simply has no listener.
async function forwardToPage(tabId, message) {
  if (typeof tabId !== 'number') {
    return;
  }
  try {
    await chrome.tabs.sendMessage(tabId, message);
  } catch (error) {
    /* no content script in that tab */
  }
}

// ---------------------------------------------------------------------------
// Offscreen document
// ---------------------------------------------------------------------------

async function hasOffscreenDocument() {
  if (chrome.runtime.getContexts) {
    const contexts = await chrome.runtime.getContexts({
      contextTypes: ['OFFSCREEN_DOCUMENT'],
      documentUrls: [chrome.runtime.getURL(OFFSCREEN_PATH)]
    });
    return contexts.length > 0;
  }
  return false;
}

let creatingOffscreen = null;

async function ensureOffscreenDocument() {
  if (await hasOffscreenDocument()) {
    return;
  }
  if (creatingOffscreen) {
    await creatingOffscreen;
    return;
  }
  creatingOffscreen = chrome.offscreen.createDocument({
    url: OFFSCREEN_PATH,
    reasons: ['USER_MEDIA'],
    justification:
      'Hold the captured WhatsApp Web tab audio stream and convert it to PCM for local analysis.'
  });
  try {
    await creatingOffscreen;
  } finally {
    creatingOffscreen = null;
  }
}

async function closeOffscreenDocument() {
  if (await hasOffscreenDocument()) {
    try {
      await chrome.offscreen.closeDocument();
    } catch (error) {
      /* already closed */
    }
  }
}

// ---------------------------------------------------------------------------
// Tab verification
// ---------------------------------------------------------------------------

function isWhatsAppWebUrl(url) {
  if (!url) {
    return false;
  }
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'https:' && WHATSAPP_HOSTS.includes(parsed.hostname);
  } catch (error) {
    return false;
  }
}

async function isTestingModeEnabled() {
  try {
    const stored = await chrome.storage.local.get(TESTING_MODE_KEY);
    return Boolean(stored[TESTING_MODE_KEY]);
  } catch (error) {
    return false;
  }
}

async function findActiveTab() {
  // When the popup has focus, the service worker's notion of "current window"
  // is not always the window the user is looking at, so fall back to the last
  // focused window before giving up.
  let [active] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!active) {
    [active] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  }
  if (!active) {
    [active] = await chrome.tabs.query({ active: true });
  }
  return active || null;
}

function describeTab(tab) {
  // Hostname only. Enough for the user to see which tab was picked up,
  // without putting full URLs into messages or logs.
  if (!tab || !tab.url) {
    return 'a tab whose address this extension cannot read';
  }
  try {
    return new URL(tab.url).hostname || tab.url;
  } catch (error) {
    return 'an unreadable address';
  }
}

async function findTargetTab() {
  const active = await findActiveTab();
  if (!active) {
    return { error: 'No active tab was found in this window.' };
  }

  // Testing mode captures whatever tab is in front, so you can validate the
  // capture/PCM/alert chain against a tab playing a known sample before
  // spending call minutes. It needs no extra Chrome permission precisely
  // because the host check is skipped: the tab URL is never read.
  if (await isTestingModeEnabled()) {
    return { tab: active, testingMode: true };
  }

  if (isWhatsAppWebUrl(active.url)) {
    return { tab: active, testingMode: false };
  }

  return {
    error:
      `The active tab is ${describeTab(active)}, not web.whatsapp.com. ` +
      'Click the WhatsApp Web tab first, then press Start Protection — or ' +
      'tick "Capture any tab (testing)" below to capture whatever tab is in front.'
  };
}

// ---------------------------------------------------------------------------
// Backend session
// ---------------------------------------------------------------------------

async function createBackendSession() {
  const response = await fetch(`${BACKEND_ORIGIN}/api/live-calls`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      source: 'whatsapp_web_tab',
      audio_format: 'pcm_s16le',
      sample_rate: 16000,
      channels: 1
    })
  });
  if (!response.ok) {
    throw new Error(`Backend refused the session (HTTP ${response.status}).`);
  }
  return response.json();
}

// ---------------------------------------------------------------------------
// Start / stop
// ---------------------------------------------------------------------------

async function startProtection() {
  const current = await readState();
  if (current.sessionId) {
    return { ok: false, error: 'Protection is already running.' };
  }

  await clearAlertNotification();
  await resetState();
  await writeState({ status: STATUS.STARTING });
  await setBadge('...', '#2B598A');

  const { tab, testingMode, error: tabError } = await findTargetTab();
  if (tabError) {
    await failStart('NO_WHATSAPP_TAB', tabError);
    return { ok: false, error: tabError };
  }

  // The user's click on Start Protection in the popup is the gesture that
  // authorises this. Nothing is captured before it.
  let streamId;
  try {
    streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
  } catch (error) {
    const message = `Could not get tab audio permission: ${
      error && error.message ? error.message : error
    }`;
    await failStart('CAPTURE_PERMISSION', message);
    return { ok: false, error: message };
  }

  let session;
  try {
    session = await createBackendSession();
  } catch (error) {
    const message = `${
      error && error.message ? error.message : error
    } Start the backend with: uvicorn app.main:app --host 127.0.0.1 --port 8000`;
    await failStart('BACKEND_UNREACHABLE', message);
    return { ok: false, error: message };
  }

  await ensureOffscreenDocument();

  const result = await chrome.runtime.sendMessage({
    target: 'offscreen',
    type: 'START_CAPTURE',
    streamId,
    sessionId: session.session_id,
    websocketUrl: session.websocket_url
  });

  if (!result || !result.ok) {
    const message = (result && result.error) || 'Capture could not be started.';
    await failStart('CAPTURE_FAILED', message);
    await closeOffscreenDocument();
    return { ok: false, error: message };
  }

  await setBadge('ON', '#2B598A');
  const next = await writeState({
    status: STATUS.LISTENING,
    sessionId: session.session_id,
    provider: session.provider,
    mockMode: Boolean(session.mock_mode),
    startedAt: Date.now(),
    inputSampleRate: result.inputSampleRate || null,
    tabId: tab.id,
    testingMode: Boolean(testingMode),
    // Only recorded when the host check was applied, so testing mode does
    // not start logging arbitrary URLs.
    capturedUrl: testingMode ? null : 'web.whatsapp.com'
  });
  return { ok: true, state: next };
}

async function failStart(code, message) {
  await setBadge('ERR', '#B3261E');
  await writeState({
    status: STATUS.ERROR,
    errorCode: code,
    errorMessage: message,
    sessionId: null
  });
}

async function stopProtection() {
  try {
    await chrome.runtime.sendMessage({ target: 'offscreen', type: 'STOP_CAPTURE' });
  } catch (error) {
    /* the offscreen document may already be gone */
  }

  const state = await readState();
  await forwardToPage(state.tabId, { type: 'CALL_CLEAR' });
  if (state.sessionId) {
    // Best effort: release backend state even if the socket already died.
    try {
      await fetch(`${BACKEND_ORIGIN}/api/live-calls/${state.sessionId}/stop`, {
        method: 'POST'
      });
    } catch (error) {
      /* the backend may be down; the socket close already ended the stream */
    }
  }

  await closeOffscreenDocument();
  await clearAlertNotification();
  await setBadge('', '#2B598A');
  await resetState();
  return { ok: true };
}

// ---------------------------------------------------------------------------
// Backend event handling
// ---------------------------------------------------------------------------

async function handleBackendEvent(event) {
  if (!event || typeof event !== 'object') {
    return;
  }

  if (event.type === 'status') {
    await handleStatusEvent(event);
    return;
  }

  if (event.type === 'detector_result') {
    await writeState({
      lastLabel: event.label === undefined ? null : event.label,
      lastScore: event.aggregated_score === undefined ? null : event.aggregated_score,
      lastConsistency: event.consistency === undefined ? null : event.consistency,
      lastWindow:
        event.window_start_s === undefined && event.window_end_s === undefined
          ? null
          : { start: event.window_start_s, end: event.window_end_s },
      provisional: event.provisional === undefined ? null : event.provisional,
      provider: event.provider || null
    });
    return;
  }

  if (event.type === 'alert') {
    const state = await readState();
    // Notify once on the first alert; only re-notify if the code changes
    // (that is, the evidence was upgraded). Analysis windows arrive every few
    // seconds and must not each raise a toast.
    if (state.alertCode !== event.code) {
      await showAlertNotification(event);
    }
    await setBadge('AI', '#C26B0A');
    await forwardToPage(state.tabId, {
      type: 'CALL_ALERT',
      alert: {
        title: event.title,
        message: event.message,
        code: event.code,
        provisional: Boolean(event.provisional),
        riskScore: typeof event.risk_score === 'number' ? event.risk_score : null,
        reasons: Array.isArray(event.reasons) ? event.reasons : []
      }
    });
    await writeState({
      status: STATUS.ALERT,
      alertAt: Date.now(),
      alertCode: event.code,
      alertTitle: event.title,
      alertMessage: event.message,
      alertRiskScore: typeof event.risk_score === 'number' ? event.risk_score : null,
      alertReasons: Array.isArray(event.reasons) ? event.reasons : [],
      provisional: Boolean(event.provisional),
      alertLatencySeconds:
        event.latency_seconds === undefined ? null : event.latency_seconds
    });
    return;
  }

  if (event.type === 'metrics') {
    await writeState({
      firstVerdictLatencySeconds:
        event.first_verdict_latency_seconds === undefined
          ? null
          : event.first_verdict_latency_seconds,
      alertLatencySeconds:
        event.alert_latency_seconds === undefined ? null : event.alert_latency_seconds
    });
    return;
  }

  if (event.type === 'error') {
    await setBadge('ERR', '#B3261E');
    await writeState({
      status: STATUS.ERROR,
      errorCode: event.code || 'UNKNOWN',
      errorMessage: event.message || 'Detection is unavailable.',
      // An error must never leave a stale real/fake verdict on screen.
      lastLabel: null,
      lastScore: null
    });
  }
}

async function handleStatusEvent(event) {
  const status = event.status;

  if (status === 'connecting_to_detector') {
    await writeState({ status: STATUS.STARTING, detail: 'Connecting to the detector.' });
    return;
  }
  if (status === 'ready') {
    await writeState({ status: STATUS.LISTENING, detail: 'Detector ready.' });
    return;
  }
  if (status === 'analysing') {
    const state = await readState();
    if (state.status !== STATUS.ALERT) {
      await writeState({ status: STATUS.ANALYSING, detail: 'Streaming audio for analysis.' });
    }
    return;
  }
  if (status === 'inconclusive') {
    const state = await readState();
    if (state.status !== STATUS.ALERT) {
      await writeState({
        status: STATUS.INCONCLUSIVE,
        detail: event.detail || 'Insufficient speech — continuing analysis.'
      });
    }
    return;
  }
  if (status === 'no_synthetic_signal') {
    const state = await readState();
    if (state.status !== STATUS.ALERT) {
      await writeState({
        status: STATUS.ANALYSING,
        detail: event.detail || 'No synthetic signal detected in analysed audio so far.'
      });
    }
    return;
  }
  if (status === 'lagging') {
    await writeState({ detail: event.detail || 'Audio relay is lagging.' });
    return;
  }
  if (status === 'stopped') {
    await writeState({ detail: 'Detector stream closed.' });
  }
}

// ---------------------------------------------------------------------------
// Message routing
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message) {
    return false;
  }

  // From the popup
  if (message.type === 'START_PROTECTION') {
    startProtection().then(sendResponse);
    return true;
  }
  if (message.type === 'STOP_PROTECTION') {
    stopProtection().then(sendResponse);
    return true;
  }
  if (message.type === 'GET_STATE') {
    (async () => {
      const state = await readState();
      sendResponse({
        ok: true,
        state,
        testingModeEnabled: await isTestingModeEnabled()
      });
    })();
    return true;
  }
  if (message.type === 'SET_TESTING_MODE') {
    (async () => {
      await chrome.storage.local.set({
        [TESTING_MODE_KEY]: Boolean(message.enabled)
      });
      sendResponse({ ok: true, enabled: Boolean(message.enabled) });
    })();
    return true;
  }

  // From the Gmail and WhatsApp content scripts
  if (RELAY_ROUTES[message.type]) {
    relayAnalysis(message, sender).then(sendResponse);
    return true;
  }

  // From the offscreen document
  if (message.target !== 'service-worker') {
    return false;
  }

  if (message.type === 'BACKEND_EVENT') {
    handleBackendEvent(message.event).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (message.type === 'SPEECH_DETECTED') {
    writeState({ speechAt: Date.now() }).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (message.type === 'CAPTURE_READY') {
    writeState({ inputSampleRate: message.inputSampleRate }).then(() =>
      sendResponse({ ok: true })
    );
    return true;
  }
  if (message.type === 'CAPTURE_ERROR') {
    failStart('CAPTURE_ERROR', message.message || 'Capture failed.').then(() =>
      sendResponse({ ok: true })
    );
    return true;
  }
  if (message.type === 'WAITING_FOR_DETECTOR') {
    writeState({
      detail:
        'Waiting for the detector to load. If the model is not cached yet it ' +
        'is downloading now - check the backend window. Audio is held until ready.'
    }).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (message.type === 'TRANSPORT_LAG') {
    writeState({ detail: 'Audio is being dropped: the relay is not keeping up.' }).then(
      () => sendResponse({ ok: true })
    );
    return true;
  }
  if (message.type === 'CAPTURE_STOPPED') {
    (async () => {
      const state = await readState();
      // A user-initiated stop has already reset state; this covers the
      // involuntary cases (tab closed, backend died, track ended).
      if (state.sessionId && message.reason !== 'user_stop') {
        await setBadge('', '#2B598A');
        await writeState({
          status: STATUS.IDLE,
          sessionId: null,
          detail: `Capture ended (${message.reason}).`
        });
        await closeOffscreenDocument();
      }
      sendResponse({ ok: true });
    })();
    return true;
  }
  if (message.type === 'BACKEND_CLOSED') {
    return false;
  }

  return false;
});

// Clicking the warning opens nothing surprising; it just clears the toast.
chrome.notifications.onClicked.addListener(() => {
  void clearAlertNotification();
});

// If the captured tab goes away, stop cleanly.
chrome.tabs.onRemoved.addListener(async (tabId) => {
  const state = await readState();
  if (state.tabId === tabId && state.sessionId) {
    await stopProtection();
  }
});

chrome.runtime.onStartup.addListener(() => {
  void resetState();
  void setBadge('', '#2B598A');
});

chrome.runtime.onInstalled.addListener(() => {
  void resetState();
  void setBadge('', '#2B598A');
});
