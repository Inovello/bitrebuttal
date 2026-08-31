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
