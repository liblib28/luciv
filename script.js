// ── CONFIG ───────────────────────────────────────────────────────────────────

const API = '/api/reps';

let state = {
  reps: [],
  currentLevel: 'all',
  currentAddress: '',
  openstatesKey: localStorage.getItem('openstatesKey') || '',
  fecKey: localStorage.getItem('fecKey') || '',
  serverHasOpenStates: false,  // set by /api/status — keys stay on server
  serverHasFec: false,
};

// ── PARTY HELPERS ─────────────────────────────────────────────────────────────

const PARTY_CLASS = {
  'Democratic': 'party-dem', 'Democrat': 'party-dem',
  'Republican': 'party-rep',
  'Independent': 'party-ind',
  'Green': 'party-grn',
  'Libertarian': 'party-lib',
};

function partyClass(party = '') {
  for (const [k, v] of Object.entries(PARTY_CLASS)) {
    if (party.toLowerCase().includes(k.toLowerCase())) return v;
  }
  return 'party-oth';
}

function partyEmoji(party = '') {
  if (party.match(/democrat/i)) return '🔵';
  if (party.match(/republican/i)) return '🔴';
  if (party.match(/independent/i)) return '🟣';
  if (party.match(/green/i)) return '🟢';
  if (party.match(/libertarian/i)) return '🟡';
  return '⚫';
}

// ── AUTH (optional sign up / log in) ─────────────────────────────────────────

function switchAuthTab(tab, btn) {
  document.querySelectorAll('.signup-tab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('auth-login').classList.toggle('hidden', tab !== 'login');
  document.getElementById('auth-signup').classList.toggle('hidden', tab !== 'signup');
}

function handleLogin() {
  const email = document.getElementById('login-email').value.trim();
  const pass  = document.getElementById('login-password').value;
  if (!email || !pass) return;
  // Placeholder — real auth can be wired up later
  showAuthSuccess('Welcome back! Account features coming soon.');
}

function handleSignup() {
  const name  = document.getElementById('signup-name').value.trim();
  const email = document.getElementById('signup-email').value.trim();
  const pass  = document.getElementById('signup-password').value;
  if (!name || !email || !pass) return;
  // Placeholder — real auth can be wired up later
  showAuthSuccess(`Thanks, ${name}! We'll be in touch at ${email}.`);
}

function showAuthSuccess(msg) {
  const card = document.querySelector('.signup-card');
  card.innerHTML = `<div class="auth-success"><div class="auth-success-icon">✓</div><p>${msg}</p><p class="auth-skip" style="margin-top:12px" onclick="document.getElementById('intro-state').style.display='none'">Start browsing →</p></div>`;
}

// ── SETUP ────────────────────────────────────────────────────────────────────

function saveKeys() {
  const pk = document.getElementById('propublica-key-input').value.trim();
  if (pk) { localStorage.setItem('openstatesKey', pk); state.openstatesKey = pk; }
  const fk = document.getElementById('fec-key-input').value.trim();
  if (fk) { localStorage.setItem('fecKey', fk); state.fecKey = fk; }
  document.getElementById('setup-modal').classList.add('hidden');
  document.getElementById('app').classList.remove('hidden');
}

function openSettings() {
  document.getElementById('propublica-key-input').value = state.openstatesKey;
  document.getElementById('fec-key-input').value = state.fecKey;
  document.getElementById('setup-modal').classList.remove('hidden');
}

window.addEventListener('DOMContentLoaded', () => {
  // Ask server if built-in keys are available (server never reveals the actual key values)
  fetch('/api/status').then(r => r.json()).then(s => {
    if (s.hasOpenStates) state.serverHasOpenStates = true;
    if (s.hasFec) state.serverHasFec = true;
  }).catch(() => {});

  document.getElementById('setup-modal').classList.add('hidden');
  document.getElementById('app').classList.remove('hidden');

  // Close detail modal on overlay click or Escape key
  document.getElementById('detail-overlay').addEventListener('click', closeDetail);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDetail(); });

  launchConfetti();
});

// ── CONFETTI ─────────────────────────────────────────────────────────────────

function launchConfetti() {
  const COLORS = ['#DC2626', '#FFFFFF', '#1E3A8A', '#EF4444', '#93C5FD'];
  const COUNT = 120;
  const container = document.createElement('div');
  container.id = 'confetti-container';
  document.body.appendChild(container);

  for (let i = 0; i < COUNT; i++) {
    const el = document.createElement('div');
    el.className = 'confetti-piece';
    el.style.cssText = [
      `left:${Math.random() * 100}vw`,
      `background:${COLORS[Math.floor(Math.random() * COLORS.length)]}`,
      `width:${6 + Math.random() * 6}px`,
      `height:${8 + Math.random() * 8}px`,
      `animation-duration:${2.5 + Math.random() * 2}s`,
      `animation-delay:${Math.random() * 1.2}s`,
      `transform:rotate(${Math.random() * 360}deg)`,
      `border-radius:${Math.random() > 0.5 ? '50%' : '2px'}`,
    ].join(';');
    container.appendChild(el);
  }

  const toast = document.createElement('div');
  toast.id = 'confetti-toast';
  toast.textContent = '🎉 Yay! You\'re taking the first step to being civically engaged!';
  document.body.appendChild(toast);

  setTimeout(() => toast.classList.add('confetti-toast-visible'), 400);
  setTimeout(() => toast.classList.add('confetti-toast-hiding'), 3500);
  setTimeout(() => {
    toast.remove();
    container.remove();
  }, 5000);
}

// ── MAIN LOOKUP ───────────────────────────────────────────────────────────────

async function lookupReps() {
  const address = document.getElementById('address-input').value.trim();
  if (!address) return;

  setLoading(true, 'Looking up your representatives…');

  try {
    const params = new URLSearchParams({ address });
    // Only send user-supplied keys — server uses its own built-in keys automatically
    if (state.openstatesKey && !state.serverHasOpenStates) params.set('openstates_key', state.openstatesKey);
    if (state.fecKey && !state.serverHasFec) params.set('fec_key', state.fecKey);

    const res = await fetch(`${API}?${params}`);
    const data = await res.json();

    if (!res.ok) throw new Error(data.error || `Error ${res.status}`);

    state.reps = data.officials || [];
    state.currentAddress = data.normalizedAddress || address;
    state.currentLevel = 'all';
    renderResults(data);
  } catch (err) {
    if (err.message.includes('Failed to fetch') || err.message.includes('NetworkError')) {
      showError('Server not running', 'Make sure server.py is running. Open Terminal and run:\n\npython3 server.py\n\nthen refresh this page.');
    } else {
      showError('Could not load representatives', err.message);
    }
  } finally {
    setLoading(false);
  }
}

// ── FILTER ────────────────────────────────────────────────────────────────────

function filterByLevel(level) {
  state.currentLevel = level;
  document.querySelectorAll('.tab').forEach(t => {
    t.classList.toggle('active', t.dataset.level === level);
  });
  document.querySelectorAll('.rep-card').forEach(card => {
    card.style.display = (level === 'all' || card.dataset.level === level) ? '' : 'none';
  });
}

// ── RENDER ────────────────────────────────────────────────────────────────────

function renderResults(data) {
  document.getElementById('intro-state').classList.add('hidden');
  document.getElementById('loading-state').classList.add('hidden');
  document.getElementById('error-state').classList.add('hidden');
  document.getElementById('results-container').classList.remove('hidden');

  const byLevel = {};
  for (const rep of state.reps) byLevel[rep.level] = (byLevel[rep.level] || 0) + 1;
  const countStr = ['federal', 'state', 'local']
    .filter(l => byLevel[l])
    .map(l => `${byLevel[l]} ${l}`)
    .join(', ');
  const extra = !data.hasOpenStates
    ? ` · <a href="#" onclick="openSettings();return false;" style="color:var(--dem)">Add OpenStates key for state reps →</a>`
    : '';
  document.getElementById('address-display').innerHTML =
    `📍 <strong>${escHtml(state.currentAddress)}</strong> · ${countStr} officials found${extra}`;

  const grid = document.getElementById('cards-grid');
  grid.innerHTML = '';
  state.reps.forEach((rep, i) => grid.appendChild(buildCard(rep, i)));
}

// ── COMPACT CARD ─────────────────────────────────────────────────────────────

function buildCard(rep, index) {
  const pc = partyClass(rep.party);
  const card = document.createElement('div');
  card.className = `rep-card ${pc}`;
  card.dataset.level = rep.level;
  card.setAttribute('role', 'button');
  card.setAttribute('tabindex', '0');
  card.setAttribute('aria-label', `View details for ${rep.name}`);
  card.addEventListener('click', () => openDetail(index));
  card.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') openDetail(index); });

  const photoHTML = rep.photoUrl
    ? `<img class="rep-photo" src="${escHtml(rep.photoUrl)}" alt="${escHtml(rep.name)}"
         onerror="this.style.display='none';this.nextElementSibling.style.display=''">
       <div class="rep-photo-placeholder" style="display:none">${officialEmoji(rep.office)}</div>`
    : `<div class="rep-photo-placeholder">${officialEmoji(rep.office)}</div>`;

  // Show up to 3 top issues as compact tags
  const issues = rep.topIssues?.slice(0, 3) || [];
  const issueTags = issues.length
    ? `<div class="card-issue-tags">${issues.map(i => `<span class="issue-tag">${escHtml(i)}</span>`).join('')}</div>`
    : (rep.isDirectory || rep.isPlaceholder)
      ? `<div class="card-placeholder-note">${escHtml(rep.placeholderMsg || 'Click for more info')}</div>`
      : '';

  card.innerHTML = `
    <div class="card-accent-bar"></div>
    <div class="card-body">
      ${photoHTML}
      <div class="card-info">
        <div class="card-name">${escHtml(rep.name)}</div>
        <div class="card-office">${escHtml(rep.office)}</div>
        <div class="card-meta">
          <span class="party-badge">${partyEmoji(rep.party)} ${escHtml(rep.party || 'Unknown')}</span>
          <span class="level-chip">${rep.level}</span>
        </div>
      </div>
    </div>
    ${issueTags}
    <div class="card-cta">View details →</div>
  `;
  return card;
}

// ── DETAIL MODAL ──────────────────────────────────────────────────────────────

function openDetail(index) {
  const rep = state.reps[index];
  if (!rep) return;

  const pc = partyClass(rep.party);
  const websiteUrl = rep.urls?.[0] || null;

  const photoHTML = rep.photoUrl
    ? `<img class="detail-photo" src="${escHtml(rep.photoUrl)}" alt="${escHtml(rep.name)}"
         onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
       <div class="detail-photo-placeholder" style="display:none">${officialEmoji(rep.office)}</div>`
    : `<div class="detail-photo-placeholder">${officialEmoji(rep.office)}</div>`;

  document.getElementById('detail-panel').className = `detail-panel ${pc}`;
  document.getElementById('detail-content').innerHTML = `
    <div class="detail-header">
      ${photoHTML}
      <div class="detail-header-info">
        <div class="detail-name">${escHtml(rep.name)}</div>
        <div class="detail-office">${escHtml(rep.office)}</div>
        <div class="detail-meta">
          <span class="party-badge">${partyEmoji(rep.party)} ${escHtml(rep.party || 'Unknown')}</span>
          <span class="level-chip">${rep.level}</span>
        </div>
      </div>
    </div>
    ${buildDetailIssues(rep)}
    ${buildIdeologyBar(rep)}
    ${buildCampaignVsRecord(rep)}
    ${buildFunders(rep)}
    ${buildStats(rep)}
    ${buildContact(rep)}
    ${buildSocialSection(rep.channels)}
    ${websiteUrl ? `<div class="detail-footer"><a class="btn-website" href="${escHtml(websiteUrl)}" target="_blank" rel="noopener">🌐 Official Website</a></div>` : ''}
  `;

  document.getElementById('detail-modal').classList.remove('hidden');
  document.body.style.overflow = 'hidden';
  // Animate in
  requestAnimationFrame(() => {
    document.getElementById('detail-panel').classList.add('open');
  });
}

function closeDetail() {
  const panel = document.getElementById('detail-panel');
  panel.classList.remove('open');
  panel.addEventListener('transitionend', () => {
    document.getElementById('detail-modal').classList.add('hidden');
    document.body.style.overflow = '';
  }, { once: true });
}

// ── DETAIL SECTIONS ───────────────────────────────────────────────────────────

function buildDetailIssues(rep) {
  if (rep.isPlaceholder || rep.isDirectory) {
    const msg = rep.placeholderMsg || 'Visit the official website for more information.';
    return `<div class="detail-section"><div class="section-label">Info</div><div class="issues-note">${escHtml(msg)}</div></div>`;
  }
  if (rep.topIssues?.length) {
    const items = rep.topIssues.map(i =>
      `<div class="issue-item"><div class="issue-dot"></div><span>${escHtml(i)}</span></div>`
    ).join('');
    return `<div class="detail-section"><div class="section-label">Top Issues</div><div class="issues-list">${items}</div></div>`;
  }
  const roleIssue = officeToIssue(rep.office);
  if (roleIssue) {
    return `<div class="detail-section"><div class="section-label">Focus Area</div><div class="issues-list"><div class="issue-item"><div class="issue-dot"></div><span>${escHtml(roleIssue)}</span></div></div></div>`;
  }
  return '';
}

function buildCampaignVsRecord(rep) {
  const hasPositions = rep.campaignPositions?.length > 0;
  const hasVotes     = rep.keyVotes?.length > 0;
  const hasIssues    = rep.topIssues?.length > 0;
  if (!hasPositions && !hasVotes && !hasIssues) return '';

  let html = `<div class="detail-section cvr-section">`;
  html += `<div class="section-label">Campaign Promises &amp; Record</div>`;

  if (hasPositions) {
    html += `<div class="cvr-sub-label">📋 Campaign Positions</div><div class="positions-list">`;
    for (const p of rep.campaignPositions) {
      html += `<div class="position-item">
        <div class="pos-topic">${escHtml(p.topic)}</div>
        <div class="pos-stance">${escHtml(p.stance)}</div>
      </div>`;
    }
    html += `</div>`;
  } else if (hasIssues && !hasVotes) {
    html += `<div class="cvr-sub-label">📋 Stated Priorities</div><div class="positions-list">`;
    for (const issue of rep.topIssues) {
      html += `<div class="position-item">
        <div class="pos-topic">${escHtml(issue)}</div>
        <div class="pos-stance">Identified as a key focus area by this official.</div>
      </div>`;
    }
    html += `</div>`;
  } else if (hasIssues) {
    html += `<div class="cvr-sub-label">📋 Stated Priorities</div><div class="positions-list">`;
    for (const issue of rep.topIssues) {
      html += `<div class="position-item pos-item-compact"><div class="pos-topic">${escHtml(issue)}</div></div>`;
    }
    html += `</div>`;
  }

  if (hasVotes) {
    const topLabel = hasPositions || hasIssues ? '12px' : '0';
    html += `<div class="cvr-sub-label" style="margin-top:${topLabel}">🗳️ ${rep.level === 'state' ? 'Legislation Authored' : rep.level === 'local' ? 'Actions in Office' : 'Recent Votes'}</div>`;
    html += `<div class="votes-list">`;
    for (const v of rep.keyVotes) {
      const passed = v.voted === 'Yes' || v.voted === 'Passed';
      const cls  = passed ? 'vote-yes' : 'vote-no';
      const icon = passed ? '✓' : '✗';
      html += `<div class="vote-item">
        <span class="vote-badge ${cls}">${icon} ${escHtml(v.voted)}</span>
        <span class="vote-desc">${escHtml(v.description)}</span>
        ${v.month ? `<span class="vote-date">${escHtml(v.month)}</span>` : ''}
      </div>`;
    }
    html += `</div>`;
  }

  if (!hasPositions && hasIssues) {
    html += `<div class="cvr-source">Source: Official bio &amp; committee records</div>`;
  }

  html += `</div>`;
  return html;
}

function ideologyLabel(score) {
  if (score < 0.15) return 'Very Progressive';
  if (score < 0.30) return 'Progressive';
  if (score < 0.42) return 'Left-Leaning';
  if (score < 0.50) return 'Center-Left';
  if (score < 0.56) return 'Moderate';
  if (score < 0.64) return 'Center-Right';
  if (score < 0.74) return 'Right-Leaning';
  if (score < 0.87) return 'Conservative';
  return 'Very Conservative';
}

function ideologyColor(score) {
  if (score < 0.50) {
    const t = score / 0.50;
    const r = Math.round(59 + (156 - 59) * t);
    const g = Math.round(130 + (163 - 130) * t);
    const b = Math.round(246 + (175 - 246) * t);
    return `rgb(${r},${g},${b})`;
  } else {
    const t = (score - 0.50) / 0.50;
    const r = Math.round(156 + (220 - 156) * t);
    const g = Math.round(163 + (38  - 163) * t);
    const b = Math.round(175 + (38  - 175) * t);
    return `rgb(${r},${g},${b})`;
  }
}

function buildIdeologyBar(rep) {
  if (rep.isPlaceholder || rep.isDirectory) return '';
  const score = rep.ideologyScore;
  if (score == null) return '';
  const pct = Math.round(score * 100);
  const label = ideologyLabel(score);
  const color = ideologyColor(score);
  const sourceNote = rep.level === 'federal'
    ? 'Score: DW-NOMINATE (Voteview)'
    : 'Estimate based on party affiliation';
  return `<div class="detail-section ideology-section">
    <div class="section-label">Political Spectrum</div>
    <div class="ideology-bar-wrap">
      <div class="ideology-track">
        <div class="ideology-fill" style="width:${pct}%"></div>
        <div class="ideology-marker" style="left:${pct}%">
          <div class="ideology-tooltip">${escHtml(label)}</div>
        </div>
      </div>
      <div class="ideology-rail-labels">
        <span>◀ Progressive</span>
        <span>Conservative ▶</span>
      </div>
      <div class="ideology-descriptor" style="color:${color}">${escHtml(label)}</div>
      <div class="ideology-source">${escHtml(sourceNote)}</div>
    </div>
  </div>`;
}

function buildStats(rep) {
  const lines = [];
  if (rep.termStart) lines.push(`<div class="contact-item"><span class="contact-icon">📅</span><span>In office since ${escHtml(rep.termStart)}</span></div>`);
  if (rep.termEnd && rep.termEnd > new Date().getFullYear().toString()) lines.push(`<div class="contact-item"><span class="contact-icon">🗓️</span><span>Term ends ${escHtml(rep.termEnd)}</span></div>`);
  if (!lines.length) return '';
  return `<div class="detail-section"><div class="section-label">Term</div><div class="contact-list">${lines.join('')}</div></div>`;
}

function buildContact(rep) {
  const items = [];
  if (rep.phones?.[0]) items.push(`<div class="contact-item"><span class="contact-icon">📞</span><span>${escHtml(rep.phones[0])}</span></div>`);
  if (rep.emails?.[0]) items.push(`<div class="contact-item"><span class="contact-icon">✉️</span><a href="mailto:${escHtml(rep.emails[0])}">${escHtml(rep.emails[0])}</a></div>`);
  if (rep.address?.[0]?.line1) items.push(`<div class="contact-item"><span class="contact-icon">🏛️</span><span>${escHtml(rep.address[0].line1)}</span></div>`);
  if (!items.length) return '';
  return `<div class="detail-section"><div class="section-label">Contact</div><div class="contact-list">${items.join('')}</div></div>`;
}

function buildFunders(rep) {
  const hasFunders = rep.topFunders?.length > 0;
  const hasFunderUrl = rep.funderUrl;

  // State / local: no direct data — show a public disclosure link
  if (!hasFunders && hasFunderUrl) {
    const source = rep.funderSource || 'Public Records';
    const lastName = escHtml(rep.name.split(' ').slice(-1)[0]);
    return `<div class="detail-section">
      <div class="section-label">Campaign Finance</div>
      <div class="funders-source">
        Campaign finance records for ${lastName} are public at ${escHtml(source)}.
        <a class="tec-link" href="${escHtml(rep.funderUrl)}" target="_blank" rel="noopener">View on ${escHtml(source)} →</a>
      </div>
    </div>`;
  }

  if (!hasFunders) return '';

  const cycle = rep.funderCycle || rep.topFunders[0]?.cycle;
  const cycleLabel = cycle ? ` · ${cycle} election` : '';
  const sorted = [...rep.topFunders].sort((a, b) => a.amount - b.amount);
  const rows = sorted.map(f => {
    const amt = f.amount ? `$${Math.round(f.amount).toLocaleString()}` : '';
    const nameHtml = f.url
      ? `<a class="funder-link" href="${escHtml(f.url)}" target="_blank" rel="noopener">${escHtml(f.name)}</a>`
      : `<span>${escHtml(f.name)}</span>`;
    return `<div class="funder-item">
      <span class="funder-name">${nameHtml}</span>
      ${amt ? `<span class="funder-amount">${amt}</span>` : ''}
    </div>`;
  }).join('');
  return `<div class="detail-section">
    <div class="section-label">Major Financial Backers${escHtml(cycleLabel)}</div>
    <div class="funders-list">${rows}</div>
    <div class="funders-source">Source: FEC.gov — outside spending &amp; PAC contributions</div>
  </div>`;
}

function buildSocialSection(channels = []) {
  if (!channels.length) return '';
  const links = channels.map(ch => {
    const { url, emoji, cls } = socialMeta(ch.type, ch.id);
    return `<a class="social-btn social-${cls}" href="${escHtml(url)}" target="_blank" rel="noopener">${emoji} ${escHtml(ch.type)}</a>`;
  }).join('');
  return `<div class="detail-section"><div class="section-label">Social Media</div><div class="social-row">${links}</div></div>`;
}

function socialMeta(type, id) {
  switch ((type || '').toLowerCase()) {
    case 'twitter': return { url: `https://twitter.com/${id}`, emoji: '𝕏', cls: 'twitter' };
    case 'youtube': return { url: `https://youtube.com/user/${id}`, emoji: '▶', cls: 'youtube' };
    case 'facebook': return { url: `https://facebook.com/${id}`, emoji: 'f', cls: 'facebook' };
    case 'instagram': return { url: `https://instagram.com/${id}`, emoji: '📸', cls: 'instagram' };
    default: return { url: '#', emoji: '🔗', cls: 'other' };
  }
}

// ── UTILITIES ─────────────────────────────────────────────────────────────────

function officialEmoji(office = '') {
  const o = office.toLowerCase();
  if (o.includes('president') || o.includes('governor')) return '🏛️';
  if (o.includes('senator') || o.includes('senate')) return '👤';
  if (o.includes('representative') || o.includes('congress')) return '👤';
  if (o.includes('mayor')) return '🏙️';
  if (o.includes('attorney')) return '⚖️';
  if (o.includes('judge') || o.includes('justice')) return '⚖️';
  if (o.includes('sheriff')) return '🚔';
  if (o.includes('school')) return '🏫';
  return '🏛️';
}

function officeToIssue(office = '') {
  const o = office.toLowerCase();
  if (o.includes('attorney general')) return 'Law Enforcement & Legal Affairs';
  if (o.includes('treasurer')) return 'State Finances & Budget';
  if (o.includes('education')) return 'Education Policy';
  if (o.includes('health')) return 'Public Health';
  if (o.includes('transportation')) return 'Transportation & Infrastructure';
  if (o.includes('sheriff')) return 'Law Enforcement & Public Safety';
  if (o.includes('mayor')) return 'City Government & Local Services';
  if (o.includes('school') || o.includes('board of education')) return 'K-12 Education';
  return null;
}

function escHtml(str = '') {
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── UI HELPERS ────────────────────────────────────────────────────────────────

function setLoading(on, msg = '') {
  const ls = document.getElementById('loading-state');
  const btn = document.querySelector('.btn-search');
  if (on) {
    ['intro-state', 'results-container', 'error-state'].forEach(id =>
      document.getElementById(id).classList.add('hidden'));
    ls.classList.remove('hidden');
    document.getElementById('loading-msg').textContent = msg;
    btn.innerHTML = '<span id="search-btn-text">⏳</span>';
    btn.classList.add('loading');
  } else {
    ls.classList.add('hidden');
    btn.innerHTML = '<span id="search-btn-text">Search</span>';
    btn.classList.remove('loading');
  }
}

function showError(title, msg) {
  ['intro-state', 'results-container', 'loading-state'].forEach(id =>
    document.getElementById(id).classList.add('hidden'));
  document.getElementById('error-state').classList.remove('hidden');
  document.getElementById('error-title').textContent = title;
  document.getElementById('error-msg').style.whiteSpace = 'pre-wrap';
  document.getElementById('error-msg').textContent = msg;
}

function hideError() {
  document.getElementById('error-state').classList.add('hidden');
  document.getElementById('intro-state').classList.remove('hidden');
}
