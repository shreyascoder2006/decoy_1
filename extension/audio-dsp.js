/*
 * audio-dsp.js -- pure audio maths for the live-call capture path.
 *
 * Loaded twice, deliberately:
 *   1. As the first AudioWorklet module. Worklet modules share one
 *      AudioWorkletGlobalScope, so `audio-worklet.js` reads these functions
 *      off the global scope. Worklets cannot use `import`.
 *   2. As a CommonJS module in Node, so the maths is unit tested off-browser.
 *
 * No Web Audio, DOM or Chrome API is touched here. That is the whole point.
 */
(function (scope) {
  'use strict';

  var BYTES_PER_SAMPLE = 2;

  /**
   * Average N channel buffers into one mono buffer.
   * @param {Float32Array[]} channels
   * @returns {Float32Array}
   */
  function downmixToMono(channels) {
    if (!channels || channels.length === 0) {
      return new Float32Array(0);
    }
    if (channels.length === 1) {
      // Copy so a caller cannot alias the worklet's reused input buffer.
      return new Float32Array(channels[0]);
    }
    var length = channels[0].length;
    for (var c = 1; c < channels.length; c += 1) {
      if (channels[c].length < length) {
        length = channels[c].length;
      }
    }
    var out = new Float32Array(length);
    var channelCount = channels.length;
    for (var i = 0; i < length; i += 1) {
      var sum = 0;
      for (var ch = 0; ch < channelCount; ch += 1) {
        sum += channels[ch][i];
      }
      out[i] = sum / channelCount;
    }
    return out;
  }

  /**
   * Linear-interpolating resampler that keeps its fractional read position
   * and last sample across blocks, so block boundaries do not click and the
   * output rate is exact over time.
   *
   * This genuinely resamples. It does not relabel 48 kHz data as 16 kHz.
   */
  function StreamingResampler(inputRate, outputRate) {
    if (!(inputRate > 0) || !(outputRate > 0)) {
      throw new RangeError('sample rates must be positive');
    }
    this.inputRate = inputRate;
    this.outputRate = outputRate;
    // Input samples consumed per output sample.
    this.ratio = inputRate / outputRate;
    // Read position in input-sample units, relative to the current block.
    // May be as low as -1, which reads between `prev` and input[0].
    this.pos = 0;
    this.prev = 0;
    this.started = false;
  }

  StreamingResampler.prototype.reset = function reset() {
    this.pos = 0;
    this.prev = 0;
    this.started = false;
  };

  /**
   * @param {Float32Array} input mono samples at inputRate
   * @returns {Float32Array} mono samples at outputRate
   */
  StreamingResampler.prototype.process = function process(input) {
    if (!input || input.length === 0) {
      return new Float32Array(0);
    }
    if (this.inputRate === this.outputRate && !this.started) {
      // Still go through the interpolator so block seams stay consistent.
      this.started = true;
    }
    var n = input.length;
    var ratio = this.ratio;
    // Upper bound on the output count for this block, plus slack of 1.
    var capacity = Math.ceil((n - this.pos) / ratio) + 1;
    if (capacity < 0) {
      capacity = 0;
    }
    var out = new Float32Array(capacity);
    var count = 0;
    var pos = this.pos;
    var prev = this.prev;

    while (Math.floor(pos) + 1 <= n - 1) {
      var i0 = Math.floor(pos);
      var frac = pos - i0;
      var a = i0 < 0 ? prev : input[i0];
      var b = input[i0 + 1];
      out[count] = a + (b - a) * frac;
      count += 1;
      pos += ratio;
    }

    this.prev = input[n - 1];
    this.pos = pos - n;
    this.started = true;
    return count === capacity ? out : out.subarray(0, count);
  };

  /** Clamp to the [-1, 1] range Web Audio nominally produces. */
  function clampSample(value) {
    if (!(value === value)) {
      return 0; // NaN
    }
    if (value > 1) {
      return 1;
    }
    if (value < -1) {
      return -1;
    }
    return value;
  }

  /**
   * Convert float samples to signed 16-bit little-endian PCM bytes.
   *
   * Scaling uses 32767 symmetrically, so +1.0 -> 32767 and -1.0 -> -32767.
   * DataView with an explicit little-endian flag is used rather than
   * Int16Array, so the byte order does not depend on the host CPU.
   *
   * @param {Float32Array} samples
   * @returns {ArrayBuffer} s16le bytes
   */
  function floatToPcm16Bytes(samples) {
    var length = samples ? samples.length : 0;
    var buffer = new ArrayBuffer(length * BYTES_PER_SAMPLE);
    var view = new DataView(buffer);
    for (var i = 0; i < length; i += 1) {
      var clamped = clampSample(samples[i]);
      view.setInt16(i * BYTES_PER_SAMPLE, Math.round(clamped * 32767), true);
    }
    return buffer;
  }

  /** Read s16le bytes back to floats. Used by tests and nothing else. */
  function pcm16BytesToFloat(buffer) {
    var view = new DataView(buffer);
    var count = Math.floor(buffer.byteLength / BYTES_PER_SAMPLE);
    var out = new Float32Array(count);
    for (var i = 0; i < count; i += 1) {
      out[i] = view.getInt16(i * BYTES_PER_SAMPLE, true) / 32767;
    }
    return out;
  }

  /**
   * Accumulate resampled samples and emit fixed-size PCM frames.
   * At 16 kHz, 100 ms is 1600 samples, which is 3200 bytes.
   */
  function PcmFramer(samplesPerFrame) {
    if (!(samplesPerFrame > 0)) {
      throw new RangeError('samplesPerFrame must be positive');
    }
    this.samplesPerFrame = samplesPerFrame;
    this.buffer = new Float32Array(samplesPerFrame);
    this.filled = 0;
  }

  PcmFramer.prototype.reset = function reset() {
    this.filled = 0;
  };

  /**
   * @param {Float32Array} samples
   * @returns {ArrayBuffer[]} zero or more complete frames
   */
  PcmFramer.prototype.push = function push(samples) {
    var frames = [];
    if (!samples || samples.length === 0) {
      return frames;
    }
    var offset = 0;
    while (offset < samples.length) {
      var room = this.samplesPerFrame - this.filled;
      var take = Math.min(room, samples.length - offset);
      this.buffer.set(samples.subarray(offset, offset + take), this.filled);
      this.filled += take;
      offset += take;
      if (this.filled === this.samplesPerFrame) {
        frames.push(floatToPcm16Bytes(this.buffer));
        this.filled = 0;
      }
    }
    return frames;
  };

  /**
   * Crude energy gate, used only to decide when speech first began so the
   * alert-latency clock starts at first speech rather than at Start
   * Protection. It is not a verdict and never gates the audio itself.
   */
  function SpeechGate(options) {
    var opts = options || {};
    this.threshold = opts.threshold === undefined ? 0.01 : opts.threshold;
    this.requiredSeconds =
      opts.requiredSeconds === undefined ? 0.3 : opts.requiredSeconds;
    this.sampleRate = opts.sampleRate === undefined ? 16000 : opts.sampleRate;
    this.activeSamples = 0;
    this.speechDetected = false;
  }

  SpeechGate.prototype.rms = function rms(samples) {
    if (!samples || samples.length === 0) {
      return 0;
    }
    var sum = 0;
    for (var i = 0; i < samples.length; i += 1) {
      sum += samples[i] * samples[i];
    }
    return Math.sqrt(sum / samples.length);
  };

  /**
   * @returns {boolean} true on the single block where speech is first
   *   confirmed, false every other time.
   */
  SpeechGate.prototype.push = function push(samples) {
    if (this.speechDetected || !samples || samples.length === 0) {
      return false;
    }
    if (this.rms(samples) >= this.threshold) {
      this.activeSamples += samples.length;
    } else {
      // Require contiguous-ish activity; a lone click should not count.
      this.activeSamples = 0;
    }
    if (this.activeSamples >= this.requiredSeconds * this.sampleRate) {
      this.speechDetected = true;
      return true;
    }
    return false;
  };

  var api = {
    BYTES_PER_SAMPLE: BYTES_PER_SAMPLE,
    downmixToMono: downmixToMono,
    StreamingResampler: StreamingResampler,
    clampSample: clampSample,
    floatToPcm16Bytes: floatToPcm16Bytes,
    pcm16BytesToFloat: pcm16BytesToFloat,
    PcmFramer: PcmFramer,
    SpeechGate: SpeechGate
  };

  scope.LiveCallDSP = api;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : self);
