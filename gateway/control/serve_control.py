"""Control-plane server (R6 §B/§R): Admin API + Admin UI on 127.0.0.1:4111
by default — a SEPARATE process/port from inference (:4101). Control-plane
crash cannot affect inference; inference crash cannot affect the admin UI.

Admin UI pages (R6 §L): dashboard, providers, models, policy, overrides,
simulator, audit/revisions, cache/health/metrics via JSON panes.
"""
from __future__ import annotations

import json
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .admin_api import router as admin_router
from .routing_explain import router as explain_router
from . import store

CONTROL_HOST = os.environ.get("GW_CONTROL_HOST", "127.0.0.1")
CONTROL_PORT = int(os.environ.get("GW_CONTROL_PORT", "4111"))

app = FastAPI(title="gateway-v2-control-plane", docs_url=None, redoc_url=None)
app.include_router(admin_router)
app.include_router(explain_router)


# R11: SIGUSR1 dumps all thread stacks to stderr (journald) — deadlock triage
# without py-spy/sudo privileges.
def _dump_stacks(signum, frame):  # noqa: ARG001
    import faulthandler
    import sys
    faulthandler.dump_traceback(file=sys.stderr)


try:
    import signal
    signal.signal(signal.SIGUSR1, _dump_stacks)
except (ValueError, OSError):
    pass  # non-main thread or restricted env


@app.on_event("startup")
async def _start_refresh_scheduler():
    # R13 §5: safe auto-refresh (catalog 6h / market 1h / pool availability 30m)
    from . import refresh_scheduler
    refresh_scheduler.start_scheduler()


@app.on_event("shutdown")
async def _stop_refresh_scheduler():
    from . import refresh_scheduler
    refresh_scheduler.stop_scheduler()


@app.get("/admin-ui/")
async def admin_index():
    return FileResponse("gateway/control/static/admin/index.html")


@app.get("/admin-ui/app.js")
async def admin_app():
    return FileResponse("gateway/control/static/admin/app.js")


@app.get("/admin-ui/styles.css")
async def admin_styles():
    return FileResponse("gateway/control/static/admin/styles.css")


app.mount("/admin-ui", StaticFiles(directory="gateway/control/static/admin", html=True), name="admin-ui")
@app.get("/healthz")
async def root_health():
    return {"ok": True, "service": "gateway-v2-control-plane", "port": CONTROL_PORT}
