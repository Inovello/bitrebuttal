# Prompt for OpenDesign (copy everything below the line)

---

Design and build the complete frontend for **Long Rebuttal**, a local web app for downloading huge AI model files (tens to hundreds of gigabytes) without babysitting them. The backend supervises aria2c downloads, auto-recovers from stalls, expired links, crashes, and even full PC reboots, and verifies files with SHA256 when done. Your job is ONLY the frontend. A separate backend team will wire it to a real API later.

## Hard technical constraints

- Plain **HTML + CSS + vanilla JavaScript** only. No React, no build step, no npm, no external CDNs or fonts. Everything must work by opening `index.html` from disk.
- Deliver exactly these files: `index.html`, `styles.css`, `app.js`, `mock-data.js`.
- All dynamic data comes from `mock-data.js` (realistic fake jobs, defined below). `app.js` renders everything from that data so the backend team can swap it for `fetch()` calls.
- Every element that shows dynamic data or triggers an action gets a `data-` attribute naming it, e.g. `data-field="job-speed"`, `data-action="pause-job"`. Be consistent.
- Desktop layout only, works from 1100px wide and up. One single-page app; switch views by showing/hiding sections in JS.

## Look and feel

Dark theme only. Think clean mission-control / terminal aesthetic: near-black background (#0d1117 range), one accent color (electric green or cyan) used for progress and success, amber for warnings/recovering, red for failures. Monospace font for numbers (sizes, speeds, hashes), system sans-serif for everything else. Generous spacing, no clutter, no stock illustrations. Progress bars are the heroes of this UI — make them large and satisfying.

## Views

### 1. Dashboard (default view)

- **Top bar:** app name "Long Rebuttal", tagline "Downloads that finish.", a status dot showing backend health, gear icon → Settings view.
- **Add-download panel:** one large text input, placeholder "Paste a HuggingFace repo or direct file URL…", button "Resolve". Below it, three swappable states:
  - *resolving:* spinner + "Contacting server…"
  - *resolved:* a **file picker card** — repo name, list of files with checkboxes, each row: filename (monospace), size, small "SHA256 ✓ available" tag when a hash exists. Footer: "N files selected — 87.3 GB total", free-disk-space line ("412 GB free on D:"), destination folder path with a "change" affordance, and a big "Start download" button. If selected size exceeds free space, show a red error line and disable the button.
  - *error:* red inline message, e.g. "Could not reach huggingface.co — check the URL or your connection."
- **Jobs list:** one card per download job. Each card shows:
  - repo/file name, overall progress bar with percent, downloaded/total ("64.2 / 169.2 GB"), current speed, ETA
  - a **status chip**: `DOWNLOADING` (accent), `RECOVERING` (amber, with spinner — shown when the supervisor is relaunching aria2c), `VERIFYING` (accent, "checking SHA256…"), `PAUSED` (gray), `COMPLETE ✓` (green), `FAILED` (red)
  - a small resilience line: "↻ 13 auto-recoveries · 0 bytes lost" — this is the product's pride, make it visible
  - buttons: Pause/Resume (toggles), Cancel (with confirm dialog), and "Details ›"
- **Empty state** when no jobs: centered message "Nothing downloading. Paste a link above — then feel free to reboot, sleep, or walk away."

### 2. Job detail view (opens from "Details ›", has a "‹ Back" link)

- Header repeats name, status chip, big progress bar.
- **Per-file table:** filename, size, individual progress bar, per-file state (queued / downloading / done ✓ / verifying / corrupt ✗).
- **Event log:** reverse-chronological monospace list with timestamps, e.g. "22:14 Stall detected (speed 8 KB/s for 12 min) — restarting aria2c", "22:14 Re-resolved fresh download URL", "09:02 Host rebooted — download resumed automatically", "10:31 shard-002 SHA256 verified ✓".
- A stats row: total recoveries, average speed, elapsed time, started date.

### 3. Settings view

Simple form: default destination folder, connections per download (number, default 4), stall sensitivity (Low / Normal / High radio), and a **"Start on boot" section** showing service status ("Installed ✓ — downloads resume after reboot" or "Not installed") with an Install/Remove button. Save button with a saved-confirmation flash.

## Mock data (`mock-data.js`)

Provide at least 4 jobs covering the states: one DOWNLOADING at 38% (169.2 GB, 6 files, 2.1 MB/s, 13 recoveries), one RECOVERING, one COMPLETE with all files verified, one FAILED with one file marked corrupt. Include a resolved-repo object for the file picker (use repo "unsloth/Qwen3.8-Flash-Next-GGUF" with 6 .gguf shard files ~28 GB each). Timestamps, speeds, and event logs must look realistic.

## Behavior to implement in `app.js` (against mock data)

- Render dashboard from mock jobs; clicking Details swaps to detail view for that job; Back returns.
- Resolve button cycles input panel through resolving → resolved states; checkbox changes update the size total and disable/enable Start correctly.
- Pause/Resume toggles the chip and button label; Cancel shows a confirm dialog then removes the card.
- Settings save shows the confirmation flash.
- Tick download progress of the DOWNLOADING job slightly every second so the UI feels alive.

Deliver all four files, complete and functional, no placeholders or TODOs.
