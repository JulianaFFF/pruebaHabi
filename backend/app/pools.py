"""Vacas: fondos compartidos con meta (el regalo de la mamá, el arriendo, la cena).

Una vaca es una cuenta más del libro contable (kind='pool'), así que hereda todas las
garantías del núcleo. Ciclo de vida:

    open --(se llega a la meta, el dueño la libera)--> released  (todo va al beneficiario)
    open --(el dueño la cancela)--------------------> cancelled (a cada uno se le devuelve lo suyo)

Orden de bloqueo: SIEMPRE primero la fila de la vaca y luego las cuentas (ordenadas por id).
Bloquear la vaca serializa aportes, liberación y cancelación: no puede entrar un aporte
"a mitad" de una liberación.
"""

from uuid import UUID

from psycopg import Connection

from app.errors import Forbidden, InvalidOperation, NotFound, PoolNotOpen
from app.ledger import apply_movements, lock_accounts, new_transaction, run_idempotent

_POOL_SELECT = """
    SELECT p.id, p.account_id, p.name, p.goal_amount, p.status, p.created_at, p.closed_at,
           p.owner_id, o.name AS owner_name, p.beneficiary_id, b.name AS beneficiary_name,
           a.balance
      FROM pools p
      JOIN accounts a ON a.id = p.account_id
      JOIN accounts o ON o.id = p.owner_id
      JOIN accounts b ON b.id = p.beneficiary_id
"""


def create_pool(
    conn: Connection, name: str, goal_amount: int, owner_id: UUID, beneficiary_id: UUID
) -> dict:
    with conn.transaction():
        for account_id in {owner_id, beneficiary_id}:
            account = conn.execute(
                "SELECT kind FROM accounts WHERE id = %s", (account_id,)
            ).fetchone()
            if account is None:
                raise NotFound(f"La cuenta {account_id} no existe")
            if account["kind"] != "user":
                raise InvalidOperation("Dueño y beneficiario deben ser cuentas de usuario")
        pool_account = conn.execute(
            "INSERT INTO accounts (name, kind) VALUES (%s, 'pool') RETURNING id",
            (f"Vaca: {name.strip()}",),
        ).fetchone()
        pool_id = conn.execute(
            "INSERT INTO pools (account_id, name, goal_amount, owner_id, beneficiary_id) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (pool_account["id"], name.strip(), goal_amount, owner_id, beneficiary_id),
        ).fetchone()["id"]
    return get_pool(conn, pool_id)


def list_pools(conn: Connection) -> list[dict]:
    pools = conn.execute(_POOL_SELECT + " ORDER BY p.created_at DESC").fetchall()
    return [_with_contributions(conn, pool) for pool in pools]


def get_pool(conn: Connection, pool_id: UUID) -> dict:
    pool = conn.execute(_POOL_SELECT + " WHERE p.id = %s", (pool_id,)).fetchone()
    if pool is None:
        raise NotFound(f"La vaca {pool_id} no existe")
    return _with_contributions(conn, pool)


def _contributions(conn: Connection, pool_account_id: UUID) -> list[dict]:
    """Cuánto aportó cada persona, derivado del libro contable (no de un contador aparte)."""
    return conn.execute(
        """
        SELECT src.account_id, a.name AS account_name, -SUM(src.amount) AS amount
          FROM transactions t
          JOIN entries dst ON dst.transaction_id = t.id AND dst.account_id = %(pool)s
          JOIN entries src ON src.transaction_id = t.id AND src.account_id <> %(pool)s
          JOIN accounts a ON a.id = src.account_id
         WHERE t.kind = 'pool_contribution'
         GROUP BY src.account_id, a.name
         ORDER BY amount DESC
        """,
        {"pool": pool_account_id},
    ).fetchall()


def _with_contributions(conn: Connection, pool: dict) -> dict:
    pool["remaining"] = max(pool["goal_amount"] - pool["balance"], 0) if pool["status"] == "open" else 0
    pool["contributions"] = _contributions(conn, pool["account_id"])
    return pool


def _lock_open_pool(conn: Connection, pool_id: UUID) -> dict:
    pool = conn.execute("SELECT * FROM pools WHERE id = %s FOR UPDATE", (pool_id,)).fetchone()
    if pool is None:
        raise NotFound(f"La vaca {pool_id} no existe")
    if pool["status"] != "open":
        raise PoolNotOpen(f"La vaca ya está cerrada ({pool['status']})")
    return pool


def contribute(
    conn: Connection, pool_id: UUID, account_id: UUID, amount: int, idempotency_key: str
) -> dict:
    name = get_pool(conn, pool_id)["name"]

    def apply(transaction_id: UUID) -> None:
        pool = _lock_open_pool(conn, pool_id)
        locked = lock_accounts(conn, {account_id, pool["account_id"]})
        if locked[account_id]["kind"] != "user":
            raise InvalidOperation("Solo los usuarios pueden aportar a una vaca")
        remaining = pool["goal_amount"] - locked[pool["account_id"]]["balance"]
        if amount > remaining:
            raise InvalidOperation(f"El aporte supera lo que falta para la meta ({remaining})")
        apply_movements(
            conn, transaction_id, locked, {account_id: -amount, pool["account_id"]: amount}
        )

    return run_idempotent(
        conn,
        kind="pool_contribution",
        idempotency_key=idempotency_key,
        request={"pool_id": pool_id, "account_id": account_id, "amount": amount},
        description=f"Aporte a la vaca «{name}»",
        apply=apply,
    )


# Liberar y cancelar no usan Idempotency-Key: son idempotentes por la máquina de estados.
# Con la vaca bloqueada, solo la primera llamada la encuentra 'open'; las demás reciben 409.


def release(conn: Connection, pool_id: UUID, requested_by: UUID) -> dict:
    with conn.transaction():
        pool = _lock_open_pool(conn, pool_id)
        if requested_by != pool["owner_id"]:
            raise Forbidden("Solo quien creó la vaca puede liberarla")
        locked = lock_accounts(conn, {pool["account_id"], pool["beneficiary_id"]})
        balance = locked[pool["account_id"]]["balance"]
        if balance < pool["goal_amount"]:
            raise InvalidOperation(
                f"La vaca aún no llega a la meta (faltan {pool['goal_amount'] - balance})"
            )
        transaction_id = new_transaction(
            conn, "pool_release", f"Vaca «{pool['name']}» liberada"
        )
        apply_movements(
            conn,
            transaction_id,
            locked,
            {pool["account_id"]: -balance, pool["beneficiary_id"]: balance},
        )
        _close(conn, pool_id, "released")
    return get_pool(conn, pool_id)


def cancel(conn: Connection, pool_id: UUID, requested_by: UUID) -> dict:
    with conn.transaction():
        pool = _lock_open_pool(conn, pool_id)
        if requested_by != pool["owner_id"]:
            raise Forbidden("Solo quien creó la vaca puede cancelarla")
        refunds = {c["account_id"]: c["amount"] for c in _contributions(conn, pool["account_id"])}
        locked = lock_accounts(conn, {pool["account_id"], *refunds})
        balance = locked[pool["account_id"]]["balance"]
        if balance != sum(refunds.values()):
            # Nunca debería pasar: la única entrada de plata a una vaca son los aportes.
            raise RuntimeError(f"La vaca {pool_id} tiene {balance} pero los aportes suman {sum(refunds.values())}")
        if balance > 0:
            transaction_id = new_transaction(
                conn, "pool_refund", f"Devolución: vaca «{pool['name']}» cancelada"
            )
            apply_movements(
                conn, transaction_id, locked, {pool["account_id"]: -balance, **refunds}
            )
        _close(conn, pool_id, "cancelled")
    return get_pool(conn, pool_id)


def _close(conn: Connection, pool_id: UUID, status: str) -> None:
    conn.execute(
        "UPDATE pools SET status = %s, closed_at = now() WHERE id = %s", (status, pool_id)
    )
