/*
 * Runs in WhatsApp Web's own page context (manifest "world": "MAIN") so it can
 * see audio that is played through an element never attached to the page, which
 * the isolated content script cannot find by scanning the DOM.
 *
 * It wraps HTMLMediaElement.prototype.play and, for a blob: source, tells the
 * extension's content script the address by window.postMessage. It then calls
 * the original play() unchanged, so playback behaves exactly as before. It reads
 * no audio and sends nothing anywhere itself.
 */
(() => {
  'use strict';
  if (window.__decoyAudioHookInstalled) {
    return;
  }
  window.__decoyAudioHookInstalled = true;

  const original = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function play(...args) {
    try {
      const src = this.currentSrc || this.src || '';
      if (src.startsWith('blob:')) {
        window.postMessage({ source: 'decoy-audio-hook', src }, window.location.origin);
      }
    } catch (error) {
      /* never let the hook interfere with playback */
    }
    return original.apply(this, args);
  };
})();
