/*
 * audio-worklet.js -- runs on the Web Audio render thread.
 *
 * Loaded AFTER audio-dsp.js via a second addModule() call. Worklet modules
 * share one AudioWorkletGlobalScope, so the DSP helpers are read off the
 * global scope here; AudioWorklet has no import() and no importScripts().
 *
 * Responsibilities, in order:
 *   1. downmix the captured tab audio to mono
 *   2. resample from the real AudioContext rate to 16 kHz
 *   3. clamp and convert to signed 16-bit little-endian PCM
 *   4. emit ~100 ms frames to the offscreen page as transferable buffers
 *
 * It never writes to its own output, so the capture branch of the graph is
 * silent and cannot feed back into playback.
 */

const DSP = globalThis.LiveCallDSP;

class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();

    if (!DSP) {
      // Surfaced to the offscreen page rather than thrown into silence.
      this.port.postMessage({
        type: 'fatal',
        reason: 'audio-dsp.js was not loaded before audio-worklet.js'
      });
      this.broken = true;
      return;
    }

    const config = (options && options.processorOptions) || {};
    this.targetSampleRate = config.targetSampleRate || 16000;
    this.frameMs = config.frameMs || 100;

    // `sampleRate` is a global in AudioWorkletGlobalScope: the context's real
    // rate. Never assume 48 kHz.
    this.inputSampleRate = sampleRate;

    this.resampler = new DSP.StreamingResampler(
      this.inputSampleRate,
      this.targetSampleRate
    );
    this.framer = new DSP.PcmFramer(
      Math.round((this.targetSampleRate * this.frameMs) / 1000)
    );
    this.gate = new DSP.SpeechGate({
      sampleRate: this.targetSampleRate,
      threshold: config.speechThreshold || 0.01,
      requiredSeconds: config.speechRequiredSeconds || 0.3
    });

    this.stopped = false;
    this.broken = false;
    this.framesEmitted = 0;

    this.port.onmessage = (event) => {
      const data = event.data || {};
      if (data.type === 'stop') {
        this.stopped = true;
      }
    };

    this.port.postMessage({
      type: 'worklet_ready',
      inputSampleRate: this.inputSampleRate,
      targetSampleRate: this.targetSampleRate,
      samplesPerFrame: this.framer.samplesPerFrame
    });
  }

  process(inputs) {
    if (this.broken) {
      return false;
    }
    if (this.stopped) {
      return false; // lets the node be garbage collected
    }

    const input = inputs[0];
    if (!input || input.length === 0) {
      return true; // capture not yet flowing; keep the processor alive
    }

    const mono = DSP.downmixToMono(input);
    const resampled = this.resampler.process(mono);
    if (resampled.length === 0) {
      return true;
    }

    // Only used to start the latency clock at first speech.
    if (this.gate.push(resampled)) {
      this.port.postMessage({ type: 'speech_start' });
    }

    const frames = this.framer.push(resampled);
    for (let i = 0; i < frames.length; i += 1) {
      this.framesEmitted += 1;
      // Transfer, do not copy: the buffer moves to the offscreen page.
      this.port.postMessage({ type: 'pcm', buffer: frames[i] }, [frames[i]]);
    }

    return true;
  }
}

registerProcessor('pcm-capture-processor', PcmCaptureProcessor);
