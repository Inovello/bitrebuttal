# Resilient aria2c Downloader — Field Notes

**Context:** built 2026-08-29 to pull `unsloth/Qwen3.8-Flash-Next-GGUF @ UD-Q6_K_XL`
(169.17 GB, 6 shards) onto the homelab box over a link that ranged from 250 KB/s to 6.4 MB/s.

**Outcome:** completed 2026-08-30T22:50:19Z. **169.17 GB, byte-exact against HuggingFace's
authoritative sizes, 13 unattended recoveries, 0 manual interventions, 0 bytes lost.**

This document exists because the setup solved a specific recurring problem — *"I set a download
overnight and in the morning it went far but stopped and I have to manually resume it"* — and the
architecture is worth generalizing into a real tool.

---

## 1. The problem it solves

Three distinct failure modes kill long downloads, and most tools handle none of them:

| # | Failure | Why standard tools fail |
|---|---------|------------------------|
| 1 | **Signed CDN URL expiry** | HF/S3/CloudFront URLs carry `Expires=` and die in 1–3h. A downloader that retries the *redirected* URL hammers a dead signature forever. |
| 2 | **Silent stall** | Connections stay open, throughput goes to zero. No error, no exit — it just sits there all night. |
| 3 | **Host reboot / process death** | Progress is lost, or the download simply never restarts. |

`huggingface-cli download` / `hf_transfer` are fast and resumable but have **no supervision** —
they will happily stall until morning. `aria2` alone has no supervision either. Persepolis and
uGet are aria2 GUIs with no reboot survival or real stall detection.

---

## 2. Architecture — three-layer defense

Each layer catches what the one below it misses. This is the part worth keeping.

```
┌─ Layer 3: systemd ─────────────────────────────────────────────┐
│  Restart=on-failure, enabled → survives reboot & power loss.    │
│  Relaunches the supervisor, which re-resolves URLs from scratch │
│  ┌─ Layer 2: supervisor + RPC watchdog ───────────────────────┐ │
│  │  Polls aria2's AGGREGATE throughput every 60s.             │ │
│  │  12 consecutive polls under 50 KB/s → kill aria2c.         │ │
│  │  Also detects queue-drain (RPC mode never self-exits).     │ │
│  │  ┌─ Layer 1: aria2c ────────────────────────────────────┐  │ │
│  │  │  --max-tries=0 (infinite), --continue=true,          │  │ │
│  │  │  .aria2 control files = byte-level resume state      │  │ │
│  │  └──────────────────────────────────────────────────────┘  │ │
│  └────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────┘
```

### The two non-obvious insights

**(a) Stall detection belongs at the AGGREGATE layer, never per-connection.**
This is the single most important lesson here — see the shipped bug in §5.1.

**(b) Restart-to-re-resolve.**
When aria2c is relaunched it re-requests the *original* `huggingface.co/.../resolve/...` URL,
which issues a **fresh signed redirect**. This is why 13 restarts cost zero progress. Nothing
mainstream does this, and it is the #1 cause of dead overnight downloads.

---

## 3. Where everything lives (server: `ai-server`, 10.10.10.2, user `evans`)

| Path | Purpose |
|------|---------|
| `/home/evans/bin/qwen38next-dl.sh` | Supervisor + RPC watchdog (main script) |
| `/home/evans/bin/qwen38next-status.sh` | Human-readable progress readout |
| `/etc/systemd/system/qwen38next-dl.service` | systemd unit (`enabled`) |
| `/home/evans/models/gguf/Qwen3.8-Flash-Next-UD-Q6_K_XL/` | Destination |
| `└── urls.txt` | Generated aria2 input file (URL + `out=` per shard) |
| `└── aria2.log` | aria2's own log |
| `└── supervisor.log` | Supervisor/watchdog events |
| `└── .download-complete` | Completion marker (ISO timestamp) |
| `└── *.gguf.aria2` | Resume control files — **absence = verified complete** |

### Operating it

```bash
sudo systemctl start   qwen38next-dl.service    # resume
sudo systemctl stop    qwen38next-dl.service    # pause (progress safe)
sudo systemctl disable qwen38next-dl.service    # also stay paused across reboot
/home/evans/bin/qwen38next-status.sh            # progress
```

**Do NOT use aria2's own RPC pause** (`aria2.pause`) — the watchdog sees zero throughput and
kills/restarts aria2c to "fix" the stall. `systemctl stop` is the only clean pause, because it
takes the watchdog down too.

---

## 4. The aria2c invocation, annotated

```bash
aria2c \
  --dir="$DEST" --input-file="$URLS" \
  --continue=true --auto-file-renaming=false --allow-overwrite=false --conditional-get=false \
  --max-concurrent-downloads=1 --max-connection-per-server=4 --split=4 --min-split-size=128M \
  --file-allocation=falloc --disk-cache=64M --remote-time=true \
  --max-tries=0 --retry-wait=20 --timeout=60 --connect-timeout=30 \
  --lowest-speed-limit=0 --max-file-not-found=10 \
  --summary-interval=60 --console-log-level=notice \
  --log-level=notice --log="$ARIA_LOG" \
  --enable-rpc=true --rpc-listen-all=false --rpc-listen-port=6801 --rpc-secret="..." \
  --no-conf=true
```

| Flag | Why |
|------|-----|
| `--lowest-speed-limit=0` | **CRITICAL.** This is a *per-connection* floor. Non-zero values are actively harmful on slow links — see §5.1. |
| `--max-tries=0` | Infinite. aria2 must never give up and mark a file errored; the watchdog owns stall policy. |
| `--continue=true` | Resume from `.aria2` control files. |
| `--file-allocation=falloc` | Instant preallocation on ext4, avoids fragmentation of a 54 GB file. **Side effect: file size ≠ progress, so size-based watchdogs break — use RPC.** |
| `--enable-rpc` | Exact `completedLength` / `downloadSpeed` for the watchdog. **Caveat: aria2 in RPC mode never exits on queue drain — the watchdog must detect `numActive==0 && numWaiting==0` and kill it.** |
| `--max-concurrent-downloads=1` | Sequential shards. Simpler progress, less disk seek. |
| `--split=4` | 4 was right for this link. More connections did **not** add throughput (verified: link-limited, not per-client throttled). |
| `--no-conf=true` | Ignore `~/.aria2/aria2.conf` so behaviour is reproducible. |

### systemd unit essentials

```ini
Restart=on-failure
RestartSec=20
TimeoutStopSec=45
KillMode=control-group     # NOT "mixed" — see §5.3
[Install]
WantedBy=multi-user.target # + systemctl enable → survives reboot
```

---

## 5. Failures encountered

### 5.1 — The shipped bug: `--lowest-speed-limit` silently abandoned a shard

**Severity: critical. This is the most important lesson in the document.**

Initial config used `--lowest-speed-limit=100K` *as stall protection*. What actually happened:

1. Link was ~1.4 MB/s total; aria2 split it across 8 connections
2. Each connection got ~175 KB/s, and dipped below the 100 KB/s floor under normal variance
3. aria2 **killed its own connections** as "Too slow"
4. After `--max-tries=5` the URI list was exhausted → `errorCode=5 No URI available.`
5. aria2 **silently abandoned shard 2 at 395MB/682MB and moved to shard 3**

```
errorCode=5  Too slow Downloading speed: 38746 <= 102400(B/s)
errorCode=5  No URI available.   → shard 2 ABANDONED
```

It looked healthy the whole time. Unattended, this "completes" with a missing file.

**Fix:** `--lowest-speed-limit=0` + `--max-tries=0`, and move stall detection to aggregate
throughput via RPC. After the fix: **0 further aborts**, and shard 2 resumed from 395MB
automatically on the next restart.

**Generalizable rules:**
- Never treat a *per-connection* metric as a *download health* signal.
- A downloader must **fail loudly, never skip**. Silent skip-and-continue is the worst outcome.

### 5.2 — Signed CDN URLs expire mid-transfer

HF redirects to `cas-bridge.xethub.hf.co` / `us.aws.cdn.hf.co` with `X-Amz-Expires=3600` and an
outer `Expires=` ~1–3h out. On a multi-day download the signature dies repeatedly.

Observable in the logs as restarts at ~2–3.5h intervals — matching the signature lifetime almost
exactly. Handled correctly by design (restart → re-resolve), cost 0 bytes.

### 5.3 — `KillMode=mixed` SIGKILLed aria2c on stop

With `mixed`, systemd SIGTERMs only the main process (the bash supervisor, which had no trap),
then SIGKILLs the rest of the cgroup. aria2c never got a chance to flush its control file — risking
loss of up to `--auto-save-interval` (default 60s) of progress.

**Fix:** `KillMode=control-group` so aria2c receives SIGTERM and shuts down gracefully.

### 5.4 — Diagnostic dead-ends worth recording

- **`--file-allocation=falloc` breaks size-based progress checks.** The file is full-size instantly.
  Must use RPC (or sparse-file `du` with `--file-allocation=none`) for real progress.
- **More connections did not help.** Verified with a controlled A/B: server alone 0.64 MB/s;
  server + a second client 0.45–0.50 + 0.12 = ~0.60 total. Zero-sum → link-limited, not
  per-client throttled. Don't tune connection count against a saturated link.

---

## 6. Final statistics

| Metric | Value |
|--------|-------|
| Downloaded | **169.17 GB** / 6 shards |
| Duration | ~3 days (2026-08-29 02:02Z → 2026-08-30 22:50Z) |
| aria2c launches | **14** (13 recoveries) |
| systemd service starts | 15 |
| Watchdog *forced* restarts | 0 (aria2 retry + systemd covered everything) |
| Manual interventions | **0** after the §5.1 fix |
| Bytes lost to interruptions | **0** |
| Throughput range handled | 250 KB/s → 6.4 MB/s (**25× swing**) |
| Verification | All 6 shards byte-exact vs HF `x-linked-size` |

---

## 7. What's brittle (fix before generalizing)

1. **Everything hardcoded** — repo, subdir, the `%05d-of-00006` filename pattern, dest path,
   RPC port `6801`, RPC secret as a string literal. No CLI args, no config file.
2. **Size-only verification, no checksums.** *Biggest correctness gap.* HF exposes SHA256; two
   files can be the right length and still be corrupt. In a quantized tensor a flipped byte won't
   crash — it silently degrades output.
3. **Hand-tuned thresholds.** `MIN_SPEED=50KB/s` / 12-minute window were fitted to one link. On
   gigabit that floor is so low real stalls go undetected for hours. Should be adaptive — e.g. a
   fraction of trailing median throughput.
4. **Single instance.** Fixed RPC port means two concurrent downloads collide.
5. **Linux-only.** systemd does the heavy lifting: reboot survival, restart-on-failure,
   supervision, cgroup cleanup. Windows has none of it. **This is a rewrite, not a port.**
6. **No notification, no queue, no bandwidth scheduling, no rate limit.**
7. **Weak cold-start handling.** If HF is unreachable at launch, the size manifest is empty and
   completion detection falls back to "no `.aria2` file present" only.

---

## 8. If building this as an application

**Don't port the bash — rewrite, keeping the architecture.**

- **Python + [`aria2p`](https://pypi.org/project/aria2p/)** — proper RPC wrapper, cross-platform.
  Ship or require the `aria2c` binary.
- **Go** — single static binary, best distribution story, but more to reimplement.

Service layer is per-OS: systemd unit (Linux), Task Scheduler "at startup" or NSSM (Windows).

**Minimum viable feature set:**

1. CLI: `dl <url | hf-repo[@revision]> [--dest] [--verify sha256] [--jobs N]`
2. Aggregate stall watchdog with **adaptive** threshold (fraction of trailing median)
3. **Restart-to-re-resolve** for signed URLs — the differentiator
4. SHA256 verification, not just size
5. Cross-platform service install (`dl service install`)
6. Notification on completion/failure
7. Fail loudly; never silently skip a file

**The niche is narrower than "a download manager":** *unattended multi-day transfers that survive
reboots and actually detect stalls.* Nothing mainstream does this well — but it is a real gap.

---

## 9. Full source

Both scripts and the unit file are on the server at the paths in §3. Retrieve with:

```bash
ssh evans@10.10.10.2 'cat /home/evans/bin/qwen38next-dl.sh'
ssh evans@10.10.10.2 'cat /home/evans/bin/qwen38next-status.sh'
ssh evans@10.10.10.2 'cat /etc/systemd/system/qwen38next-dl.service'
```

### Supervisor control flow (reference)

```
preflight: disk space check → exit 78 if < 181 GB free
if .download-complete exists → exit 0
fetch_sizes()  # authoritative x-linked-size per shard from HF
loop forever:
    if all_complete()  → write marker, exit 0
    launch aria2c (background, RPC enabled)
    launch watchdog(aria2_pid) (background)
    wait aria2_pid
    kill watchdog
    if all_complete() → write marker, exit 0
    sleep 15

watchdog(pid):
    every 60s:
        stat = rpc(aria2.getGlobalStat)
        if numActive==0 && numWaiting==0  → kill aria2c (queue drained)
        if downloadSpeed < 50 KB/s        → stalls++ ; if stalls>=12 → kill aria2c
        else                              → stalls = 0

all_complete():
    for each shard:
        file exists?                    else incomplete
        NO .aria2 control file present?  else incomplete
        size == HF x-linked-size?        else incomplete
```
