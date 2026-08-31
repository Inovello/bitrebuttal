/* Bit Rebuttal — mock backend payload.
   Swap this file for fetch() calls; shapes below are the contract. */
(function () {
  const GB = 1e9;
  const MB = 1e6;

  window.LR_MOCK = {
    backend: { healthy: true, label: "supervisor online", version: "0.9.4", uptime: "6d 04h" },

    disk: { path: "D:\\models", freeBytes: 142.6 * GB, volumeLabel: "D:" },

    settings: {
      destination: "D:\\models\\huggingface",
      connections: 4,
      stallSensitivity: "Normal",
      serviceInstalled: true
    },

    resolved: {
      repo: "unsloth/Qwen3.8-Flash-Next-GGUF",
      revision: "main",
      resolvedAt: "22:41:07",
      files: [
        { name: "Qwen3.8-Flash-Next-Q4_K_M-00001-of-00006.gguf", bytes: 28.4 * GB, sha256: true,  selected: true },
        { name: "Qwen3.8-Flash-Next-Q4_K_M-00002-of-00006.gguf", bytes: 28.1 * GB, sha256: true,  selected: true },
        { name: "Qwen3.8-Flash-Next-Q4_K_M-00003-of-00006.gguf", bytes: 28.9 * GB, sha256: true,  selected: true },
        { name: "Qwen3.8-Flash-Next-Q4_K_M-00004-of-00006.gguf", bytes: 27.6 * GB, sha256: true,  selected: true },
        { name: "Qwen3.8-Flash-Next-Q4_K_M-00005-of-00006.gguf", bytes: 28.2 * GB, sha256: true,  selected: false },
        { name: "Qwen3.8-Flash-Next-Q4_K_M-00006-of-00006.gguf", bytes: 27.9 * GB, sha256: false, selected: false }
      ]
    },

    jobs: [
      {
        id: "job-qwen38-flash",
        name: "unsloth/Qwen3.8-Flash-Next-GGUF",
        subtitle: "6 files · Q4_K_M shards",
        status: "DOWNLOADING",
        dest: "D:\\models\\huggingface\\unsloth_Qwen3.8-Flash-Next-GGUF",
        totalBytes: 169.2 * GB,
        doneBytes: 64.3 * GB,
        speedBps: 2.1 * MB,
        etaSeconds: 49700,
        recoveries: 13,
        bytesLost: 0,
        startedLabel: "Aug 28, 2026 · 19:04",
        elapsedLabel: "3d 03h 37m",
        avgSpeedBps: 2.4 * MB,
        files: [
          { name: "…-00001-of-00006.gguf", bytes: 28.4 * GB, progress: 100, state: "done" },
          { name: "…-00002-of-00006.gguf", bytes: 28.1 * GB, progress: 100, state: "done" },
          { name: "…-00003-of-00006.gguf", bytes: 28.9 * GB, progress: 27,  state: "downloading" },
          { name: "…-00004-of-00006.gguf", bytes: 27.6 * GB, progress: 0,   state: "queued" },
          { name: "…-00005-of-00006.gguf", bytes: 28.2 * GB, progress: 0,   state: "queued" },
          { name: "…-00006-of-00006.gguf", bytes: 27.9 * GB, progress: 0,   state: "queued" }
        ],
        log: [
          { time: "22:41", level: "info", text: "shard-00003 resumed at offset 7.8 GB · 4 connections" },
          { time: "22:39", level: "warn", text: "Stall detected (speed 8 KB/s for 12 min) — restarting aria2c" },
          { time: "22:39", level: "info", text: "Re-resolved fresh download URL (previous CDN token expired)" },
          { time: "18:12", level: "ok",   text: "shard-00002 SHA256 verified ✓ 0 bytes lost" },
          { time: "18:11", level: "info", text: "shard-00002 complete — 28.1 GB in 3h 22m" },
          { time: "09:02", level: "warn", text: "Host rebooted — download resumed automatically" },
          { time: "09:02", level: "info", text: "Supervisor recovered 2 partial shards from .aria2 control files" },
          { time: "04:47", level: "warn", text: "Connection reset by peer (attempt 1/8) — backing off 15 s" },
          { time: "01:30", level: "ok",   text: "shard-00001 SHA256 verified ✓" },
          { time: "19:04", level: "info", text: "Job created — 6 files queued, 169.2 GB total" }
        ]
      },
      {
        id: "job-deepseek-v32",
        name: "deepseek-ai/DeepSeek-V3.2-Base",
        subtitle: "8 files · safetensors",
        status: "RECOVERING",
        dest: "D:\\models\\huggingface\\deepseek-ai_DeepSeek-V3.2-Base",
        totalBytes: 412.8 * GB,
        doneBytes: 210.5 * GB,
        speedBps: 0,
        etaSeconds: null,
        recoveries: 41,
        bytesLost: 0,
        startedLabel: "Aug 21, 2026 · 07:52",
        elapsedLabel: "10d 14h 49m",
        avgSpeedBps: 1.6 * MB,
        files: [
          { name: "model-00001-of-00008.safetensors", bytes: 51.6 * GB, progress: 100, state: "done" },
          { name: "model-00002-of-00008.safetensors", bytes: 51.6 * GB, progress: 100, state: "done" },
          { name: "model-00003-of-00008.safetensors", bytes: 51.6 * GB, progress: 100, state: "done" },
          { name: "model-00004-of-00008.safetensors", bytes: 51.6 * GB, progress: 100, state: "done" },
          { name: "model-00005-of-00008.safetensors", bytes: 51.6 * GB, progress: 8,   state: "downloading" },
          { name: "model-00006-of-00008.safetensors", bytes: 51.6 * GB, progress: 0,   state: "queued" },
          { name: "model-00007-of-00008.safetensors", bytes: 51.6 * GB, progress: 0,   state: "queued" },
          { name: "model-00008-of-00008.safetensors", bytes: 51.6 * GB, progress: 0,   state: "queued" }
        ],
        log: [
          { time: "22:40", level: "warn", text: "Relaunching aria2c (attempt 2/8) — holding 210.5 GB on disk" },
          { time: "22:40", level: "warn", text: "aria2c exited with code 3 — download URL returned 403" },
          { time: "22:38", level: "info", text: "Re-resolving download URL from repo metadata" },
          { time: "21:07", level: "info", text: "Speed floor raised to 64 KB/s for this host" },
          { time: "16:22", level: "warn", text: "Stall detected (speed 0 KB/s for 4 min) — restarting aria2c" },
          { time: "16:23", level: "ok",   text: "Resumed at offset 208.1 GB · 0 bytes lost" },
          { time: "02:14", level: "ok",   text: "model-00004 SHA256 verified ✓" },
          { time: "07:52", level: "info", text: "Job created — 8 files queued, 412.8 GB total" }
        ]
      },
      {
        id: "job-qwen3-coder",
        name: "Qwen/Qwen3-Coder-480B-A35B-Instruct-GGUF",
        subtitle: "8 files · checking SHA256",
        status: "VERIFYING",
        dest: "D:\\models\\huggingface\\Qwen_Qwen3-Coder-480B",
        totalBytes: 241.0 * GB,
        doneBytes: 241.0 * GB,
        speedBps: 0,
        etaSeconds: 640,
        recoveries: 7,
        bytesLost: 0,
        startedLabel: "Aug 25, 2026 · 11:20",
        elapsedLabel: "6d 11h 21m",
        avgSpeedBps: 3.1 * MB,
        files: [
          { name: "…-00001-of-00008.gguf", bytes: 30.1 * GB, progress: 100, state: "done" },
          { name: "…-00002-of-00008.gguf", bytes: 30.1 * GB, progress: 100, state: "done" },
          { name: "…-00003-of-00008.gguf", bytes: 30.1 * GB, progress: 100, state: "verifying" },
          { name: "…-00004-of-00008.gguf", bytes: 30.1 * GB, progress: 100, state: "queued" },
          { name: "…-00005-of-00008.gguf", bytes: 30.1 * GB, progress: 100, state: "queued" },
          { name: "…-00006-of-00008.gguf", bytes: 30.1 * GB, progress: 100, state: "queued" },
          { name: "…-00007-of-00008.gguf", bytes: 30.2 * GB, progress: 100, state: "queued" },
          { name: "…-00008-of-00008.gguf", bytes: 30.2 * GB, progress: 100, state: "queued" }
        ],
        log: [
          { time: "22:35", level: "info", text: "Hashing shard-00003 — 18.4 GB / 30.1 GB read" },
          { time: "22:31", level: "ok",   text: "shard-00002 SHA256 verified ✓" },
          { time: "22:24", level: "ok",   text: "shard-00001 SHA256 verified ✓" },
          { time: "22:23", level: "info", text: "All transfers finished — starting verification pass" },
          { time: "13:58", level: "warn", text: "Stall detected (speed 12 KB/s for 9 min) — restarting aria2c" }
        ]
      },
      {
        id: "job-mixtral-8x22b",
        name: "mistralai/Mixtral-8x22B-Instruct-v0.3-GGUF",
        subtitle: "3 files · all verified",
        status: "COMPLETE",
        dest: "D:\\models\\huggingface\\mistralai_Mixtral-8x22B",
        totalBytes: 84.6 * GB,
        doneBytes: 84.6 * GB,
        speedBps: 0,
        etaSeconds: 0,
        recoveries: 6,
        bytesLost: 0,
        startedLabel: "Aug 18, 2026 · 21:40",
        elapsedLabel: "1d 08h 12m",
        avgSpeedBps: 2.9 * MB,
        files: [
          { name: "mixtral-8x22b-Q5_K_M-00001-of-00003.gguf", bytes: 28.2 * GB, progress: 100, state: "done" },
          { name: "mixtral-8x22b-Q5_K_M-00002-of-00003.gguf", bytes: 28.2 * GB, progress: 100, state: "done" },
          { name: "mixtral-8x22b-Q5_K_M-00003-of-00003.gguf", bytes: 28.2 * GB, progress: 100, state: "done" }
        ],
        log: [
          { time: "05:52", level: "ok",   text: "Job complete — 84.6 GB, 3/3 files SHA256 verified ✓" },
          { time: "05:49", level: "ok",   text: "shard-00003 SHA256 verified ✓" },
          { time: "05:31", level: "ok",   text: "shard-00002 SHA256 verified ✓" },
          { time: "05:12", level: "ok",   text: "shard-00001 SHA256 verified ✓" },
          { time: "23:18", level: "warn", text: "Host rebooted — download resumed automatically" },
          { time: "21:40", level: "info", text: "Job created — 3 files queued, 84.6 GB total" }
        ]
      },
      {
        id: "job-llama4-scout",
        name: "meta-llama/Llama-4-Scout-109B-FP8",
        subtitle: "6 files · 1 corrupt",
        status: "FAILED",
        dest: "D:\\models\\huggingface\\meta-llama_Llama-4-Scout-109B-FP8",
        totalBytes: 128.4 * GB,
        doneBytes: 107.0 * GB,
        speedBps: 0,
        etaSeconds: null,
        recoveries: 22,
        bytesLost: 21.4 * GB,
        startedLabel: "Aug 14, 2026 · 08:15",
        elapsedLabel: "2d 19h 06m",
        avgSpeedBps: 1.9 * MB,
        files: [
          { name: "model-00001-of-00006.safetensors", bytes: 21.4 * GB, progress: 100, state: "done" },
          { name: "model-00002-of-00006.safetensors", bytes: 21.4 * GB, progress: 100, state: "done" },
          { name: "model-00003-of-00006.safetensors", bytes: 21.4 * GB, progress: 100, state: "done" },
          { name: "model-00004-of-00006.safetensors", bytes: 21.4 * GB, progress: 100, state: "done" },
          { name: "model-00005-of-00006.safetensors", bytes: 21.4 * GB, progress: 100, state: "corrupt" },
          { name: "model-00006-of-00006.safetensors", bytes: 21.4 * GB, progress: 0,   state: "queued" }
        ],
        log: [
          { time: "03:21", level: "err",  text: "Job failed — shard-00005 hash mismatch after 3 re-downloads" },
          { time: "03:21", level: "err",  text: "shard-00005 SHA256 mismatch ✗ expected 9f2c…a41d, got 4b70…c092" },
          { time: "02:04", level: "info", text: "Re-downloading shard-00005 from scratch (attempt 3/3)" },
          { time: "01:58", level: "err",  text: "shard-00005 SHA256 mismatch ✗ (attempt 2/3)" },
          { time: "22:40", level: "warn", text: "Repository gated — refreshed access token from keyring" },
          { time: "14:02", level: "warn", text: "Stall detected (speed 3 KB/s for 15 min) — restarting aria2c" },
          { time: "08:15", level: "info", text: "Job created — 6 files queued, 128.4 GB total" }
        ]
      }
    ]
  };
})();
