"""FastAPI wiring: routes -> engine methods, exception mapping, static UI.

The engine (``longrebuttal/engine.py``) owns all the logic; this module is only
HTTP shape. Two public callables:

    create_app(engine)  -> FastAPI   (no side effects; tests use an unstarted Engine)
    run(port, headless) -> int       (CLI entry: Engine().start(port), uvicorn on 127.0.0.1)
"""

from __future__ import annotations

import threading
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import service
from .engine import DiskSpaceError, Engine, EngineError
from .resolve import ResolveError

DEFAULT_PORT = 7451


class ResolveBody(BaseModel):
    url: str


class JobBody(BaseModel):
    url: str
    files: Optional[List[str]] = None
    dest: Optional[str] = None


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": message})


def _validation_message(exc: RequestValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "invalid request body"
    first = errors[0]
    loc = ".".join(str(x) for x in first.get("loc", ()) if x != "body")
    msg = str(first.get("msg", "invalid value"))
    return f"invalid request body: {loc} {msg}" if loc else f"invalid request body: {msg}"


def _service_response(result: Dict[str, Any]) -> JSONResponse:
    if result.get("error"):
        message = str(result["error"])
        if result.get("command"):
            message += " Run manually: " + str(result["command"])
        return JSONResponse(status_code=400, content={
            "error": message, "installed": bool(result.get("installed"))})
    return JSONResponse(status_code=200, content=result)


def create_app(engine: Engine) -> FastAPI:
    app = FastAPI()
    port = int(getattr(engine, "_port", 0) or DEFAULT_PORT)

    @app.exception_handler(ResolveError)
    async def _on_resolve_error(request: Request, exc: ResolveError) -> JSONResponse:
        return _error(400, str(exc))

    # Registered before EngineError: it is a subclass and must win (HTTP 409).
    @app.exception_handler(DiskSpaceError)
    async def _on_disk_space(request: Request, exc: DiskSpaceError) -> JSONResponse:
        return _error(409, str(exc))

    @app.exception_handler(EngineError)
    async def _on_engine_error(request: Request, exc: EngineError) -> JSONResponse:
        return _error(400, str(exc))

    # Malformed bodies (missing "url", wrong types, non-JSON) -> 400 {"error": ...},
    # never FastAPI's default 422 detail shape.
    @app.exception_handler(RequestValidationError)
    async def _on_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(400, _validation_message(exc))

    @app.get("/api/status")
    async def api_status() -> Dict[str, Any]:
        return engine.status_payload()

    @app.post("/api/resolve")
    async def api_resolve(body: ResolveBody) -> Dict[str, Any]:
        return engine.resolve_payload(body.url)

    @app.post("/api/jobs", status_code=201)
    async def api_add_job(body: JobBody) -> Dict[str, Any]:
        return engine.add_job(body.url, body.files, body.dest)

    @app.post("/api/jobs/{job_id}/pause")
    async def api_pause(job_id: str) -> Dict[str, Any]:
        return engine.pause_job(job_id)

    @app.post("/api/jobs/{job_id}/resume")
    async def api_resume(job_id: str) -> Dict[str, Any]:
        return engine.resume_job(job_id)

    @app.delete("/api/jobs/{job_id}")
    async def api_delete(job_id: str, deleteFiles: str = "false") -> Dict[str, Any]:
        return engine.delete_job(job_id, deleteFiles.strip().lower() == "true")

    @app.put("/api/settings")
    async def api_settings(body: dict) -> Dict[str, Any]:
        return engine.update_settings(body)

    @app.post("/api/service/install")
    async def api_service_install() -> JSONResponse:
        return _service_response(service.install(port=port))

    @app.post("/api/service/remove")
    async def api_service_remove() -> JSONResponse:
        return _service_response(service.uninstall())

    # Static UI: mounted last so every /api route above wins.
    app.mount("/", StaticFiles(directory=str(Path(__file__).resolve().parent / "static"),
                               html=True), name="ui")
    return app


def run(port: int = DEFAULT_PORT, headless: bool = False) -> int:
    import uvicorn

    engine = Engine()
    engine.start(port=port)
    app = create_app(engine)
    if not headless:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
    finally:
        engine.stop()
    return 0
