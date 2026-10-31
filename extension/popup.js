/*
 * popup.js -- the Start/Stop surface and status readout.
 *
 * The popup is a view only. It owns no capture state, because the user can
 * close it at any time while the call continues; the service worker holds the
 * state and the desktop notification is what reaches the user when the popup
 * is shut.
 */
'use strict';

const el = (id) => document.getElementById(id);

const ui = {
  statusCard: el('status-card'),
  statusValue: el('status-value'),
  statusDetail: el('status-detail'),
  mockBanner: el('mock-banner'),
  testingBanner: el('testing-banner'),
  testingMode: el('testing-mode'),
  alertBox: el('alert-box'),
  alertTitle: el('alert-title'),
  alertMessage: el('alert-message'),
  alertQualifier: el('alert-qualifier'),
  alertScore: el('alert-score'),
  alertWhy: el('alert-why'),
  alertReasons: el('alert-reasons'),
  errorBox: el('error-box'),
  errorCode: el('error-code'),
  errorMessage: el('error-message'),
  start: el('start'),
  stop: el('stop'),
  elapsed: el('m-elapsed'),
  alertLatency: el('m-alert-latency'),
  label: el('m-label'),
  score: el('m-score'),
  window: el('m-window'),
  provider: el('m-provider'),
  rate: el('m-rate'),
  provisional: el('m-provisional')
};

let elapsedTimer = null;
let currentState = null;

function formatSeconds(value) {
  if (value === null || value === undefined) {
    return '—';
  }
  return `${Number(value).toFixed(2)} s`;
}

function formatElapsed(startedAt) {
  if (!startedAt) {
    return '—';
  }
  const total = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  const minutes = String(Math.floor(total / 60)).padStart(2, '0');
  const seconds = String(total % 60).padStart(2, '0');
  return `${minutes}:${seconds}`;
}

/**
 * Human wording for a provider label. A `real` label must never read as
 * "verified" or "safe" -- only as the absence of a signal in what was
 * analysed so far.
 */
function describeLabel(label) {
  if (label === 'fake') {
    return { text: 'Synthetic-speech signal', tone: 'fake' };
  }
  if (label === 'real') {
    return { text: 'No synthetic signal so far', tone: 'real' };
  }
  if (label === null || label === undefined) {
    return { text: '—', tone: null };
  }
  return { text: String(label), tone: null };
}

function render(state) {
  currentState = state;
  const running = Boolean(state.sessionId);

  ui.statusValue.textContent = state.status || 'Idle';
  ui.statusDetail.textContent = state.detail || '';

  let tone = 'normal';
  if (state.status === 'Potential AI voice') {
    tone = 'alert';
  } else if (state.status === 'Error') {
    tone = 'error';
  }
  ui.statusCard.dataset.tone = tone;

  ui.start.disabled = running;
  ui.stop.disabled = !running;

  ui.mockBanner.hidden = !state.mockMode;
  ui.testingBanner.hidden = !state.testingMode;
  // The toggle is a setting, so it stays editable until capture starts.
  ui.testingMode.disabled = running;

  // --- alert ------------------------------------------------------------
  if (state.alertCode) {
    ui.alertBox.hidden = false;
    ui.alertTitle.textContent = state.alertTitle || 'Potential AI-generated voice detected';
    ui.alertMessage.textContent = state.alertMessage || '';
    // Two analysis windows in a row are required before the first alert, so
    // "provisional" means the minimum run was met and more audio is still coming.
    ui.alertQualifier.textContent = state.provisional
      ? 'Provisional: two analysis windows in a row were flagged; more audio is still being analysed.'
      : 'Repeated across several analysed windows.';

    const hasScore = typeof state.alertRiskScore === 'number';
    ui.alertScore.hidden = !hasScore;
    if (hasScore) {
      ui.alertScore.textContent = `Risk score ${state.alertRiskScore}/100 (the detector's own average confidence)`;
    }
    const reasons = Array.isArray(state.alertReasons) ? state.alertReasons : [];
    ui.alertWhy.hidden = reasons.length === 0;
    ui.alertReasons.replaceChildren(
      ...reasons.map((text) => {
        const item = document.createElement('li');
        item.textContent = text;
        return item;
      })
    );
  } else {
    ui.alertBox.hidden = true;
  }

  // --- error ------------------------------------------------------------
  if (state.errorCode) {
    ui.errorBox.hidden = false;
    ui.errorCode.textContent =
      state.errorCode === 'DETECTOR_UNAVAILABLE'
        ? 'Detection unavailable'
        : state.errorCode;
    ui.errorMessage.textContent = state.errorMessage || '';
  } else {
    ui.errorBox.hidden = true;
  }

  // --- metrics ----------------------------------------------------------
  ui.elapsed.textContent = formatElapsed(state.startedAt);
  ui.alertLatency.textContent = formatSeconds(
    state.alertLatencySeconds !== null && state.alertLatencySeconds !== undefined
      ? state.alertLatencySeconds
      : state.firstVerdictLatencySeconds
  );

  const described = describeLabel(state.lastLabel);
  ui.label.textContent = described.text;
  if (described.tone) {
    ui.label.dataset.tone = described.tone;
  } else {
    delete ui.label.dataset.tone;
  }

  ui.score.textContent =
    state.lastScore === null || state.lastScore === undefined
      ? '—'
      : Number(state.lastScore).toFixed(3);

  ui.window.textContent =
    state.lastWindow && state.lastWindow.start !== undefined
      ? `${Number(state.lastWindow.start).toFixed(1)}–${Number(
          state.lastWindow.end
        ).toFixed(1)} s`
      : '—';

  ui.provider.textContent = state.provider
    ? state.mockMode
      ? `${state.provider} (mock)`
      : state.provider
    : '—';

  ui.rate.textContent = state.inputSampleRate
    ? `${(state.inputSampleRate / 1000).toFixed(1)} kHz → 16 kHz`
    : '—';

  ui.provisional.textContent =
    state.provisional === null || state.provisional === undefined
      ? '—'
      : state.provisional
        ? 'Provisional'
        : 'Corroborated';

  // Only tick the clock while a session is live.
  if (running && !elapsedTimer) {
    elapsedTimer = setInterval(() => {
      if (currentState) {
        ui.elapsed.textContent = formatElapsed(currentState.startedAt);
      }
    }, 1000);
  } else if (!running && elapsedTimer) {
    clearInterval(elapsedTimer);
    elapsedTimer = null;
  }
}

async function refresh() {
  try {
    const response = await chrome.runtime.sendMessage({ type: 'GET_STATE' });
    if (response && response.ok) {
      ui.testingMode.checked = Boolean(response.testingModeEnabled);
      render(response.state);
    }
  } catch (error) {
    ui.statusDetail.textContent = 'Could not reach the extension service worker.';
  }
}

ui.start.addEventListener('click', async () => {
  ui.start.disabled = true;
  ui.statusValue.textContent = 'Starting';
  ui.statusDetail.textContent = 'Requesting tab audio capture…';
  try {
    // The click itself is the user gesture that authorises tab capture.
    const response = await chrome.runtime.sendMessage({ type: 'START_PROTECTION' });
    if (!response || !response.ok) {
      ui.statusValue.textContent = 'Error';
      ui.statusDetail.textContent = (response && response.error) || 'Could not start.';
    }
  } catch (error) {
    ui.statusValue.textContent = 'Error';
    ui.statusDetail.textContent = error && error.message ? error.message : String(error);
  }
  await refresh();
});

ui.stop.addEventListener('click', async () => {
  ui.stop.disabled = true;
  ui.statusDetail.textContent = 'Stopping capture…';
  try {
    await chrome.runtime.sendMessage({ type: 'STOP_PROTECTION' });
  } catch (error) {
    /* the service worker may have already torn down */
  }
  await refresh();
});

ui.testingMode.addEventListener('change', async () => {
  try {
    await chrome.runtime.sendMessage({
      type: 'SET_TESTING_MODE',
      enabled: ui.testingMode.checked
    });
  } catch (error) {
    ui.statusDetail.textContent = 'Could not save the testing-mode setting.';
  }
});

chrome.runtime.onMessage.addListener((message) => {
  if (message && message.target === 'popup' && message.type === 'STATE') {
    render(message.state);
  }
  return false;
});

window.addEventListener('unload', () => {
  if (elapsedTimer) {
    clearInterval(elapsedTimer);
  }
});

void refresh();
