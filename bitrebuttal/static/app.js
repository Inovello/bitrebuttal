/* Bit Rebuttal — frontend.
 *
 * Plain ES5+/ES2017 in one IIFE. No framework, no build step, no CDN.
 * Served by FastAPI StaticFiles at /, or opened straight off disk with ?mock=1.
 *
 * Layout of this file:
 *   1. constants + formatting helpers   (ported verbatim from the design's DCLogic)
 *   2. state
 *   3. api            — httpApi / mockApi behind one interface
 *   4. deco()         — job -> view model (the design's deco())
 *   5. render*()      — pure-ish renderers per view
 *   6. actions        — event delegation over [data-action]
 *   7. polling + boot
 */
(function () {
  'use strict';

  // ───────────────────────────── 1. constants ─────────────────────────────

  var STATUS = {
    DOWNLOADING: { label: 'DOWNLOADING', color: '#3fd3dd' },
    RECOVERING:  { label: 'RECOVERING',  color: '#d6982f', spinner: true, note: 'Relaunching aria2c' },
    VERIFYING:   { label: 'VERIFYING',   color: '#3fd3dd', spinner: true, note: 'Checking SHA256…' },
    PAUSED:      { label: 'PAUSED',      color: '#7a756e' },
    COMPLETE:    { label: 'COMPLETE ✓',  color: '#63c98a' },
    FAILED:      { label: 'FAILED',      color: '#e0574f' }
  };

  var FILE_STATE = {
    queued:      { label: 'Queued',      color: '#4a4744' },
    downloading: { label: 'Downloading', color: '#3fd3dd' },
    done:        { label: 'Done ✓',      color: '#63c98a' },
    verifying:   { label: 'Verifying',   color: '#d6982f' },
    corrupt:     { label: 'Corrupt ✗',   color: '#e0574f' }
  };

  var LEVEL = {
    info: { color: '#8a857d', glyph: '·' },
    warn: { color: '#d6982f', glyph: '!' },
    ok:   { color: '#63c98a', glyph: '✓' },
    err:  { color: '#e0574f', glyph: '✗' }
  };

  var HEARTBEAT_WAVE = [17, 13, 9, 6, 5, 5, 5, 5];
  var BEAT_OK   = ['#5ee3ec', '#3fd3dd', '#1f6d73', '#1c1f21'];
  var BEAT_DOWN = ['#e0574f', '#8f3833', '#43201e', '#1c1f21'];

  function gb(b) { return (b / 1e9).toFixed(1) + ' GB'; }

  function speedLabel(bps) {
    if (!bps) return '—';
    if (bps >= 1e6) return (bps / 1e6).toFixed(1) + ' MB/s';
    return Math.round(bps / 1e3) + ' KB/s';
  }

  function etaLabel(sec) {
    if (sec === null || sec === undefined) return '—';
    if (sec <= 0) return 'done';
    var d = Math.floor(sec / 86400),
        h = Math.floor((sec % 86400) / 3600),
        m = Math.floor((sec % 3600) / 60);
    if (d) return d + 'd ' + h + 'h';
    if (h) return h + 'h ' + String(m).padStart(2, '0') + 'm';
    return m + 'm ' + String(Math.floor(sec % 60)).padStart(2, '0') + 's';
  }

  // ─────────────────────────────── 2. state ───────────────────────────────

  var state = {
    view: 'dashboard',          // dashboard | detail | settings  (client-side only)
    jobId: null,

    // last known server payload
    backend:  { healthy: false, label: '', version: '—', uptime: '' },
    disk:     { path: '', freeBytes: 0, volumeLabel: '' },
    settings: { destination: '', connections: 4, stallSensitivity: 'Normal', serviceInstalled: false },
    jobs: [],

    connected: false,           // last poll succeeded
    loadedOnce: false,
    beat: 0,

    // add-panel
    url: '',
    resolve: 'idle',            // idle | resolving | resolved | error
    resolveError: '',
    resolved: null,             // { repo, revision, resolvedAt }
    picker: [],                 // [{ name, bytes, sha256, selected }]
    pickerToken: 0,             // bumped whenever the rows must be rebuilt
    starting: false,            // POST /api/jobs in flight
    startError: '',
    destOverride: null,         // set by [Change] — used for the next job only

    // settings view
    draft: null,                // editable copy of settings
    draftDirty: false,
    saved: false,
    settingsError: '',

    // cancel dialog
    confirm: null,              // { id, name, done }
    confirmDeleteFiles: true,
    confirmError: ''
  };

  var MOCK = (typeof window !== 'undefined' && window.LR_MOCK) || null;

  // ──────────────────────────────── 3. api ────────────────────────────────

  function jsonOrThrow(res) {
    return res.text().then(function (body) {
      var data = null;
      if (body) { try { data = JSON.parse(body); } catch (e) { /* non-JSON */ } }
      if (!res.ok) {
        var msg = (data && data.error) || ('Request failed (' + res.status + ')');
        var err = new Error(msg);
        err.status = res.status;
        throw err;
      }
      return data;
    });
  }

  function send(method, path, body) {
    var opts = { method: method, headers: {} };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(jsonOrThrow);
  }

  var httpApi = {
    status:       function ()             { return send('GET', 'api/status'); },
    resolve:      function (url)          { return send('POST', 'api/resolve', { url: url }); },
    createJob:    function (payload)      { return send('POST', 'api/jobs', payload); },
    pause:        function (id)           { return send('POST', 'api/jobs/' + encodeURIComponent(id) + '/pause'); },
    resume:       function (id)           { return send('POST', 'api/jobs/' + encodeURIComponent(id) + '/resume'); },
    cancel:       function (id, del)      { return send('DELETE', 'api/jobs/' + encodeURIComponent(id) + '?deleteFiles=' + (del ? 'true' : 'false')); },
    saveSettings: function (s)            { return send('PUT', 'api/settings', s); },
    service:      function (op)           { return send('POST', 'api/service/' + op); }
  };

  // Mock implementation — same interface, same shapes, plus the design's fake
  // behaviours (1s progress tick, 1.4s resolve delay).
  function makeMockApi(seed) {
    var db = {
      backend:  Object.assign({}, seed.backend),
      disk:     Object.assign({}, seed.disk),
      settings: Object.assign({}, seed.settings),
      resolved: seed.resolved,
      jobs:     seed.jobs.map(function (j) { return Object.assign({}, j); })
    };

    function tick() {
      db.jobs = db.jobs.map(function (j) {
        if (j.status !== 'DOWNLOADING') return j;
        var speed = j.speedBps * (0.9 + Math.random() * 0.22);
        var done = Math.min(j.totalBytes, j.doneBytes + speed);
        var remain = j.totalBytes - done;
        return Object.assign({}, j, {
          doneBytes: done,
          speedBps: speed,
          etaSeconds: speed > 0 ? Math.round(remain / speed) : null
        });
      });
    }

    function later(value, ms) {
      return new Promise(function (resolve) { setTimeout(function () { resolve(value); }, ms || 0); });
    }

    function fail(msg) { return Promise.reject(new Error(msg)); }

    return {
      status: function () {
        tick();
        return later({
          backend: db.backend,
          disk: db.disk,
          settings: db.settings,
          jobs: db.jobs.map(function (j) { return Object.assign({}, j); })
        }, 40);
      },
      resolve: function (url) {
        var v = (url || '').trim();
        if (!v) return fail('Paste a HuggingFace repo id or a direct file URL first.');
        if (/^https?:\/\//i.test(v) && !/huggingface\.co/i.test(v)) {
          return later(null, 700).then(function () {
            return fail('Could not reach ' + v.replace(/^https?:\/\//i, '').split('/')[0] +
                        ' — check the URL or your connection.');
          });
        }
        return later({
          repo: db.resolved.repo,
          revision: db.resolved.revision,
          resolvedAt: db.resolved.resolvedAt,
          files: db.resolved.files.map(function (f) { return Object.assign({}, f); })
        }, 1400);
      },
      createJob: function (payload) {
        var names = payload.files || [];
        var picked = db.resolved.files.filter(function (f) { return names.indexOf(f.name) !== -1; });
        var total = picked.reduce(function (a, f) { return a + f.bytes; }, 0);
        var job = {
          id: 'job-' + Date.now(),
          name: db.resolved.repo,
          subtitle: picked.length + ' files · queued just now',
          status: 'DOWNLOADING',
          dest: payload.dest,
          totalBytes: total,
          doneBytes: 0,
          speedBps: 2.4e6,
          etaSeconds: Math.round(total / 2.4e6),
          recoveries: 0,
          bytesLost: 0,
          startedLabel: 'just now',
          elapsedLabel: '0m',
          avgSpeedBps: 2.4e6,
          files: picked.map(function (f, i) {
            return { name: f.name, bytes: f.bytes, progress: 0, state: i === 0 ? 'downloading' : 'queued' };
          }),
          log: [{ time: 'now', level: 'info',
                  text: 'Job created — ' + picked.length + ' files queued, ' + gb(total) + ' total' }]
        };
        db.jobs = [job].concat(db.jobs);
        return later(job, 250);
      },
      pause: function (id) {
        db.jobs = db.jobs.map(function (j) {
          return j.id === id ? Object.assign({}, j, { status: 'PAUSED', speedBps: 0, etaSeconds: null }) : j;
        });
        return later({ ok: true }, 120);
      },
      resume: function (id) {
        db.jobs = db.jobs.map(function (j) {
          return j.id === id
            ? Object.assign({}, j, { status: 'DOWNLOADING', speedBps: j.avgSpeedBps || 2.1e6 })
            : j;
        });
        return later({ ok: true }, 120);
      },
      cancel: function (id) {
        db.jobs = db.jobs.filter(function (j) { return j.id !== id; });
        return later({ ok: true }, 150);
      },
      saveSettings: function (s) {
        db.settings = Object.assign({}, db.settings, s);
        return later(Object.assign({}, db.settings), 220);
      },
      service: function (op) {
        db.settings = Object.assign({}, db.settings, { serviceInstalled: op === 'install' });
        return later({ installed: db.settings.serviceInstalled }, 220);
      }
    };
  }

  var api = MOCK ? makeMockApi(MOCK) : httpApi;

  // ────────────────────────────── dom helpers ─────────────────────────────

  function qs(sel, root) { return (root || document).querySelector(sel); }
  function field(name, root) { return (root || document).querySelector('[data-field="' + name + '"]'); }
  function setText(el, text) { if (el && el.textContent !== text) el.textContent = text; }
  function fieldText(name, text, root) { setText(field(name, root), text); }
  function show(el, on) { if (el) el.hidden = !on; }
  function color(el, c) { if (el) el.style.setProperty('--c', c); }
  function tpl(id) { return document.getElementById(id).content.firstElementChild; }

  function setProgress(bar, pct, c) {
    if (!bar) return;
    color(bar, c);
    var fill = qs('.progress-fill', bar);
    var w = pct + '%';
    if (fill.style.width !== w) fill.style.width = w;
  }

  // ──────────────────────────────── 4. deco ───────────────────────────────

  var BLANK_JOB = {
    id: '', name: '', subtitle: '', status: 'PAUSED', dest: '',
    totalBytes: 1, doneBytes: 0, speedBps: 0, etaSeconds: null,
    recoveries: 0, bytesLost: 0, avgSpeedBps: 0,
    startedLabel: '', elapsedLabel: '', files: [], log: []
  };

  function deco(j, i) {
    var meta = STATUS[j.status] || STATUS.PAUSED;
    var pct = j.totalBytes ? (j.doneBytes / j.totalBytes) * 100 : 0;
    var lost = j.bytesLost > 0 ? gb(j.bytesLost) + ' lost' : '0 bytes lost';
    return {
      id: j.id,
      index: String((i || 0) + 1).padStart(2, '0'),
      name: j.name,
      subtitle: j.subtitle,
      color: meta.color,
      spinner: !!meta.spinner,
      statusLabel: meta.label,
      statusNote: meta.note || '',
      pct: pct.toFixed(2),
      pctLabel: pct.toFixed(1) + '%',
      transferred: gb(j.doneBytes) + ' / ' + gb(j.totalBytes),
      speedLabel: speedLabel(j.speedBps),
      etaLabel: j.status === 'COMPLETE' ? '—' : etaLabel(j.etaSeconds),
      dest: j.dest,
      recoveries: String(j.recoveries),
      avgSpeedLabel: speedLabel(j.avgSpeedBps),
      elapsedLabel: j.elapsedLabel,
      startedLabel: j.startedLabel,
      resilience: '↻ ' + j.recoveries + ' auto-recoveries · ' + lost,
      lossy: j.bytesLost > 0,
      showPause: j.status === 'DOWNLOADING' || j.status === 'PAUSED' || j.status === 'RECOVERING',
      pauseLabel: j.status === 'PAUSED' ? 'Resume' : 'Pause',
      paused: j.status === 'PAUSED',
      doneLabel: gb(j.doneBytes),
      files: (j.files || []).map(function (f) {
        var fs = FILE_STATE[f.state] || FILE_STATE.queued;
        return { name: f.name, sizeLabel: gb(f.bytes), progress: f.progress, stateLabel: fs.label, color: fs.color };
      }),
      log: (j.log || []).map(function (e) {
        var lv = LEVEL[e.level] || LEVEL.info;
        return { time: e.time, text: e.text, color: lv.color, glyph: lv.glyph };
      })
    };
  }

  function effectiveDest() {
    return state.destOverride !== null ? state.destOverride : state.settings.destination;
  }

  function selectedFiles() {
    return state.picker.filter(function (f) { return f.selected; });
  }

  // ────────────────────────────── 5. renderers ────────────────────────────

  function render() {
    renderNav();
    renderSidebar();
    renderTelemetry();
    renderResolvePanel();
    renderJobs();
    renderDetail();
    renderSettings();
    renderConfirm();
  }

  function renderNav() {
    var dash = qs('[data-action="nav-dashboard"]');
    var set = qs('[data-action="nav-settings"]');
    // The design highlights DASHBOARD for both dashboard and detail views.
    dash.classList.toggle('is-active', state.view !== 'settings');
    set.classList.toggle('is-active', state.view === 'settings');
    qs('#view-dashboard').classList.toggle('is-active', state.view === 'dashboard');
    qs('#view-detail').classList.toggle('is-active', state.view === 'detail');
    qs('#view-settings').classList.toggle('is-active', state.view === 'settings');
  }

  function renderSidebar() {
    var healthy = state.connected && state.backend.healthy;
    var palette = healthy ? BEAT_OK : BEAT_DOWN;

    var hb = field('heartbeat');
    while (hb.children.length < 8) hb.appendChild(document.createElement('i'));
    for (var i = 0; i < 8; i++) {
      var d = (i - state.beat + 8) % 8;
      var bar = hb.children[i];
      bar.style.height = HEARTBEAT_WAVE[d] + 'px';
      bar.style.background = d === 0 ? palette[0] : d < 3 ? palette[1] : d < 5 ? palette[2] : palette[3];
    }

    var status = field('backend-status');
    status.classList.toggle('is-down', !healthy);
    fieldText('backend-status-text', healthy ? 'Supervisor is watching' : 'Supervisor unreachable');
    fieldText('beat-age', (state.beat % 4) + 's ago');
    fieldText('backend-version', state.backend.version || '—');

    var badge = field('service-badge');
    var installed = !!state.settings.serviceInstalled;
    badge.classList.toggle('is-installed', installed);
    setText(badge, installed ? 'Installed, resumes after reboot' : 'Not installed');
  }

  function renderTelemetry() {
    var active = state.jobs.filter(function (j) { return j.status === 'DOWNLOADING'; });
    var agg = active.reduce(function (a, j) { return a + j.speedBps; }, 0);
    var recoveries = state.jobs.reduce(function (a, j) { return a + j.recoveries; }, 0);

    fieldText('telemetry-active',
      String(active.length).padStart(2, '0') + ' / ' + String(state.jobs.length).padStart(2, '0'));
    fieldText('telemetry-aggregate', speedLabel(agg));
    fieldText('telemetry-disk', gb(state.disk.freeBytes));
    fieldText('telemetry-recoveries', String(recoveries));
  }

  // --- add panel / file picker -------------------------------------------

  var pickerRendered = -1;

  function renderResolvePanel() {
    var input = field('url-input');
    if (document.activeElement !== input && input.value !== state.url) input.value = state.url;

    show(field('resolve-state-resolving'), state.resolve === 'resolving');
    show(field('resolve-state-error'), state.resolve === 'error');
    show(field('resolve-state-resolved'), state.resolve === 'resolved');
    fieldText('resolve-error-text', state.resolveError);

    if (state.resolve !== 'resolved' || !state.resolved) { pickerRendered = state.pickerToken - 1; return; }

    fieldText('resolved-repo', state.resolved.repo);
    fieldText('resolved-meta', '@' + state.resolved.revision + ' · resolved ' + state.resolved.resolvedAt);

    if (pickerRendered !== state.pickerToken) {
      pickerRendered = state.pickerToken;
      var rows = qs('#picker-rows');
      rows.textContent = '';
      state.picker.forEach(function (f, i) {
        var row = tpl('tpl-picker-row').cloneNode(true);
        row.dataset.index = String(i);
        qs('input', row).checked = !!f.selected;
        row.classList.toggle('is-off', !f.selected);
        fieldText('picker-filename', f.name, row);
        fieldText('picker-filesize', gb(f.bytes), row);
        var sha = field('picker-sha-tag', row);
        setText(sha, f.sha256 ? 'SHA256 ✓ available' : 'No hash published');
        sha.classList.toggle('is-none', !f.sha256);
        rows.appendChild(row);
      });
    }

    renderSelectionSummary();
  }

  function renderSelectionSummary() {
    var selected = selectedFiles();
    var selBytes = selected.reduce(function (a, f) { return a + f.bytes; }, 0);
    var free = state.disk.freeBytes;
    var over = selBytes > free;

    fieldText('selection-summary',
      selected.length + (selected.length === 1 ? ' file — ' : ' files — ') + gb(selBytes));

    var freeEl = field('free-space');
    setText(freeEl, gb(free) + ' free on ' + (state.disk.volumeLabel || '—'));
    freeEl.classList.toggle('is-over', over);

    fieldText('destination-path', effectiveDest());

    show(field('capacity-error'), over);
    fieldText('capacity-error-text',
      'Selection exceeds free space by ' + gb(selBytes - free) + ' — deselect files or pick another drive.');

    show(field('start-error'), !!state.startError);
    fieldText('start-error-text', state.startError);

    var start = qs('[data-action="start-download"]');
    start.disabled = over || selected.length === 0 || state.starting;
  }

  // --- job list (keyed reconcile, so the .9s width transition survives) ---

  var jobNodes = new Map();

  function renderJobs() {
    var list = qs('#job-list');
    show(field('empty-state'), state.jobs.length === 0 && state.loadedOnce);
    fieldText('job-count', state.jobs.length === 0 ? 'EMPTY' : String(state.jobs.length).padStart(2, '0'));

    var seen = new Set();
    state.jobs.forEach(function (job, i) {
      var d = deco(job, i);
      var el = jobNodes.get(d.id);
      if (!el) {
        el = tpl('tpl-job-card').cloneNode(true);
        el.dataset.jobId = d.id;
        jobNodes.set(d.id, el);
      }
      updateJobCard(el, d);
      seen.add(d.id);
      if (list.children[i] !== el) list.insertBefore(el, list.children[i] || null);
    });
    jobNodes.forEach(function (el, id) {
      if (!seen.has(id)) { el.remove(); jobNodes.delete(id); }
    });
  }

  function updateJobCard(el, d) {
    fieldText('job-index', d.index, el);
    fieldText('job-name', d.name, el);
    fieldText('job-subtitle', d.subtitle, el);
    show(field('job-spinner', el), d.spinner);
    color(field('job-dot', el), d.color);
    var st = field('job-status', el);
    color(st, d.color); setText(st, d.statusLabel);

    var pctEl = field('job-percent', el);
    color(pctEl, d.color); setText(pctEl, d.pctLabel);
    setProgress(field('job-progress', el), d.pct, d.color);

    fieldText('job-transferred', d.transferred, el);
    fieldText('job-speed', d.speedLabel, el);
    fieldText('job-eta', d.etaLabel, el);

    var note = field('job-status-note', el);
    show(note, !!d.statusNote);
    color(note, d.color);
    setText(note, d.statusNote);

    var pause = qs('[data-action="pause-job"]', el);
    pause.hidden = !d.showPause;
    setText(pause, '[' + d.pauseLabel + ']');
    pause.dataset.paused = d.paused ? '1' : '0';

    var res = field('job-resilience', el);
    setText(res, d.resilience);
    res.classList.toggle('is-lossy', d.lossy);
  }

  // --- detail -------------------------------------------------------------

  function renderDetail() {
    if (state.view !== 'detail') return;
    var target = null;
    for (var i = 0; i < state.jobs.length; i++) {
      if (state.jobs[i].id === state.jobId) { target = state.jobs[i]; break; }
    }
    if (!target) {
      // Job vanished server-side (cancelled / pruned) — fall back to the queue.
      if (state.loadedOnce) { state.view = 'dashboard'; state.jobId = null; renderNav(); return; }
      target = BLANK_JOB;
    }
    var d = deco(target, state.jobs.indexOf(target));

    fieldText('detail-name', d.name);
    show(field('detail-spinner'), d.spinner);
    color(field('detail-dot'), d.color);
    var st = field('detail-status'); color(st, d.color); setText(st, d.statusLabel);
    fieldText('detail-dest', d.dest);

    var pctEl = field('detail-percent'); color(pctEl, d.color); setText(pctEl, d.pctLabel);
    setProgress(field('detail-progress'), d.pct, d.color);
    fieldText('detail-transferred', d.transferred + ' · ' + d.speedLabel + ' · ETA ' + d.etaLabel);

    fieldText('stat-recoveries', d.recoveries);
    fieldText('stat-avg-speed', d.avgSpeedLabel);
    fieldText('stat-elapsed', d.elapsedLabel);
    fieldText('stat-started', d.startedLabel);

    var files = qs('#detail-files');
    syncRows(files, d.files.length, 'tpl-file-row');
    d.files.forEach(function (f, i) {
      var row = files.children[i];
      fieldText('file-name', f.name, row);
      fieldText('file-size', f.sizeLabel, row);
      setProgress(field('file-progress', row), f.progress, f.color);
      var s = field('file-state', row); color(s, f.color); setText(s, f.stateLabel);
    });

    var log = qs('#event-log');
    syncRows(log, d.log.length, 'tpl-log-row');
    d.log.forEach(function (e, i) {
      var row = log.children[i];
      fieldText('log-time', e.time, row);
      var g = field('log-glyph', row); color(g, e.color); setText(g, e.glyph);
      var t = field('log-text', row); color(t, e.color); setText(t, e.text);
    });
  }

  function syncRows(container, count, templateId) {
    while (container.children.length > count) container.lastElementChild.remove();
    while (container.children.length < count) container.appendChild(tpl(templateId).cloneNode(true));
  }

  // --- settings -----------------------------------------------------------

  function renderSettings() {
    var d = state.draft || state.settings;

    var dest = field('setting-destination');
    if (document.activeElement !== dest && dest.value !== d.destination) dest.value = d.destination || '';

    var conns = field('setting-connections');
    if (document.activeElement !== conns && conns.value !== String(d.connections)) conns.value = d.connections;

    ['Low', 'Normal', 'High'].forEach(function (level) {
      var radio = qs('[data-action="set-stall-' + level.toLowerCase() + '"]');
      radio.checked = d.stallSensitivity === level;
    });

    var installed = !!state.settings.serviceInstalled;
    var svc = field('service-status');
    svc.classList.toggle('is-installed', installed);
    setText(svc, installed
      ? 'Installed ✓ — downloads resume after reboot'
      : 'Not installed — downloads stop when you log out');
    setText(qs('[data-action="toggle-service"]'), '[' + (installed ? 'Remove' : 'Install') + ']');

    show(field('save-flash'), state.saved);
    show(field('settings-error'), !!state.settingsError);
    fieldText('settings-error-text', state.settingsError);
  }

  // --- confirm dialog -----------------------------------------------------

  function renderConfirm() {
    var open = !!state.confirm;
    show(field('confirm-dialog'), open);
    if (!open) return;
    fieldText('confirm-target', state.confirm.name);
    fieldText('confirm-note', state.confirmDeleteFiles
      ? state.confirm.done + ' already on disk will be deleted. This cannot be undone.'
      : state.confirm.done + ' already on disk will be kept — only the job is removed.');
    qs('[data-action="confirm-delete-files"]').checked = state.confirmDeleteFiles;
    show(field('confirm-error'), !!state.confirmError);
    fieldText('confirm-error-text', state.confirmError);
  }

  // ────────────────────────────── 6. actions ──────────────────────────────

  var savedTimer = null;

  function jobIdOf(el) {
    var card = el.closest('[data-job-id]');
    return card ? card.dataset.jobId : null;
  }

  document.addEventListener('click', function (ev) {
    var el = ev.target.closest('[data-action]');
    if (!el || el.tagName === 'INPUT') return;
    var action = el.dataset.action;

    switch (action) {
      case 'nav-dashboard':
      case 'back-to-dashboard':
        state.view = 'dashboard';
        render();
        break;

      case 'nav-settings':
        state.view = 'settings';
        state.saved = false;
        state.settingsError = '';
        render();
        break;

      case 'open-job-details':
        state.jobId = jobIdOf(el);
        state.view = 'detail';
        render();
        break;

      case 'resolve-url':
        doResolve();
        break;

      case 'change-destination':
        var next = window.prompt('Destination folder', effectiveDest());
        if (next) { state.destOverride = next; renderSelectionSummary(); }
        break;

      case 'start-download':
        doStart();
        break;

      case 'pause-job':
        doPauseToggle(jobIdOf(el), el.dataset.paused === '1');
        break;

      case 'cancel-job':
        openConfirm(jobIdOf(el));
        break;

      case 'confirm-cancel-dismiss':
        state.confirm = null;
        state.confirmError = '';
        renderConfirm();
        break;

      case 'confirm-cancel-job':
        doCancel();
        break;

      case 'toggle-service':
        doService();
        break;

      case 'save-settings':
        doSave();
        break;
    }
  });

  document.addEventListener('change', function (ev) {
    var el = ev.target.closest('[data-action]');
    if (!el) return;
    switch (el.dataset.action) {
      case 'toggle-file':
        var i = Number(el.closest('.picker-row').dataset.index);
        state.picker[i].selected = el.checked;
        el.closest('.picker-row').classList.toggle('is-off', !el.checked);
        state.startError = '';
        renderSelectionSummary();
        break;
      case 'set-stall-low':    setStall('Low'); break;
      case 'set-stall-normal': setStall('Normal'); break;
      case 'set-stall-high':   setStall('High'); break;
      case 'confirm-delete-files':
        state.confirmDeleteFiles = el.checked;
        renderConfirm();
        break;
    }
  });

  document.addEventListener('input', function (ev) {
    var f = ev.target.dataset ? ev.target.dataset.field : null;
    if (f === 'url-input') { state.url = ev.target.value; }
    else if (f === 'setting-destination') { draft().destination = ev.target.value; markDirty(); }
    else if (f === 'setting-connections') { draft().connections = ev.target.value; markDirty(); }
  });

  // Enter in the URL field resolves, as the design's [ RESOLVE ] does.
  document.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Enter') return;
    if (ev.target.dataset && ev.target.dataset.field === 'url-input') { ev.preventDefault(); doResolve(); }
  });

  function draft() {
    if (!state.draft) state.draft = Object.assign({}, state.settings);
    return state.draft;
  }
  function markDirty() { state.draftDirty = true; state.saved = false; state.settingsError = ''; }
  function setStall(level) { draft().stallSensitivity = level; markDirty(); renderSettings(); }

  function doResolve() {
    state.resolve = 'resolving';
    state.resolveError = '';
    state.startError = '';
    renderResolvePanel();
    var url = state.url;
    api.resolve(url).then(function (data) {
      if (state.url !== url) return;              // user typed something else meanwhile
      state.resolved = { repo: data.repo, revision: data.revision, resolvedAt: data.resolvedAt };
      state.picker = (data.files || []).map(function (f) {
        return { name: f.name, bytes: f.bytes, sha256: !!f.sha256, selected: f.selected !== false };
      });
      state.pickerToken++;
      state.resolve = 'resolved';
      renderResolvePanel();
    }).catch(function (err) {
      state.resolve = 'error';
      state.resolveError = err.message || 'Could not resolve that URL.';
      renderResolvePanel();
    });
  }

  function doStart() {
    var selected = selectedFiles();
    if (!selected.length || state.starting) return;
    state.starting = true;
    state.startError = '';
    renderSelectionSummary();
    api.createJob({
      url: state.url,
      files: selected.map(function (f) { return f.name; }),
      dest: effectiveDest()
    }).then(function () {
      state.starting = false;
      state.resolve = 'idle';
      state.resolved = null;
      state.picker = [];
      state.pickerToken++;
      state.url = '';
      state.destOverride = null;
      field('url-input').value = '';
      render();
      poll();
    }).catch(function (err) {
      state.starting = false;
      state.startError = err.message || 'Could not start the download.';
      renderSelectionSummary();
    });
  }

  function doPauseToggle(id, paused) {
    if (!id) return;
    (paused ? api.resume(id) : api.pause(id)).then(poll).catch(function () { poll(); });
  }

  function openConfirm(id) {
    var job = state.jobs.filter(function (j) { return j.id === id; })[0];
    if (!job) return;
    state.confirm = { id: id, name: job.name, done: gb(job.doneBytes) };
    state.confirmDeleteFiles = true;
    state.confirmError = '';
    renderConfirm();
  }

  function doCancel() {
    if (!state.confirm) return;
    var id = state.confirm.id;
    api.cancel(id, state.confirmDeleteFiles).then(function () {
      state.confirm = null;
      state.confirmError = '';
      if (state.jobId === id) { state.view = 'dashboard'; state.jobId = null; }
      state.jobs = state.jobs.filter(function (j) { return j.id !== id; });
      render();
      poll();
    }).catch(function (err) {
      state.confirmError = err.message || 'Could not cancel that job.';
      renderConfirm();
    });
  }

  function doSave() {
    var d = draft();
    var conns = Math.max(1, Math.min(16, parseInt(d.connections, 10) || 4));
    d.connections = conns;
    state.settingsError = '';
    api.saveSettings({
      destination: d.destination,
      connections: conns,
      stallSensitivity: d.stallSensitivity
    }).then(function (saved) {
      if (saved) state.settings = Object.assign({}, state.settings, saved);
      state.draft = Object.assign({}, state.settings);
      state.draftDirty = false;
      state.saved = true;
      state.destOverride = null;
      renderSettings();
      clearTimeout(savedTimer);
      savedTimer = setTimeout(function () { state.saved = false; renderSettings(); }, 2600);
    }).catch(function (err) {
      state.saved = false;
      state.settingsError = err.message || 'Could not save settings.';
      renderSettings();
    });
  }

  function doService() {
    var op = state.settings.serviceInstalled ? 'remove' : 'install';
    state.settingsError = '';
    api.service(op).then(function (res) {
      if (res && typeof res.installed === 'boolean') state.settings.serviceInstalled = res.installed;
      renderSettings();
      renderSidebar();
      poll();
    }).catch(function (err) {
      state.settingsError = err.message || ('Could not ' + op + ' the service.');
      renderSettings();
    });
  }

  // ─────────────────────────── 7. polling + boot ──────────────────────────

  var pollTimer = null;
  var inFlight = false;

  function applyStatus(data) {
    if (data.backend) state.backend = data.backend;
    if (data.disk) state.disk = data.disk;
    if (data.settings) {
      state.settings = data.settings;
      if (!state.draftDirty) state.draft = Object.assign({}, data.settings);
    }
    if (Array.isArray(data.jobs)) state.jobs = data.jobs;
    state.loadedOnce = true;
  }

  function poll() {
    if (inFlight) return Promise.resolve();
    inFlight = true;
    return api.status().then(function (data) {
      applyStatus(data);
      state.connected = true;
    }).catch(function () {
      // keep last known jobs on screen; just flag the backend as unreachable
      state.connected = false;
    }).then(function () {
      inFlight = false;
      state.beat = (state.beat + 1) % 8;
      render();
    });
  }

  function startPolling() {
    if (pollTimer) return;
    poll();
    pollTimer = setInterval(poll, 1000);
  }

  function stopPolling() {
    clearInterval(pollTimer);
    pollTimer = null;
  }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) stopPolling(); else startPolling();
  });

  render();
  startPolling();

  // handy for debugging from the console
  window.LR = { state: state, api: api, render: render, poll: poll };
})();
