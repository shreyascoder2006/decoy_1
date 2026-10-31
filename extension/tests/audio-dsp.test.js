/*
 * Unit tests for the capture-path audio maths.
 * Run with: node --test extension/tests/
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const DSP = require('../audio-dsp.js');

// --- downmix ------------------------------------------------------------

test('mono input passes through as a copy, not an alias', () => {
  const source = new Float32Array([0.1, -0.2, 0.3]);
  const out = DSP.downmixToMono([source]);
  assert.deepEqual(Array.from(out), [0.1, -0.2, 0.3].map(Math.fround));
  source[0] = 9;
  assert.notEqual(out[0], 9, 'output must not alias the input buffer');
});

test('stereo is averaged per sample', () => {
  const left = new Float32Array([1.0, 0.0, -1.0, 0.5]);
  const right = new Float32Array([0.0, 1.0, 1.0, 0.5]);
  const out = DSP.downmixToMono([left, right]);
  assert.equal(out.length, 4);
  assert.ok(Math.abs(out[0] - 0.5) < 1e-6);
  assert.ok(Math.abs(out[1] - 0.5) < 1e-6);
  assert.ok(Math.abs(out[2] - 0.0) < 1e-6);
  assert.ok(Math.abs(out[3] - 0.5) < 1e-6);
});

test('out-of-phase stereo cancels to silence', () => {
  const left = new Float32Array([0.7, -0.4, 0.9]);
  const right = new Float32Array([-0.7, 0.4, -0.9]);
  const out = DSP.downmixToMono([left, right]);
  for (const sample of out) {
    assert.ok(Math.abs(sample) < 1e-6);
  }
});

test('five channels are averaged', () => {
  const channels = [0.5, 0.5, 0.5, 0.5, 1.5].map((v) => new Float32Array([v]));
  const out = DSP.downmixToMono(channels);
  assert.ok(Math.abs(out[0] - 0.7) < 1e-6);
});

test('ragged channel lengths use the shortest', () => {
  const out = DSP.downmixToMono([
    new Float32Array([1, 1, 1, 1]),
    new Float32Array([1, 1])
  ]);
  assert.equal(out.length, 2);
});

test('empty channel list is handled', () => {
  assert.equal(DSP.downmixToMono([]).length, 0);
  assert.equal(DSP.downmixToMono(null).length, 0);
});

// --- resampler ----------------------------------------------------------

test('48 kHz to 16 kHz yields one third of the samples over one second', () => {
  const resampler = new DSP.StreamingResampler(48000, 16000);
  let produced = 0;
  // 375 render quanta of 128 frames == exactly 48000 input samples == 1 s.
  for (let block = 0; block < 375; block += 1) {
    produced += resampler.process(new Float32Array(128)).length;
  }
  assert.equal(produced, 16000, 'one second of 48 kHz must give 16000 samples');
});

test('44.1 kHz to 16 kHz yields the expected count within one sample', () => {
  const resampler = new DSP.StreamingResampler(44100, 16000);
  let produced = 0;
  for (let block = 0; block < 44100 / 147; block += 1) {
    produced += resampler.process(new Float32Array(147)).length;
  }
  assert.ok(
    Math.abs(produced - 16000) <= 1,
    `expected ~16000 samples, got ${produced}`
  );
});

test('16 kHz to 16 kHz is a near-exact passthrough', () => {
  const resampler = new DSP.StreamingResampler(16000, 16000);
  const input = new Float32Array(1000);
  for (let i = 0; i < input.length; i += 1) {
    input[i] = Math.sin((2 * Math.PI * 440 * i) / 16000);
  }
  const out = resampler.process(input);
  assert.ok(Math.abs(out.length - 999) <= 1);
  for (let i = 0; i < out.length; i += 1) {
    assert.ok(Math.abs(out[i] - input[i]) < 1e-5, `sample ${i} changed`);
  }
});

test('a 440 Hz tone keeps its frequency after 48k -> 16k', () => {
  const inputRate = 48000;
  const outputRate = 16000;
  const freq = 440;
  const resampler = new DSP.StreamingResampler(inputRate, outputRate);

  const collected = [];
  for (let block = 0; block < 375; block += 1) {
    const chunk = new Float32Array(128);
    for (let i = 0; i < 128; i += 1) {
      const n = block * 128 + i;
      chunk[i] = Math.sin((2 * Math.PI * freq * n) / inputRate);
    }
    const out = resampler.process(chunk);
    for (let i = 0; i < out.length; i += 1) {
      collected.push(out[i]);
    }
  }

  assert.equal(collected.length, 16000);

  // Count zero crossings: a 440 Hz tone crosses zero 880 times per second.
  let crossings = 0;
  for (let i = 1; i < collected.length; i += 1) {
    if (collected[i - 1] <= 0 && collected[i] > 0) crossings += 1;
    if (collected[i - 1] >= 0 && collected[i] < 0) crossings += 1;
  }
  assert.ok(
    Math.abs(crossings - 880) <= 4,
    `expected ~880 zero crossings for 440 Hz, got ${crossings}`
  );

  // Linear interpolation attenuates but must not destroy the signal.
  let peak = 0;
  for (const sample of collected) {
    peak = Math.max(peak, Math.abs(sample));
  }
  assert.ok(peak > 0.9, `expected a healthy peak amplitude, got ${peak}`);
});

test('resampling is continuous across block boundaries', () => {
  const inputRate = 48000;
  const whole = new DSP.StreamingResampler(inputRate, 16000);
  const split = new DSP.StreamingResampler(inputRate, 16000);

  const signal = new Float32Array(1536);
  for (let i = 0; i < signal.length; i += 1) {
    signal[i] = Math.sin((2 * Math.PI * 300 * i) / inputRate);
  }

  const wholeOut = Array.from(whole.process(signal));
  const splitOut = [];
  for (let offset = 0; offset < signal.length; offset += 128) {
    const part = split.process(signal.subarray(offset, offset + 128));
    for (let i = 0; i < part.length; i += 1) splitOut.push(part[i]);
  }

  assert.equal(splitOut.length, wholeOut.length);
  for (let i = 0; i < wholeOut.length; i += 1) {
    assert.ok(
      Math.abs(wholeOut[i] - splitOut[i]) < 1e-6,
      `block seam changed sample ${i}`
    );
  }
});

test('an upsampling ratio also works', () => {
  const resampler = new DSP.StreamingResampler(16000, 48000);
  let produced = 0;
  for (let block = 0; block < 125; block += 1) {
    produced += resampler.process(new Float32Array(128)).length;
  }
  assert.ok(Math.abs(produced - 48000) <= 2, `got ${produced}`);
});

test('empty input and bad rates are handled', () => {
  const resampler = new DSP.StreamingResampler(48000, 16000);
  assert.equal(resampler.process(new Float32Array(0)).length, 0);
  assert.equal(resampler.process(null).length, 0);
  assert.throws(() => new DSP.StreamingResampler(0, 16000), RangeError);
  assert.throws(() => new DSP.StreamingResampler(48000, -1), RangeError);
});

// --- float to PCM16 -----------------------------------------------------

function int16At(buffer, index) {
  return new DataView(buffer).getInt16(index * 2, true);
}

test('float to s16le conversion scales correctly', () => {
  const buffer = DSP.floatToPcm16Bytes(new Float32Array([0, 1, -1, 0.5, -0.5]));
  assert.equal(buffer.byteLength, 10);
  assert.equal(int16At(buffer, 0), 0);
  assert.equal(int16At(buffer, 1), 32767);
  assert.equal(int16At(buffer, 2), -32767);
  assert.equal(int16At(buffer, 3), Math.round(0.5 * 32767));
  assert.equal(int16At(buffer, 4), Math.round(-0.5 * 32767));
});

test('out-of-range samples clip instead of wrapping', () => {
  const buffer = DSP.floatToPcm16Bytes(
    new Float32Array([2.5, -2.5, 1.0001, -1.0001, Infinity, -Infinity])
  );
  assert.equal(int16At(buffer, 0), 32767);
  assert.equal(int16At(buffer, 1), -32767);
  assert.equal(int16At(buffer, 2), 32767);
  assert.equal(int16At(buffer, 3), -32767);
  assert.equal(int16At(buffer, 4), 32767, 'Infinity must clip, not wrap');
  assert.equal(int16At(buffer, 5), -32767);
});

test('NaN becomes silence rather than a wrapped value', () => {
  const buffer = DSP.floatToPcm16Bytes(new Float32Array([NaN]));
  assert.equal(int16At(buffer, 0), 0);
});

test('byte order is little-endian regardless of host CPU', () => {
  // 0x1234 == 4660; little-endian means the low byte comes first.
  const value = 4660 / 32767;
  const bytes = new Uint8Array(DSP.floatToPcm16Bytes(new Float32Array([value])));
  assert.equal(bytes[0], 0x34);
  assert.equal(bytes[1], 0x12);
});

test('round trip through PCM16 stays close to the original', () => {
  const original = new Float32Array(256);
  for (let i = 0; i < original.length; i += 1) {
    original[i] = Math.sin((2 * Math.PI * i) / 64) * 0.8;
  }
  const restored = DSP.pcm16BytesToFloat(DSP.floatToPcm16Bytes(original));
  assert.equal(restored.length, original.length);
  for (let i = 0; i < original.length; i += 1) {
    assert.ok(Math.abs(restored[i] - original[i]) < 1e-4);
  }
});

test('clampSample boundaries', () => {
  assert.equal(DSP.clampSample(0.5), 0.5);
  assert.equal(DSP.clampSample(1.5), 1);
  assert.equal(DSP.clampSample(-1.5), -1);
  assert.equal(DSP.clampSample(NaN), 0);
});

// --- framer -------------------------------------------------------------

test('framer emits exactly 3200-byte frames for 100 ms at 16 kHz', () => {
  const framer = new DSP.PcmFramer(1600);
  // 1599 samples is not yet a frame.
  assert.equal(framer.push(new Float32Array(1599)).length, 0);
  // One more completes it.
  const frames = framer.push(new Float32Array(1));
  assert.equal(frames.length, 1);
  assert.equal(frames[0].byteLength, 3200);
});

test('framer splits a large push into several whole frames', () => {
  const framer = new DSP.PcmFramer(1600);
  const frames = framer.push(new Float32Array(1600 * 3 + 400));
  assert.equal(frames.length, 3);
  for (const frame of frames) {
    assert.equal(frame.byteLength, 3200);
  }
  // The 400-sample remainder is retained, not dropped.
  assert.equal(framer.filled, 400);
  assert.equal(framer.push(new Float32Array(1200)).length, 1);
});

test('framer preserves sample order across pushes', () => {
  const framer = new DSP.PcmFramer(4);
  const frames = [
    ...framer.push(new Float32Array([0.1, 0.2])),
    ...framer.push(new Float32Array([0.3, 0.4, 0.5]))
  ];
  assert.equal(frames.length, 1);
  const restored = DSP.pcm16BytesToFloat(frames[0]);
  const expected = [0.1, 0.2, 0.3, 0.4];
  for (let i = 0; i < expected.length; i += 1) {
    assert.ok(Math.abs(restored[i] - expected[i]) < 1e-4);
  }
});

test('framer rejects a nonsense frame size', () => {
  assert.throws(() => new DSP.PcmFramer(0), RangeError);
});

// --- speech gate --------------------------------------------------------

test('silence never trips the speech gate', () => {
  const gate = new DSP.SpeechGate({ sampleRate: 16000 });
  for (let i = 0; i < 100; i += 1) {
    assert.equal(gate.push(new Float32Array(1600)), false);
  }
  assert.equal(gate.speechDetected, false);
});

test('sustained energy trips the gate exactly once', () => {
  const gate = new DSP.SpeechGate({
    sampleRate: 16000,
    threshold: 0.01,
    requiredSeconds: 0.3
  });
  const loud = new Float32Array(1600).fill(0.3);

  // 0.3 s at 16 kHz is 4800 samples == three 1600-sample blocks.
  assert.equal(gate.push(loud), false);
  assert.equal(gate.push(loud), false);
  assert.equal(gate.push(loud), true, 'should trip on the third block');
  assert.equal(gate.push(loud), false, 'must not re-trip');
  assert.equal(gate.speechDetected, true);
});

test('an isolated click does not trip the gate', () => {
  const gate = new DSP.SpeechGate({ sampleRate: 16000, requiredSeconds: 0.3 });
  const click = new Float32Array(1600);
  click[0] = 1.0; // one loud sample -> low RMS over the block
  const quiet = new Float32Array(1600);
  for (let i = 0; i < 20; i += 1) {
    assert.equal(gate.push(click), false);
    assert.equal(gate.push(quiet), false);
  }
  assert.equal(gate.speechDetected, false);
});

test('rms of a known signal', () => {
  const gate = new DSP.SpeechGate({});
  assert.ok(Math.abs(gate.rms(new Float32Array([1, -1, 1, -1])) - 1) < 1e-6);
  assert.equal(gate.rms(new Float32Array(0)), 0);
});

// --- end-to-end capture chain -------------------------------------------

test('full chain: stereo 48 kHz in, 3200-byte 16 kHz frames out', () => {
  const resampler = new DSP.StreamingResampler(48000, 16000);
  const framer = new DSP.PcmFramer(1600);
  const emitted = [];

  // One second of a stereo 48 kHz signal in 128-frame render quanta.
  for (let block = 0; block < 375; block += 1) {
    const left = new Float32Array(128);
    const right = new Float32Array(128);
    for (let i = 0; i < 128; i += 1) {
      const n = block * 128 + i;
      left[i] = Math.sin((2 * Math.PI * 300 * n) / 48000) * 0.6;
      right[i] = Math.sin((2 * Math.PI * 300 * n) / 48000) * 0.6;
    }
    const mono = DSP.downmixToMono([left, right]);
    const resampled = resampler.process(mono);
    for (const frame of framer.push(resampled)) {
      emitted.push(frame);
    }
  }

  // 1 s at 16 kHz in 100 ms frames == 10 frames of 3200 bytes.
  assert.equal(emitted.length, 10);
  for (const frame of emitted) {
    assert.equal(frame.byteLength, 3200);
  }
  // The audio must have survived, not been zeroed.
  const first = DSP.pcm16BytesToFloat(emitted[0]);
  let peak = 0;
  for (const sample of first) peak = Math.max(peak, Math.abs(sample));
  assert.ok(peak > 0.5, `expected real audio content, peak was ${peak}`);
});
