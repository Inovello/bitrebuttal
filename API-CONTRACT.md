# Bit Rebuttal — API Contract (v1, frozen)

Both the backend and the frontend implement THIS file. JSON field names are camelCase and match
`design-reference/mock-data.js` **verbatim** — the frontend was designed against those shapes, and the
server does any formatting (labels) so the UI stays dumb. Base URL: `http://127.0.0.1:7451`. No auth.

## GET /api/status  — the single 1s polling endpoint

Returns everything the UI renders:

```jsonc
{
  "backend":  { "healthy": true, "label": "supervisor online", "version": "0.1.0", "uptime": "6d 04h" },
  "disk":     { "path": "D:\\models", "freeBytes": 142600000000, "volumeLabel": "D:" },  // for current default destination
  "settings": { "destination": "D:\\models\\huggingface", "connections": 4,
                "stallSensitivity": "Normal", "serviceInstalled": true },
  "jobs":     [ /* Job objects, newest first, see below */ ]
}
```

### Job object

Exactly the shape of `mock-data.js` jobs:

```jsonc
{
  "id": "job-a1b2c3",
  "name": "unsloth/Qwen3.8-Flash-Next-GGUF",     // repo id or filename for direct URLs
  "subtitle": "6 files · Q4_K_M shards",
  "status": "DOWNLOADING",   // DOWNLOADING | RECOVERING | VERIFYING | PAUSED | COMPLETE | FAILED
  "dest": "D:\\models\\huggingface\\unsloth_Qwen3.8-Flash-Next-GGUF",
  "totalBytes": 169200000000,
  "doneBytes": 64300000000,          // from aria2 RPC completedLength, never file sizes
  "speedBps": 2100000,               // aggregate, 0 when not transferring
  "etaSeconds": 49700,               // null when unknown/paused; 0 when done
  "recoveries": 13,                  // supervisor relaunch count
  "bytesLost": 0,                    // always 0 unless a verified file later failed; keep for the UI line
  "startedLabel": "Aug 28, 2026 · 19:04",   // server-formatted, local time
  "elapsedLabel": "3d 03h 37m",
  "avgSpeedBps": 2400000,            // doneBytes / active elapsed
  "files": [ { "name": "…-00001-of-00006.gguf", "bytes": 28400000000, "progress": 100,
               "state": "done" } ],  // state: queued | downloading | done | verifying | corrupt
  "log": [ { "time": "22:41", "level": "info", "text": "shard-00003 resumed at offset 7.8 GB" } ]
                                     // level: info | warn | ok | err. Newest first. Cap at last 100.
}
```

`status` mapping from the engine: supervisor relaunching/waiting-to-relaunch aria2c → RECOVERING;
hashing → VERIFYING; all files done+verified → COMPLETE; any file corrupt or unrecoverable → FAILED.

## POST /api/resolve   `{ "url": "..." }`

Accepts an HF repo id (`org/repo`), HF repo URL, HF file URL, or any direct URL.

- 200 → `{ "repo": "unsloth/…", "revision": "main", "resolvedAt": "22:41:07",
           "files": [ { "name": "...", "bytes": 28400000000, "sha256": true, "selected": true } ] }`
  (`sha256` is a boolean "hash available"; direct URLs yield one file, `selected: true`.
  Default selection: all files. For GGUF repos with multiple quants, all files still listed — the user unticks.)
- 4xx → `{ "error": "Could not reach huggingface.co — check the URL or your connection." }`
  (message is user-facing; be specific: bad URL / unreachable / 404 / gated repo)

## POST /api/jobs   `{ "url": "...", "files": ["name", ...], "dest": "D:\\models\\..." }`

`files` = names selected from the resolve response (omit → all). Re-resolves server-side (never trusts
client sizes), preflights disk space (total + 5% headroom → 409 `{ "error": ... }` if short), creates the
job, starts the supervisor. 201 → the Job object.

## Job actions

- `POST /api/jobs/{id}/pause`  → clean stop (engine stops watchdog + aria2c gracefully; never aria2 RPC pause)
- `POST /api/jobs/{id}/resume` → relaunch supervisor for the job
- `DELETE /api/jobs/{id}?deleteFiles=true|false` → cancel; optionally remove partial files + `.aria2` controls
- All → 200 `{ "ok": true }` or 4xx `{ "error": "..." }`

## Settings & service

- `PUT /api/settings` `{ "destination", "connections", "stallSensitivity" }` → 200 with the settings object.
  Applies to new aria2c launches (a running job picks it up on its next supervisor relaunch).
- `POST /api/service/install` / `POST /api/service/remove` → 200 `{ "installed": true|false }`,
  4xx with `{ "error": ... }` (e.g. needs elevation — tell the user the exact command to run instead).

## Frontend integration rules

- Poll `GET /api/status` every 1000 ms; render everything from that one payload. Stop polling while the
  tab is hidden (`visibilitychange`), resume on show.
- If the poll fails, show backend-unhealthy state (sidebar dot red, "Supervisor unreachable") and keep
  retrying; do not blank the last known jobs.
- The UI performs NO calculations beyond percent (`doneBytes/totalBytes`) and byte/speed/ETA formatting.
  Keep the exact formatting helpers from the design (`gb()`, `speedLabel()`, `etaLabel()`).

---

# v2 additions (Bit Rebuttal redesign — frozen)

v1 shapes above stay intact. v2 adds fields and endpoints; nothing is renamed or removed.

## GET /api/status — added fields

```jsonc
{
  "backend":  { /* v1 fields */, "aria2cVersion": "1.37.0",
                "gui": false },                    // true when running inside the native shell
  "disk":     { /* v1 fields */, "afterQueueBytes": 189100000000 },  // freeBytes minus bytes still to download
  "settings": { /* v1 fields */,
                "verifyChecksums": true,           // false -> size check only, hashing skipped
                "bandwidthCapMBs": 0,              // 0 = uncapped; else 10..120, applied LIVE via aria2 RPC
                "quietHours": { "enabled": false, "start": "23:00", "end": "07:30" },  // throttle to 5 MB/s inside window
                "theme": "mauve",                  // mauve | graphite | ink | slate
                "hfTokenSet": false },             // NEVER echo the token itself anywhere
  "recents":  [ "unsloth/Qwen3.8-Flash-Next-GGUF", "..." ],   // last 6 distinct resolved sources, newest first
  "completedToday": 3,
  "connections": [ { "id": "c-01", "speedBps": 9300000, "host": "cas-bridge.xethub.hf.co" } ],
                // per-server rates of the ACTIVE download (aria2 getServers), [] when idle.
                // DISPLAY ONLY — never used as a health signal (field notes 5.1).
  "library":  [ { "jobId": "job-a1b2c3", "name": "mistralai/Mixtral-8x22B", "path": "D:\models\...",
                  "sizeBytes": 84600000000, "integrity": "sha256 3/3", // or "size-only" | "1 corrupt"
                  "finishedLabel": "Aug 30 · 22:50" } ]        // COMPLETE and FAILED jobs, newest first
}
```

Job objects gain `"archived": false`. The dashboard renders non-archived jobs; the Library view renders
the `library` array (archived or not).

## New/extended endpoints

- `PUT /api/settings` accepts all new settings fields. `hfToken` is accepted as a WRITE-ONLY field
  ("" clears it); it is stored in settings.json, used for HF metadata/resolve requests only (NOT passed
  to aria2c — a global header would leak it to CDN redirect hosts), and never echoed back.
  `bandwidthCapMBs` takes effect immediately on a running aria2c via `aria2.changeGlobalOption`
  (`max-overall-download-limit`); quiet hours are enforced by the engine each watchdog poll
  (inside window: 5 MB/s; outside: the configured cap).
- `POST /api/jobs/pause-all` / `POST /api/jobs/resume-all` → `{"ok": true}` (applies to every job it can).
- `POST /api/jobs/{id}/reverify` → re-runs the verification pass on a COMPLETE or FAILED job → `{"ok": true}`.
- `POST /api/jobs/{id}/open-folder` → opens the job's dest in Explorer/Finder/xdg-open → `{"ok": true}`.
- `POST /api/jobs/clear-finished` → sets `archived: true` on all COMPLETE jobs → `{"ok": true}`.
  Files and library entries are untouched.
- `POST /api/browse-dest` → native folder dialog when the GUI shell is attached → `{"path": "C:\..."}`;
  400 `{"error": "..."}` when not running in the shell. Server wiring: `create_app(engine, folder_picker=None)`
  — the shell passes a callable returning the chosen path or None; `backend.gui` is true iff it is set.

## Frontend rules (additions)

- Theme comes from `settings.theme` (PUT to change); apply the design's oklch variable sets for
  mauve/graphite/ink/slate. Hide the Browse button when `backend.gui` is false.
- The connection-health panel is informational only; render whatever `connections` contains, no coloring
  by "slow" thresholds beyond neutral styling.
- Dropped from the design, deliberately: queue drag-reordering, "Sort: priority", "Skip verify",
  "Export manifest", "Verify all" (Library has per-item Re-verify).

## v2.1: job repair (added 2026-08-31)

- `POST /api/jobs/{id}/repair` — for a COMPLETE or FAILED job with `corrupt` files: deletes ONLY the
  corrupt files (and their `.aria2` controls) from disk, resets them to `queued`, clears the job's
  failure, and returns it to the supervisor queue (relaunch re-resolves URLs; verified files are
  untouched and not re-downloaded). 200 `{"ok": true}`; 4xx `{"error": ...}` when nothing is corrupt
  or the id is unknown. This deletion is user-invoked — the "verification never deletes" rule applies
  to the automatic pass, not to an explicit repair request.
- The detail view surfaces `Re-verify` (existing endpoint) and `Redownload corrupt (N)` on settled jobs.

## v2.2: per-job connections + quiet-hours speed (added 2026-08-31, pre-1.0)

- `settings.quietHours` gains `"limitMBs"` (int, clamped 1..50, default 5): the throttle applied
  inside the window. PUT accepts it; the engine's quiet-hours enforcement uses it instead of the
  fixed 5 MB/s.
- The Settings "connections" UI is gone. `settings.connections` REMAINS in the payload as the
  DEFAULT for new jobs (the source-bar "× N conn" chip cycles it via PUT).
- Job objects gain `"connections"` (int 1..16, effective value). `POST /api/jobs` accepts an
  optional `"connections"`; omitted -> the settings default.
- `POST /api/jobs/{id}/connections` `{"connections": n}` -> 200 `{"ok": true}` (clamped 1..16;
  unknown id -> 4xx error shape). Applies from the job's NEXT aria2c (re)launch; if the job is
  actively transferring, the engine logs an event saying the new count applies on the next
  relaunch (it may also trigger a clean relaunch — implementation's choice, logged either way).
- aria2c `split` / `max-connection-per-server` become PER-DOWNLOAD options passed with each
  file's addUri from the owning job's connections value.
- Detail view now carries the job actions: Pause/Resume, Cancel, Re-verify, Redownload corrupt,
  and the per-job connections pills (in the stats row).
