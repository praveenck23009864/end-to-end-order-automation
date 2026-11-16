"""Authenticated local API serving protected customer records."""
from contextlib import asynccontextmanager
import secrets
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

import database as db
from new import validate_order
from sensitive_data import (
    SensitiveDataError, assert_protected_order, get_api_token,
    protect_order, SAFE_STAGES,
)

bearer = HTTPBearer(auto_error=False)


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
    try:
        db.check_stored_protection()
    except (ValueError, TypeError):
        raise RuntimeError(
            "Database protection check failed. Migrate the legacy database before starting."
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
        content={"detail": "Record conflict. Check the existing run before retrying."},
    )


@app.exception_handler(SensitiveDataError)
async def sensitive_failure(request, error):
    return JSONResponse(
        status_code=422,
        content={"detail": "Protected customer data could not be verified."},
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


@app.get("/health")
def health():
    with db.connection() as con:
        con.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/orders")
def orders(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    return db.list_orders(limit, offset)


@app.post("/orders", status_code=201)
def create_order(order: NewOrder):
    try:
        assert_protected_order(order.data, order.id)
        validate_order(order.data)
        # Authenticate encrypted payloads before accepting writes.
        protected = protect_order(order.data, order.id)
    except ValueError:
        raise HTTPException(422, "Invalid protected order fields.") from None
    result = db.import_orders({order.id: protected})
    if result["skipped"]:
        raise HTTPException(409, "Order ID already exists.")
    return {"id": order.id}


@app.get("/orders/{order_id}")
def order(order_id: str):
    return db.get_order(order_id)


@app.post("/orders/{order_id}/runs")
def start(order_id: str, body: StartRun):
    return db.start_run(order_id, str(body.run_id))


@app.get("/runs")
def runs(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    return db.list_runs(limit, offset)


@app.get("/runs/{run_id}")
def run(run_id: str):
    return db.get_run(run_id)


@app.patch("/runs/{run_id}")
def progress(run_id: str, body: Progress):
    if body.stage not in SAFE_STAGES:
        raise HTTPException(422, "Use a predefined workflow stage.")
    return db.update_run(run_id, body.status, body.stage, body.error_code)
