/*
 * offscreen.js -- owns the capture graph and the backend WebSocket.
 *
 * Audio graph:
 *
 *   tab MediaStream ──┬── playbackGain (gain 1) ── destination   (you hear it)
 *                     └── workletNode ── muteGain (gain 0) ── destination
 *
 * The playback branch is mandatory: once a tab is captured, Chrome stops
 * routing that tab's audio to the speakers automatically, so without it the
 * call would go silent. The capture branch is terminated through a zero-gain
 * node purely so the graph is pulled; the worklet writes no output, so no
 * audio can loop back.
 *
 * Nothing here holds the provider credential. It only knows a loopback
 * WebSocket URL handed to it by the service worker.
 */
'use strict';

const TARGET_SAMPLE_RATE = 16000;
const FRAME_MS = 100;

const state = {
  sessionId: null,
  stream: null,
  audioContext: null,
  sourceNode: null,
  workletNode: null,
  playbackGain: null,
  muteGain: null,
  socket: null,
  running: false,
  framesSent: 0,
  bytesSent: 0,
  speechSignalled: false,
  inputSampleRate: null
};

function report(payload) {
  // Fire-and-forget: the service worker may be asleep, which is not an error.
  chrome.runtime.sendMessage({ target: 'service-worker', ...payload }).catch(() => {});
}

function log(message, detail) {
  // Never log audio payloads, only shapes and counts.
  console.log(`[offscreen] ${message}`, detail === undefined ? '' : detail);
}

// ---------------------------------------------------------------------------
// Capture
// ---------------------------------------------------------------------------

async function openTabStream(streamId) {
  // Audio only. No video constraint is requested at all, so Chrome never
  // asks for camera-adjacent capture and no frames are ever decoded.
  return navigator.mediaDevices.getUserMedia({
    audio: {
      mandatory: {
        chromeMediaSource: 'tab',
        chromeMediaSourceId: streamId
      }
    },
    video: false
  });
}

async function buildGraph(stream) {
  // No sampleRate option: take the hardware rate and resample honestly.
  const audioContext = new AudioContext();
  if (audioContext.state === 'suspended') {
    await audioContext.resume();
  }

  await audioContext.audioWorklet.addModule('audio-dsp.js');
  await audioContext.audioWorklet.addModule('audio-worklet.js');

  const sourceNode = audioContext.createMediaStreamSource(stream);

  // --- playback branch: keep the call audible -----------------------------
  const playbackGain = audioContext.createGain();
  playbackGain.gain.value = 1.0;
  sourceNode.connect(playbackGain);
  playbackGain.connect(audioContext.destination);

  // --- capture branch -----------------------------------------------------
  const workletNode = new AudioWorkletNode(audioContext, 'pcm-capture-processor', {
    numberOfInputs: 1,
    numberOfOutputs: 1,
    outputChannelCount: [1],
    processorOptions: {
      targetSampleRate: TARGET_SAMPLE_RATE,
      frameMs: FRAME_MS
    }
  });
  const muteGain = audioContext.createGain();
  muteGain.gain.value = 0.0; // guarantees the capture branch is inaudible
  sourceNode.connect(workletNode);
  workletNode.connect(muteGain);
  muteGain.connect(audioContext.destination);

  workletNode.port.onmessage = onWorkletMessage;

  return { audioContext, sourceNode, workletNode, playbackGain, muteGain };
}

function onWorkletMessage(event) {
  const data = event.data || {};

  if (data.type === 'worklet_ready') {
    state.inputSampleRate = data.inputSampleRate;
    log('worklet ready', {
      inputSampleRate: data.inputSampleRate,
      targetSampleRate: data.targetSampleRate,
      samplesPerFrame: data.samplesPerFrame
    });
    report({
      type: 'CAPTURE_READY',
      inputSampleRate: data.inputSampleRate,
      targetSampleRate: data.targetSampleRate
    });
    return;
  }

  if (data.type === 'fatal') {
    report({ type: 'CAPTURE_ERROR', message: data.reason });
    void teardown('worklet_fatal');
    return;
  }

  if (data.type === 'speech_start') {
    if (!state.speechSignalled) {
      state.speechSignalled = true;
      sendControl({ type: 'speech_start' });
      report({ type: 'SPEECH_DETECTED' });
    }
    return;
  }

  if (data.type === 'pcm' && data.buffer) {
    sendAudio(data.buffer);
  }
}

// ---------------------------------------------------------------------------
// Backend transport
// ---------------------------------------------------------------------------

function sendControl(message) {
  const socket = state.socket;
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(message));
  }
}

function sendAudio(buffer) {
  const socket = state.socket;
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    return;
  }
  // Drop rather than queue without bound if the socket is congested.
  // 10 frames == 1 s of audio; beyond that the relay is not keeping up.
  if (socket.bufferedAmount > 10 * TARGET_SAMPLE_RATE * 0.1 * 2) {
    report({ type: 'TRANSPORT_LAG', bufferedAmount: socket.bufferedAmount });
    return;
  }
  socket.send(buffer);
  state.framesSent += 1;
  state.bytesSent += buffer.byteLength;
}

function openBackendSocket(websocketUrl) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const socket = new WebSocket(websocketUrl);
    socket.binaryType = 'arraybuffer';

    const timeout = setTimeout(() => {
      if (!settled) {
        settled = true;
        socket.close();
        reject(new Error('Timed out connecting to the local backend.'));
      }
    }, 10000);

    socket.onopen = () => {
      clearTimeout(timeout);
      if (!settled) {
        settled = true;
        resolve(socket);
      }
    };

    socket.onerror = () => {
      clearTimeout(timeout);
      if (!settled) {
        settled = true;
        reject(new Error('Could not reach the local backend. Is FastAPI running?'));
      }
    };

    socket.onmessage = (event) => {
      if (typeof event.data !== 'string') {
        return;
      }
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch (error) {
        log('discarding a non-JSON backend frame');
        return;
      }
      report({ type: 'BACKEND_EVENT', event: payload });
    };

    socket.onclose = (event) => {
      log('backend socket closed', { code: event.code });
      report({ type: 'BACKEND_CLOSED', code: event.code });
      if (state.running) {
        void teardown('backend_closed');
      }
    };
  });
}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

async function start(request) {
  if (state.running) {
    return { ok: false, error: 'Capture is already running.' };
  }

  state.sessionId = request.sessionId;
  state.framesSent = 0;
  state.bytesSent = 0;
  state.speechSignalled = false;

  try {
    state.socket = await openBackendSocket(request.websocketUrl);
  } catch (error) {
    await teardown('backend_unreachable');
    return { ok: false, error: error.message };
  }

  sendControl({
    type: 'start',
    sample_rate: TARGET_SAMPLE_RATE,
    channels: 1,
    encoding: 'pcm_s16le',
    frame_ms: FRAME_MS
  });

  try {
    state.stream = await openTabStream(request.streamId);
  } catch (error) {
    await teardown('capture_failed');
    return {
      ok: false,
      error: `Tab audio capture failed: ${error && error.message ? error.message : error}`
    };
  }

  // If the captured tab is closed or stops producing audio, tear down.
  for (const track of state.stream.getTracks()) {
    track.addEventListener('ended', () => {
      log('capture track ended');
      void teardown('track_ended');
    });
  }

  try {
    const graph = await buildGraph(state.stream);
    state.audioContext = graph.audioContext;
    state.sourceNode = graph.sourceNode;
    state.workletNode = graph.workletNode;
    state.playbackGain = graph.playbackGain;
    state.muteGain = graph.muteGain;
  } catch (error) {
    await teardown('graph_failed');
    return {
      ok: false,
      error: `Audio graph setup failed: ${error && error.message ? error.message : error}`
    };
  }

  state.running = true;
  log('capture started', {
    sessionId: state.sessionId,
    contextSampleRate: state.audioContext.sampleRate
  });
  return {
    ok: true,
    inputSampleRate: state.audioContext.sampleRate
  };
}

async function teardown(reason) {
  const wasRunning = state.running;
  state.running = false;
  log('tearing down', { reason, framesSent: state.framesSent });

  // 1. Tell the backend to finish the provider stream cleanly.
  if (state.socket && state.socket.readyState === WebSocket.OPEN) {
    try {
      sendControl({ type: 'stop' });
    } catch (error) {
      /* the socket is already gone */
    }
  }

  // 2. Stop the worklet, then the graph, then the context.
  if (state.workletNode) {
    try {
      state.workletNode.port.postMessage({ type: 'stop' });
      state.workletNode.port.onmessage = null;
      state.workletNode.disconnect();
    } catch (error) {
      /* already disconnected */
    }
  }
  for (const node of [state.sourceNode, state.playbackGain, state.muteGain]) {
    if (node) {
      try {
        node.disconnect();
      } catch (error) {
        /* already disconnected */
      }
    }
  }
  if (state.stream) {
    for (const track of state.stream.getTracks()) {
      try {
        track.stop();
      } catch (error) {
        /* already stopped */
      }
    }
  }
  if (state.audioContext && state.audioContext.state !== 'closed') {
    try {
      await state.audioContext.close();
    } catch (error) {
      /* already closed */
    }
  }

  // 3. Give the backend a moment to flush, then close the socket.
  if (state.socket) {
    const socket = state.socket;
    setTimeout(() => {
      try {
        socket.onclose = null;
        socket.close();
      } catch (error) {
        /* already closed */
      }
    }, 1500);
  }

  const summary = {
    framesSent: state.framesSent,
    bytesSent: state.bytesSent,
    sessionId: state.sessionId
  };

  state.stream = null;
  state.audioContext = null;
  state.sourceNode = null;
  state.workletNode = null;
  state.playbackGain = null;
  state.muteGain = null;
  state.socket = null;
  state.sessionId = null;

  if (wasRunning) {
    report({ type: 'CAPTURE_STOPPED', reason, ...summary });
  }
  return summary;
}

// ---------------------------------------------------------------------------
// Message routing
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.target !== 'offscreen') {
    return false;
  }

  if (message.type === 'START_CAPTURE') {
    start(message).then(sendResponse);
    return true; // async response
  }

  if (message.type === 'STOP_CAPTURE') {
    teardown('user_stop').then((summary) => sendResponse({ ok: true, ...summary }));
    return true;
  }

  if (message.type === 'GET_CAPTURE_STATE') {
    sendResponse({
      ok: true,
      running: state.running,
      framesSent: state.framesSent,
      bytesSent: state.bytesSent,
      inputSampleRate: state.inputSampleRate,
      sessionId: state.sessionId
    });
    return false;
  }

  return false;
});
