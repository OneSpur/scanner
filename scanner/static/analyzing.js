function showError(msg) {
  errorBox.style.display = 'block';
  const isRateLimit = msg.toLowerCase().includes('rate limit');
  const isNotFound  = msg.toLowerCase().includes('not found');
  const isAiLimit   = msg.toLowerCase().includes('ai model') || msg.toLowerCase().includes('claude code error') || msg.toLowerCase().includes('hit your limit');

  if (isAiLimit) {
    errorBox.innerHTML = `<strong>AI model unavailable</strong><br><br>
      ${msg.replace(/\n/g, '<br>')}
      <div class="error-retry"><a href="/">← Try again or switch to a different model</a></div>`;
  } else if (isRateLimit) {
    const hasToken = msg.toLowerCase().includes('ghp_') || msg.toLowerCase().includes('token');
    errorBox.innerHTML = `<strong>GitHub API rate limit exceeded</strong>
      ${hasToken ? 'Your token may be invalid or the rate limit was hit mid-analysis. Try again in a few minutes.' : 'Large public repos require a GitHub token. Create one at github.com/settings/tokens (public_repo scope).'}<br><br>
      <a href="https://github.com/settings/tokens/new?description=1spur-scanner&scopes=public_repo" target="_blank">→ Create a GitHub token</a>
      <div class="error-retry"><a href="/">← Try again</a></div>`;
  } else if (isNotFound) {
    errorBox.innerHTML = `<strong>Repository not found</strong>${msg}
      <div class="error-retry"><a href="/">← Go back</a></div>`;
  } else {
    errorBox.innerHTML = `<strong>Something went wrong</strong>${msg}
      <div class="error-retry"><a href="/">← Try again</a></div>`;
  }
}

const jobId        = window.__JOB_ID__;
const log          = document.getElementById('log');
const errorBox     = document.getElementById('error-box');
const progressFill = document.getElementById('progress-fill');
const progressLabel = document.getElementById('progress-label');

const STEP_PROGRESS = { 1: 15, 2: 55, 3: 85 };
const STEP_LABELS   = { 1: 'Fetching GitHub data...', 2: 'Analysing drift...', 3: 'Building report...' };

function setProgress(pct, label) {
  progressFill.style.width = pct + '%';
  if (label) progressLabel.textContent = label;
}

let creepPct = 0;
let creepTarget = 14;
const creepInterval = setInterval(() => {
  if (creepPct < creepTarget) {
    creepPct = Math.min(creepPct + 0.4, creepTarget);
    progressFill.style.width = creepPct + '%';
  }
}, 300);

function setStep(n, state) {
  const icon  = document.getElementById(`icon-${n}`);
  const title = document.getElementById(`title-${n}`);
  icon.className  = 'step-icon ' + state;
  title.className = 'step-title ' + state;
  if (state === 'active') {
    icon.innerHTML = `<svg width="10" height="10" viewBox="0 0 10 10" fill="none"><circle cx="5" cy="5" r="4" stroke="currentColor" stroke-width="1.5" stroke-dasharray="20" stroke-dashoffset="0" style="animation:spin 1s linear infinite;transform-origin:center"/></svg>`;
  } else if (state === 'done') {
    icon.innerHTML = `<svg width="10" height="10" viewBox="0 0 10 10" fill="none"><path d="M2 5l2.5 2.5L8 3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  }
}

function addLog(msg) {
  const lines = log.querySelectorAll('.log-line');
  lines.forEach(l => l.classList.remove('latest'));
  const line = document.createElement('div');
  line.className = 'log-line latest';
  line.textContent = '› ' + msg;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

const startTime = Date.now();
const etaLabel  = document.getElementById('eta-label');
let _tokens = null;
let _tokEst = false;

function fmtElapsed(s) {
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}
function updateCounter() {
  const elapsed = Math.floor((Date.now() - startTime) / 1000);
  let text = fmtElapsed(elapsed);
  if (_tokens !== null) {
    const t = _tokEst ? `~${_tokens.toLocaleString()}` : _tokens.toLocaleString();
    text += ` · ${t} tokens used so far`;
  }
  etaLabel.textContent = text;
}
const etaInterval = setInterval(updateCounter, 1000);

setStep(1, 'active');

const es = new EventSource(`/progress/${jobId}`);
es.onmessage = (e) => {
  const data = JSON.parse(e.data);

  if (data.error) { es.close(); showError(data.error); return; }

  if (data.tokens != null) {
    _tokens = data.tokens;
    _tokEst = data.estimated || false;
    updateCounter();
  }

  if (data.msg) {
    addLog(data.msg);
    if (data.step === 1) { setStep(1, 'active'); creepPct = STEP_PROGRESS[1]; creepTarget = 54; setProgress(STEP_PROGRESS[1], STEP_LABELS[1]); }
    if (data.step === 2) { setStep(1, 'done'); setStep(2, 'active'); creepPct = STEP_PROGRESS[2]; creepTarget = 84; setProgress(STEP_PROGRESS[2], STEP_LABELS[2]); }
    if (data.step === 3) { setStep(2, 'done'); setStep(3, 'active'); creepPct = STEP_PROGRESS[3]; creepTarget = 97; setProgress(STEP_PROGRESS[3], STEP_LABELS[3]); }
  }

  if (data.done) {
    es.close();
    clearInterval(creepInterval);
    clearInterval(etaInterval);
    etaLabel.textContent = `done in ${fmtElapsed(Math.floor((Date.now() - startTime) / 1000))}`;
    setStep(3, 'done');
    setProgress(100, 'Done! Loading report...');
    addLog('Report ready - redirecting...');
    setTimeout(() => { window.location.href = `/report/${data.job_id}`; }, 800);
  }
};

es.onerror = () => { es.close(); showError('Connection lost. Please try again.'); };
