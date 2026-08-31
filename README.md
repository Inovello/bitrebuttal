# Long Rebuttal

Downloads that finish.

Long Rebuttal is a resilient downloader for huge AI model files, for people who pull multi-gigabyte models from HuggingFace and want to start a download, walk away, reboot the machine, and still find it finished and hash-verified. It wraps `aria2c` under a supervision layer that survives stalls, expired signed URLs, process death, and full reboots. It runs on Windows and Linux from the same codebase.

## Why

Three failure modes kill long downloads:

- **Signed CDN URL expiry.** HuggingFace URLs redirect to CDN links whose signatures die in 1-3 hours. A downloader that retries the redirected URL hammers a dead signature forever. Long Rebuttal restarts with the *original* URL, which re-resolves and issues a fresh signed redirect.
- **Silent stalls.** Connections stay open, throughput goes to zero, nothing errors. Long Rebuttal's watchdog polls aria2's aggregate throughput and kills and relaunches aria2c when it stalls.
- **Host reboot / process death.** Progress is lost, or the download simply never restarts. Long Rebuttal runs as an OS service (systemd on Linux, Task Scheduler on Windows) that resumes byte-exact from aria2's `.aria2` control files with no user action.

The design is not theoretical: the prototype that proved it downloaded 169.17 GB over ~3 days with 13 unattended recoveries, 0 manual interventions, and 0 bytes lost.

## How it works

Three layers, each catching what the one below it misses. The outermost layer is an OS service: it keeps the whole thing alive across crashes and reboots, and on startup it finds unfinished jobs and resumes them automatically. Inside it, the supervisor owns exactly one aria2c child process and makes every restart decision. A watchdog polls aria2's RPC every 60 seconds and watches aggregate throughput, never per-connection speed. If the aggregate stays below an adaptive threshold (a fraction of the trailing median, floored at 10 KB/s) for 12 consecutive polls, the watchdog kills aria2c and the supervisor relaunches it. The watchdog also detects when the queue has drained, because aria2 in RPC mode never exits on its own. When aria2c exits for any reason, the supervisor verifies what finished, waits 15 seconds, and relaunches it with the original URLs so signed CDNs re-resolve fresh. That restart-to-re-resolve is why repeated restarts cost zero progress. aria2c itself does the transfer work: multiple connections per file, infinite retries, and `.aria2` control files that make every resume byte-exact. When every file is done, Long Rebuttal checks size and SHA256 before the job is marked complete.

## Install

### Option 1 - download the app

Grab the binary for your OS from the [GitHub releases page](https://github.com/Inovello/longrebuttal/releases). The binary is a single file, no installer:

- **Windows:** run `LongRebuttal-windows-x64.exe`. Double-clicking it serves the UI and opens the browser.
- **macOS:** `chmod +x` the binary. On first run, right-click the app and choose **Open** (or run `xattr -d com.apple.quarantine <file>`) — the binary is unsigned, so Gatekeeper asks the first time.
- **Linux:** `chmod +x` the binary and run it.

On every platform you still need `aria2c` on your PATH: `winget install aria2` (Windows), `brew install aria2` (macOS), or `sudo apt install aria2` (Linux).

### Option 2 - install the CLI (pip)

Requirements:

- Python 3.10+
- `aria2c` on your PATH:
  - Windows: `winget install aria2`
  - Linux: `sudo apt install aria2`

Then from a clone of this repository:

```
pip install .
```

Every binary is also the full CLI: `LongRebuttal-windows-x64.exe add <url>`, `LongRebuttal-linux-x64 status`, and so on.

## Usage

Start the local web UI:

```
longrebuttal serve
```

Then open http://127.0.0.1:7451. Paste a HuggingFace repo link or any direct URL, pick the files you want, and watch live progress.

Add a job and check on it from the command line:

```
longrebuttal add <url>
longrebuttal status
```

Install the OS service so downloads survive reboots and start-on-boot:

```
longrebuttal service install
```

## Verification

Every file is checked against its manifest before a job is marked complete. Size is always checked, and when the source publishes a hash (HuggingFace does for LFS files), the SHA256 is checked too, streamed over the whole file.

A mismatch is not a warning: the file is marked CORRUPT and the job FAILED, loudly, in the UI and the exit code. Long Rebuttal never deletes, never skips-and-continues, and never silently reports a bad file as done.

## Design notes

Rules inherited from the prototype:

- Never treat per-connection speed as a health signal. A slow-but-alive link must not cause connections to be killed or shards to be abandoned.
- Stall detection runs only on aggregate throughput from aria2's global stats, never per-file, never file size on disk.
- Every restart re-requests the original URL so signed CDNs re-resolve with a fresh redirect. Never retry a cached redirect.
- Pause means stopping the supervisor and aria2c cleanly so control files flush. Never use aria2's RPC pause while the watchdog is running.
- Fail loudly, never skip. A file that can't complete or verify marks the whole job FAILED.

## License

MIT