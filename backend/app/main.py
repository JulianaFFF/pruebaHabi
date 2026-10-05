from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg import Connection

from app import ledger, pools
from app.db import DATABASE_URL, create_pool, init_schema
from app.errors import AppError
from app.schemas import (
    AccountCreate,
    ContributionRequest,
    DepositRequest,
    PoolAction,
    PoolCreate,
    TransferRequest,
)

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=8, max_length=100,
                description="Identificador único de la operación (p. ej. un UUID). Reintentar con la misma clave no duplica el movimiento.")
]


def create_app(database_url: str = DATABASE_URL) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.pool = create_pool(database_url)
        init_schema(app.state.pool)
        yield
        app.state.pool.close()

    app = FastAPI(title="Billetera con vacas", lifespan=lifespan)

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.status_code, content={"error": {"code": exc.code, "message": exc.message}}
        )

    # Los endpoints son `def` (no async): FastAPI los corre en un threadpool y psycopg es síncrono.
    def get_conn(request: Request):
        with request.app.state.pool.connection() as conn:
            yield conn

    Conn = Annotated[Connection, Depends(get_conn)]

    # ------------------------------------------------------------ núcleo

    @app.post("/api/accounts", status_code=201)
    def create_account(body: AccountCreate, conn: Conn):
        return ledger.create_account(conn, body.name)

    @app.get("/api/accounts")
    def list_accounts(conn: Conn):
        return ledger.list_accounts(conn)

    @app.get("/api/accounts/{account_id}")
    def get_account(account_id: UUID, conn: Conn):
        return ledger.get_account(conn, account_id)

    @app.post("/api/accounts/{account_id}/deposits", status_code=201)
    def deposit(account_id: UUID, body: DepositRequest, key: IdempotencyKey, conn: Conn):
        return ledger.deposit(conn, account_id, body.amount, body.description, key)

    @app.post("/api/transfers", status_code=201)
    def transfer(body: TransferRequest, key: IdempotencyKey, conn: Conn):
        return ledger.transfer(
            conn, body.from_account_id, body.to_account_id, body.amount, body.description, key
        )

    @app.get("/api/accounts/{account_id}/history")
    def history(
        account_id: UUID,
        conn: Conn,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        before: Annotated[int | None, Query(description="Cursor: next_cursor de la página anterior")] = None,
    ):
        return ledger.history(conn, account_id, limit, before)

    # ------------------------------------------------------------ vacas

    @app.post("/api/pools", status_code=201)
    def create_pool_(body: PoolCreate, conn: Conn):
        return pools.create_pool(conn, body.name, body.goal_amount, body.owner_id, body.beneficiary_id)

    @app.get("/api/pools")
    def list_pools(conn: Conn):
        return pools.list_pools(conn)

    @app.get("/api/pools/{pool_id}")
    def get_pool(pool_id: UUID, conn: Conn):
        return pools.get_pool(conn, pool_id)

    @app.post("/api/pools/{pool_id}/contributions", status_code=201)
    def contribute(pool_id: UUID, body: ContributionRequest, key: IdempotencyKey, conn: Conn):
        return pools.contribute(conn, pool_id, body.account_id, body.amount, key)

    @app.post("/api/pools/{pool_id}/release")
    def release(pool_id: UUID, body: PoolAction, conn: Conn):
        return pools.release(conn, pool_id, body.requested_by)

    @app.post("/api/pools/{pool_id}/cancel")
    def cancel(pool_id: UUID, body: PoolAction, conn: Conn):
        return pools.cancel(conn, pool_id, body.requested_by)

    # ------------------------------------------------------------ auditoría

    @app.get("/api/reconciliation")
    def reconciliation(conn: Conn):
        return ledger.reconcile(conn)

    if FRONTEND_DIR.exists():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

    return app


app = create_app()
