"""FastAPI service layer.

No sign-in: a signed, HttpOnly session cookie (1-year TTL) is the identity. Everything
is namespaced by ``session_id``; every query filters by it. The browser uploads the
movie straight to S3 with a presigned PUT (the API never touches the large file), then
the front half is enqueued; a per-language run kicks off the back half after a pre-flight
budget check. Progress streams over SSE; the recap is fetched via a presigned GET.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import BaseModel
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from jieshuoforge.config import _slugify
from jieshuoforge.pipeline import BACK_HALF, FRONT_HALF, STAGE_COMPUTE, SUPPORTED_LANGS

from . import logging_config, metrics
from .budget import Budget
from .db import (
    Movie,
    MovieStatus,
    Run,
    RunStatus,
    init_db,
    session_scope,
    touch_session,
)
from .settings import get_settings

log = logging.getLogger("jieshuoforge_web.api")
S = get_settings()
_signer = URLSafeSerializer(S.session_secret, salt="jf-session")
_STATIC = Path(__file__).parent / "static"

@asynccontextmanager
async def lifespan(app: FastAPI):
    logging_config.configure()
    init_db()
    metrics.register_api_collector()
    yield


app = FastAPI(title="Movie Recap 電影解說", docs_url="/api/docs", lifespan=lifespan)


# ---------------------------------------------------------------------------
# session: signed cookie, minted on first request, valid 1 year
# ---------------------------------------------------------------------------
def get_session(response: Response, jf_session: str | None = Cookie(default=None)) -> str:
    sid: str | None = None
    if jf_session:
        try:
            sid = _signer.loads(jf_session)
        except BadSignature:
            sid = None
    if not sid:
        import uuid

        sid = str(uuid.uuid4())
        response.set_cookie(
            S.session_cookie, _signer.dumps(sid),
            max_age=S.session_max_age, httponly=True,
            samesite="lax", secure=S.cookie_secure, path="/",
        )
    with session_scope() as db:
        touch_session(db, sid)
    return sid


SessionId = Depends(get_session)


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------
class CreateMovie(BaseModel):
    filename: str
    content_type: str = "video/mp4"
    lang: str = "zh"      # output language chosen at upload; back half auto-runs in it


class CreateRun(BaseModel):
    movie_id: str
    lang: str = "zh"
    force: bool = False   # regenerate a fresh recap even if one already exists for this language


def _stage_rows(stages: list | None) -> list[dict]:
    # ``at`` is the row's last-update epoch; for the currently-running stage that's its
    # start time, so the browser can show a live elapsed timer (now - at).
    return [
        {"stage": s.stage, "status": s.status, "seconds": s.seconds,
         "at": s.recorded_at.timestamp() if s.recorded_at else None}
        for s in (stages or [])
    ]


def _movie_view(m: Movie, stages: list | None = None) -> dict:
    return {
        "id": m.id, "filename": m.original_filename, "slug": m.slug,
        "status": m.status.value, "duration_sec": m.duration_sec, "error": m.error,
        "has_source": bool(m.source_key) and m.status != MovieStatus.registered,
        "now": time.time(),                    # server clock, for skew-free elapsed in the UI
        "all_stages": list(FRONT_HALF),       # ordered front-half pipeline (ingest -> scenes)
        "stage_kinds": STAGE_COMPUTE,         # stage -> "llm"|"gpu"|"cpu" for UI styling
        "stages": _stage_rows(stages),        # per-stage timing/state recorded so far
    }


def _run_view(r: Run, stages: list | None = None) -> dict:
    return {
        "id": r.id, "movie_id": r.movie_id, "lang": r.lang, "status": r.status.value,
        "llm_tokens_in": r.llm_tokens_in, "llm_tokens_out": r.llm_tokens_out,
        "llm_cost_usd": round(r.llm_cost_usd, 5), "output_duration_sec": r.output_duration_sec,
        "has_output": bool(r.output_key), "error": r.error,
        "now": time.time(),                    # server clock, for skew-free elapsed in the UI
        "all_stages": list(BACK_HALF),        # ordered back-half pipeline (understand -> render)
        "stage_kinds": STAGE_COMPUTE,         # stage -> "llm"|"gpu"|"cpu" for UI styling
        "stages": _stage_rows(stages),
    }


# ---------------------------------------------------------------------------
# movies
# ---------------------------------------------------------------------------
@app.post("/api/movies")
def create_movie(body: CreateMovie, sid: str = SessionId):
    from .storage import storage_for_web

    slug = _slugify(Path(body.filename).stem)
    ext = Path(body.filename).suffix or ".mp4"
    # Output language picked at upload time; the front half auto-starts the recap in it (one
    # step). Coerce anything unsupported to the default so an upload is never blocked on it.
    lang = body.lang.lower()
    if lang not in SUPPORTED_LANGS:
        lang = SUPPORTED_LANGS[0]
    with session_scope() as db:
        # MAX_MOVIES_PER_SESSION <= 0 disables the per-session cap (unlimited uploads).
        if S.max_movies_per_session > 0:
            n = db.query(Movie).filter(Movie.session_id == sid).count()
            if n >= S.max_movies_per_session:
                raise HTTPException(429, f"movie limit reached ({S.max_movies_per_session} per session)")
        movie = Movie(
            session_id=sid, original_filename=body.filename, slug=slug,
            s3_prefix="", source_key="", status=MovieStatus.registered, default_lang=lang,
        )
        db.add(movie)
        db.flush()
        mid = movie.id
        # scope by the unique movie_id so two same-named uploads in one session don't collide
        movie.s3_prefix = f"{sid}/{mid}/{slug}"
        source_key = f"sources/{sid}/{mid}/{slug}{ext}"
        movie.source_key = source_key
    store = storage_for_web(S)
    # Do NOT sign Content-Type into the presigned PUT: mobile browsers (esp. iOS) often report an
    # empty file.type, so a signed content_type wouldn't match the header the browser sends and S3
    # rejects with 403 SignatureDoesNotMatch (every mobile upload failed this way). The stored
    # Content-Type is irrelevant — the worker re-downloads and ffmpeg sniffs the real format.
    upload_url = store.presign_put(source_key, expires=S.upload_url_ttl)
    return {"movie_id": mid, "upload_url": upload_url, "method": "PUT", "source_key": source_key}


@app.post("/api/movies/{movie_id}/complete")
def complete_movie(movie_id: str, sid: str = SessionId):
    from .storage import storage_for_web

    from .tasks import start_front_half

    with session_scope() as db:
        movie = db.get(Movie, movie_id)
        if movie is None or movie.session_id != sid:
            raise HTTPException(404, "movie not found")
        source_key = movie.source_key
        already = movie.status not in (MovieStatus.registered, MovieStatus.error)
    store = storage_for_web(S)
    if not store.exists(source_key):
        raise HTTPException(400, "upload not found in storage; PUT the file to upload_url first")
    if not already:
        with session_scope() as db:
            db.get(Movie, movie_id).status = MovieStatus.uploaded
        start_front_half(movie_id)
    return {"ok": True, "movie_id": movie_id}


@app.get("/api/movies")
def list_movies(sid: str = SessionId):
    with session_scope() as db:
        movies = db.scalars(
            select(Movie).where(Movie.session_id == sid).order_by(Movie.uploaded_at.desc())
        ).all()
        return {"movies": [_movie_view(m, m.stages) for m in movies]}


@app.get("/api/movies/{movie_id}/source-url")
def movie_source_url(movie_id: str, sid: str = SessionId):
    """Presigned GET for the original uploaded movie, so the browser can stream it in a
    player (S3/MinIO honour range requests, so large files seek without downloading)."""
    from .storage import storage_for_web

    with session_scope() as db:
        movie = db.get(Movie, movie_id)
        if movie is None or movie.session_id != sid:
            raise HTTPException(404, "movie not found")
        if not movie.source_key or movie.status == MovieStatus.registered:
            raise HTTPException(409, "source not uploaded yet")
        key, filename = movie.source_key, movie.original_filename
    store = storage_for_web(S)
    return JSONResponse({"url": store.presign_get(key, expires=S.result_url_ttl), "filename": filename})


@app.get("/api/movies/{movie_id}/runs")
def list_movie_runs(movie_id: str, sid: str = SessionId):
    with session_scope() as db:
        movie = db.get(Movie, movie_id)
        if movie is None or movie.session_id != sid:
            raise HTTPException(404, "movie not found")
        runs = db.scalars(select(Run).where(Run.movie_id == movie_id).order_by(Run.created_at)).all()
        return {"runs": [_run_view(r, r.stages) for r in runs]}


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------
@app.post("/api/runs")
def create_run(body: CreateRun, sid: str = SessionId):
    from .tasks import clear_back_half_artifacts, start_back_half

    lang = body.lang.lower()
    if lang not in SUPPORTED_LANGS:
        raise HTTPException(400, f"unsupported lang {lang!r}; expected {SUPPORTED_LANGS}")
    slug = None
    with session_scope() as db:
        movie = db.get(Movie, body.movie_id)
        if movie is None or movie.session_id != sid:
            raise HTTPException(404, "movie not found")
        if movie.status != MovieStatus.ready:
            raise HTTPException(409, f"movie not ready (status={movie.status.value})")
        slug = movie.slug
        # Idempotent per (movie, lang) — return the existing recap — UNLESS force=True, which
        # regenerates a fresh one (any language, even one that's already done).
        existing = db.scalar(select(Run).where(Run.movie_id == body.movie_id, Run.lang == lang))
        if existing is not None and existing.status != RunStatus.error and not body.force:
            return _run_view(existing)
        # per-session concurrency cap (a finished run being regenerated doesn't count as active)
        active = db.query(Run).filter(
            Run.session_id == sid, Run.status.in_([RunStatus.queued, RunStatus.running])
        ).count()
        if active >= S.max_concurrent_runs_per_session:
            raise HTTPException(429, f"too many concurrent runs ({S.max_concurrent_runs_per_session})")

        # No money budget: enqueue straight away (token/cost usage is still recorded per
        # run for display). The concurrency cap above protects the GPU box, not a wallet.
        run = existing
        if run is None:
            run = Run(movie_id=body.movie_id, session_id=sid, lang=lang, status=RunStatus.queued)
            db.add(run)
        else:                                  # re-queue: clear the prior result so the UI resets
            run.status = RunStatus.queued
            run.error = None
            run.output_key = None
            run.output_duration_sec = None
        db.flush()
        rid = run.id
    # Regenerate: drop the cached back-half artifacts for this (movie, lang) so the chain
    # recomputes a FRESH recap rather than returning the old one. The shared front-half cache is
    # left intact (ASR/scenes aren't redone). Must happen before the chain re-materializes from S3.
    if body.force:
        clear_back_half_artifacts(sid, body.movie_id, slug, lang)
    start_back_half(rid)
    with session_scope() as db:
        return _run_view(db.get(Run, rid))


def _fetch_run(run_id: str, sid: str) -> dict:
    with session_scope() as db:
        run = db.get(Run, run_id)
        if run is None or run.session_id != sid:
            raise HTTPException(404, "run not found")
        return _run_view(run, run.stages)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, sid: str = SessionId):
    return _fetch_run(run_id, sid)


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, sid: str = SessionId):
    """Server-sent events: poll the run row until it reaches a terminal state."""

    async def stream():
        last = None
        for _ in range(3600):  # safety bound (~2h at 2s)
            try:
                view = await run_in_threadpool(_fetch_run, run_id, sid)
            except HTTPException:
                yield f"event: error\ndata: {json.dumps({'error': 'not found'})}\n\n"
                return
            # Dedup on real state, ignoring the volatile ``now``: the browser ticks the
            # current stage's elapsed locally, so we only push on an actual change.
            sig = json.dumps({k: v for k, v in view.items() if k != "now"}, ensure_ascii=False)
            if sig != last:
                yield f"data: {json.dumps(view, ensure_ascii=False)}\n\n"
                last = sig
            if view["status"] in ("done", "error"):
                return
            await asyncio.sleep(2)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/runs/{run_id}/result")
def run_result(run_id: str, sid: str = SessionId):
    from .storage import storage_for_web

    with session_scope() as db:
        run = db.get(Run, run_id)
        if run is None or run.session_id != sid:
            raise HTTPException(404, "run not found")
        if not run.output_key:
            raise HTTPException(409, "result not ready")
        key = run.output_key
    store = storage_for_web(S)
    return JSONResponse({"url": store.presign_get(key, expires=S.result_url_ttl)})


# ---------------------------------------------------------------------------
# budget + ops
# ---------------------------------------------------------------------------
@app.get("/api/budget")
def budget_status(sid: str = SessionId):
    b = Budget()
    return {
        "remaining_usd": round(b.remaining_usd(), 4),
        "spent_usd": round(b.spent_usd(), 4),
        "cap_usd": S.llm_max_cost_usd,
        "per_run_estimate_usd": round(S.run_cost_estimate_usd(), 4),
    }


@app.get("/metrics")
def prometheus_metrics():
    body, content_type = metrics.render_latest()
    return Response(content=body, media_type=content_type)


@app.get("/healthz")
def healthz():
    return {"ok": True}


# static frontend (mounted last so /api/* and /metrics win)
if _STATIC.exists():
    @app.get("/")
    def index():
        return FileResponse(_STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
