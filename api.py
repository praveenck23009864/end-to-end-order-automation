"""Authenticated local API serving protected customer records."""
from contextlib import asynccontextmanager
import os
import re
import secrets
import threading
from typing import Literal
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, StrictBool

import database as db
from new import validate_order
from sensitive_data import (
    SensitiveDataError, assert_non_sensitive_order, get_api_token, SAFE_STAGES,
)
from jwe_crypto import JWEError, MAX_JWE_BYTES, decrypt_sensitive_submission, public_key_document
from sensitive_schemas import SensitivePayloadError

bearer = HTTPBearer(auto_error=False)
_ACTIVE_WORKER_RUNS = set()
_ACTIVE_WORKER_LOCK = threading.Lock()


def is_local_worker_active(run_id):
    with _ACTIVE_WORKER_LOCK:
        return run_id in _ACTIVE_WORKER_RUNS


def _safe_worker_detail(error, payload=None, order=None):
    detail = str(error) or type(error).__name__
    values = set()
    if isinstance(payload, dict):
        values.update(v for v in payload.values() if isinstance(v, str) and v)
    def collect_order_values(value):
        if isinstance(value, dict):
            for item in value.values():
                collect_order_values(item)
        elif isinstance(value, list):
            for item in value:
                collect_order_values(item)
        elif isinstance(value, str) and len(value) >= 3:
            values.add(value)

    collect_order_values(order)
    for value in sorted(values, key=len, reverse=True):
        detail = detail.replace(value, "[redacted]")
    detail = re.sub(r"(?<!\w)(?:\d[ -]?){9,19}(?!\w)", "[redacted-number]", detail)
    detail = re.sub(r"\b\d{3,4}\b", "[redacted-number]", detail)
    detail = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "[redacted-date]", detail)
    detail = re.sub(r"\b[0-9a-fA-F]{64}\b", "[redacted-token]", detail)
    return detail[:1000]


def authorize(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
):
    if (
        credentials is None
        or not secrets.compare_digest(
            credentials.credentials.encode("utf-8"), request.app.state.api_token.encode("ascii")
        )
    ):
        raise HTTPException(401, "Authentication required.")


@asynccontextmanager
async def lifespan(app):
    app.state.api_token = get_api_token()
    db.init_db()
    print(f"Run Management API database: {db.DB_PATH}", flush=True)
    try:
        db.check_stored_protection()
    except (ValueError, TypeError):
        raise RuntimeError(
            "Database contains sensitive stored fields. Run scrub_sensitive_data.py before starting."
        ) from None
    yield


app = FastAPI(
    title="Allconnect protected local API",
    lifespan=lifespan,
    dependencies=[Depends(authorize)],
)


@app.middleware("http")
async def no_cache(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(db.Missing)
async def missing(request, error):
    return JSONResponse(status_code=404, content={"detail": "Record not found."})


@app.exception_handler(db.Conflict)
async def conflict(request, error):
    return JSONResponse(
        status_code=409,
        content={"detail": str(error) or "Record conflict. Check the existing run before retrying."},
    )


@app.exception_handler(SensitiveDataError)
async def sensitive_failure(request, error):
    return JSONResponse(
        status_code=422,
        content={"detail": "Protected customer data could not be verified."},
    )


@app.exception_handler(JWEError)
@app.exception_handler(SensitivePayloadError)
async def invalid_sensitive_payload(request, error):
    return JSONResponse(
        status_code=422, content={"detail": "Sensitive submission could not be verified."}
    )


@app.exception_handler(RequestValidationError)
async def invalid_request(request, error):
    return JSONResponse(
        status_code=422, content={"detail": "Invalid request fields."}
    )


class NewOrder(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    data: dict


class StartRun(BaseModel):
    run_id: UUID


class Progress(BaseModel):
    status: Literal["running", "installation_completed", "failed", "cancelled"]
    stage: str = Field(min_length=1, max_length=120)
    error_code: Literal[
        "automation_error", "api_unavailable", "interrupted",
        "startup_error", "manual_recovery",
    ] | None = None


class JweSubmission(BaseModel):
    jwe: str = Field(min_length=1, max_length=MAX_JWE_BYTES)


class RunRecovery(BaseModel):
    outcome: Literal["submitted", "not_submitted", "unknown"]
    worker_stopped: StrictBool


@app.get("/health")
def health():
    with db.connection() as con:
        con.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/crypto/public-key")
def encryption_public_key():
    return public_key_document()


@app.get("/orders")
def orders(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    return db.list_orders(limit, offset)


@app.post("/orders", status_code=201)
def create_order(order: NewOrder):
    try:
        assert_non_sensitive_order(order.data, order.id)
        validate_order(order.data)
    except ValueError:
        raise HTTPException(422, "Invalid non-sensitive order fields.") from None
    result = db.import_orders({order.id: order.data})
    if result["skipped"]:
        raise HTTPException(409, "Order ID already exists.")
    return {"id": order.id}


@app.get("/orders/{order_id}")
def order(order_id: str):
    return db.get_order(order_id)


@app.post("/orders/{order_id}/runs")
def start(order_id: str, body: StartRun):
    return db.start_run(order_id, str(body.run_id))


def _run_sensitive_workflow(run_id, order, payload, payment_required, worker_id):
    """Track this in-process background worker and always release its lease."""
    stop = threading.Event()
    heartbeat_thread = None
    with _ACTIVE_WORKER_LOCK:
        _ACTIVE_WORKER_RUNS.add(run_id)
    try:
        def heartbeat():
            while not stop.wait(db.WORKER_HEARTBEAT_SECONDS):
                try:
                    db.heartbeat_run_worker(run_id, worker_id)
                except Exception as error:
                    print(
                        f"Worker heartbeat failed run_id={run_id} "
                        f"type={type(error).__name__}: "
                        f"{_safe_worker_detail(error, payload, order)}",
                        flush=True,
                    )
                    return

        heartbeat_thread = threading.Thread(
            target=heartbeat,
            name=f"run-heartbeat-{run_id[:8]}",
            daemon=True,
        )
        heartbeat_thread.start()
        from run_from_api import run_backend_workflow

        run_backend_workflow(run_id, order, payload, payment_required)
    except BaseException as error:
        try:
            current = db.get_run(run_id)
            if current.get("submission_state") in ("attempting", "ambiguous", "unknown"):
                if current.get("submission_state") == "attempting":
                    db.set_submission_state(run_id, "ambiguous")
                db.update_run(
                    run_id, "running", "submission outcome ambiguous", "manual_recovery"
                )
            else:
                code = (
                    "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit))
                    else "api_unavailable" if isinstance(error, api_client.ApiError)
                    else "startup_error"
                )
                db.update_run(run_id, "failed", "opening simulator", code)
        except Exception as persist_error:
            print(
                f"BACKGROUND WORKER ERROR run_id={run_id} "
                f"type={type(error).__name__} detail={_safe_worker_detail(error, payload, order)}; "
                f"STATUS WRITE ERROR type={type(persist_error).__name__} "
                f"detail={_safe_worker_detail(persist_error, payload, order)}",
                flush=True,
            )
        else:
            print(
                f"BACKGROUND WORKER ERROR run_id={run_id} "
                f"type={type(error).__name__} detail={_safe_worker_detail(error, payload, order)}",
                flush=True,
            )
    finally:
        stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=2)
        with _ACTIVE_WORKER_LOCK:
            _ACTIVE_WORKER_RUNS.discard(run_id)
        try:
            db.unregister_run_worker(run_id, worker_id)
        except Exception as cleanup_error:
            print(
                f"WORKER LEASE CLEANUP ERROR run_id={run_id} "
                f"type={type(cleanup_error).__name__} "
                f"detail={_safe_worker_detail(cleanup_error, payload, order)}",
                flush=True,
            )
        try:
            from jwe_crypto import clear_sensitive_mapping
            clear_sensitive_mapping(payload)
        except Exception as clear_error:
            print(
                f"SENSITIVE MEMORY CLEANUP ERROR run_id={run_id} "
                f"type={type(clear_error).__name__}",
                flush=True,
            )


@app.post("/runs/{run_id}/sensitive", status_code=202)
def submit_sensitive(run_id: str, body: JweSubmission, background_tasks: BackgroundTasks):
    current = db.get_run(run_id)
    if current["status"] != "running":
        raise db.Conflict("Sensitive data can only be attached to an active run.")
    payload = None
    worker_id = None
    try:
        order_id = current["order_id"]
        order = db.get_order(order_id)["data"]
        payload, payment_required = decrypt_sensitive_submission(
            body.jwe, order_id=order_id, run_id=run_id
        )
        if str(order.get("EZ Enroll", "")).strip().lower() == "yes" and not payment_required:
            raise JWEError("EZ Enroll requires payment field verification.")
        worker_id = uuid4().hex
        db.register_run_worker(run_id, worker_id, os.getpid())
        background_tasks.add_task(
            _run_sensitive_workflow, run_id, order, payload, payment_required, worker_id
        )
    except BaseException as error:
        if worker_id is not None:
            try:
                db.unregister_run_worker(run_id, worker_id)
            except Exception as cleanup_error:
                print(
                    f"WORKER LEASE CLEANUP ERROR run_id={run_id} "
                    f"type={type(cleanup_error).__name__}",
                    flush=True,
                )
        try:
            latest = db.get_run(run_id)
            if latest.get("status") == "running" and latest.get("submission_state") in (
                "not_attempted", "not_submitted"
            ):
                db.update_run(
                    run_id, "failed", "preparing automation steps", "startup_error"
                )
        except Exception as persist_error:
            print(
                f"SENSITIVE SUBMISSION ERROR run_id={run_id} "
                f"type={type(error).__name__} "
                f"detail={_safe_worker_detail(error, payload)}; "
                f"STATUS WRITE ERROR type={type(persist_error).__name__} "
                f"detail={_safe_worker_detail(persist_error, payload)}",
                flush=True,
            )
        finally:
            if payload is not None:
                from jwe_crypto import clear_sensitive_mapping
                clear_sensitive_mapping(payload)
        raise
    return {"accepted": True}


@app.get("/runs")
def runs(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    records = db.list_runs(limit, offset)
    for record in records:
        record["worker_lease"] = db.get_run_worker(record["id"])
    return records


@app.get("/runs/{run_id}")
def run(run_id: str):
    record = db.get_run(run_id)
    record["worker_lease"] = db.get_run_worker(record["id"])
    return record


@app.post("/runs/{run_id}/recovery")
def recover_run(run_id: str, body: RunRecovery):
    return db.recover_stale_run(
        run_id,
        outcome=body.outcome,
        worker_stopped=body.worker_stopped,
        local_worker_active=is_local_worker_active(run_id),
    )


@app.patch("/runs/{run_id}")
def progress(run_id: str, body: Progress):
    if body.stage not in SAFE_STAGES:
        raise HTTPException(422, "Use a predefined workflow stage.")
    return db.update_run(run_id, body.status, body.stage, body.error_code)
