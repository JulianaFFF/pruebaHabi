import os
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://wallet:wallet@localhost:5432/wallet")
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def create_pool(url: str = DATABASE_URL, max_size: int = 10) -> ConnectionPool:
    # autocommit=True: cada operación abre su propia transacción explícita con conn.transaction().
    return ConnectionPool(
        url,
        min_size=1,
        max_size=max_size,
        kwargs={"row_factory": dict_row, "autocommit": True},
        open=True,
    )


def init_schema(pool: ConnectionPool) -> None:
    from app.ledger import seed_system_account

    with pool.connection() as conn:
        conn.execute(SCHEMA_PATH.read_text())
        seed_system_account(conn)
