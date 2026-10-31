/*
 * WhatsApp Web content script. Three jobs, all advisory:
 *
 *   1. Chat text   - scores each received message for scam signals. A compact
 *                    chip appears under the message (medium and high), and a
 *                    full card opens for high risk or when the chip is clicked.
 *   2. Voice notes - when a voice note is played, fetches its audio from the
 *                    page, decodes it to 16 kHz mono and has the local detector
 *                    score it. The result shows as a card with a window strip.
 *   3. Live calls  - shows the call alert (score and reasons) sent by the
 *                    service worker while Start Protection is running.
 *
 * Every result uses one card layout: a risk label, a title and the score out of
 * 100; a meter marked at the medium and high cut-offs; a list of signals with
 * their points and a collapsed "why this matters"; and a collapsed footer on
 * how the score works.
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
  const DEFAULT_THRESHOLDS = { medium: 25, high: 55 };

  const LEVELS = {
    high: { label: 'HIGH RISK', pill: '#c0392b', bg: '#fff5f4', border: '#e3b3ae', text: '#3b1a17' },
    medium: { label: 'MEDIUM RISK', pill: '#b9770e', bg: '#fffaf0', border: '#ecd3a0', text: '#3d2e0a' },
    low: { label: 'LOW RISK', pill: '#2e7d32', bg: '#f5faf5', border: '#bcd9bd', text: '#1d3a1f' },
    unknown: { label: 'NOT CHECKED', pill: '#6b7280', bg: '#f4f5f6', border: '#d1d5db', text: '#2b2f36' },
    info: { label: 'CHECKING', pill: '#3b6ea5', bg: '#f3f7fc', border: '#c5d6ea', text: '#1c2f45' }
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

  // A collapsed section: a small link-style summary that opens to some text.
  function collapsible(summaryText, bodyText) {
    const section = document.createElement('details');
    section.style.marginTop = '3px';
    section.appendChild(
      el('summary', summaryText, { cursor: 'pointer', fontSize: '11px', color: '#2a5db0' })
    );
    section.appendChild(el('div', bodyText, { fontSize: '11px', opacity: '0.85', margin: '3px 0 0' }));
    return section;
  }

  function pointsChip(points) {
    return el('span', `+${points}`, {
      flex: 'none',
      fontSize: '11px',
      fontWeight: '700',
      padding: '1px 7px',
      borderRadius: '9px',
      background: 'rgba(0,0,0,0.08)'
    });
  }

  /*
   * opts: { title, detail, score?, thresholds?, extra?, itemsHeading?, items?,
   *         footer?, onClose? }
   * items: [{ text, points?, reason? }]
   */
  function buildCard(level, opts) {
    const theme = LEVELS[level] || LEVELS.unknown;
    const card = el('div', undefined, {
      background: theme.bg,
      border: `1px solid ${theme.border}`,
      borderLeft: `4px solid ${theme.pill}`,
      color: theme.text,
      borderRadius: '8px',
      padding: '10px 12px',
      margin: '0',
      font: '12px/1.45 Arial, sans-serif',
      textAlign: 'left',
      boxSizing: 'border-box',
      width: '100%',
      boxShadow: '0 1px 4px rgba(0,0,0,0.25)'
    });
    card.setAttribute('role', 'status');

    // Header: risk label, title, score, close.
    const header = el('div', undefined, { display: 'flex', alignItems: 'center', gap: '8px' });
    header.appendChild(
      el('span', theme.label, {
        flex: 'none',
        background: theme.pill,
        color: '#fff',
        fontSize: '10px',
        fontWeight: '700',
        letterSpacing: '0.06em',
        padding: '2px 8px',
        borderRadius: '10px'
      })
    );
    header.appendChild(el('div', opts.title, { flex: '1', minWidth: '0', fontWeight: '700', fontSize: '13px' }));
    if (typeof opts.score === 'number') {
      const score = el('div', undefined, { flex: 'none', whiteSpace: 'nowrap' });
      score.appendChild(el('span', String(opts.score), { fontSize: '20px', fontWeight: '800' }));
      score.appendChild(el('span', '/100', { fontSize: '11px', opacity: '0.7' }));
      header.appendChild(score);
    }
    if (opts.onClose) {
      const close = el('button', '×', {
        flex: 'none',
        border: 'none',
        background: 'transparent',
        color: theme.text,
        cursor: 'pointer',
        fontSize: '18px',
        lineHeight: '1',
        padding: '0 2px'
      });
      close.setAttribute('aria-label', 'Dismiss');
      close.addEventListener('click', opts.onClose);
      header.appendChild(close);
    }
    card.appendChild(header);

    // Meter, with ticks at the medium and high cut-offs when they are known.
    if (typeof opts.score === 'number') {
      const meter = el('div', undefined, {
        position: 'relative',
        height: '6px',
        background: 'rgba(0,0,0,0.12)',
        borderRadius: '3px',
        margin: '9px 0 2px'
      });
      meter.appendChild(
        el('div', undefined, {
          height: '100%',
          width: `${Math.max(0, Math.min(100, opts.score))}%`,
          background: theme.pill,
          borderRadius: '3px'
        })
      );
      if (opts.thresholds) {
        for (const at of [opts.thresholds.medium, opts.thresholds.high]) {
          meter.appendChild(
            el('div', undefined, {
              position: 'absolute',
              left: `${at}%`,
              top: '-2px',
              bottom: '-2px',
              width: '2px',
              background: 'rgba(0,0,0,0.45)'
            })
          );
        }
      }
      card.appendChild(meter);
      if (opts.thresholds) {
        const scale = el('div', undefined, { position: 'relative', height: '13px', fontSize: '10px', opacity: '0.7' });
        scale.appendChild(el('span', 'Low', { position: 'absolute', left: '0' }));
        scale.appendChild(el('span', `Medium ${opts.thresholds.medium}`, { position: 'absolute', left: `${opts.thresholds.medium}%` }));
        scale.appendChild(el('span', `High ${opts.thresholds.high}`, { position: 'absolute', left: `${opts.thresholds.high}%` }));
        card.appendChild(scale);
      }
    }

    if (opts.detail) {
      card.appendChild(el('div', opts.detail, { margin: '8px 0 0' }));
    }
    if (opts.extra) {
      card.appendChild(opts.extra);
    }

    // Signals.
    if (opts.items && opts.items.length) {
      card.appendChild(
        el('div', `${opts.itemsHeading || 'Signals found'} (${opts.items.length})`, {
          fontSize: '10px',
          fontWeight: '700',
          letterSpacing: '0.06em',
          textTransform: 'uppercase',
          opacity: '0.7',
          margin: '12px 0 2px'
        })
      );
      for (const item of opts.items) {
        const row = el('div', undefined, { padding: '6px 0', borderTop: '1px solid rgba(0,0,0,0.08)' });
        const line = el('div', undefined, {
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
          gap: '8px'
        });
        line.appendChild(el('div', item.text, { fontWeight: item.points === undefined ? '400' : '600' }));
        if (item.points !== undefined) {
          line.appendChild(pointsChip(item.points));
        }
        row.appendChild(line);
        if (item.reason) {
          row.appendChild(collapsible('Why this matters', item.reason));
        }
        card.appendChild(row);
      }
    }

    if (opts.footer) {
      const foot = el('div', undefined, { marginTop: '4px', borderTop: `1px solid ${theme.border}`, paddingTop: '4px' });
      foot.appendChild(collapsible('How this score works', opts.footer));
      card.appendChild(foot);
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
      width: '380px',
      maxHeight: '92vh',
      overflowY: 'auto',
      zIndex: '2147483647',
      pointerEvents: 'none',
      display: 'flex',
      flexDirection: 'column',
      gap: '8px'
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
    const { medium, high } = result.thresholds || DEFAULT_THRESHOLDS;
    let text = `Each signal adds the points shown; ${medium}+ is medium and ${high}+ is high.`;
    if (result.raw_points > result.score) {
      text += ` The signals total ${result.raw_points}, capped at 100.`;
    }
    const model = result.model;
    if (model && model.status === 'unavailable') {
      text += ` The language model was unavailable (${model.detail || 'unknown reason'}), so this is rules only.`;
    } else if (model && model.id && model.p_phishing !== null && model.p_phishing !== undefined) {
      text += ` Language model ${model.id} rated the message ${Math.round(100 * model.p_phishing)}% scam-like (its own score, not a calibrated probability).`;
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

  // The full card for one checked message.
  function chatCardOpts(result, sender) {
    const who = sender ? ` from ${sender}` : '';
    return {
      title: `${result.level === 'high' ? 'Scam message' : 'Possible scam message'}${who}`,
      detail: result.summary,
      score: result.score,
      thresholds: result.thresholds || DEFAULT_THRESHOLDS,
      items: result.findings.map((f) => ({ text: f.message, points: f.points, reason: f.reason })),
      footer: scoreFooter(result)
    };
  }

  function openChatCard(key, result, sender) {
    showToast(result.level, chatCardOpts(result, sender), { id: `chat-${key}` });
  }

  // Under the message: one quiet line when clean, or a compact one-line chip
  // (risk label, score, top signal, Details) when flagged. A chip fits any bubble
  // width; the full card opens on click.
  function renderChatMarker(row, outcome, key, sender) {
    const result = outcome.result;
    const existing = row.querySelector(`[${BANNER_ATTR}]`);
    if (existing) {
      existing.remove();
    }
    const holder = row.querySelector(SELECTORS.bubble);
    const target = (holder && holder.parentElement) || row;

    if (result.level === 'low') {
      // A clean result gets one quiet line, not a card, and never says "genuine".
      const note = el(
        'div',
        `Checked: no scam signals found (score ${result.score}/100). Not proof the message is genuine.`,
        { fontSize: '11px', opacity: '0.7', fontStyle: 'italic', margin: '3px 0 0' }
      );
      note.setAttribute(BANNER_ATTR, 'low');
      target.appendChild(note);
      return note;
    }

    const theme = LEVELS[result.level] || LEVELS.unknown;
    const top = result.findings.length ? result.findings[0].message : result.summary;
    const chip = el('div', undefined, {
      display: 'flex',
      alignItems: 'center',
      flexWrap: 'wrap',
      gap: '6px',
      boxSizing: 'border-box',
      maxWidth: '100%',
      margin: '4px 0 0',
      padding: '4px 8px',
      background: theme.bg,
      border: `1px solid ${theme.border}`,
      borderLeft: `4px solid ${theme.pill}`,
      borderRadius: '6px',
      color: theme.text,
      font: '11px/1.3 Arial, sans-serif'
    });
    chip.setAttribute(BANNER_ATTR, result.level);
    chip.appendChild(
      el('span', theme.label, {
        background: theme.pill,
        color: '#fff',
        fontWeight: '700',
        fontSize: '10px',
        letterSpacing: '0.05em',
        padding: '1px 7px',
        borderRadius: '9px'
      })
    );
    chip.appendChild(el('span', `${result.score}/100`, { fontWeight: '700' }));
    chip.appendChild(
      el('span', top, { flex: '1', minWidth: '0', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '240px' })
    );
    const details = el('button', 'Details', {
      border: `1px solid ${theme.pill}`,
      background: 'transparent',
      color: theme.pill,
      borderRadius: '9px',
      padding: '0 8px',
      cursor: 'pointer',
      fontSize: '11px'
    });
    details.addEventListener('click', () => openChatCard(key, result, sender));
    chip.appendChild(details);
    target.appendChild(chip);
    return chip;
  }

  async function processChat(row) {
    const payload = extractChat(row);
    if (!payload.body.trim() && !payload.links.length) {
      return;
    }
    const key = hash(
      [payload.sender_name, payload.body, payload.links.map((l) => `${l.text}>${l.href}`).join('\u0002')].join('\u0001')
    );
    // WhatsApp re-renders rows as you scroll, which drops our marker; the cache
    // lets it come back without another request or another card.
    if (chatResults.has(key)) {
      const cached = chatResults.get(key);
      if (cached.result.level !== 'unknown' && !row.querySelector(`[${BANNER_ATTR}]`)) {
        renderChatMarker(row, cached, key, payload.sender_name);
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
      // Quiet on purpose: a marker on every message would drown the chat if the
      // backend is simply not running. The console carries the reason.
      console.info('[decoy] chat check unavailable:', outcome && outcome.error);
      return;
    }
    chatResults.set(key, outcome);
    const { level } = outcome.result;
    if (level !== 'unknown') {
      renderChatMarker(row, outcome, key, payload.sender_name);
    }
    if (level === 'high') {
      openChatCard(key, outcome.result, payload.sender_name);
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

  // One block per analysed window: red synthetic, green human-like, grey too quiet.
  function windowStrip(result) {
    const wrap = el('div', undefined, { margin: '10px 0 0' });
    wrap.appendChild(
      el('div', 'Windows analysed', {
        fontSize: '10px',
        fontWeight: '700',
        letterSpacing: '0.06em',
        textTransform: 'uppercase',
        opacity: '0.7',
        margin: '0 0 4px'
      })
    );
    const strip = el('div', undefined, { display: 'flex', gap: '3px' });
    for (const w of result.windows || []) {
      let colour = '#9ca3af';
      let label = 'too quiet to judge';
      if (w.p_fake !== null && w.p_fake !== undefined) {
        const synthetic = w.p_fake >= result.threshold;
        colour = synthetic ? '#c0392b' : '#2e7d32';
        label = `${Math.round(100 * w.p_fake)}% synthetic (${synthetic ? 'flagged' : 'human-like'})`;
      }
      const block = el('div', undefined, { flex: '1', height: '16px', borderRadius: '3px', background: colour });
      block.title = `${w.start_s}s to ${w.end_s}s: ${label}`;
      strip.appendChild(block);
    }
    wrap.appendChild(strip);
    wrap.appendChild(
      el('div', 'Each block is one window of about 4 seconds: red synthetic, green human-like, grey too quiet. Hover for details.', {
        fontSize: '10px',
        opacity: '0.7',
        margin: '4px 0 0'
      })
    );
    return wrap;
  }

  function processVoice(audio) {
    return checkVoiceSrc(audio.currentSrc || audio.src || '');
  }

  async function checkVoiceSrc(src) {
    if (!src.startsWith('blob:')) {
      if (src && !voiceSeen.has(src)) {
        voiceSeen.add(src);
        console.info('[decoy] voice: ignoring audio with a non-blob source:', src.slice(0, 40));
      }
      return;
    }
    if (voiceSeen.has(src)) {
      return;
    }
    voiceSeen.add(src);
    console.info('[decoy] voice: checking a voice note', src.slice(0, 40));
    const id = `voice-${hash(src)}`;
    showToast('info', { title: 'Voice note', detail: 'Decoding the audio and running the detector…' }, { id });

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
      console.warn('[decoy] voice: could not read or decode the audio:', error && error.name, error && error.message);
      showToast(
        'unknown',
        { title: 'Voice note', detail: 'Not checked: Chrome could not read or decode this voice note’s audio.' },
        { id, autoCloseMs: TOAST_AUTOCLOSE_LOW_MS }
      );
      return;
    }

    const outcome = await relay({ type: 'VOICE_ANALYZE', key: src, pcmBase64 });
    if (!outcome || !outcome.ok) {
      showToast(
        'unknown',
        { title: 'Voice note', detail: `Not checked: ${(outcome && outcome.error) || 'no response'}.` },
        { id, autoCloseMs: TOAST_AUTOCLOSE_LOW_MS }
      );
      return;
    }
    const result = outcome.result;
    const checked = result.level !== 'unknown';
    showToast(
      result.level,
      {
        title: 'Voice note analysis',
        detail: result.summary,
        score: checked ? result.score : undefined,
        extra: checked ? windowStrip(result) : undefined,
        itemsHeading: 'Evidence',
        items: result.reasons.map((text) => ({ text })),
        footer: checked
          ? `Score is the share of analysed windows classified as synthetic. Model: ${result.model_id}, threshold ${result.threshold}. ${result.disclaimer}`
          : undefined
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
          itemsHeading: 'Evidence',
          items: (alert.reasons || []).map((text) => ({ text })),
          footer: alert.provisional
            ? 'Provisional: more audio is still being analysed. The score is the detector’s own average confidence over the flagged windows. Advisory only; confirm the caller another way.'
            : 'The score is the detector’s own average confidence over the flagged windows. Advisory only; confirm the caller another way.'
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
  let lastAudioCount = -1;
  function scan() {
    const rows = receivedMessageRows();
    if (rows.length !== lastRowCount) {
      lastRowCount = rows.length;
      console.info('[decoy] received messages in view:', rows.length);
    }
    for (const row of rows) {
      void processChat(row);
    }
    const audios = document.querySelectorAll(SELECTORS.audio);
    if (audios.length !== lastAudioCount) {
      lastAudioCount = audios.length;
      console.info('[decoy] audio elements on the page:', audios.length);
    }
    for (const audio of audios) {
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
  // Audio played through an element that is never attached to the page is
  // reported by whatsapp-audio-hook.js (which runs in the page's own context).
  window.addEventListener('message', (event) => {
    const data = event.data;
    if (
      event.source === window &&
      data &&
      data.source === 'decoy-audio-hook' &&
      typeof data.src === 'string' &&
      data.src.startsWith(`blob:${window.location.origin}/`)
    ) {
      console.info('[decoy] voice: play reported by the page hook');
      void checkVoiceSrc(data.src);
    }
  });

  scan();
})();
