/* Bit Rebuttal — frontend (v2).
 *
 * Plain ES2017 in one IIFE. No framework, no build step, no CDN.
 * Served by FastAPI StaticFiles at /, or opened straight off disk with ?mock=1.
 *
 * Layout of this file:
 *   1. constants + formatting helpers   (gb/speedLabel/etaLabel kept verbatim from v1)
 *   2. state
 *   3. api            — httpApi / mockApi behind one interface
 *   4. deco()         — job -> view model
 *   5. render*()      — one renderer per region
 *   6. actions        — event delegation over [data-action]
 *   7. polling + boot
 */
(function () {
  'use strict';

  // ───────────────────────────── 1. constants ─────────────────────────────

  // The design's THEMES map, verbatim. `sw` = swatch ground, `ac` = swatch dot.
  var THEMES = {
    graphite: { name: 'Graphite · Violet', nh: 155, nc: 0.009, ah: 295, sh: 160, wh: 15, sw: 'oklch(0.20 0.010 155)', ac: 'oklch(0.70 0.17 295)' },
    ink:      { name: 'Deep ink · Cyan',   nh: 255, nc: 0.014, ah: 205, sh: 165, wh: 18, sw: 'oklch(0.20 0.014 255)', ac: 'oklch(0.72 0.15 205)' },
    slate:    { name: 'Cool slate · Lime', nh: 215, nc: 0.008, ah: 128, sh: 196, wh: 12, sw: 'oklch(0.20 0.008 215)', ac: 'oklch(0.74 0.17 128)' },
    mauve:    { name: 'Mauve · Azure',     nh: 325, nc: 0.011, ah: 248, sh: 150, wh: 10, sw: 'oklch(0.20 0.011 325)', ac: 'oklch(0.70 0.16 248)' }
  };
  var THEME_ORDER = ['mauve', 'graphite', 'ink', 'slate'];

  var ACC = 'var(--acc)', OK = 'var(--ok)', WARN = 'var(--warn)',
      ERR = 'var(--err)', DIM = 'var(--dim)', FAINT = 'var(--faint)';

  var STATUS = {
    DOWNLOADING: { label: 'Downloading', color: ACC,  live: true,  canPause: true },
    RECOVERING:  { label: 'Recovering',  color: WARN, live: true,  canPause: true },
    VERIFYING:   { label: 'Verifying',   color: OK },
    PAUSED:      { label: 'Paused',      color: DIM,  canResume: true },
    COMPLETE:    { label: 'Complete',    color: OK },
    FAILED:      { label: 'Failed',      color: ERR }
  };

  var FILE_STATE = {
    queued:      { label: 'Queued',      color: FAINT, bar: FAINT },
    downloading: { label: 'Downloading', color: ACC,   bar: ACC },
    done:        { label: 'Verified',    color: OK,    bar: OK },
    verifying:   { label: 'Verifying',   color: WARN,  bar: WARN },
    corrupt:     { label: 'Corrupt',     color: ERR,   bar: ERR }
  };

  var LEVEL = { info: DIM, warn: WARN, ok: OK, err: ERR };

  // 8x8 pixel face — 'o' outline, 'x' feature, '.' empty.
  var FACE = {
    idle:  ['..oooo..', '.o....o.', 'o.x..x.o', 'o......o', 'o.x..x.o', 'o..xx..o', '.o....o.', '..oooo..'],
    blink: ['..oooo..', '.o....o.', 'o......o', 'o.xxxx.o', 'o.x..x.o', 'o..xx..o', '.o....o.', '..oooo..'],
    grin:  ['..oooo..', '.o....o.', 'o.x..x.o', 'o......o', 'o.x..x.o', 'o.xxxx.o', '.o....o.', '..oooo..']
  };

  var SPARK_SAMPLES = 40;
  var HEART_BARS = 14;

  function gb(b) { return ((b || 0) / 1e9).toFixed(1) + ' GB'; }
  function gbNum(b) { return ((b || 0) / 1e9).toFixed(1); }

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

  function pad2(n) { return String(n).padStart(2, '0'); }
  function clockLabel() { var d = new Date(); return pad2(d.getHours()) + ':' + pad2(d.getMinutes()); }

  // ─────────────────────────────── 2. state ───────────────────────────────

  var state = {
    view: 'dashboard',          // dashboard | detail | library | settings
    jobId: null,

    // last known server payload (v1 + v2 shapes)
    backend:  { healthy: false, label: '', version: '—', uptime: '', aria2cVersion: '—', gui: false },
    disk:     { path: '', freeBytes: 0, volumeLabel: '', afterQueueBytes: 0 },
    settings: {
      destination: '', connections: 4, stallSensitivity: 'Normal', serviceInstalled: false,
      verifyChecksums: true, bandwidthCapMBs: 0,
      quietHours: { enabled: false, start: '23:00', end: '07:30' },
      theme: 'mauve', hfTokenSet: false
    },
    jobs: [],
    recents: [],
    completedToday: 0,
    connections: [],
    library: [],

    connected: false,
    loadedOnce: false,
    tick: 0,                    // poll counter, drives face/heartbeat animation
    lastOkAt: 0,                // ms of the last successful poll
    spark: [],                  // rolling aggregate speed samples (bps)
    heart: Array.from({ length: 14 }, function () { return 0.12 + Math.random() * 0.2; }),

    // source bar / picker
    url: '',
    resolve: 'idle',            // idle | resolving | resolved | error
    resolveError: '',
    resolved: null,             // { repo, revision, resolvedAt }
    picker: [],                 // [{ name, bytes, sha256, selected }]
    pickerToken: 0,
    starting: false,
    startError: '',
    destOverride: null,         // set by clicking the dest chip — next job only

    // settings view
    draft: null,                // editable copy (destination / connections / stall)
    draftDirty: false,
    tokenTyped: false,
    tokenTag: '',
    tokenTagWarn: false,
    savedAt: '',
    settingsError: '',
    themeApplied: null,

    // cancel dialog
    confirm: null,              // { id, name, done }
    confirmDeleteFiles: true,
    confirmError: ''
  };

  var MOCK = (typeof window !== 'undefined' && window.BR_MOCK) || null;

  // ──────────────────────────────── 3. api ────────────────────────────────

  function jsonOrThrow(res) {
    return res.text().then(function (body) {
      var data = null;
      if (body) { try { data = JSON.parse(body); } catch (e) { /* non-JSON */ } }
      if (!res.ok) {
        var err = new Error((data && data.error) || ('Request failed (' + res.status + ')'));
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

  function jobPath(id, suffix) { return 'api/jobs/' + encodeURIComponent(id) + suffix; }

  var httpApi = {
    status:        function ()        { return send('GET', 'api/status'); },
    resolve:       function (url)     { return send('POST', 'api/resolve', { url: url }); },
    createJob:     function (payload) { return send('POST', 'api/jobs', payload); },
    pause:         function (id)      { return send('POST', jobPath(id, '/pause')); },
    resume:        function (id)      { return send('POST', jobPath(id, '/resume')); },
    cancel:        function (id, del) { return send('DELETE', jobPath(id, '?deleteFiles=' + (del ? 'true' : 'false'))); },
    pauseAll:      function ()        { return send('POST', 'api/jobs/pause-all'); },
    resumeAll:     function ()        { return send('POST', 'api/jobs/resume-all'); },
    reverify:      function (id)      { return send('POST', jobPath(id, '/reverify')); },
    openFolder:    function (id)      { return send('POST', jobPath(id, '/open-folder')); },
    clearFinished: function ()        { return send('POST', 'api/jobs/clear-finished'); },
    browseDest:    function ()        { return send('POST', 'api/browse-dest'); },
    saveSettings:  function (s)       { return send('PUT', 'api/settings', s); },
    service:       function (op)      { return send('POST', 'api/service/' + op); }
  };

  // Mock implementation — same interface, same shapes, plus fake behaviour:
  // 1s progress tick, 1.4s resolve delay, settings round-trip, pause/resume all.
  function makeMockApi(seed) {
    var db = {
      backend:  Object.assign({}, seed.backend),
      disk:     Object.assign({}, seed.disk),
      settings: JSON.parse(JSON.stringify(seed.settings)),
      recents:  (seed.recents || []).slice(),
      completedToday: seed.completedToday || 0,
      connections: (seed.connections || []).map(function (c) { return Object.assign({}, c); }),
      library:  (seed.library || []).map(function (l) { return Object.assign({}, l); }),
      resolved: seed.resolved,
      jobs:     (seed.jobs || []).map(function (j) { return JSON.parse(JSON.stringify(j)); })
    };

    function capBps() {
      var mb = db.settings.bandwidthCapMBs;
      return mb ? mb * 1e6 : Infinity;
    }

    function tick() {
      var live = db.jobs.filter(function (j) { return j.status === 'DOWNLOADING'; }).length || 1;
      db.jobs = db.jobs.map(function (j) {
        if (j.status === 'VERIFYING') {
          var vdone = Math.min(j.totalBytes, j.doneBytes + j.totalBytes * 0.004);
          var vjob = Object.assign({}, j, { doneBytes: vdone });
          if (vdone >= j.totalBytes) {
            vjob.status = 'COMPLETE';
            vjob.etaSeconds = 0;
            vjob.speedBps = 0;
            vjob.files = (j.files || []).map(function (f) { return Object.assign({}, f, { state: 'done', progress: 100 }); });
            db.completedToday += 1;
            db.library = [{
              jobId: j.id, name: j.name, path: j.dest, sizeBytes: j.totalBytes,
              integrity: 'sha256 ' + (j.files || []).length + '/' + (j.files || []).length,
              finishedLabel: clockLabel()
            }].concat(db.library);
          }
          return vjob;
        }
        if (j.status !== 'DOWNLOADING') return j;
        // anchored to avgSpeedBps so the demo speed jitters instead of drifting
        var base = j.avgSpeedBps || j.speedBps || 2.4e7;
        var speed = Math.min(capBps() / live, base * (0.82 + Math.random() * 0.36));
        var done = Math.min(j.totalBytes, j.doneBytes + speed);
        var remain = j.totalBytes - done;
        var next = Object.assign({}, j, {
          doneBytes: done,
          speedBps: speed,
          etaSeconds: speed > 0 && remain > 0 ? Math.round(remain / speed) : (remain <= 0 ? 0 : null)
        });
        if (remain <= 0) { next.status = 'VERIFYING'; next.speedBps = 0; }
        // walk file progress so the detail view moves too
        var budget = (done / j.totalBytes) * (j.files || []).reduce(function (a, f) { return a + f.bytes; }, 0);
        next.files = (j.files || []).map(function (f) {
          var take = Math.max(0, Math.min(f.bytes, budget));
          budget -= take;
          var p = f.bytes ? (take / f.bytes) * 100 : 100;
          return Object.assign({}, f, {
            progress: Math.round(p),
            state: p >= 100 ? 'done' : p > 0 ? 'downloading' : 'queued'
          });
        });
        return next;
      });

      // connection rates jitter with the active job
      var active = db.jobs.filter(function (j) { return j.status === 'DOWNLOADING'; })[0];
      if (!active) { db.connections = []; }
      else if (!db.connections.length) {
        db.connections = (seed.connections || []).map(function (c) { return Object.assign({}, c); });
      } else {
        db.connections = db.connections.map(function (c) {
          return Object.assign({}, c, { speedBps: Math.max(2e5, c.speedBps * (0.85 + Math.random() * 0.32)) });
        });
      }
    }

    function later(value, ms) {
      return new Promise(function (resolve) { setTimeout(function () { resolve(value); }, ms || 0); });
    }
    function fail(msg) { return Promise.reject(new Error(msg)); }

    function toDownload(bytes) {
      return db.jobs.reduce(function (a, j) {
        return a + (j.archived || j.status === 'COMPLETE' || j.status === 'FAILED' ? 0 : j.totalBytes - j.doneBytes);
      }, 0);
    }

    return {
      status: function () {
        tick();
        return later({
          backend: Object.assign({}, db.backend),
          disk: Object.assign({}, db.disk, { afterQueueBytes: Math.max(0, db.disk.freeBytes - toDownload()) }),
          settings: JSON.parse(JSON.stringify(db.settings)),
          jobs: JSON.parse(JSON.stringify(db.jobs)),
          recents: db.recents.slice(),
          completedToday: db.completedToday,
          connections: db.connections.map(function (c) { return Object.assign({}, c); }),
          library: db.library.map(function (l) { return Object.assign({}, l); })
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
        var set = /big|huge|xxl|deepseek/i.test(v) ? db.resolved.huge : db.resolved.normal;
        db.recents = [set.repo].concat(db.recents.filter(function (r) { return r !== set.repo; })).slice(0, 6);
        return later({
          repo: set.repo,
          revision: set.revision,
          resolvedAt: new Date().toTimeString().slice(0, 8),
          files: set.files.map(function (f) { return Object.assign({}, f); })
        }, 1400);
      },

      createJob: function (payload) {
        var names = payload.files || [];
        var pool = db.resolved.normal.files.concat(db.resolved.huge.files);
        var picked = [];
        names.forEach(function (n) {
          var f = pool.filter(function (x) { return x.name === n; })[0];
          if (f && picked.indexOf(f) === -1) picked.push(f);
        });
        var total = picked.reduce(function (a, f) { return a + f.bytes; }, 0);
        if (total > db.disk.freeBytes) {
          return later(null, 260).then(function () {
            return fail('Not enough free space on ' + (db.disk.volumeLabel || db.disk.path) +
                        ' — needs ' + gb(total * 1.05) + ' with headroom, ' + gb(db.disk.freeBytes) + ' free.');
          });
        }
        var job = {
          id: 'job-' + Date.now().toString(36),
          name: payload.url || 'new-job',
          subtitle: picked.length + ' files · ' + gb(total) + ' · queued just now',
          status: 'DOWNLOADING',
          archived: false,
          dest: payload.dest,
          totalBytes: total,
          doneBytes: 0,
          speedBps: 3.2e7,
          etaSeconds: Math.round(total / 3.2e7),
          recoveries: 0,
          bytesLost: 0,
          startedLabel: clockLabel(),
          elapsedLabel: '00h 00m',
          avgSpeedBps: 3.2e7,
          files: picked.map(function (f, i) {
            return { name: f.name, bytes: f.bytes, progress: 0, state: i === 0 ? 'downloading' : 'queued' };
          }),
          log: [{ time: clockLabel(), level: 'info',
                  text: 'Job created — ' + picked.length + ' files queued, ' + gb(total) + ' total' }]
        };
        db.jobs = [job].concat(db.jobs);
        return later(JSON.parse(JSON.stringify(job)), 250);
      },

      pause: function (id) {
        db.jobs = db.jobs.map(function (j) {
          return j.id === id ? Object.assign({}, j, { status: 'PAUSED', speedBps: 0, etaSeconds: null }) : j;
        });
        return later({ ok: true }, 120);
      },
      resume: function (id) {
        db.jobs = db.jobs.map(function (j) {
          return j.id === id ? Object.assign({}, j, { status: 'DOWNLOADING', speedBps: j.avgSpeedBps || 2.1e7 }) : j;
        });
        return later({ ok: true }, 120);
      },
      cancel: function (id) {
        db.jobs = db.jobs.filter(function (j) { return j.id !== id; });
        return later({ ok: true }, 150);
      },
      pauseAll: function () {
        db.jobs = db.jobs.map(function (j) {
          return (j.status === 'DOWNLOADING' || j.status === 'RECOVERING')
            ? Object.assign({}, j, { status: 'PAUSED', speedBps: 0, etaSeconds: null }) : j;
        });
        return later({ ok: true }, 160);
      },
      resumeAll: function () {
        db.jobs = db.jobs.map(function (j) {
          return j.status === 'PAUSED'
            ? Object.assign({}, j, { status: 'DOWNLOADING', speedBps: j.avgSpeedBps || 2.1e7 }) : j;
        });
        return later({ ok: true }, 160);
      },
      reverify: function (id) {
        db.jobs = db.jobs.map(function (j) {
          return j.id === id ? Object.assign({}, j, { status: 'VERIFYING', doneBytes: 0, archived: false }) : j;
        });
        db.library = db.library.map(function (l) {
          return l.jobId === id ? Object.assign({}, l, { integrity: 're-verifying' }) : l;
        });
        return later({ ok: true }, 200);
      },
      openFolder: function () { return later({ ok: true }, 120); },
      clearFinished: function () {
        db.jobs = db.jobs.map(function (j) {
          return j.status === 'COMPLETE' ? Object.assign({}, j, { archived: true }) : j;
        });
        return later({ ok: true }, 150);
      },
      browseDest: function () {
        if (!db.backend.gui) return fail('Folder picker is only available in the desktop shell.');
        return later({ path: db.settings.destination }, 200);
      },
      saveSettings: function (s) {
        // mirrors the server: write-only hfToken, clamped cap, enum fields reset
        // to their default when unknown — the echoed object is the truth.
        var next = Object.assign({}, db.settings);
        Object.keys(s).forEach(function (k) {
          if (k === 'hfToken') { next.hfTokenSet = !!s.hfToken; return; }
          if (k === 'quietHours') { next.quietHours = Object.assign({}, next.quietHours, s.quietHours); return; }
          if (k === 'bandwidthCapMBs') {
            var v = Number(s[k]) || 0;
            next[k] = v ? Math.max(10, Math.min(120, v)) : 0;
            return;
          }
          if (k === 'theme') { next[k] = THEMES[s[k]] ? s[k] : 'mauve'; return; }
          if (k === 'connections') { next[k] = Math.max(1, Math.min(16, Number(s[k]) || 4)); return; }
          if (k === 'stallSensitivity') {
            next[k] = ['Low', 'Normal', 'High'].indexOf(s[k]) !== -1 ? s[k] : 'Normal';
            return;
          }
          next[k] = s[k];
        });
        db.settings = next;
        return later(JSON.parse(JSON.stringify(db.settings)), 220);
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
  function showField(name, on, root) { show(field(name, root), on); }
  function color(el, c) { if (el) el.style.setProperty('--c', c); }
  function cls(el, name, on) { if (el) el.classList.toggle(name, !!on); }
  function tpl(id) { return document.getElementById(id).content.firstElementChild.cloneNode(true); }

  function syncRows(container, count, templateId) {
    while (container.children.length > count) container.lastElementChild.remove();
    while (container.children.length < count) container.appendChild(tpl(templateId));
  }

  function setBar(bar, pct, c) {
    if (!bar) return;
    color(bar, c);
    var fill = qs('.bar-fill', bar);
    var w = Math.max(0, Math.min(100, pct)).toFixed(2) + '%';
    if (fill.style.width !== w) fill.style.width = w;
  }

  function fillN(container, n, tag) {
    while (container.children.length > n) container.lastElementChild.remove();
    while (container.children.length < n) container.appendChild(document.createElement(tag || 'i'));
  }

  // ─────────────────────── theme (oklch custom props) ─────────────────────

  function themeKey() {
    var k = state.settings.theme;
    return THEMES[k] ? k : 'mauve';
  }

  // Always re-applies: the server may echo back a different theme than we sent
  // (an unknown enum is reset to its default server-side), so no memoisation.
  function applyTheme() {
    var k = themeKey();
    state.themeApplied = k;
    var t = THEMES[k];
    var r = document.documentElement.style;
    r.setProperty('--nh', t.nh);
    r.setProperty('--nc', t.nc);
    r.setProperty('--ah', t.ah);
    r.setProperty('--sh', t.sh);
    r.setProperty('--wh', t.wh);
  }

  // ──────────────────────────────── 4. deco ───────────────────────────────

  function liveJobs() {
    return state.jobs.filter(function (j) { return !j.archived; });
  }

  function isActive(j) {
    return j.status === 'DOWNLOADING' || j.status === 'RECOVERING' || j.status === 'VERIFYING';
  }
  function isRunning(j) {
    return j.status === 'DOWNLOADING' || j.status === 'RECOVERING';
  }

  function deco(j, i) {
    var meta = STATUS[j.status] || STATUS.PAUSED;
    var pct = j.totalBytes ? (j.doneBytes / j.totalBytes) * 100 : 0;
    var lost = j.bytesLost > 0 ? gb(j.bytesLost) + ' lost' : '0 bytes lost';
    return {
      id: j.id,
      index: pad2((i || 0) + 1),
      name: j.name,
      subtitle: j.subtitle || '',
      color: meta.color,
      live: !!meta.live,
      statusLabel: meta.label,
      pct: pct,
      pctLabel: pct > 0 ? pct.toFixed(1) + '%' : '—',
      pctColor: pct > 0 ? meta.color : FAINT,
      transferred: gb(j.doneBytes) + ' / ' + gb(j.totalBytes),
      speedLabel: j.status === 'VERIFYING' ? 'Hashing' : speedLabel(j.speedBps),
      speedColor: j.status === 'DOWNLOADING' ? ACC : 'oklch(0.55 var(--nc) var(--nh))',
      etaLabel: j.status === 'COMPLETE' ? '—' : etaLabel(j.etaSeconds),
      dest: j.dest || '',
      recoveries: String(j.recoveries || 0),
      resilience: (j.recoveries || 0) + ' recoveries · ' + lost,
      lossy: j.bytesLost > 0,
      avgSpeedLabel: speedLabel(j.avgSpeedBps),
      elapsedLabel: j.elapsedLabel || '—',
      startedLabel: j.startedLabel || '—',
      showAction: !!(meta.canPause || meta.canResume),
      actionLabel: meta.canResume ? 'Resume' : 'Pause',
      paused: !!meta.canResume,
      files: (j.files || []).map(function (f) {
        var fs = FILE_STATE[f.state] || FILE_STATE.queued;
        return {
          name: f.name, sizeLabel: gb(f.bytes), progress: f.progress || 0,
          stateLabel: fs.label, color: fs.color, bar: fs.bar
        };
      }),
      log: (j.log || []).map(function (e) {
        return { time: e.time, text: e.text, color: LEVEL[e.level] || DIM };
      })
    };
  }

  function effectiveDest() {
    return state.destOverride !== null ? state.destOverride : (state.settings.destination || '');
  }

  function shortPath(p) {
    if (!p) return '—';
    var parts = String(p).split(/[\\/]/).filter(Boolean);
    return parts.length > 2 ? '…' + (p.indexOf('\\') !== -1 ? '\\' : '/') + parts.slice(-2).join(p.indexOf('\\') !== -1 ? '\\' : '/') : p;
  }

  function selectedFiles() {
    return state.picker.filter(function (f) { return f.selected; });
  }

  // ────────────────────────────── 5. renderers ────────────────────────────

  function render() {
    applyTheme();
    renderNav();
    renderSidebar();
    renderHeader();
    renderSource();
    renderPicker();
    renderJobs();
    renderPanels();
    renderDetail();
    renderLibrary();
    renderSettings();
    renderConfirm();
  }

  function renderNav() {
    cls(field('nav-dashboard'), 'is-active', state.view === 'dashboard' || state.view === 'detail');
    cls(field('nav-library'), 'is-active', state.view === 'library');
    cls(field('nav-settings'), 'is-active', state.view === 'settings');
    cls(field('view-dashboard'), 'is-active', state.view === 'dashboard');
    cls(field('view-detail'), 'is-active', state.view === 'detail');
    cls(field('view-library'), 'is-active', state.view === 'library');
    cls(field('view-settings'), 'is-active', state.view === 'settings');
  }

  function renderSidebar() {
    var jobs = liveJobs();
    var healthy = state.connected && state.backend.healthy;
    fieldText('nav-active-count', pad2(jobs.filter(isActive).length));

    // heartbeat bars
    var hb = field('heartbeat');
    fillN(hb, HEART_BARS);
    for (var i = 0; i < HEART_BARS; i++) {
      var v = state.heart[i] === undefined ? 0.06 : state.heart[i];
      hb.children[i].style.height = Math.max(3, v * 22) + 'px';
      hb.children[i].style.background = healthy ? 'oklch(0.42 0.10 var(--ah))' : 'oklch(0.42 0.10 25)';
    }

    // 8x8 pixel face
    var faceEl = field('face');
    var step = state.tick % 9;
    var key = !healthy ? 'blink' : step === 4 ? 'blink' : step === 7 ? 'grin' : 'idle';
    var rows = FACE[key];
    fillN(faceEl, 64);
    var n = 0;
    for (var r = 0; r < 8; r++) {
      for (var c = 0; c < 8; c++) {
        var ch = rows[r][c];
        faceEl.children[n++].style.background =
          ch === 'o' ? 'oklch(0.46 var(--nc) var(--nh))'
          : ch === 'x' ? (healthy ? 'var(--acc)' : 'var(--err)')
          : 'transparent';
      }
    }

    cls(field('supervisor-dot'), 'is-down', !healthy);
    var sup = field('supervisor-text');
    cls(sup, 'is-down', !healthy);
    setText(sup, healthy ? (state.backend.label || 'Supervisor watching') : 'Supervisor unreachable');
    fieldText('heartbeat-age', state.lastOkAt
      ? String(Math.min(999, Math.round((Date.now() - state.lastOkAt) / 1000)))
      : '—');

    var boot = field('boot-service');
    var installed = !!state.settings.serviceInstalled;
    cls(boot, 'is-installed', installed);
    setText(boot, installed ? 'Boot service installed' : 'Boot service not installed');

    renderCap();
  }

  var capDragging = false;

  function capFromSlider(v) { return Number(v) >= 120 ? 0 : Number(v); }
  function sliderFromCap(mb) { return !mb ? 120 : Math.max(10, Math.min(120, mb)); }

  // The knob follows settings.bandwidthCapMBs except during an in-flight drag
  // (capDragging, cleared 400ms after the last input). Focus alone must NOT hold
  // the knob: the server clamps any non-zero cap into 10..120 and echoes the real
  // value back, and that echo has to win over whatever the user dragged to.
  function renderCap(force) {
    var slider = field('cap-slider');
    var mb = state.settings.bandwidthCapMBs || 0;
    if (force || !capDragging) {
      var want = String(sliderFromCap(mb));
      if (slider.value !== want) slider.value = want;
    }
    var shown = capFromSlider(slider.value);
    fieldText('cap-value', shown ? String(shown) : '∞');
    fieldText('cap-unit', shown ? 'MB/s' : 'Uncapped');
  }

  function renderHeader() {
    var jobs = liveJobs();
    var active = jobs.filter(isActive);
    var agg = jobs.reduce(function (a, j) { return a + (j.status === 'DOWNLOADING' ? j.speedBps : 0); }, 0);
    var recoveries = jobs.reduce(function (a, j) { return a + (j.recoveries || 0); }, 0);

    fieldText('hdr-active', pad2(active.length));
    fieldText('hdr-total', '/ ' + pad2(jobs.length));
    fieldText('hdr-aggregate', (agg / 1e6).toFixed(1));
    fieldText('hdr-disk', gbNum(state.disk.freeBytes));
    fieldText('hdr-after-queue', '→ ' + gb(state.disk.afterQueueBytes || 0) + ' after queue');
    fieldText('hdr-recovered', String(recoveries));

    // sparkline: rolling last 40 aggregate samples, scaled to the window max
    var sparkEl = field('sparkline');
    fillN(sparkEl, SPARK_SAMPLES);
    var max = Math.max.apply(null, state.spark.concat([1]));
    var offset = SPARK_SAMPLES - state.spark.length;
    for (var i = 0; i < SPARK_SAMPLES; i++) {
      var v = i < offset ? 0 : state.spark[i - offset] / max;
      sparkEl.children[i].style.height = Math.max(6, v * 40) + 'px';
    }
    cls(sparkEl, 'is-idle', agg <= 0);

    var running = jobs.some(isRunning);
    var btn = field('pause-all');
    setText(btn, running ? 'Pause all' : 'Resume all');
    cls(btn, 'is-paused', !running);
    btn.disabled = jobs.length === 0;
  }

  function renderSource() {
    var input = field('url-input');
    if (document.activeElement !== input && input.value !== state.url) input.value = state.url;

    fieldText('source-dest', shortPath(effectiveDest()));
    fieldText('source-conn', (state.settings.connections || 4) + ' conn');

    var box = field('recents');
    var list = state.recents || [];
    showField('recents-empty', list.length === 0);
    syncRows(box, list.length, 'tpl-recent-chip');
    list.forEach(function (r, i) {
      var chip = box.children[i];
      setText(chip, r);
      chip.dataset.value = r;
    });

    var btn = field('resolve-btn');
    btn.disabled = state.resolve === 'resolving';
    setText(btn, state.resolve === 'resolving' ? 'Resolving' : 'Resolve');
  }

  var pickerRendered = -1;

  function renderPicker() {
    var open = state.resolve !== 'idle';
    cls(field('picker'), 'is-open', open);
    showField('picker-resolving', state.resolve === 'resolving');
    showField('picker-error', state.resolve === 'error');
    showField('picker-body', state.resolve === 'resolved');
    fieldText('picker-error-text', state.resolveError);

    if (state.resolve !== 'resolved' || !state.resolved) { pickerRendered = state.pickerToken - 1; return; }

    fieldText('picker-repo', state.resolved.repo);
    fieldText('picker-meta',
      '@' + state.resolved.revision + ' · resolved ' + state.resolved.resolvedAt +
      ' · ' + state.picker.length + ' files');

    if (pickerRendered !== state.pickerToken) {
      pickerRendered = state.pickerToken;
      var rows = field('picker-rows');
      rows.textContent = '';
      state.picker.forEach(function (f, i) {
        var row = tpl('tpl-picker-row');
        row.dataset.index = String(i);
        qs('input', row).checked = !!f.selected;
        cls(row, 'is-off', !f.selected);
        fieldText('picker-name', f.name, row);
        fieldText('picker-size', gb(f.bytes), row);
        var sha = field('picker-sha', row);
        setText(sha, f.sha256 ? 'sha256 ✓' : 'no hash');
        cls(sha, 'is-none', !f.sha256);
        rows.appendChild(row);
      });
    }
    renderSelection();
  }

  function renderSelection() {
    var selected = selectedFiles();
    var selBytes = selected.reduce(function (a, f) { return a + f.bytes; }, 0);
    var free = state.disk.freeBytes || 0;
    var over = selBytes > free;

    fieldText('picker-total',
      selected.length + (selected.length === 1 ? ' file — ' : ' files — ') + gb(selBytes));

    var freeEl = field('picker-free');
    setText(freeEl, gb(free) + ' free on ' + (state.disk.volumeLabel || state.disk.path || '—'));
    cls(freeEl, 'is-over', over);
    fieldText('picker-dest', '→ ' + effectiveDest());

    showField('capacity-error', over);
    fieldText('capacity-error-text',
      'Selection exceeds free space by ' + gb(selBytes - free) + ' — deselect files or pick another drive.');

    showField('start-error', !!state.startError);
    fieldText('start-error-text', state.startError);

    var start = field('start-btn');
    start.disabled = over || selected.length === 0 || state.starting;
    setText(start, state.starting ? 'Starting' : 'Start');
  }

  // --- job list (keyed reconcile, so the width transition survives) --------

  var jobNodes = new Map();

  function renderJobs() {
    var list = field('job-list');
    var jobs = liveJobs();

    showField('queue-empty', jobs.length === 0 && state.loadedOnce);
    fieldText('queue-count',
      jobs.filter(isActive).length + ' active · ' + (state.completedToday || 0) + ' completed today');

    var seen = new Set();
    jobs.forEach(function (job, i) {
      var d = deco(job, i);
      var el = jobNodes.get(d.id);
      if (!el) {
        el = tpl('tpl-job-row');
        el.dataset.jobId = d.id;
        jobNodes.set(d.id, el);
      }
      updateJobRow(el, d);
      seen.add(d.id);
      if (list.children[i] !== el) list.insertBefore(el, list.children[i] || null);
    });
    jobNodes.forEach(function (el, id) {
      if (!seen.has(id)) { el.remove(); jobNodes.delete(id); }
    });
  }

  function updateJobRow(el, d) {
    fieldText('job-idx', d.index, el);
    fieldText('job-name', d.name, el);
    fieldText('job-meta', d.subtitle, el);

    var dot = field('job-dot', el);
    color(dot, d.color);
    cls(dot, 'is-live', d.live);

    var st = field('job-state', el);
    color(st, d.color);
    setText(st, d.statusLabel);

    var pctEl = field('job-pct', el);
    color(pctEl, d.pctColor);
    setText(pctEl, d.pctLabel);
    setBar(field('job-bar', el), d.pct, d.color);

    fieldText('job-bytes', d.transferred, el);
    var sp = field('job-speed', el);
    color(sp, d.speedColor);
    sp.style.color = 'var(--c)';
    setText(sp, d.speedLabel);
    fieldText('job-eta', d.etaLabel, el);
    var recov = field('job-recov', el);
    setText(recov, d.resilience);
    cls(recov, 'is-lossy', d.lossy);

    var act = field('job-pause', el);
    act.hidden = !d.showAction;
    setText(act, d.actionLabel);
    act.dataset.paused = d.paused ? '1' : '0';
  }

  function renderPanels() {
    // Connection health — display only, neutral bars scaled to the window max.
    var box = field('conn-list');
    var conns = state.connections || [];
    showField('conn-empty', conns.length === 0);
    syncRows(box, conns.length, 'tpl-conn-row');
    var max = conns.reduce(function (a, c) { return Math.max(a, c.speedBps || 0); }, 1);
    conns.forEach(function (c, i) {
      var row = box.children[i];
      fieldText('conn-id', c.id, row);
      fieldText('conn-rate', speedLabel(c.speedBps), row);
      fieldText('conn-host', c.host || '', row);
      field('conn-fill', row).style.width = (((c.speedBps || 0) / max) * 100).toFixed(1) + '%';
    });

    // Activity — merged newest log lines across the live jobs.
    var feed = [];
    liveJobs().forEach(function (j) {
      (j.log || []).slice(0, 8).forEach(function (e) { feed.push(e); });
    });
    feed.sort(function (a, b) { return String(b.time).localeCompare(String(a.time)); });
    feed = feed.slice(0, 6);
    showField('activity-empty', feed.length === 0);
    renderFeed(field('activity-list'), feed);
  }

  function renderFeed(box, entries) {
    syncRows(box, entries.length, 'tpl-feed-row');
    entries.forEach(function (e, i) {
      var row = box.children[i];
      fieldText('feed-time', e.time, row);
      color(field('feed-dot', row), LEVEL[e.level] || e.color || DIM);
      fieldText('feed-msg', e.text, row);
    });
  }

  // --- detail -------------------------------------------------------------

  function renderDetail() {
    if (state.view !== 'detail') return;
    var jobs = state.jobs;
    var target = null;
    for (var i = 0; i < jobs.length; i++) {
      if (jobs[i].id === state.jobId) { target = jobs[i]; break; }
    }
    if (!target) {
      if (state.loadedOnce) { state.view = 'dashboard'; state.jobId = null; renderNav(); }
      return;
    }
    var d = deco(target, jobs.indexOf(target));
    var verified = d.files.filter(function (f) { return f.stateLabel === 'Verified'; }).length;

    fieldText('detail-name', d.name);
    fieldText('detail-sub', d.dest + ' · ' + d.files.length + ' files · verified ' + verified + '/' + d.files.length);

    var chip = field('detail-chip');
    chip.style.borderColor = d.color;
    var chipDot = field('detail-chip-dot');
    color(chipDot, d.color);
    cls(chipDot, 'is-live', d.live);
    var chipLabel = field('detail-chip-label');
    color(chip, d.color);
    setText(chipLabel, d.statusLabel);

    var pctEl = field('detail-pct');
    color(pctEl, d.pctColor);
    setText(pctEl, d.pct > 0 ? d.pct.toFixed(1) + '%' : '0.0%');
    setBar(field('detail-bar'), d.pct, d.color);
    fieldText('detail-bytes', d.transferred);
    var sp = field('detail-speed');
    color(sp, d.speedColor);
    sp.style.color = 'var(--c)';
    setText(sp, d.speedLabel);
    fieldText('detail-eta', d.etaLabel);

    fieldText('stat-recoveries', d.recoveries);
    fieldText('stat-avg', d.avgSpeedLabel);
    fieldText('stat-elapsed', d.elapsedLabel);
    fieldText('stat-started', d.startedLabel);
    ['stat-avg', 'stat-elapsed', 'stat-started'].forEach(function (n) {
      var el = field(n);
      cls(el, 'is-long', el.textContent.length > 12);
    });

    var files = field('detail-files');
    syncRows(files, d.files.length, 'tpl-file-row');
    d.files.forEach(function (f, i) {
      var row = files.children[i];
      fieldText('file-name', f.name, row);
      fieldText('file-size', f.sizeLabel, row);
      var fill = field('file-fill', row);
      fill.style.width = Math.max(0, Math.min(100, f.progress)) + '%';
      fill.style.background = f.bar;
      var s = field('file-state', row);
      color(s, f.color);
      setText(s, f.stateLabel);
    });

    renderFeed(field('detail-log'), (target.log || []).slice(0, 12));
  }

  // --- library ------------------------------------------------------------

  function libraryTone(integrity) {
    var v = String(integrity || '').toLowerCase();
    if (/corrupt|mismatch|fail/.test(v)) return WARN;
    if (/^sha256/.test(v)) return OK;
    return DIM;
  }

  function renderLibrary() {
    var lib = state.library || [];
    var total = lib.reduce(function (a, l) { return a + (l.sizeBytes || 0); }, 0);
    fieldText('library-summary',
      'Completed jobs, on disk · ' + gb(total) + ' across ' + lib.length + (lib.length === 1 ? ' item' : ' items'));

    var box = field('library-rows');
    showField('library-empty', lib.length === 0);
    syncRows(box, lib.length, 'tpl-library-row');
    lib.forEach(function (l, i) {
      var row = box.children[i];
      row.dataset.jobId = l.jobId || '';
      fieldText('lib-name', l.name, row);
      fieldText('lib-path', l.path || '', row);
      fieldText('lib-size', gb(l.sizeBytes), row);
      var chk = field('lib-check', row);
      color(chk, libraryTone(l.integrity));
      setText(chk, l.integrity || '—');
      fieldText('lib-when', l.finishedLabel || '—', row);
    });
  }

  // --- settings -----------------------------------------------------------

  function draft() {
    if (!state.draft) {
      state.draft = {
        destination: state.settings.destination,
        connections: state.settings.connections,
        stallSensitivity: state.settings.stallSensitivity
      };
    }
    return state.draft;
  }

  function renderSettings() {
    var s = state.settings;
    var d = draft();

    var dest = field('setting-destination');
    if (document.activeElement !== dest && dest.value !== (d.destination || '')) dest.value = d.destination || '';
    show(field('browse-btn'), !!state.backend.gui);

    Array.prototype.forEach.call(field('conn-pills').children, function (p) {
      cls(p, 'is-on', Number(p.dataset.value) === Number(d.connections));
    });
    Array.prototype.forEach.call(field('stall-pills').children, function (p) {
      cls(p, 'is-on', p.dataset.value === d.stallSensitivity);
    });

    cls(field('verify-toggle'), 'is-on', !!s.verifyChecksums);

    var token = field('setting-hf-token');
    token.placeholder = s.hfTokenSet ? 'hf_••••••••••••••••••••••••••' : 'hf_…';
    if (!state.tokenTyped && document.activeElement !== token && token.value !== '') token.value = '';
    var tag = field('hf-token-tag');
    setText(tag, state.tokenTag);
    cls(tag, 'is-warn', state.tokenTagWarn);

    var quiet = s.quietHours || { enabled: false, start: '23:00', end: '07:30' };
    cls(field('quiet-toggle'), 'is-on', !!quiet.enabled);
    cls(qs('.quiet-ctl'), 'is-off', !quiet.enabled);
    var qs1 = field('quiet-start'), qs2 = field('quiet-end');
    if (document.activeElement !== qs1 && qs1.value !== quiet.start) qs1.value = quiet.start || '';
    if (document.activeElement !== qs2 && qs2.value !== quiet.end) qs2.value = quiet.end || '';

    var installed = !!s.serviceInstalled;
    var svc = field('service-status');
    cls(svc, 'is-installed', installed);
    setText(svc, installed
      ? 'Installed — downloads resume after reboot'
      : 'Not installed — downloads stop when you log out');
    setText(field('service-btn'), installed ? 'Remove' : 'Install');

    // theme swatches
    var sw = field('theme-swatches');
    syncRows(sw, THEME_ORDER.length, 'tpl-swatch');
    THEME_ORDER.forEach(function (k, i) {
      var t = THEMES[k], on = k === themeKey();
      var el = sw.children[i];
      el.dataset.value = k;
      el.style.background = t.sw;
      el.style.borderColor = on ? t.ac : 'oklch(0.32 var(--nc) var(--nh))';
      cls(el, 'is-on', on);
      qs('.swatch-dot', el).style.background = t.ac;
      el.title = t.name;
    });
    fieldText('theme-name', THEMES[themeKey()].name);

    var status = field('save-status');
    var line = 'Bit Rebuttal v' + (state.backend.version || '—') +
               ' · aria2c ' + (state.backend.aria2cVersion || '—');
    setText(status, state.savedAt ? 'Saved ' + state.savedAt + ' · ' + line : line);
    cls(status, 'is-ok', !!state.savedAt);

    showField('settings-error', !!state.settingsError);
    fieldText('settings-error-text', state.settingsError);
  }

  // --- confirm dialog -----------------------------------------------------

  function renderConfirm() {
    var open = !!state.confirm;
    showField('confirm-dialog', open);
    if (!open) return;
    fieldText('confirm-target', state.confirm.name);
    fieldText('confirm-note', state.confirmDeleteFiles
      ? state.confirm.done + ' already on disk will be deleted. This cannot be undone.'
      : state.confirm.done + ' already on disk will be kept — only the job is removed.');
    qs('[data-action="confirm-delete-files"]').checked = state.confirmDeleteFiles;
    showField('confirm-error', !!state.confirmError);
    fieldText('confirm-error-text', state.confirmError);
  }

  // ────────────────────────────── 6. actions ──────────────────────────────

  function jobIdOf(el) {
    var row = el.closest('[data-job-id]');
    return row ? row.dataset.jobId : null;
  }

  document.addEventListener('click', function (ev) {
    var el = ev.target.closest('[data-action]');
    if (!el || el.tagName === 'INPUT') return;
    var action = el.dataset.action;

    switch (action) {
      case 'nav-dashboard':
      case 'back-to-dashboard':
        state.view = 'dashboard'; render(); break;

      case 'nav-library':
        state.view = 'library'; render(); break;

      case 'nav-settings':
        state.view = 'settings';
        state.settingsError = '';
        render();
        break;

      case 'open-detail':
        state.jobId = jobIdOf(el);
        state.view = 'detail';
        render();
        break;

      case 'resolve-url':   doResolve(); break;
      case 'use-recent':
        state.url = el.dataset.value || '';
        field('url-input').value = state.url;
        doResolve();
        break;

      case 'change-destination':
        var next = window.prompt('Destination folder for this job', effectiveDest());
        if (next) { state.destOverride = next; renderSource(); renderPicker(); }
        break;

      case 'picker-dismiss':
        state.resolve = 'idle'; state.resolved = null; state.picker = [];
        state.pickerToken++; state.startError = ''; renderPicker();
        break;
      case 'picker-all':
      case 'picker-none':
        state.picker.forEach(function (f) { f.selected = action === 'picker-all'; });
        state.pickerToken++; renderPicker();
        break;

      case 'start-download': doStart(); break;

      case 'pause-job':
        doPauseToggle(jobIdOf(el), el.dataset.paused === '1'); break;
      case 'cancel-job':
        openConfirm(jobIdOf(el)); break;
      case 'toggle-pause-all':
        doPauseAll(); break;
      case 'clear-finished':
        api.clearFinished().then(poll).catch(poll); break;

      case 'open-folder':
        api.openFolder(jobIdOf(el)).catch(function () {}); break;
      case 'reverify':
        api.reverify(jobIdOf(el)).then(poll).catch(poll); break;

      case 'confirm-dismiss':
        state.confirm = null; state.confirmError = ''; renderConfirm(); break;
      case 'confirm-cancel-job':
        doCancel(); break;

      case 'toggle-verify':
        putSettings({ verifyChecksums: !state.settings.verifyChecksums }); break;
      case 'toggle-quiet':
        var q = state.settings.quietHours || {};
        putSettings({ quietHours: { enabled: !q.enabled, start: q.start || '23:00', end: q.end || '07:30' } });
        break;

      case 'set-conn':
        draft().connections = Number(el.dataset.value); markDirty(); renderSettings(); break;
      case 'set-stall':
        draft().stallSensitivity = el.dataset.value; markDirty(); renderSettings(); break;
      case 'set-theme':
        putSettings({ theme: el.dataset.value }); break;

      case 'browse-dest':   doBrowse(); break;
      case 'toggle-service': doService(); break;
      case 'save-settings':  doSave(); break;
    }
  });

  document.addEventListener('change', function (ev) {
    var el = ev.target.closest('[data-action]');
    if (!el) return;
    switch (el.dataset.action) {
      case 'toggle-file':
        var row = el.closest('.picker-row');
        state.picker[Number(row.dataset.index)].selected = el.checked;
        cls(row, 'is-off', !el.checked);
        state.startError = '';
        renderSelection();
        break;
      case 'confirm-delete-files':
        state.confirmDeleteFiles = el.checked;
        renderConfirm();
        break;
      case 'set-quiet-time':
        var q = state.settings.quietHours || {};
        putSettings({ quietHours: {
          enabled: !!q.enabled,
          start: field('quiet-start').value.trim(),
          end: field('quiet-end').value.trim()
        } });
        break;
    }
  });

  // bandwidth slider: live label, debounced PUT
  var capTimer = null;
  document.addEventListener('input', function (ev) {
    var t = ev.target;
    var f = t.dataset ? t.dataset.field : null;
    if (f === 'cap-slider') {
      capDragging = true;
      renderCap();
      clearTimeout(capTimer);
      capTimer = setTimeout(function () {
        capDragging = false;
        putSettings({ bandwidthCapMBs: capFromSlider(field('cap-slider').value) })
          .then(function () { renderCap(true); });
      }, 400);
      return;
    }
    if (f === 'url-input') { state.url = t.value; return; }
    if (f === 'setting-destination') { draft().destination = t.value; markDirty(); return; }
    if (f === 'setting-hf-token') { state.tokenTyped = true; state.tokenTag = ''; markDirty(); return; }
  });

  // Enter in the source field resolves.
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && state.confirm) {
      state.confirm = null; state.confirmError = ''; renderConfirm(); return;
    }
    if (ev.key !== 'Enter') return;
    var f = ev.target.dataset ? ev.target.dataset.field : null;
    if (f === 'url-input') { ev.preventDefault(); doResolve(); }
    else if (f === 'quiet-start' || f === 'quiet-end') { ev.target.blur(); }
  });

  function markDirty() { state.draftDirty = true; state.savedAt = ''; state.settingsError = ''; }

  // Instant-apply settings (theme / toggles / quiet times / bandwidth cap).
  function putSettings(partial) {
    var merged = Object.assign({}, state.settings, partial);
    if (partial.quietHours) merged.quietHours = Object.assign({}, state.settings.quietHours, partial.quietHours);
    state.settings = merged;
    state.settingsError = '';
    applyTheme();
    render();
    return api.saveSettings(partial).then(function (saved) {
      if (saved) applySettings(saved);
      applyTheme();
      render();
    }).catch(function (err) {
      state.settingsError = err.message || 'Could not save that setting.';
      renderSettings();
    });
  }

  function doResolve() {
    if (!state.url.trim()) {
      state.resolve = 'error';
      state.resolveError = 'Paste a HuggingFace repo id or a direct file URL first.';
      renderPicker();
      return;
    }
    state.resolve = 'resolving';
    state.resolveError = '';
    state.startError = '';
    renderSource();
    renderPicker();
    var url = state.url;
    api.resolve(url).then(function (data) {
      if (state.url !== url) return;
      state.resolved = { repo: data.repo, revision: data.revision, resolvedAt: data.resolvedAt };
      state.picker = (data.files || []).map(function (f) {
        return { name: f.name, bytes: f.bytes, sha256: !!f.sha256, selected: f.selected !== false };
      });
      state.pickerToken++;
      state.resolve = 'resolved';
      renderSource();
      renderPicker();
    }).catch(function (err) {
      state.resolve = 'error';
      state.resolveError = err.message || 'Could not resolve that URL.';
      renderSource();
      renderPicker();
    });
  }

  function doStart() {
    var selected = selectedFiles();
    if (!selected.length || state.starting) return;
    state.starting = true;
    state.startError = '';
    renderSelection();
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
      renderSelection();
    });
  }

  function doPauseToggle(id, paused) {
    if (!id) return;
    (paused ? api.resume(id) : api.pause(id)).then(poll).catch(poll);
  }

  function doPauseAll() {
    var running = liveJobs().some(isRunning);
    (running ? api.pauseAll() : api.resumeAll()).then(poll).catch(poll);
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

  function doBrowse() {
    api.browseDest().then(function (res) {
      // 200 with path:null == the user dismissed the native dialog — silent no-op.
      // Only "not running in the shell" comes back as a 4xx and surfaces below.
      if (res && res.path) { draft().destination = res.path; markDirty(); renderSettings(); }
    }).catch(function (err) {
      state.settingsError = err.message || 'Folder picker unavailable.';
      renderSettings();
    });
  }

  function doSave() {
    var d = draft();
    var conns = Math.max(1, Math.min(16, parseInt(d.connections, 10) || 4));
    d.connections = conns;
    var payload = {
      destination: d.destination,
      connections: conns,
      stallSensitivity: d.stallSensitivity
    };
    var token = field('setting-hf-token');
    var sendToken = state.tokenTyped;
    if (sendToken) payload.hfToken = token.value.trim();
    state.settingsError = '';

    api.saveSettings(payload).then(function (saved) {
      if (saved) applySettings(saved);
      state.draft = null;
      state.draftDirty = false;
      state.destOverride = null;
      state.savedAt = clockLabel();
      if (sendToken) {
        state.tokenTyped = false;
        token.value = '';
        state.tokenTag = payload.hfToken ? 'Valid' : 'Cleared';
        state.tokenTagWarn = !payload.hfToken;
      }
      render();
    }).catch(function (err) {
      state.savedAt = '';
      state.settingsError = err.message || 'Could not save settings.';
      renderSettings();
    });
  }

  function doService() {
    var op = state.settings.serviceInstalled ? 'remove' : 'install';
    state.settingsError = '';
    api.service(op).then(function (res) {
      if (res && typeof res.installed === 'boolean') state.settings.serviceInstalled = res.installed;
      render();
      poll();
    }).catch(function (err) {
      state.settingsError = err.message || ('Could not ' + op + ' the service.');
      renderSettings();
    });
  }

  // ─────────────────────────── 7. polling + boot ──────────────────────────

  var pollTimer = null;
  var inFlight = false;

  function applySettings(s) {
    state.settings = Object.assign({}, state.settings, s);
    if (s.quietHours) state.settings.quietHours = Object.assign({}, s.quietHours);
    if (!state.draftDirty) state.draft = null;
  }

  function applyStatus(data) {
    if (data.backend) state.backend = Object.assign({}, state.backend, data.backend);
    if (data.disk) state.disk = Object.assign({}, state.disk, data.disk);
    if (data.settings) applySettings(data.settings);
    if (Array.isArray(data.jobs)) state.jobs = data.jobs;
    if (Array.isArray(data.recents)) state.recents = data.recents;
    if (Array.isArray(data.connections)) state.connections = data.connections;
    if (Array.isArray(data.library)) state.library = data.library;
    if (typeof data.completedToday === 'number') state.completedToday = data.completedToday;
    state.loadedOnce = true;
  }

  function pushSamples() {
    var healthy = state.connected && state.backend.healthy;
    var agg = liveJobs().reduce(function (a, j) {
      return a + (j.status === 'DOWNLOADING' ? j.speedBps : 0);
    }, 0);
    state.spark.push(agg);
    if (state.spark.length > SPARK_SAMPLES) state.spark.shift();
    state.heart.push(healthy ? (agg > 0 ? 0.35 + Math.random() * 0.6 : 0.12 + Math.random() * 0.12) : 0.06);
    if (state.heart.length > HEART_BARS) state.heart.shift();
  }

  function poll() {
    if (inFlight) return Promise.resolve();
    inFlight = true;
    return api.status().then(function (data) {
      applyStatus(data);
      state.connected = true;
      state.lastOkAt = Date.now();
    }).catch(function () {
      // keep the last known payload on screen; only flag the backend
      state.connected = false;
    }).then(function () {
      inFlight = false;
      state.tick += 1;
      pushSamples();
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
  window.BR = { state: state, api: api, render: render, poll: poll, THEMES: THEMES };
})();

// ── native shell window controls (frameless pywebview) ─────────────────────
(function () {
  var bar = document.querySelector('[data-field="shellbar"]');
  if (!bar) return;
  function show() {
    bar.hidden = false;
    document.body.classList.add('shell');
  }
  window.addEventListener('pywebviewready', show);
  if (window.pywebview) show();
  bar.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-action]');
    if (!btn || !window.pywebview) return;
    var a = btn.getAttribute('data-action');
    if (a === 'win-min') window.pywebview.api.minimize();
    else if (a === 'win-max') window.pywebview.api.toggle_maximize();
    else if (a === 'win-close') window.pywebview.api.close();
  });
  bar.querySelector('.shellbar-drag').addEventListener('dblclick', function () {
    if (window.pywebview) window.pywebview.api.toggle_maximize();
  });
})();
