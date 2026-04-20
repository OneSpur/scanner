// ── Mode switching ────────────────────────────────────────────────────────────

function selectMode(mode) {
  document.getElementById('auto-panel').style.display = mode === 'auto' ? 'flex' : 'none';
  document.getElementById('manual-panel').style.display = mode === 'manual' ? 'flex' : 'none';
  var cardAuto = document.getElementById('mode-card-auto');
  var cardManual = document.getElementById('mode-card-manual');
  if (cardAuto) cardAuto.classList.toggle('active', mode === 'auto');
  if (cardManual) cardManual.classList.toggle('active', mode === 'manual');
  localStorage.setItem('sd_mode', mode);
}

// ── Manual mode helpers ───────────────────────────────────────────────────────

function syncAutoRepo(val) {
  const manualInput = document.getElementById('repo-url-input');
  if (manualInput) manualInput.value = val;
  localStorage.setItem('sd_repo_url', val);
  _schedulePrefetch(val.trim());
}

function selectProvider(provider) {
  document.querySelectorAll('.provider-card').forEach(c => c.classList.remove('active'));
  document.getElementById('card-' + provider).classList.add('active');

  const section = document.getElementById('repo-section');
  section.classList.add('visible');

  const repoInput = document.getElementById('repo-url-input');
  repoInput.placeholder = provider === 'gitlab'
    ? 'https://gitlab.com/owner/repo'
    : 'https://github.com/owner/repo';

  document.getElementById('github-token-section').style.display = provider === 'github' ? '' : 'none';
  document.getElementById('gitlab-token-section').style.display = provider === 'gitlab' ? '' : 'none';

  repoInput.focus();
}

function selectModel(card, provider, model) {
  // Update whichever set of cards is visible
  card.closest('.model-cards').querySelectorAll('.model-card').forEach(c => c.classList.remove('active'));
  card.classList.add('active');
  localStorage.setItem('sd_provider', provider);
  localStorage.setItem('sd_model', model);

  // Auto-mode hidden inputs
  var pi = document.getElementById('provider-input');
  var mi = document.getElementById('model-input');
  if (pi) pi.value = provider;
  if (mi) mi.value = model;

  // Manual-mode hidden inputs
  var pim = document.getElementById('provider-input-m');
  var mim = document.getElementById('model-input-m');
  if (pim) pim.value = provider;
  if (mim) mim.value = model;

  // API key field — auto mode
  var field = document.getElementById('api-key-field');
  var label = document.getElementById('api-key-label');
  var input = document.getElementById('api-key-input');
  if (field) {
    if (provider === 'claude') {
      field.style.display = 'flex';
      label.textContent = 'Anthropic API key';
      input.placeholder = 'sk-ant-...';
    } else if (provider === 'openai') {
      field.style.display = 'flex';
      label.textContent = 'OpenAI API key';
      input.placeholder = 'sk-...';
    } else {
      field.style.display = 'none';
    }
  }

  // API key field — manual mode
  var fieldM = document.getElementById('api-key-field-m');
  var labelM = document.getElementById('api-key-label-m');
  var inputM = document.getElementById('api-key-input-m');
  if (fieldM) {
    if (provider === 'claude') {
      fieldM.style.display = 'flex';
      labelM.textContent = 'Anthropic API key';
      inputM.placeholder = 'sk-ant-...';
    } else if (provider === 'openai') {
      fieldM.style.display = 'flex';
      labelM.textContent = 'OpenAI API key';
      inputM.placeholder = 'sk-...';
    } else {
      fieldM.style.display = 'none';
    }
  }
}

function selectWindow(card, days) {
  card.closest('.window-cards').querySelectorAll('.window-card').forEach(c => c.classList.remove('active'));
  card.classList.add('active');
  localStorage.setItem('sd_window', days);
  // Auto mode
  var pi = document.getElementById('period-input');
  if (pi) pi.value = days;
  // Manual mode
  var pim = document.getElementById('period-input-m');
  if (pim) pim.value = days;
}

// ── Step lock/unlock ──────────────────────────────────────────────────────────

function checkStep2() {
  var gh = localStorage.getItem('sd_gh_token');
  var gl = localStorage.getItem('sd_gl_token');
  var repoUrl = (localStorage.getItem('sd_repo_url') || '').trim();
  var step2 = document.getElementById('step2-block');
  if (!step2) return;
  if ((gh || gl) && repoUrl) {
    step2.classList.remove('step-locked');
  } else {
    step2.classList.add('step-locked');
  }
}

// ── Auto-mode: OAuth via relay ────────────────────────────────────────────────

var _TOKEN_KEYS = {
  github: 'sd_gh_token',
  gitlab: 'sd_gl_token',
  linear: 'sd_linear_token',
  slack:  'sd_slack_token',
  jira:   'sd_auto_jira_token',
};

// Cached list of GitHub repos [{full_name, html_url, private}]
var _ghRepos = [];

function connectService(service) {
  var url = '/connect/' + service;
  var popup = window.open(url, 'oauth_' + service,
    'width=660,height=720,left=200,top=80,noopener=0');
  if (!popup || popup.closed || typeof popup.closed === 'undefined') {
    window.location.href = url;
  }
}

// Receive token back from the /callback page via postMessage
window.addEventListener('message', function(e) {
  if (e.origin !== window.location.origin) return;
  var data = e.data;
  if (!data || !data.service) return;

  var service = data.service;
  if (data.error || !data.token) {
    console.warn('OAuth error for ' + service + ':', data.error);
    var btn = document.getElementById('connect-btn-' + service);
    if (btn) btn.textContent = 'Retry →';
    return;
  }

  var key = _TOKEN_KEYS[service];
  if (key) localStorage.setItem(key, data.token);
  if (data.jira_cloud_url) localStorage.setItem('sd_auto_jira_url', data.jira_cloud_url);

  markConnected(service);

  if (service === 'github') loadGithubRepos(data.token);

  checkStep2();
});

function disconnectService(service) {
  var key = _TOKEN_KEYS[service];
  if (key) localStorage.removeItem(key);
  if (service === 'jira') {
    localStorage.removeItem('sd_auto_jira_url');
    localStorage.removeItem('sd_auto_jira_refresh_token');
  }
  if (service === 'slack') {
    _slackAllChannels = [];
    _slackScopeError = false;
    _slackSelectedChannels = [];
    localStorage.removeItem('sd_slack_channels');
    var picker = document.getElementById('slack-channel-picker');
    if (picker) picker.classList.remove('visible');
    var searchInput = document.getElementById('slack-channel-search');
    if (searchInput) searchInput._slackBound = false;
    var status = document.getElementById('slack-channel-status');
    if (status) status.textContent = '';
  }
  if (service === 'github') {
    var ghInput = document.getElementById('auto-repo-input');
    if (ghInput) ghInput.setAttribute('disabled', '');
    _ghRepos = [];
  }
  if (service === 'gitlab') {
    var glInput = document.getElementById('auto-repo-input-gl');
    if (glInput) glInput.setAttribute('disabled', '');
  }
  var btn = document.getElementById('connect-btn-' + service);
  if (btn) {
    btn.innerHTML = 'Connect →';
    btn.classList.remove('connected');
    btn.onclick = function() { connectService(service); };
  }
  var tile = document.getElementById('tile-' + service);
  if (tile) tile.classList.remove('tile-connected');
  checkStep2();
}

function markConnected(service) {
  var btn = document.getElementById('connect-btn-' + service);
  if (btn) {
    btn.innerHTML = '<svg class="btn-check-icon" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>'
      + '<span class="btn-label-connected">Connected</span>'
      + '<span class="btn-label-disconnect">Disconnect</span>';
    btn.classList.add('connected');
    btn.onclick = function() { disconnectService(service); };
  }
  var tile = document.getElementById('tile-' + service);
  if (tile) tile.classList.add('tile-connected');

  if (service === 'slack') {
    var picker = document.getElementById('slack-channel-picker');
    if (picker) picker.classList.add('visible');
    _slackInitPicker();
  }
  if (service === 'github') {
    var ghInput = document.getElementById('auto-repo-input');
    if (ghInput) ghInput.removeAttribute('disabled');
  }
  if (service === 'gitlab') {
    var glInput = document.getElementById('auto-repo-input-gl');
    if (glInput) {
      glInput.removeAttribute('disabled');
      glInput.value = localStorage.getItem('sd_repo_url') || '';
    }
  }

  // Re-estimate whenever an integration is connected
  if (['jira', 'linear', 'slack'].includes(service)) {
    _schedulePrefetch();
  }
}

// ── Slack channel picker ──────────────────────────────────────────────────────

var _slackSelectedChannels = []; // [{id, name}]
var _slackSearchTimeout = null;
var _slackAllChannels = [];    // cached full list
var _slackScopeError = false;

function _slackInitPicker() {
  // Restore previously selected channels from localStorage
  try {
    var saved = localStorage.getItem('sd_slack_channels');
    if (saved) _slackSelectedChannels = JSON.parse(saved);
  } catch(e) { _slackSelectedChannels = []; }
  _slackRenderChips();

  // Clear stale status if channels already loaded
  if (_slackAllChannels.length > 0) _slackSetStatus('');

  var input = document.getElementById('slack-channel-search');
  if (!input || input._slackBound) return;
  input._slackBound = true;

  input.addEventListener('focus', function() {
    _slackOpenDropdown(this.value);
  });
  input.addEventListener('input', function() {
    clearTimeout(_slackSearchTimeout);
    var q = this.value;
    _slackSearchTimeout = setTimeout(function() { _slackOpenDropdown(q); }, 150);
  });
  input.addEventListener('blur', function() {
    setTimeout(function() {
      var dd = document.getElementById('slack-channel-dropdown');
      if (dd) dd.classList.remove('open');
    }, 180);
  });

  // Load channels immediately in background (like GitHub repos)
  _slackLoadChannels();
}

function _slackSetStatus(html) {
  var el = document.getElementById('slack-channel-status');
  if (!el) return;
  el.innerHTML = html;
}

function _slackLoadChannels() {
  if (_slackAllChannels.length > 0 || _slackScopeError) return;
  var token = localStorage.getItem('sd_slack_token') || '';
  if (!token) return;

  _slackSetStatus('<span class="channel-status-loading">Loading channels…</span>');

  fetch('/api/slack/channels', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token: token, query: '' })
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data && data.error === 'invalid_auth') {
      // Token is revoked/expired — prompt reconnect
      _slackScopeError = true;
      _slackSetStatus('<span class="channel-status-warn">Token expired — <button type="button" class="channel-reconnect-btn" onclick="disconnectService(\'slack\')">Reconnect Slack</button></span>');
      return;
    }
    if (!Array.isArray(data)) {
      // missing_scope or other — fallback to manual entry
      _slackScopeError = true;
      _slackSetStatus('');
      var input = document.getElementById('slack-channel-search');
      if (input && document.activeElement === input) _slackRenderFallbackDropdown(input.value);
      return;
    }
    _slackAllChannels = data;
    _slackSetStatus('');
    var input = document.getElementById('slack-channel-search');
    if (input && document.activeElement === input) {
      _slackRenderDropdown(input.value, data);
    }
  })
  .catch(function(err) {
    console.error('[Slack channels] fetch error:', err);
    _slackScopeError = true;
    _slackSetStatus('');
  });
}

function _slackOpenDropdown(query) {
  if (_slackScopeError) {
    _slackRenderFallbackDropdown(query);
    return;
  }
  if (_slackAllChannels.length > 0) {
    _slackRenderDropdown(query, _slackAllChannels);
    return;
  }
  // Still loading — dropdown stays closed; status line shows "Loading channels…"
  var dd = document.getElementById('slack-channel-dropdown');
  if (dd) dd.classList.remove('open');
}

function _slackRenderFallbackDropdown(query) {
  var dd = document.getElementById('slack-channel-dropdown');
  if (!dd) return;
  var name = (query || '').trim().replace(/^#/, '');
  if (!name) {
    dd.innerHTML = '<div class="channel-dropdown-empty">Type a channel name to add it</div>';
    dd.classList.add('open');
  } else {
    dd.innerHTML = '<button type="button" class="channel-dropdown-item channel-add-item" id="slack-add-btn">'
                 + '<span class="ch-hash">#</span><strong>' + name + '</strong>'
                 + '<span class="ch-count" style="margin-left:auto;color:#6366f1">Add →</span>'
                 + '</button>';
    dd.classList.add('open');
    var btn = document.getElementById('slack-add-btn');
    if (btn) btn.addEventListener('mousedown', function(e) {
      e.preventDefault();
      _slackSelectChannel({ id: name, name: name });
      var input = document.getElementById('slack-channel-search');
      if (input) input.value = '';
      _slackRenderFallbackDropdown('');
    });
  }
  // Bind Enter key once
  var input = document.getElementById('slack-channel-search');
  if (input && !input._enterBound) {
    input._enterBound = true;
    input.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        var n = this.value.trim().replace(/^#/, '');
        if (n) { _slackSelectChannel({ id: n, name: n }); this.value = ''; _slackRenderFallbackDropdown(''); }
      }
    });
  }
}

function _slackRenderDropdown(query, channels) {
  var dd = document.getElementById('slack-channel-dropdown');
  if (!dd) return;
  var q = (query || '').toLowerCase().replace(/^#/, '');
  var selectedIds = _slackSelectedChannels.map(function(c) { return c.id; });

  var filtered = channels.filter(function(c) {
    return (!q || c.name.toLowerCase().indexOf(q) !== -1) &&
           selectedIds.indexOf(c.id) === -1;
  }).slice(0, 25);

  if (!filtered.length) {
    dd.innerHTML = '<div class="channel-dropdown-empty">' + (q ? 'No channels match "' + q + '"' : 'No channels') + '</div>';
  } else {
    dd.innerHTML = filtered.map(function(c) {
      return '<button type="button" class="channel-dropdown-item" data-id="' + c.id + '" data-name="' + c.name + '">'
           + '<span class="ch-hash">#</span>'
           + c.name
           + (c.num_members ? '<span class="ch-count">' + c.num_members + '</span>' : '')
           + '</button>';
    }).join('');
    dd.querySelectorAll('.channel-dropdown-item').forEach(function(el) {
      el.addEventListener('mousedown', function(e) {
        e.preventDefault();
        _slackSelectChannel({ id: this.dataset.id, name: this.dataset.name });
        var input = document.getElementById('slack-channel-search');
        if (input) { input.value = ''; }
        _slackRenderDropdown('', _slackAllChannels);
      });
    });
  }
  dd.classList.add('open');
}

function _slackSelectChannel(ch) {
  if (_slackSelectedChannels.some(function(c) { return c.id === ch.id; })) return;
  _slackSelectedChannels.push(ch);
  localStorage.setItem('sd_slack_channels', JSON.stringify(_slackSelectedChannels));
  _slackRenderChips();
}

function _slackRemoveChannel(id) {
  _slackSelectedChannels = _slackSelectedChannels.filter(function(c) { return c.id !== id; });
  localStorage.setItem('sd_slack_channels', JSON.stringify(_slackSelectedChannels));
  _slackRenderChips();
  // Re-render dropdown so removed channel reappears
  var input = document.getElementById('slack-channel-search');
  _slackRenderDropdown(input ? input.value : '', _slackAllChannels);
}

function _slackRenderChips() {
  var container = document.getElementById('slack-channel-chips');
  if (!container) return;
  container.innerHTML = _slackSelectedChannels.map(function(c) {
    return '<span class="channel-chip">#' + c.name
         + '<button type="button" class="channel-chip-remove" onclick="_slackRemoveChannel(\'' + c.id + '\')" title="Remove">×</button>'
         + '</span>';
  }).join('');
}

function restoreConnections() {
  Object.keys(_TOKEN_KEYS).forEach(function(service) {
    if (localStorage.getItem(_TOKEN_KEYS[service])) {
      markConnected(service);
    }
  });

  var ghToken = localStorage.getItem('sd_gh_token');
  if (ghToken) loadGithubRepos(ghToken);

  checkStep2();
}

// ── Repo picker ───────────────────────────────────────────────────────────────

function loadGithubRepos(token) {
  fetch('https://api.github.com/user/repos?per_page=100&sort=updated&visibility=all', {
    headers: { 'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json' }
  })
  .then(function(r) { return r.json(); })
  .then(function(repos) {
    if (!Array.isArray(repos)) return;
    _ghRepos = repos.map(function(r) {
      return { name: r.full_name, url: r.html_url, priv: r.private };
    });
    var hint = document.getElementById('repo-picker-hint');
    var input = document.getElementById('auto-repo-input');
    if (hint) hint.style.display = 'none';
    if (input && !input.value) input.placeholder = 'Search or paste URL…';
  })
  .catch(function() {});
}

function onRepoFocus() {
  var input = document.getElementById('auto-repo-input');
  if (_ghRepos.length) showRepoDrop((input && input.value) || '');
}

function onRepoInput(val) {
  syncAutoRepo(val);
  checkStep2();
  if (_ghRepos.length) showRepoDrop(val);
  else hideRepoDrop();
}

function showRepoDrop(filter) {
  var drop = document.getElementById('repo-dropdown');
  if (!drop || !_ghRepos.length) return;
  var card = drop.closest('.service-card');
  if (card) card.classList.add('dropdown-open');

  var q = (filter || '').trim().toLowerCase();
  if (q.startsWith('http')) { hideRepoDrop(); return; }

  var matches = _ghRepos.filter(function(r) {
    return !q || r.name.toLowerCase().includes(q);
  }).slice(0, 8);

  if (!matches.length) { hideRepoDrop(); return; }

  drop.innerHTML = matches.map(function(r) {
    return '<li class="repo-drop-item" onclick="pickRepo(\'' + r.url + '\')">'
      + '<span class="repo-drop-name">' + r.name + '</span>'
      + (r.priv ? '<span class="repo-drop-badge">private</span>' : '<span class="repo-drop-badge repo-drop-badge-pub">public</span>')
      + '</li>';
  }).join('');
  drop.classList.add('open');
}

function hideRepoDrop() {
  var drop = document.getElementById('repo-dropdown');
  if (drop) {
    drop.classList.remove('open');
    var card = drop.closest('.service-card');
    if (card) card.classList.remove('dropdown-open');
  }
}

function pickRepo(url) {
  var input = document.getElementById('auto-repo-input');
  if (input) { input.value = url; syncAutoRepo(url); }
  hideRepoDrop();
  checkStep2();
}

document.addEventListener('click', function(e) {
  var drop = document.getElementById('repo-dropdown');
  var input = document.getElementById('auto-repo-input');
  if (drop && input && !input.contains(e.target) && !drop.contains(e.target)) {
    hideRepoDrop();
  }
});

// ── Spec document upload ──────────────────────────────────────────────────────

var _docFiles = [];

function onDocDrop(e) {
  e.preventDefault();
  document.getElementById('dropzone').classList.remove('dragover');
  addDocFiles(Array.from(e.dataTransfer.files));
}

function onDocPick(fileList) {
  addDocFiles(Array.from(fileList));
  document.getElementById('docs-input').value = '';
}

function addDocFiles(files) {
  var allowed = ['.txt', '.md', '.pdf', '.docx'];
  files.forEach(function(f) {
    var ext = f.name.slice(f.name.lastIndexOf('.')).toLowerCase();
    if (!allowed.includes(ext)) return;
    if (_docFiles.find(function(x) { return x.name === f.name; })) return;
    _docFiles.push(f);
  });
  renderDocChips();
  _schedulePrefetch();
}

function removeDoc(name) {
  _docFiles = _docFiles.filter(function(f) { return f.name !== name; });
  renderDocChips();
  _schedulePrefetch();
}

function renderDocChips() {
  var container = document.getElementById('doc-chips');
  if (!container) return;
  container.innerHTML = _docFiles.map(function(f) {
    var kb = Math.round(f.size / 1024);
    var safeName = f.name.replace(/'/g, "\\'");
    return '<span class="doc-chip">'
      + '<span class="doc-chip-name">' + f.name + '</span>'
      + '<span class="doc-chip-size">' + kb + 'kb</span>'
      + '<button type="button" class="doc-chip-remove" onclick="removeDoc(\'' + safeName + '\')">'
      + '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>'
      + '</button></span>';
  }).join('');

  var dropzone = document.getElementById('dropzone');
  if (dropzone) dropzone.classList.toggle('has-files', _docFiles.length > 0);
}

// ── Auto-mode: submit ─────────────────────────────────────────────────────────

function submitAuto() {
  console.log('[submitAuto] Function called');
  var repoUrl = (localStorage.getItem('sd_repo_url') || '').trim();
  console.log('[submitAuto] Repo URL:', repoUrl);
  if (!repoUrl) {
    console.log('[submitAuto] No repo URL - focusing input');
    var ghInput = document.getElementById('auto-repo-input');
    var glInput = document.getElementById('auto-repo-input-gl');
    if (ghInput && ghInput.closest('#github-repo-picker') && ghInput.closest('#github-repo-picker').style.display !== 'none') ghInput.focus();
    else if (glInput) glInput.focus();
    return;
  }
  console.log('[submitAuto] Proceeding with form submission');

  document.getElementById('af-repo').value         = repoUrl;
  document.getElementById('af-gh-token').value     = localStorage.getItem('sd_gh_token') || '';
  document.getElementById('af-gl-token').value     = localStorage.getItem('sd_gl_token') || '';
  document.getElementById('af-jira-url').value          = localStorage.getItem('sd_auto_jira_url') || '';
  document.getElementById('af-jira-email').value        = '';
  document.getElementById('af-jira-token').value        = localStorage.getItem('sd_auto_jira_token') || '';
  document.getElementById('af-jira-refresh-token').value = localStorage.getItem('sd_auto_jira_refresh_token') || '';
  document.getElementById('af-linear-token').value = localStorage.getItem('sd_linear_token') || '';
  document.getElementById('af-slack-token').value    = localStorage.getItem('sd_slack_token') || '';
  document.getElementById('af-slack-channels').value = _slackSelectedChannels.map(function(c) { return c.name; }).join(',');
  document.getElementById('af-provider').value     = document.getElementById('provider-input').value || 'claude-code';
  document.getElementById('af-model').value        = document.getElementById('model-input').value || 'claude-sonnet-4-6';
  document.getElementById('af-api-key').value      = (document.getElementById('api-key-input') || {}).value || '';
  document.getElementById('af-period').value       = document.getElementById('period-input').value || '90';

  if (_docFiles.length) {
    var dt = new DataTransfer();
    _docFiles.forEach(function(f) { dt.items.add(f); });
    document.getElementById('af-docs').files = dt.files;
  }

  console.log('[submitAuto] Submitting form');
  console.log('[submitAuto] Form data:', {
    repo: document.getElementById('af-repo').value,
    provider: document.getElementById('af-provider').value,
    model: document.getElementById('af-model').value,
  });
  document.getElementById('auto-form').submit();
}

// ── Prefetch estimate ─────────────────────────────────────────────────────────

var _prefetchTimer = null;
var _prefetchPollTimer = null;

var _MODE_LABELS = {
  'drift': 'pairs',
  'spec-scan': 'spec sections',
  'jira-first': 'tickets',
  'linear-first': 'tickets',
};

function _showEstimate(el, data) {
  var units = data.pairs || 0;
  var tokens = data.tokens_total || 0;
  var secs = data.time_seconds || 0;
  var mode = data.mode || 'drift';
  var unitLabel = _MODE_LABELS[mode] || 'pairs';
  var timeStr = secs < 60 ? secs + 's' : Math.round(secs / 60) + ' min';
  var tokStr = tokens >= 1000 ? Math.round(tokens / 1000) + 'k' : tokens;
  el.className = 'prefetch-estimate ready';
  el.style.display = 'flex';
  el.innerHTML =
    '<div class="prefetch-stat"><div class="prefetch-stat-value">' + units + '</div><div class="prefetch-stat-label">' + unitLabel + '</div></div>' +
    '<div class="prefetch-divider"></div>' +
    '<div class="prefetch-stat"><div class="prefetch-stat-value">' + tokStr + '</div><div class="prefetch-stat-label">tokens est.</div></div>' +
    '<div class="prefetch-divider"></div>' +
    '<div class="prefetch-stat"><div class="prefetch-stat-value">' + timeStr + '</div><div class="prefetch-stat-label">time est.</div></div>';
}

function _gatherPrefetchPayload(repoUrl) {
  var ghToken  = localStorage.getItem('sd_gh_token') || '';
  var period   = parseInt(localStorage.getItem('sd_window') || '90');
  var jiraUrl  = localStorage.getItem('sd_jira_url') || '';
  var jiraToken = localStorage.getItem('sd_jira_token') || '';
  var linearToken = localStorage.getItem('sd_linear_token') || '';
  var slackToken = localStorage.getItem('sd_slack_token') || '';

  // Estimate PDF size from uploaded files
  var pdfChars = 0;
  if (typeof _docFiles !== 'undefined' && _docFiles.length) {
    _docFiles.forEach(function(f) {
      // PDF: ~2 chars per byte after text extraction (rough)
      // Other text files: ~1 char per byte
      var mult = f.name && f.name.toLowerCase().endsWith('.pdf') ? 2 : 1;
      pdfChars += (f.size || 0) * mult;
    });
    pdfChars = Math.min(pdfChars, 60000);  // cap at reasonable max
  }

  return {
    repo_url:     repoUrl,
    github_token: ghToken,
    period_days:  period,
    has_pdf:      pdfChars > 0,
    pdf_chars:    pdfChars,
    has_jira:     !!(jiraUrl && jiraToken),
    has_linear:   !!linearToken,
    has_slack:    !!slackToken,
  };
}

function _triggerPrefetch(repoUrl) {
  if (!repoUrl || !repoUrl.includes('github.com')) return;

  var estimateEls = [
    document.getElementById('prefetch-estimate'),
    document.getElementById('prefetch-estimate-manual'),
  ].filter(Boolean);

  estimateEls.forEach(function(el) {
    el.className = 'prefetch-estimate loading';
    el.style.display = 'flex';
    el.textContent = 'Estimating...';
  });

  fetch('/api/prefetch', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(_gatherPrefetchPayload(repoUrl)),
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (!data.prefetch_id) return;
    var id = data.prefetch_id;
    var attempts = 0;
    function poll() {
      fetch('/api/prefetch/' + id)
        .then(function(r) { return r.json(); })
        .then(function(res) {
          if (res.status === 'done') {
            estimateEls.forEach(function(el) { _showEstimate(el, res.estimate); });
          } else if (res.status === 'error' || attempts > 20) {
            estimateEls.forEach(function(el) { el.style.display = 'none'; });
          } else {
            attempts++;
            _prefetchPollTimer = setTimeout(poll, 1500);
          }
        })
        .catch(function() { estimateEls.forEach(function(el) { el.style.display = 'none'; }); });
    }
    poll();
  })
  .catch(function() { estimateEls.forEach(function(el) { el.style.display = 'none'; }); });
}

function _schedulePrefetch(repoUrl) {
  if (!repoUrl) repoUrl = (localStorage.getItem('sd_repo_url') || '').trim();
  clearTimeout(_prefetchTimer);
  clearTimeout(_prefetchPollTimer);
  _prefetchTimer = setTimeout(function() { _triggerPrefetch(repoUrl); }, 1500);
}

// ── Initialise ────────────────────────────────────────────────────────────────

(function() {
  function bind(inputId, storageKey) {
    var input = document.getElementById(inputId);
    if (!input) return;
    var saved = localStorage.getItem(storageKey);
    if (saved) input.value = saved;
    input.addEventListener('input', function() {
      if (this.value) localStorage.setItem(storageKey, this.value);
      else localStorage.removeItem(storageKey);
    });
  }

  bind('repo-url-input',       'sd_repo_url');
  bind('auto-repo-input',      'sd_repo_url');

  // Trigger prefetch when repo URL changes
  ['repo-url-input', 'auto-repo-input', 'auto-repo-input-gl'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.addEventListener('input', function() { _schedulePrefetch(this.value.trim()); });
  });
  bind('github-token-input',   'sd_gh_token');
  bind('gitlab-token-input',   'sd_gl_token');
  bind('jira-url-input',       'sd_jira_url');
  bind('jira-email-input',     'sd_jira_email');
  bind('jira-token-input',     'sd_jira_token');
  bind('linear-token-input',   'sd_linear_token');
  bind('slack-token-input',    'sd_slack_token');
  bind('slack-channels-input', 'sd_slack_channels');

  // Restore model selection
  var savedProvider = localStorage.getItem('sd_provider');
  var savedModel    = localStorage.getItem('sd_model');
  if (savedProvider && savedModel) {
    var modelCard = document.querySelector(`.model-card[onclick*="'${savedProvider}'"]`);
    if (modelCard) selectModel(modelCard, savedProvider, savedModel);
  }

  // Restore window selection
  var savedWindow = localStorage.getItem('sd_window');
  if (savedWindow) {
    var windowCard = document.querySelector(`.window-card[onclick*="${savedWindow})"]`);
    if (windowCard) selectWindow(windowCard, parseInt(savedWindow));
  }

  // Default to auto mode
  var savedMode = localStorage.getItem('sd_mode') || 'auto';
  selectMode(savedMode);

  // In manual mode, restore platform selection
  var savedRepo = localStorage.getItem('sd_repo_url');
  if (savedRepo && savedMode === 'manual') {
    selectProvider(savedRepo.includes('gitlab') ? 'gitlab' : 'github');
  }

  bind('auto-repo-input-gl', 'sd_repo_url');

  restoreConnections();
  checkStep2();

  // Prefetch on load if repo URL already saved
  var initRepo = localStorage.getItem('sd_repo_url') || '';
  if (initRepo) _schedulePrefetch(initRepo);
})();
