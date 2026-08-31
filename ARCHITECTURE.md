# Long Rebuttal — Architecture Plan

*Name: **Long Rebuttal** — display name "Long Rebuttal"; package/CLI name `longrebuttal` (no space).*

**One-liner:** Paste a link (HuggingFace repo or any direct URL). Long Rebuttal downloads it with aria2c under a supervision layer that survives stalls, expired signed URLs, process death, and full host reboots — and verifies the bytes at the end. Fail loudly, never skip.

This generalizes the proven setup in `aria2c-resilient-downloader.md` (169 GB, 13 unattended recoveries, 0 bytes lost). The architecture is inherited from there; read §2 and §5 of that doc before building. **Every rule in that doc's §5 is load-bearing.**

---

## 1. Goals / Non-goals

**Goals**
- Unattended multi-day downloads that survive: silent stalls, signed-URL expiry, process death, reboots.
- Windows 10/11 + Linux, same codebase.
- Local web UI (paste link → watch progress) + minimal CLI for headless boxes.
- SHA256 verification when the source publishes hashes (HF does); size verification always.
- Simple to install: Python package + aria2c on PATH.

**Non-goals (v1 — do not build these)**
- Torrents/metalink, browser extension, bandwidth scheduling, multi-user/auth, Electron packaging, tray icon, download history analytics, mobile layout.

---

## 2. Stack

| Piece | Choice | Why |
|---|---|---|
| Language | Python 3.10+ | Cross-platform, fast to build, aria2c does the heavy lifting anyway |
| Backend | FastAPI + uvicorn, bound to `127.0.0.1` | Serves the static UI + a small JSON API |
| aria2 RPC | Minimal hand-rolled JSON-RPC client over `httpx` (~60 lines) | We need 5 methods (`addUri`, `tellStatus`, `tellActive`, `getGlobalStat`, `shutdown`); a dependency isn't warranted |
| Frontend | Static HTML/CSS/vanilla JS (from OpenDesign), polling the API at 1s | No build tooling, served straight by FastAPI |
| State | One `state.json` per data dir, atomic writes (write temp + rename) | aria2's `.aria2` control files hold byte-level resume state; ours holds job metadata only |
| aria2c | External binary, required on PATH | Preflight check with per-OS install hint (`winget install aria2` / `apt install aria2`) |

Package layout:

```
longrebuttal/
  __main__.py        # CLI entry: serve | add | status | service
  server.py          # FastAPI app + static files
  engine.py          # supervisor loop + watchdog (the core; port of the bash control flow)
  aria2.py           # spawn aria2c + JSON-RPC client
  resolve.py         # URL validation, HF repo/file resolution, size+sha256 manifest
  verify.py          # post-download size + SHA256 checks
  service.py         # systemd unit install (Linux) / Task Scheduler at-startup (Windows)
  state.py           # jobs model + atomic state.json persistence
  static/            # UI from OpenDesign
```

---

## 3. Core engine (inherited design — do not deviate)

**One aria2c child process** per Long Rebuttal instance, spawned by the engine with a random localhost RPC port and random secret (fixes the single-instance collision from field notes §7.4). All files from all jobs go into its queue, `--max-concurrent-downloads=1` (sequential = simpler progress, less seek).

**Supervisor loop** (direct port of the bash reference in field notes §9):

```
loop forever:
    if all files complete+verified → mark job done, idle
    launch aria2c with ORIGINAL urls (never cached redirects)  ← restart-to-re-resolve
    watchdog polls RPC every 60s:
        numActive==0 && numWaiting==0        → kill aria2c (queue drained; RPC mode never self-exits)
        aggregate speed < adaptive threshold  → stalls++; at 12 → kill aria2c, log recovery event
        else stalls = 0
    on aria2c exit → verify what finished → relaunch if work remains (15s backoff)
```

**Adaptive stall threshold** (fixes field notes §7.3): floor = `max(10 KB/s, 5% of trailing 30-min median aggregate throughput)`. Persist nothing; recompute in memory.

**aria2c flags** — copy the annotated invocation from field notes §4 verbatim, with these cross-platform deltas:
- `--file-allocation=falloc` on Linux; **`prealloc` on Windows** (falloc needs privileges on NTFS).
- RPC port/secret: generated per launch, not hardcoded.

**Non-negotiable rules from field notes §5:**
1. `--lowest-speed-limit=0` and `--max-tries=0` always. Per-connection metrics are NEVER health signals (§5.1 shipped bug — silent shard abandonment).
2. Stall detection reads **aggregate** `downloadSpeed` from `getGlobalStat`, never per-file, never file size on disk (falloc makes size meaningless).
3. Restarting aria2c must re-submit the *original* URL so signed CDNs re-resolve (§5.2).
4. Graceful shutdown: SIGTERM aria2c (or RPC `shutdown`) and wait ≤45s so control files flush (§5.3). On Windows use RPC `shutdown` (no SIGTERM semantics).
5. A file that can't complete = job **FAILED**, loudly, in UI and exit code. Never skip-and-continue.
6. Pause = stop the supervisor + aria2c cleanly. Never use aria2's RPC pause while the watchdog runs (§3 of field notes).

---

## 4. URL resolution (`resolve.py`)

Accepted inputs:
1. **Direct file URL** → HEAD request: reachable? size (`Content-Length` / HF `x-linked-size`)? sha256 if HF LFS headers expose it.
2. **HF repo URL or `org/repo` id** (optionally `@revision`) → HF API `/api/models/{repo}/tree/{rev}?recursive=true` → file list with sizes + LFS sha256. UI lets the user tick which files (e.g. one quant of a GGUF set).

Validation happens *before* the job starts: show resolved file list, total size, and a disk-space check (need total size + 5% headroom on dest volume — hard error if short, like the field notes preflight).

If HF is unreachable at add-time → refuse to create the job with a clear error (fixes weak cold-start, field notes §7.7). Already-created jobs retry resolution inside the supervisor loop.

---

## 5. Verification (`verify.py`)

After each file finishes (no `.aria2` control file remains):
- size == manifest size, always;
- SHA256 == manifest hash when known (stream the file, show "verifying…" state in UI — a 50 GB hash takes minutes);
- mismatch → delete nothing, mark file `CORRUPT`, job `FAILED`, surface loudly.

Completion marker: `.longrebuttal-complete` JSON in dest dir (timestamp, per-file size+hash results).

---

## 6. API (all under `127.0.0.1`, no auth)

```
POST /api/resolve        {url}                      → {files:[{name,size,sha256?}], total, disk_free, warnings}
POST /api/jobs           {url, files?, dest?}       → job          (starts immediately)
GET  /api/jobs                                      → all jobs w/ live progress (speed, eta, per-file %, recoveries)
GET  /api/jobs/{id}/events                          → event log (launches, stall kills, re-resolves, verification)
POST /api/jobs/{id}/pause | /resume | DELETE        → clean stop / relaunch / cancel(+optional file cleanup)
GET  /api/system                                    → aria2c found?, version, disk free, service installed?, defaults
PUT  /api/settings       {dest_default, connections, stall_sensitivity}
```

Progress numbers come from aria2 RPC `tellStatus` (`completedLength`), never file sizes.

---

## 7. Reboot survival (`service.py`)

`longrebuttal service install|uninstall|status`
- **Linux:** write a user-scoped systemd unit (`~/.config/systemd/user/` with lingering, or system unit with sudo): `Restart=on-failure`, `RestartSec=20`, `TimeoutStopSec=45`, `KillMode=control-group`, `WantedBy=default.target`. These exact values are from the proven unit — keep them.
- **Windows:** `schtasks /create /sc onstart` running `longrebuttal serve --headless` (plus a logon trigger for non-admin). Document NSSM as the more robust alternative but don't depend on it.

On startup the engine reads `state.json`, finds unfinished jobs, and resumes automatically — no user action (this is the "PC turns back on and it just continues" requirement; `.aria2` control files make resume byte-exact).

---

## 8. Build phases (for the orchestrator, after OpenDesign returns the UI)

1. **Engine first** (`aria2.py`, `engine.py`, `state.py`, `resolve.py`, `verify.py`) + CLI `add`/`status` — testable headless before any UI exists. Acceptance: start a multi-GB HF download, `kill -9` aria2c mid-transfer → auto-relaunch, resumes at same byte, fresh signed URL in logs; final SHA256 passes.
2. **API + wire the OpenDesign static UI** into FastAPI. Acceptance: paste repo link → file picker → live progress → verified badge.
3. **Service install** both OSes. Acceptance: reboot mid-download → download resumes with zero interaction.
4. **Polish:** README (GitHub-ready), error states, `--headless` flag.

Each phase is independently shippable; stop after any of them and the tool still works.

---

## 9. Acceptance checklist (the whole point of the tool)

- [ ] Kill aria2c mid-download → resumes byte-exact, re-resolved URL
- [ ] Kill the Python process → service layer relaunches, resumes
- [ ] Reboot the machine → resumes with no interaction
- [ ] Throttle link to ~20 KB/s → watchdog kills+relaunches, no silent sit-forever
- [ ] Corrupt one byte of a finished file, re-verify → job FAILED loudly, file marked CORRUPT
- [ ] Slow-but-alive link (per-connection < any floor) → **no** connections killed, no shards abandoned
- [ ] Two Long Rebuttal instances on one box → no port collision
