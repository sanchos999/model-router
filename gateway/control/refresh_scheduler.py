"""R13 §5: safe background refresh scheduler (control plane).

Three SEPARATE jobs with SEPARATE TTLs (defaults tuned for provider limits —
both marketplaces are catalog-GET friendly; catalog changes rarely, market
pricing moves hourly, health evidence is cheap for pool routes only):

  catalog+pricing (discovery refresh): every 6h  — includes market prices
       and price-history snapshots (the /admin/discovery/refresh path with
       all R13 guards: sanity + shrink).
  market/pricing extra pass: every 1h — same refresh call, guarded by TTL
       so it is cheap; keeps discounts/freshness current.
  availability (pool routes only): every 30min — FREE catalog probes for
       POOL models only. NEVER paid inference probes, NEVER hundreds of
       unmatched models.

Last/next run + last error per job are stored in control.db (refresh_jobs)
and exposed via /admin/refresh/schedule for the UI.
Every job is best-effort: a failure records the error and retries next tick.
"""
from __future__ import annotations

import asyncio
import time

from . import store

# seconds
CATALOG_TTL_S = 6 * 3600
MARKET_TTL_S = 3600
AVAILABILITY_TTL_S = 1800

JOBS = {
    "catalog": {"ttl": CATALOG_TTL_S, "label": "Каталог и цены"},
    "market": {"ttl": MARKET_TTL_S, "label": "Цены рынка"},
    "availability": {"ttl": AVAILABILITY_TTL_S, "label": "Доступность (пул)"},
}

_task: asyncio.Task | None = None
_stop = asyncio.Event()


def _table(c) -> None:
    c.executescript("""
CREATE TABLE IF NOT EXISTS refresh_jobs (
    job TEXT PRIMARY KEY,
    last_run REAL, last_ok INTEGER, last_error TEXT,
    next_run REAL, runs INTEGER NOT NULL DEFAULT 0
);
""")


def _get_state(c, job: str) -> dict:
    r = c.execute("SELECT * FROM refresh_jobs WHERE job=?", (job,)).fetchone()
    if r is None:
        return {}
    return {"last_run": r["last_run"], "last_ok": bool(r["last_ok"]),
            "last_error": r["last_error"], "next_run": r["next_run"],
            "runs": r["runs"]}


def _set_state(c, job: str, *, ok: bool, err: str | None,
               next_run: float, runs: int) -> None:
    c.execute(
        "INSERT INTO refresh_jobs (job, last_run, last_ok, last_error, next_run, runs)"
        " VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(job) DO UPDATE SET last_run=excluded.last_run,"
        " last_ok=excluded.last_ok, last_error=excluded.last_error,"
        " next_run=excluded.next_run, runs=excluded.runs",
        (job, time.time(), 1 if ok else 0, err, next_run, runs))


async def _run_discovery() -> tuple[bool, str | None]:
    from . import discovery
    try:
        out = await discovery.refresh_all()
        ok = bool(out.get("provider_a", {}).get("ok")) or \
             bool(out.get("provider_b", {}).get("ok"))
        errs = [f"{p}: {v.get('error') or v.get('blocked_by')}"
                for p, v in out.items()
                if isinstance(v, dict) and not v.get("ok")]
        return ok, ("; ".join(errs) or None)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


async def _run_availability() -> tuple[bool, str | None]:
    """FREE catalog probes for POOL models only (never unmatched, never paid)."""
    from . import inventory, probe as _probe
    try:
        inv = inventory.build_inventory()
        pool = [g for g in inv["models"] if g.get("in_pool") and g.get("canonical")]
        sem = asyncio.Semaphore(2)
        err_n = 0
        for g in pool:
            async with sem:
                res = await _probe.probe_route(
                    g["routes"][0]["provider"],
                    g["routes"][0]["provider_model_id"], deep=False)
                if not res.get("ok"):
                    err_n += 1
        # job is "ok" when it ran; per-model failures are normal market state
        return True, (f"{err_n}/{len(pool)} недоступны" if err_n else None)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


async def _tick() -> None:
    while not _stop.is_set():
        c = store.conn()
        try:
            _table(c)
        except Exception:  # noqa: BLE001
            pass
        now = time.time()
        for job, meta in JOBS.items():
            try:
                st = _get_state(c, job)
                if st and st.get("next_run") and now < st["next_run"]:
                    continue
            except Exception:  # noqa: BLE001
                st = {}
            if job == "availability":
                ok, err = await _run_availability()
            else:
                ok, err = await _run_discovery()
            try:
                _set_state(c, job, ok=ok, err=err,
                           next_run=time.time() + meta["ttl"],
                           runs=(st.get("runs") or 0) + 1)
                c.commit()
                # R14 §27: every auto job run is an audit event too
                store.audit("scheduler", "availability.check" if job == "availability"
                            else "market.refresh", job, {"ok": ok, "error": err})
            except Exception:  # noqa: BLE001
                pass
            except Exception:  # noqa: BLE001
                pass
        # wake every minute, jobs decide by TTL
        try:
            await asyncio.wait_for(_stop.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            pass


def start_scheduler() -> None:
    global _task
    if _task is None or _task.done():
        _stop.clear()
        _task = asyncio.get_event_loop().create_task(_tick())


def stop_scheduler() -> None:
    _stop.set()


def schedule_state() -> dict:
    """For /admin/refresh/schedule + the UI banner."""
    c = store.conn()
    try:
        _table(c)
    except Exception:  # noqa: BLE001
        pass
    out = {}
    for job, meta in JOBS.items():
        st = _get_state(c, job) or {}
        out[job] = {"label": meta["label"], "ttl_s": meta["ttl"], **st}
    return {"jobs": out, "now": time.time()}
