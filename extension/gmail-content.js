/*
 * Gmail content script: reads each opened message, asks the local backend for
 * a phishing-signal score (via the service worker) and shows a banner above
 * the message body.
 *
 * Gmail's markup is undocumented and changes. The selectors below are the ones
 * Gmail has used for years, but if the banner stops appearing, these are what
 * to check. Everything the page supplies is inserted with textContent, never
 * innerHTML.
 */
(() => {
  'use strict';

  console.info('[decoy] Gmail email check loaded');

  const SELECTORS = {
    message: 'div.adn',
    body: 'div.a3s',
    sender: 'span.gD',
    subject: 'h2.hP',
    attachment: 'span.aV3'
  };
  const MAX_BODY = 20000;
  const MAX_LINKS = 200;
  const DEBOUNCE_MS = 500;
  const BANNER_ATTR = 'data-decoy-email-banner';

  const COLOURS = {
    high: { bg: '#fde8e8', border: '#c0392b', text: '#7a1d14' },
    medium: { bg: '#fff4d6', border: '#d68910', text: '#6b4400' },
    low: { bg: '#eef6ee', border: '#7aa77a', text: '#2d4a2d' },
    unknown: { bg: '#eeeeee', border: '#999999', text: '#444444' }
  };
  const TITLES = {
    high: 'High phishing risk',
    medium: 'Some phishing signals',
    low: 'No phishing signals found',
    unknown: 'Email check unavailable'
  };

  const resultsByKey = new Map();
  const inFlight = new Set();

  function hash(text) {
    let h = 5381;
    for (let i = 0; i < text.length; i += 1) {
      h = ((h << 5) + h + text.charCodeAt(i)) | 0;
    }
    return String(h >>> 0);
  }

  function extract(messageEl) {
    const bodyEl = messageEl.querySelector(SELECTORS.body);
    if (!bodyEl) {
      return null;
    }
    const senderEl = messageEl.querySelector(SELECTORS.sender);
    const subjectEl = document.querySelector(SELECTORS.subject);

    const links = [];
    for (const a of bodyEl.querySelectorAll('a[href]')) {
      const href = a.getAttribute('href') || '';
      if (!/^https?:\/\//i.test(href)) {
        continue;
      }
      links.push({
        text: (a.textContent || '').trim().slice(0, 500),
        href: href.slice(0, 2048)
      });
      if (links.length >= MAX_LINKS) {
        break;
      }
    }

    const attachments = Array.from(messageEl.querySelectorAll(SELECTORS.attachment))
      .map((el) => (el.textContent || '').trim().slice(0, 200))
      .filter(Boolean)
      .slice(0, 50);

    return {
      sender_name: ((senderEl && senderEl.getAttribute('name')) || '').slice(0, 300),
      sender_email: ((senderEl && senderEl.getAttribute('email')) || '').slice(0, 320),
      subject: ((subjectEl && subjectEl.textContent) || '').trim().slice(0, 1000),
      body: (bodyEl.innerText || '').slice(0, MAX_BODY),
      links,
      attachments
    };
  }

  function renderBanner(messageEl, level, lines, detail) {
    const bodyEl = messageEl.querySelector(SELECTORS.body);
    if (!bodyEl || !bodyEl.parentElement) {
      return;
    }
    const existing = messageEl.querySelector(`[${BANNER_ATTR}]`);
    if (existing) {
      existing.remove();
    }

    const colour = COLOURS[level] || COLOURS.unknown;
    const banner = document.createElement('div');
    banner.setAttribute(BANNER_ATTR, level);
    banner.setAttribute('role', 'status');
    Object.assign(banner.style, {
      background: colour.bg,
      border: `1px solid ${colour.border}`,
      borderLeft: `5px solid ${colour.border}`,
      color: colour.text,
      borderRadius: '6px',
      padding: '8px 12px',
      margin: '8px 0',
      font: '13px/1.4 Arial, sans-serif'
    });

    const title = document.createElement('div');
    title.style.fontWeight = '700';
    // "No signals found" above a list of signals would contradict itself.
    title.textContent =
      level === 'low' && lines.length ? 'Minor phishing signals' : TITLES[level] || TITLES.unknown;
    banner.appendChild(title);

    const summary = document.createElement('div');
    summary.textContent = detail;
    banner.appendChild(summary);

    if (lines.length) {
      const list = document.createElement('ul');
      list.style.margin = '4px 0 0 18px';
      list.style.padding = '0';
      for (const line of lines) {
        const item = document.createElement('li');
        item.textContent = line;
        list.appendChild(item);
      }
      banner.appendChild(list);
    }

    bodyEl.parentElement.insertBefore(banner, bodyEl);
  }

  function showResult(messageEl, outcome) {
    if (!outcome || !outcome.ok) {
      renderBanner(
        messageEl,
        'unknown',
        [],
        `Could not check this email: ${(outcome && outcome.error) || 'no response from the extension'}. Start the backend and reopen the message.`
      );
      return;
    }
    const { level, findings, summary, disclaimer } = outcome.result;
    const lines = findings.map((f) => f.message);
    // For a low result the title already says it, so show the caveat instead.
    renderBanner(messageEl, level, lines, level === 'low' ? disclaimer : summary);
  }

  async function processMessage(messageEl) {
    const payload = extract(messageEl);
    if (!payload || (!payload.body.trim() && !payload.links.length)) {
      return;
    }
    // Link targets are part of the identity: the same text with a different
    // destination is a different email and must not reuse a cached verdict.
    const key = hash(
      [
        payload.sender_name,
        payload.sender_email,
        payload.subject,
        payload.body,
        payload.links.map((l) => `${l.text}>${l.href}`).join('\u0002'),
        payload.attachments.join('\u0002')
      ].join('\u0001')
    );

    if (messageEl.dataset.decoyKey === key && messageEl.querySelector(`[${BANNER_ATTR}]`)) {
      return;
    }
    messageEl.dataset.decoyKey = key;

    if (resultsByKey.has(key)) {
      showResult(messageEl, resultsByKey.get(key));
      return;
    }
    if (inFlight.has(key)) {
      return;
    }
    inFlight.add(key);
    let outcome;
    try {
      outcome = await chrome.runtime.sendMessage({ type: 'EMAIL_ANALYZE', key, payload });
    } catch (error) {
      outcome = { ok: false, error: 'the extension was reloaded; refresh Gmail' };
    } finally {
      inFlight.delete(key);
    }
    if (outcome && outcome.ok) {
      resultsByKey.set(key, outcome);
    }
    showResult(messageEl, outcome);
  }

  function scan() {
    for (const messageEl of document.querySelectorAll(SELECTORS.message)) {
      void processMessage(messageEl);
    }
  }

  let timer = null;
  const observer = new MutationObserver(() => {
    clearTimeout(timer);
    timer = setTimeout(scan, DEBOUNCE_MS);
  });
  observer.observe(document.body, { childList: true, subtree: true });
  scan();
})();
