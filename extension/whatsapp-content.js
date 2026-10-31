/*
 * WhatsApp Web content script. Three jobs, all advisory:
 *
 *   1. Chat text   - scores each received message for scam signals and shows a
 *                    banner under it (medium and high only) plus a toast (high).
 *   2. Voice notes - when a voice note is played, fetches its audio from the
 *                    page, decodes it to 16 kHz mono and has the local detector
 *                    score it. The result shows as a toast.
 *   3. Live calls  - shows the call alert (score and reasons) sent by the
 *                    service worker while Start Protection is running.
 *
 * WhatsApp Web's markup is undocumented and changes. The selectors below are
 * the likeliest breakage points. Everything the page supplies is inserted with
 * textContent, never innerHTML. Message text and voice audio go only to the
 * local backend on 127.0.0.1, through the service worker.
 */
(() => {
  'use strict';

  console.info('[decoy] WhatsApp check loaded');

  const SELECTORS = {
    text: 'span.selectable-text',
    meta: '[data-pre-plain-text]',
    bubble: 'div.copyable-text',
    audio: 'audio'
  };
  const MAX_BODY = 20000;
  const MAX_LINKS = 200;
  const MAX_VOICE_SECONDS = 300;
  const TARGET_RATE = 16000;
  const DEBOUNCE_MS = 400;
  const BANNER_ATTR = 'data-decoy-chat-banner';
  const TOAST_AUTOCLOSE_LOW_MS = 10000;

  const COLOURS = {
    high: { bg: '#fde8e8', border: '#c0392b', text: '#7a1d14' },
    medium: { bg: '#fff4d6', border: '#d68910', text: '#6b4400' },
    low: { bg: '#eef6ee', border: '#7aa77a', text: '#2d4a2d' },
    unknown: { bg: '#eeeeee', border: '#999999', text: '#444444' }
  };

  // -------------------------------------------------------------------------
  // Shared card UI
  // -------------------------------------------------------------------------

  function el(tag, text, style) {
    const node = document.createElement(tag);
    if (text !== undefined) {
      node.textContent = text;
    }
    if (style) {
      Object.assign(node.style, style);
    }
    return node;
  }

  /*
   * opts: { title, detail, score?, items?, footer?, onClose?, compact? }
   * items: [{ text, points?, reason? }]
   */
  function buildCard(level, opts) {
    const colour = COLOURS[level] || COLOURS.unknown;
    const card = el('div', undefined, {
      background: colour.bg,
      border: `1px solid ${colour.border}`,
      borderLeft: `5px solid ${colour.border}`,
      color: colour.text,
      borderRadius: '6px',
      padding: '8px 10px',
      margin: '6px 0',
      font: '12px/1.4 Arial, sans-serif',
      textAlign: 'left',
      boxSizing: 'border-box'
    });
    card.setAttribute('role', 'status');

    const header = el('div', undefined, {
      display: 'flex',
      justifyContent: 'space-between',
      alignItems: 'baseline',
      gap: '10px'
    });
    header.appendChild(el('div', opts.title, { fontWeight: '700', fontSize: '13px' }));
    const right = el('div', undefined, { display: 'flex', gap: '8px', alignItems: 'baseline' });
    if (typeof opts.score === 'number') {
      right.appendChild(el('span', `Risk score ${opts.score}/100`, { fontWeight: '700', whiteSpace: 'nowrap' }));
    }
    if (opts.onClose) {
      const close = el('button', '×', {
        border: 'none',
        background: 'transparent',
        color: colour.text,
        cursor: 'pointer',
        fontSize: '16px',
        lineHeight: '1',
        padding: '0'
      });
      close.setAttribute('aria-label', 'Dismiss');
      close.addEventListener('click', opts.onClose);
      right.appendChild(close);
    }
    header.appendChild(right);
    card.appendChild(header);

    if (typeof opts.score === 'number') {
      const track = el('div', undefined, {
        height: '5px',
        background: 'rgba(0,0,0,0.12)',
        borderRadius: '3px',
        margin: '4px 0 6px'
      });
      track.appendChild(
        el('div', undefined, {
          height: '100%',
          width: `${Math.max(0, Math.min(100, opts.score))}%`,
          background: colour.border,
          borderRadius: '3px'
        })
      );
      card.appendChild(track);
    }

    if (opts.detail) {
      card.appendChild(el('div', opts.detail));
    }

    if (opts.items && opts.items.length) {
      const list = el('ul', undefined, { margin: '6px 0 0 16px', padding: '0' });
      for (const item of opts.items) {
        const li = el('li', undefined, { marginBottom: '5px' });
        li.appendChild(el('span', item.text, { fontWeight: item.points === undefined ? '400' : '600' }));
        if (item.points !== undefined) {
          li.appendChild(el('span', ` (+${item.points})`, { fontWeight: '700' }));
        }
        if (item.reason) {
          li.appendChild(el('div', `Why this matters: ${item.reason}`, { fontSize: '11px', opacity: '0.85' }));
        }
        list.appendChild(li);
      }
      card.appendChild(list);
    }

    if (opts.footer) {
      card.appendChild(
        el('div', opts.footer, {
          fontSize: '10px',
          opacity: '0.8',
          marginTop: '4px',
          borderTop: `1px solid ${colour.border}`,
          paddingTop: '4px'
        })
      );
    }
    return card;
  }

  let toastHost = null;
  function getToastHost() {
    if (toastHost && document.body.contains(toastHost)) {
      return toastHost;
    }
    toastHost = el('div', undefined, {
      position: 'fixed',
      top: '12px',
      right: '12px',
      width: '360px',
      maxHeight: '90vh',
      overflowY: 'auto',
      zIndex: '2147483647',
      pointerEvents: 'none'
    });
    toastHost.id = 'decoy-toasts';
    document.body.appendChild(toastHost);
    return toastHost;
  }

  // Cards go inside a click-through host; only the cards themselves take clicks.
  function showToast(level, opts, { id, autoCloseMs } = {}) {
    const host = getToastHost();
    if (id) {
      const existing = host.querySelector(`[data-decoy-id="${id}"]`);
      if (existing) {
        existing.remove();
      }
    }
    const holder = el('div', undefined, { pointerEvents: 'auto' });
    if (id) {
      holder.setAttribute('data-decoy-id', id);
    }
    holder.appendChild(buildCard(level, { ...opts, onClose: () => holder.remove() }));
    host.appendChild(holder);
    if (autoCloseMs) {
      setTimeout(() => holder.remove(), autoCloseMs);
    }
    return holder;
  }

  function scoreFooter(result) {
    const { medium, high } = result.thresholds || { medium: 25, high: 55 };
    let text = `Each signal adds the points in brackets; ${medium}+ is medium and ${high}+ is high.`;
    if (result.raw_points > result.score) {
      text += ` The signals total ${result.raw_points}, capped at 100.`;
    }
    const model = result.model;
    if (model && model.status === 'unavailable') {
      text += ` The language model was unavailable (${model.detail || 'unknown reason'}), so this is rules only.`;
    } else if (model && model.id && model.p_phishing !== null && model.p_phishing !== undefined) {
      text += ` Language model ${model.id} rated the message ${Math.round(100 * model.p_phishing)}% scam-like.`;
    }
    return `${text} A triage score, not a probability of fraud.`;
  }

  function hash(text) {
    let h = 5381;
    for (let i = 0; i < text.length; i += 1) {
      h = ((h << 5) + h + text.charCodeAt(i)) | 0;
    }
    return String(h >>> 0);
  }

  async function relay(message) {
    try {
      return await chrome.runtime.sendMessage(message);
    } catch (error) {
      return { ok: false, error: 'the extension was reloaded; refresh WhatsApp Web' };
    }
  }

  // -------------------------------------------------------------------------
  // 1. Chat text
  // -------------------------------------------------------------------------

  const chatResults = new Map();
  const chatInFlight = new Set();

  function senderOf(row) {
    // data-pre-plain-text looks like "[9:12 PM, 10/31/2026] Name: ".
    const meta = row.querySelector(SELECTORS.meta);
    const raw = (meta && meta.getAttribute('data-pre-plain-text')) || '';
    const match = raw.match(/\]\s*(.*?):\s*$/);
    return (match ? match[1] : '').slice(0, 300);
  }

  function extractChat(row) {
    const body = Array.from(row.querySelectorAll(SELECTORS.text))
      .map((node) => node.innerText || '')
      .join('\n')
      .slice(0, MAX_BODY);
    const links = [];
    for (const a of row.querySelectorAll('a[href]')) {
      const href = a.getAttribute('href') || '';
      if (!/^https?:\/\//i.test(href)) {
        continue;
      }
      links.push({ text: (a.textContent || '').trim().slice(0, 500), href: href.slice(0, 2048) });
      if (links.length >= MAX_LINKS) {
        break;
      }
    }
    // WhatsApp often shows a URL as styled text rather than a real <a>, so also
    // pull URLs out of the message text. Their visible text is the URL itself.
    const known = new Set(links.map((l) => l.href));
    for (const match of body.matchAll(/\bhttps?:\/\/[^\s<>"')\]]+/gi)) {
      const url = match[0].replace(/[.,;:!?]+$/, '').slice(0, 2048);
      if (!known.has(url) && links.length < MAX_LINKS) {
        known.add(url);
        links.push({ text: url.slice(0, 500), href: url });
      }
    }
    return { sender_name: senderOf(row), body, links };
  }

  function renderChatBanner(row, outcome) {
    const result = outcome.result;
    const existing = row.querySelector(`[${BANNER_ATTR}]`);
    if (existing) {
      existing.remove();
    }
    const holder = row.querySelector(SELECTORS.bubble);
    const target = (holder && holder.parentElement) || row;
    if (result.level === 'low') {
      // A clean result gets one quiet line, not a card, and never says "genuine".
      const chip = el(
        'div',
        `Checked: no scam signals found (score ${result.score}/100). Not proof the message is genuine.`,
        { fontSize: '11px', opacity: '0.7', fontStyle: 'italic', margin: '3px 0 0' }
      );
      chip.setAttribute(BANNER_ATTR, 'low');
      target.appendChild(chip);
      return chip;
    }
    const banner = buildCard(result.level, {
      title: result.level === 'high' ? 'High scam risk' : 'Some scam signals',
      detail: result.summary,
      score: result.score,
      items: result.findings.map((f) => ({ text: f.message, points: f.points, reason: f.reason })),
      footer: scoreFooter(result)
    });
    banner.setAttribute(BANNER_ATTR, result.level);
    target.appendChild(banner);
    return banner;
  }

  async function processChat(row) {
    const payload = extractChat(row);
    if (!payload.body.trim() && !payload.links.length) {
      return;
    }
    const key = hash(
      [payload.sender_name, payload.body, payload.links.map((l) => `${l.text}>${l.href}`).join('\u0002')].join('\u0001')
    );
    // WhatsApp re-renders rows as you scroll, which drops our banner; the cache
    // lets it come back without another request or another toast.
    if (chatResults.has(key)) {
      const cached = chatResults.get(key);
      const { level } = cached.result;
      if (level !== 'unknown' && !row.querySelector(`[${BANNER_ATTR}]`)) {
        renderChatBanner(row, cached);
      }
      return;
    }
    if (chatInFlight.has(key)) {
      return;
    }
    chatInFlight.add(key);
    const outcome = await relay({ type: 'CHAT_ANALYZE', key, payload });
    chatInFlight.delete(key);
    if (!outcome || !outcome.ok) {
      // Quiet on purpose: a banner on every message would drown the chat if the
      // backend is simply not running. The popup and console carry the reason.
      console.info('[decoy] chat check unavailable:', outcome && outcome.error);
      return;
    }
    chatResults.set(key, outcome);
    const { level } = outcome.result;
    if (level !== 'unknown') {
      renderChatBanner(row, outcome);
    }
    if (level === 'high') {
      const who = payload.sender_name ? ` from ${payload.sender_name}` : '';
      showToast(
        'high',
        {
          title: `High scam risk${who}`,
          detail: outcome.result.summary,
          score: outcome.result.score,
          items: outcome.result.findings.map((f) => ({ text: f.message, points: f.points, reason: f.reason })),
          footer: scoreFooter(outcome.result)
        },
        { id: `chat-${key}` }
      );
    }
  }

  // -------------------------------------------------------------------------
  // 2. Voice notes
  // -------------------------------------------------------------------------

  const voiceSeen = new Set();

  function toPcm16(audioBuffer) {
    const length = Math.min(audioBuffer.length, MAX_VOICE_SECONDS * TARGET_RATE);
    const channels = audioBuffer.numberOfChannels;
    const out = new Int16Array(length);
    for (let i = 0; i < length; i += 1) {
      let sum = 0;
      for (let c = 0; c < channels; c += 1) {
        sum += audioBuffer.getChannelData(c)[i];
      }
      const sample = Math.max(-1, Math.min(1, sum / channels));
      out[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
    }
    return out;
  }

  function bytesToBase64(bytes) {
    let binary = '';
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(binary);
  }

  async function processVoice(audio) {
    const src = audio.currentSrc || audio.src || '';
    if (!src.startsWith('blob:') || voiceSeen.has(src)) {
      return;
    }
    voiceSeen.add(src);
    const id = `voice-${hash(src)}`;
    showToast('unknown', { title: 'Checking voice note…', detail: 'Decoding the audio and running the detector.' }, { id });

    let pcmBase64;
    try {
      const encoded = await (await fetch(src)).arrayBuffer();
      const context = new AudioContext({ sampleRate: TARGET_RATE });
      let decoded;
      try {
        decoded = await context.decodeAudioData(encoded);
      } finally {
        void context.close();
      }
      const pcm = toPcm16(decoded);
      pcmBase64 = bytesToBase64(new Uint8Array(pcm.buffer));
    } catch (error) {
      showToast(
        'unknown',
        { title: 'Voice note not checked', detail: 'Chrome could not read or decode this voice note’s audio.' },
        { id, autoCloseMs: TOAST_AUTOCLOSE_LOW_MS }
      );
      return;
    }

    const outcome = await relay({ type: 'VOICE_ANALYZE', key: src, pcmBase64 });
    if (!outcome || !outcome.ok) {
      showToast(
        'unknown',
        { title: 'Voice note not checked', detail: `${(outcome && outcome.error) || 'no response'}.` },
        { id, autoCloseMs: TOAST_AUTOCLOSE_LOW_MS }
      );
      return;
    }
    const result = outcome.result;
    const titles = {
      high: 'Possible AI-generated voice note',
      medium: 'Some synthetic-speech signal in voice note',
      low: 'No synthetic-speech signal in voice note',
      unknown: 'Voice note not checked'
    };
    showToast(
      result.level,
      {
        title: titles[result.level] || titles.unknown,
        detail: result.level === 'low' ? result.disclaimer : result.summary,
        score: result.level === 'unknown' ? undefined : result.score,
        items: result.reasons.map((text) => ({ text })),
        footer:
          result.level === 'unknown'
            ? undefined
            : `Score is the share of analysed windows classified as synthetic. Model: ${result.model_id}, threshold ${result.threshold}. A triage score, not a probability of fraud.`
      },
      { id, autoCloseMs: result.level === 'low' ? TOAST_AUTOCLOSE_LOW_MS : undefined }
    );
  }

  // -------------------------------------------------------------------------
  // 3. Live-call alert
  // -------------------------------------------------------------------------

  chrome.runtime.onMessage.addListener((message) => {
    if (!message) {
      return;
    }
    if (message.type === 'CALL_ALERT' && message.alert) {
      const alert = message.alert;
      showToast(
        'high',
        {
          title: alert.title || 'Potential AI-generated voice detected',
          detail: alert.message,
          score: typeof alert.riskScore === 'number' ? alert.riskScore : undefined,
          items: (alert.reasons || []).map((text) => ({ text })),
          footer: alert.provisional
            ? 'Provisional: more audio is still being analysed. Advisory only; confirm the caller another way.'
            : 'Advisory only; confirm the caller another way.'
        },
        { id: 'call-alert' }
      );
    } else if (message.type === 'CALL_CLEAR') {
      const host = document.getElementById('decoy-toasts');
      const old = host && host.querySelector('[data-decoy-id="call-alert"]');
      if (old) {
        old.remove();
      }
    }
  });

  // -------------------------------------------------------------------------
  // Scanning
  // -------------------------------------------------------------------------

  /*
   * Received text messages currently on screen. WhatsApp dropped the old
   * div.message-in class and its row ids no longer reliably start with
   * "false_"/"true_", so messages are found by their data-pre-plain-text marker.
   * Direction comes from the id prefix when it is there; otherwise from which
   * side of the chat pane the bubble sits on (received bubbles hug the left).
   */
  function receivedMessageRows() {
    const rows = new Set();
    const pane = document.querySelector('#main') || document.body;
    const paneRect = pane.getBoundingClientRect();
    for (const meta of document.querySelectorAll(SELECTORS.meta)) {
      const row = meta.closest('[data-id]') || meta.parentElement;
      if (!row) {
        continue;
      }
      const id = row.getAttribute('data-id') || '';
      let received;
      if (id.startsWith('false_')) {
        received = true;
      } else if (id.startsWith('true_')) {
        received = false;
      } else {
        const rect = meta.getBoundingClientRect();
        received = rect.left - paneRect.left < paneRect.right - rect.right;
      }
      if (received) {
        rows.add(row);
      }
    }
    return Array.from(rows);
  }

  let lastRowCount = -1;
  function scan() {
    const rows = receivedMessageRows();
    if (rows.length !== lastRowCount) {
      lastRowCount = rows.length;
      console.info('[decoy] received messages in view:', rows.length);
    }
    for (const row of rows) {
      void processChat(row);
    }
    for (const audio of document.querySelectorAll(SELECTORS.audio)) {
      void processVoice(audio);
    }
  }

  let timer = null;
  const observer = new MutationObserver(() => {
    clearTimeout(timer);
    timer = setTimeout(scan, DEBOUNCE_MS);
  });
  observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['src'] });
  // An audio element is created or re-sourced when a note is played; its
  // `play` event does not bubble, so catch it in the capture phase too.
  document.addEventListener('play', scan, true);
  scan();
})();
