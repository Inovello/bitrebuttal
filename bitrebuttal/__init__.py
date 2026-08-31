"""Bit Rebuttal - resilient aria2c-based downloader for huge model files.

Public surface used by the CLI and (later) server.py:

    from bitrebuttal.engine import Engine
    engine = Engine()               # optional: data_dir=, poll_interval=
    engine.start(port=7451)         # writes the portfile, resumes unfinished jobs
    engine.status_payload()         # the full GET /api/status dict
    engine.resolve_payload(url)     # POST /api/resolve
    engine.add_job(url, files, dest)
    engine.pause_job(id) / resume_job(id) / delete_job(id, delete_files)
    engine.update_settings({...})
    engine.stop()
"""

__version__ = "1.0.1"
APP_NAME = "bitrebuttal"
DISPLAY_NAME = "Bit Rebuttal"

__all__ = ["__version__", "APP_NAME", "DISPLAY_NAME"]
