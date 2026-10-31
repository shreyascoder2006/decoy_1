/*
 * Shared result-card UI for the Gmail and WhatsApp content scripts, loaded before
 * either (see manifest content_scripts). One layout for every result: a risk
 * label, title and score out of 100; a meter marked at the medium and high
 * cut-offs; a list of signals with their points and a collapsed "why this
 * matters"; and a collapsed "how this score works". Everything the page or the
 * backend supplies is inserted with textContent, never innerHTML.
 */
(() => {
  'use strict';

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

  globalThis.DecoyCard = { LEVELS, DEFAULT_THRESHOLDS, el, buildCard };
})();
