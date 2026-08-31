/* Bit Rebuttal — mock payload for ?mock=1.
 *
 * window.BR_MOCK is the *seed* for the mock api in app.js; it mirrors the FULL
 * v2 GET /api/status shape (API-CONTRACT.md v1 + "v2 additions") plus a
 * `resolved` block the mock POST /api/resolve serves.
 *
 *   resolved.normal — fits comfortably in freeBytes (happy path)
 *   resolved.huge   — 336 GB, deliberately larger than freeBytes so that
 *                     resolving anything matching /big|huge|xxl|deepseek/i
 *                     exercises the capacity error + the 409 from createJob.
 *
 * backend.gui is true here so the Settings → Browse button is reachable in mock
 * mode; the real server reports false unless the desktop shell is attached.
 */
window.BR_MOCK = {

  backend: {
    healthy: true,
    label: 'Supervisor watching',
    version: '0.1.0',
    uptime: '6d 04h',
    aria2cVersion: '1.37.0',
    gui: true
  },

  disk: {
    path: 'C:\\Users\\wreck\\Downloads\\longrebuttal',
    freeBytes: 224400000000,
    volumeLabel: 'C:',
    afterQueueBytes: 189100000000
  },

  settings: {
    destination: 'C:\\Users\\wreck\\Downloads\\longrebuttal',
    connections: 4,
    stallSensitivity: 'Normal',
    serviceInstalled: false,
    verifyChecksums: true,
    bandwidthCapMBs: 0,
    quietHours: { enabled: false, start: '23:00', end: '07:30' },
    theme: 'mauve',
    hfTokenSet: false
  },

  recents: [
    'meta-llama/Llama-3.3-70B',
    'unsloth/gemma-3-27b-it-GGUF',
    'black-forest-labs/FLUX.1-dev',
    'deepseek-ai/DeepSeek-V3.1-Base',
    'cdn.example.org/dataset-04.tar'
  ],

  completedToday: 3,

  connections: [
    { id: 'c-01', speedBps: 11200000, host: 'cas-bridge.xethub.hf.co' },
    { id: 'c-02', speedBps: 10600000, host: 'cas-bridge.xethub.hf.co' },
    { id: 'c-03', speedBps: 4100000,  host: 'cdn-lfs-us-1.hf.co' },
    { id: 'c-04', speedBps: 13000000, host: 'cas-bridge.xethub.hf.co' }
  ],

  library: [
    {
      jobId: 'job-deepseek-base',
      name: 'deepseek-ai/DeepSeek-V3.1-Base',
      path: 'C:\\Users\\wreck\\Downloads\\longrebuttal\\deepseek-v3.1-base',
      sizeBytes: 129400000000,
      integrity: 'sha256 41/41',
      finishedLabel: 'Aug 29 · 04:11'
    },
    {
      jobId: 'job-gemma-gguf',
      name: 'unsloth/gemma-3-27b-it-GGUF',
      path: 'C:\\Users\\wreck\\Downloads\\longrebuttal\\gemma-3-27b-gguf',
      sizeBytes: 13800000000,
      integrity: '1 corrupt',
      finishedLabel: 'Aug 28 · 22:07'
    }
  ],

  jobs: [
    {
      id: 'job-voxtral',
      name: 'mistralai/Voxtral-Mini-4B-Realtime-2602',
      subtitle: '9 files · 17.7 GB · sha256 3/9',
      status: 'DOWNLOADING',
      archived: false,
      dest: 'C:\\Users\\wreck\\Downloads\\longrebuttal\\mistralai_Voxtral-Mini-4B-Realtime-2602',
      totalBytes: 17700000000,
      doneBytes: 7363200000,
      speedBps: 38900000,
      etaSeconds: 266,
      recoveries: 2,
      bytesLost: 0,
      startedLabel: 'Aug 31, 2026 · 10:42',
      elapsedLabel: '00h 07m',
      avgSpeedBps: 28400000,
      files: [
        { name: '.gitattributes',           bytes: 1500,       progress: 100, state: 'done' },
        { name: 'README.md',                bytes: 8400,       progress: 100, state: 'done' },
        { name: 'config.json',              bytes: 2100,       progress: 100, state: 'done' },
        { name: 'consolidated.safetensors', bytes: 8900000000, progress: 82,  state: 'downloading' },
        { name: 'generation_config.json',   bytes: 180,        progress: 0,   state: 'queued' },
        { name: 'model.safetensors',        bytes: 8790000000, progress: 0,   state: 'queued' },
        { name: 'params.json',              bytes: 640,        progress: 0,   state: 'queued' },
        { name: 'processor_config.json',    bytes: 920,        progress: 0,   state: 'queued' },
        { name: 'tekken.json',              bytes: 14700000,   progress: 0,   state: 'queued' }
      ],
      log: [
        { time: '10:49', level: 'warn', text: 'c-03 renegotiated after 12s of no bytes' },
        { time: '10:46', level: 'ok',   text: 'config.json verified — sha256 match' },
        { time: '10:45', level: 'ok',   text: 'README.md verified — sha256 match' },
        { time: '10:43', level: 'info', text: 'Split consolidated.safetensors into 4 ranges' },
        { time: '10:42', level: 'info', text: 'Launched aria2c (RPC 8717) — 9 files, URLs re-resolved' },
        { time: '10:42', level: 'info', text: 'Job created — 9 files queued, 17.7 GB total' }
      ]
    },
    {
      id: 'job-qwen32b',
      name: 'Qwen/Qwen3-32B-Instruct-GGUF',
      subtitle: '4 files · 21.4 GB · queued behind 01',
      status: 'PAUSED',
      archived: false,
      dest: 'C:\\Users\\wreck\\Downloads\\longrebuttal\\Qwen_Qwen3-32B-Instruct-GGUF',
      totalBytes: 21400000000,
      doneBytes: 0,
      speedBps: 0,
      etaSeconds: null,
      recoveries: 0,
      bytesLost: 0,
      startedLabel: 'Aug 31, 2026 · 10:44',
      elapsedLabel: '00h 00m',
      avgSpeedBps: 0,
      files: [
        { name: 'Qwen3-32B-Q4_K_M-00001-of-00004.gguf', bytes: 5400000000, progress: 0, state: 'queued' },
        { name: 'Qwen3-32B-Q4_K_M-00002-of-00004.gguf', bytes: 5400000000, progress: 0, state: 'queued' },
        { name: 'Qwen3-32B-Q4_K_M-00003-of-00004.gguf', bytes: 5400000000, progress: 0, state: 'queued' },
        { name: 'Qwen3-32B-Q4_K_M-00004-of-00004.gguf', bytes: 5200000000, progress: 0, state: 'queued' }
      ],
      log: [
        { time: '10:44', level: 'info', text: 'Qwen3-32B-Instruct-GGUF queued, 21.4 GB resolved' }
      ]
    },
    {
      id: 'job-sdturbo',
      name: 'stabilityai/sd-turbo-xl',
      subtitle: '12 files · 6.2 GB · verifying',
      status: 'VERIFYING',
      archived: false,
      dest: 'C:\\Users\\wreck\\Downloads\\longrebuttal\\stabilityai_sd-turbo-xl',
      totalBytes: 6200000000,
      doneBytes: 5468400000,
      speedBps: 0,
      etaSeconds: 55,
      recoveries: 0,
      bytesLost: 0,
      startedLabel: 'Aug 31, 2026 · 10:11',
      elapsedLabel: '00h 36m',
      avgSpeedBps: 31200000,
      files: [
        { name: 'model_index.json',                        bytes: 1600,       progress: 100, state: 'done' },
        { name: 'scheduler/scheduler_config.json',         bytes: 900,        progress: 100, state: 'done' },
        { name: 'text_encoder/model.safetensors',          bytes: 492000000,  progress: 100, state: 'done' },
        { name: 'text_encoder_2/model.safetensors',        bytes: 1390000000, progress: 100, state: 'done' },
        { name: 'tokenizer/merges.txt',                    bytes: 525000,     progress: 100, state: 'done' },
        { name: 'tokenizer/vocab.json',                    bytes: 1060000,    progress: 100, state: 'done' },
        { name: 'tokenizer_2/vocab.json',                  bytes: 1060000,    progress: 100, state: 'done' },
        { name: 'unet/diffusion_pytorch_model.safetensors', bytes: 3460000000, progress: 100, state: 'verifying' },
        { name: 'vae/diffusion_pytorch_model.safetensors', bytes: 167000000,  progress: 100, state: 'done' },
        { name: 'vae_decoder/model.safetensors',           bytes: 99000000,   progress: 100, state: 'done' },
        { name: 'vae_encoder/model.safetensors',           bytes: 68000000,   progress: 100, state: 'done' },
        { name: 'feature_extractor/preprocessor_config.json', bytes: 520,     progress: 100, state: 'done' }
      ],
      log: [
        { time: '10:47', level: 'ok',   text: 'sd-turbo-xl transfer complete — verifying 12 files' },
        { time: '10:39', level: 'info', text: 'aria2c exited cleanly — 12/12 files on disk' },
        { time: '10:11', level: 'info', text: 'Job created — 12 files queued, 6.2 GB total' }
      ]
    }
  ],

  // served by the mock POST /api/resolve
  resolved: {
    normal: {
      repo: 'unsloth/Qwen3.8-Flash-Next-GGUF',
      revision: 'main',
      files: [
        { name: 'Qwen3.8-Flash-Next-Q4_K_M-00001-of-00006.gguf', bytes: 5100000000, sha256: true,  selected: true },
        { name: 'Qwen3.8-Flash-Next-Q4_K_M-00002-of-00006.gguf', bytes: 5100000000, sha256: true,  selected: true },
        { name: 'Qwen3.8-Flash-Next-Q4_K_M-00003-of-00006.gguf', bytes: 5100000000, sha256: true,  selected: true },
        { name: 'Qwen3.8-Flash-Next-Q4_K_M-00004-of-00006.gguf', bytes: 5100000000, sha256: true,  selected: true },
        { name: 'Qwen3.8-Flash-Next-Q4_K_M-00005-of-00006.gguf', bytes: 5100000000, sha256: true,  selected: true },
        { name: 'Qwen3.8-Flash-Next-Q4_K_M-00006-of-00006.gguf', bytes: 4700000000, sha256: true,  selected: true },
        { name: 'README.md',                                     bytes: 11200,      sha256: false, selected: true },
        { name: 'config.json',                                   bytes: 1900,       sha256: false, selected: true }
      ]
    },
    huge: {
      repo: 'deepseek-ai/DeepSeek-V3.1-Base',
      revision: 'main',
      files: [
        { name: 'model-00001-of-00008.safetensors', bytes: 42000000000, sha256: true, selected: true },
        { name: 'model-00002-of-00008.safetensors', bytes: 42000000000, sha256: true, selected: true },
        { name: 'model-00003-of-00008.safetensors', bytes: 42000000000, sha256: true, selected: true },
        { name: 'model-00004-of-00008.safetensors', bytes: 42000000000, sha256: true, selected: true },
        { name: 'model-00005-of-00008.safetensors', bytes: 42000000000, sha256: true, selected: true },
        { name: 'model-00006-of-00008.safetensors', bytes: 42000000000, sha256: true, selected: true },
        { name: 'model-00007-of-00008.safetensors', bytes: 42000000000, sha256: true, selected: true },
        { name: 'model-00008-of-00008.safetensors', bytes: 42000000000, sha256: true, selected: true }
      ]
    }
  }
};
