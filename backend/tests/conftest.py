import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db import create_pool, init_schema
from app.ledger import reconcile, seed_system_account
from app.main import create_app

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://wallet:wallet@localhost:5432/wallet_test"
)


@pytest.fixture(scope="session")
def pool():
    pool = create_pool(TEST_DATABASE_URL, max_size=30)
    init_schema(pool)
    yield pool
    pool.close()


@pytest.fixture(autouse=True)
def clean_db(pool):
    # TRUNCATE no dispara los triggers append-only (son por fila), por eso sirve para limpiar.
    with pool.connection() as conn:
        conn.execute("TRUNCATE entries, transactions, pools, accounts CASCADE")
        seed_system_account(conn)
    yield
    # Después de CADA test, el sistema completo debe cuadrar.
    with pool.connection() as conn:
        result = reconcile(conn)
    assert result["ok"], result


@pytest.fixture
def conn(pool):
    with pool.connection() as conn:
        yield conn


@pytest.fixture(scope="session")
def client(pool):
    with TestClient(create_app(TEST_DATABASE_URL)) as client:
        yield client


def key() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}
