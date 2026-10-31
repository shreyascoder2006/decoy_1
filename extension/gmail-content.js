/*
 * Gmail content script: reads each opened message, asks the local backend for
 * a phishing-signal score (via the service worker) and shows a result card above
 * the message body. The card layout is shared with WhatsApp (card-ui.js).
 *
 * Gmail's markup is undocumented and changes. The selectors below are the ones
 * Gmail has used for years, but if the card stops appearing, these are what
 * to check. Everything the page supplies is inserted with textContent, never
 * innerHTML.
 */
(() => {
  'use strict';

  console.info('[decoy] Gmail email check loaded');

  // Shared card UI: card-ui.js is loaded before this script.
  const { DEFAULT_THRESHOLDS, buildCard } = globalThis.DecoyCard;

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

  const TITLES = {
    high: 'Likely phishing email',
    medium: 'Possible phishing email',
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
      .map((node) => (node.textContent || '').trim().slice(0, 200))
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

  /* opts is passed to buildCard: { title, detail, score?, thresholds?, items?, footer? } */
  function renderCard(messageEl, level, opts) {
    const bodyEl = messageEl.querySelector(SELECTORS.body);
    if (!bodyEl || !bodyEl.parentElement) {
      return;
    }
    const existing = messageEl.querySelector(`[${BANNER_ATTR}]`);
    if (existing) {
      existing.remove();
    }
    const card = buildCard(level, opts);
    card.style.margin = '8px 0';
    card.style.boxShadow = 'none';
    card.style.maxWidth = '760px';
    card.setAttribute(BANNER_ATTR, level);
    bodyEl.parentElement.insertBefore(card, bodyEl);
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
      text += ` Language model ${model.id} rated the wording ${Math.round(100 * model.p_phishing)}% phishing-like (its own score, not a calibrated probability).`;
    }
    return `${text} A triage score, not a probability of fraud.`;
  }

  function showResult(messageEl, outcome) {
    if (!outcome || !outcome.ok) {
      renderCard(messageEl, 'unknown', {
        title: TITLES.unknown,
        detail: `Could not check this email: ${(outcome && outcome.error) || 'no response from the extension'}. Start the backend and reopen the message.`
      });
      return;
    }
    const result = outcome.result;
    const { level, findings, summary, disclaimer } = result;
    const checked = level !== 'unknown';
    renderCard(messageEl, level, {
      // "No signals found" above a list of signals would contradict itself.
      title: level === 'low' && findings.length ? 'Minor phishing signals' : TITLES[level] || TITLES.unknown,
      // For a clean result the title already says it, so show the caveat instead.
      detail: level === 'low' ? disclaimer : summary,
      score: checked ? result.score : undefined,
      thresholds: checked ? result.thresholds || DEFAULT_THRESHOLDS : undefined,
      items: findings.map((f) => ({ text: f.message, points: f.points, reason: f.reason })),
      footer: checked ? scoreFooter(result) : undefined
    });
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
