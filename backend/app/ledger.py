"""Núcleo contable. TODO movimiento de plata pasa por _apply_movements.

Reglas que este módulo garantiza:
  1. Cada transacción suma 0 (doble partida) -> la plata no aparece ni desaparece.
  2. Las cuentas se bloquean (SELECT ... FOR UPDATE) siempre en el mismo orden (por id)
     -> no hay race conditions ni deadlocks entre transferencias cruzadas A->B / B->A.
  3. Ninguna cuenta de usuario o vaca queda negativa (validación aquí + CHECK en la BD).
  4. Las operaciones con Idempotency-Key se ejecutan una sola vez aunque el cliente reintente.
"""

import hashlib
import json
from typing import Callable
from uuid import UUID

from psycopg import Connection

from app.errors import IdempotencyConflict, InsufficientFunds, InvalidOperation, NotFound

SYSTEM_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000001")


def seed_system_account(conn: Connection) -> None:
    conn.execute(
        "INSERT INTO accounts (id, name, kind) VALUES (%s, 'Fondeo externo (simulado)', 'system') "
        "ON CONFLICT (id) DO NOTHING",
        (SYSTEM_ACCOUNT_ID,),
    )


# ---------------------------------------------------------------- cuentas


def create_account(conn: Connection, name: str) -> dict:
    return conn.execute(
        "INSERT INTO accounts (name, kind) VALUES (%s, 'user') "
        "RETURNING id, name, kind, balance, created_at",
        (name.strip(),),
    ).fetchone()


def list_accounts(conn: Connection) -> list[dict]:
    return conn.execute(
        "SELECT id, name, kind, balance, created_at FROM accounts WHERE kind = 'user' ORDER BY created_at"
    ).fetchall()


def get_account(conn: Connection, account_id: UUID) -> dict:
    account = conn.execute(
        "SELECT id, name, kind, balance, created_at FROM accounts WHERE id = %s", (account_id,)
    ).fetchone()
    if account is None:
        raise NotFound(f"La cuenta {account_id} no existe")
    return account


# ---------------------------------------------------------------- primitivas contables


def _request_hash(request: dict) -> str:
    canonical = json.dumps(request, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def new_transaction(
    conn: Connection,
    kind: str,
    description: str | None,
    idempotency_key: str | None = None,
    request_hash: str | None = None,
) -> UUID | None:
    """Inserta la cabecera de la transacción. Devuelve None si la idempotency_key ya existía.

    Si otra petición con la misma clave está en curso, Postgres hace esperar este INSERT
    hasta que la otra termine: si hizo COMMIT -> DO NOTHING; si hizo ROLLBACK -> insertamos.
    """
    row = conn.execute(
        "INSERT INTO transactions (kind, description, idempotency_key, request_hash) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
        (kind, description, idempotency_key, request_hash),
    ).fetchone()
    return row["id"] if row else None


def lock_accounts(conn: Connection, account_ids: set[UUID]) -> dict[UUID, dict]:
    """Bloquea las cuentas en orden de id. El orden fijo es lo que evita deadlocks."""
    locked = {}
    for account_id in sorted(account_ids):
        account = conn.execute(
            "SELECT id, name, kind, balance FROM accounts WHERE id = %s FOR UPDATE", (account_id,)
        ).fetchone()
        if account is None:
            raise NotFound(f"La cuenta {account_id} no existe")
        locked[account_id] = account
    return locked


def apply_movements(
    conn: Connection, transaction_id: UUID, locked: dict[UUID, dict], movements: dict[UUID, int]
) -> None:
    """Aplica los movimientos (cuenta -> monto con signo) sobre cuentas YA bloqueadas."""
    if sum(movements.values()) != 0:
        raise RuntimeError(f"Movimientos desbalanceados: {movements}")
    for account_id, amount in movements.items():
        account = locked[account_id]
        if account["kind"] != "system" and account["balance"] + amount < 0:
            raise InsufficientFunds(f"Saldo insuficiente en la cuenta de {account['name']}")
    for account_id, amount in movements.items():
        balance_after = conn.execute(
            "UPDATE accounts SET balance = balance + %s WHERE id = %s RETURNING balance",
            (amount, account_id),
        ).fetchone()["balance"]
        locked[account_id]["balance"] = balance_after
        conn.execute(
            "INSERT INTO entries (transaction_id, account_id, amount, balance_after) "
            "VALUES (%s, %s, %s, %s)",
            (transaction_id, account_id, amount, balance_after),
        )


def run_idempotent(
    conn: Connection,
    *,
    kind: str,
    idempotency_key: str,
    request: dict,
    description: str | None,
    apply: Callable[[UUID], None],
) -> dict:
    """Ejecuta `apply` una sola vez por idempotency_key, dentro de una transacción de BD.

    Si algo falla (saldo insuficiente, error inesperado...) se hace ROLLBACK de todo,
    incluida la cabecera, así que el cliente puede reintentar con la misma clave.
    """
    request_hash = _request_hash({"kind": kind, **request})
    with conn.transaction():
        transaction_id = new_transaction(conn, kind, description, idempotency_key, request_hash)
        if transaction_id is None:
            existing = conn.execute(
                "SELECT id, request_hash FROM transactions WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
            if existing["request_hash"] != request_hash:
                raise IdempotencyConflict(
                    "Esta Idempotency-Key ya se usó para una operación distinta"
                )
            return get_transaction(conn, existing["id"], replayed=True)
        apply(transaction_id)
        return get_transaction(conn, transaction_id)


def get_transaction(conn: Connection, transaction_id: UUID, replayed: bool = False) -> dict:
    tx = conn.execute(
        "SELECT id, kind, description, created_at FROM transactions WHERE id = %s",
        (transaction_id,),
    ).fetchone()
    tx["entries"] = conn.execute(
        "SELECT e.account_id, a.name AS account_name, e.amount, e.balance_after "
        "FROM entries e JOIN accounts a ON a.id = e.account_id "
        "WHERE e.transaction_id = %s ORDER BY e.amount",
        (transaction_id,),
    ).fetchall()
    tx["replayed"] = replayed
    return tx


def _require_kind(account: dict, kind: str, message: str) -> None:
    if account["kind"] != kind:
        raise InvalidOperation(message)


# ---------------------------------------------------------------- operaciones del núcleo


def deposit(
    conn: Connection, account_id: UUID, amount: int, description: str | None, idempotency_key: str
) -> dict:
    def apply(transaction_id: UUID) -> None:
        locked = lock_accounts(conn, {account_id, SYSTEM_ACCOUNT_ID})
        _require_kind(locked[account_id], "user", "Solo se puede cargar saldo a cuentas de usuario")
        apply_movements(
            conn, transaction_id, locked, {SYSTEM_ACCOUNT_ID: -amount, account_id: amount}
        )

    return run_idempotent(
        conn,
        kind="deposit",
        idempotency_key=idempotency_key,
        request={"account_id": account_id, "amount": amount, "description": description},
        description=description or "Carga de saldo",
        apply=apply,
    )


def transfer(
    conn: Connection,
    from_id: UUID,
    to_id: UUID,
    amount: int,
    description: str | None,
    idempotency_key: str,
) -> dict:
    if from_id == to_id:
        raise InvalidOperation("No puedes transferirte a ti mismo")

    def apply(transaction_id: UUID) -> None:
        locked = lock_accounts(conn, {from_id, to_id})
        for account_id in (from_id, to_id):
            _require_kind(locked[account_id], "user", "Solo se puede transferir entre usuarios")
        apply_movements(conn, transaction_id, locked, {from_id: -amount, to_id: amount})

    return run_idempotent(
        conn,
        kind="transfer",
        idempotency_key=idempotency_key,
        request={"from": from_id, "to": to_id, "amount": amount, "description": description},
        description=description,
        apply=apply,
    )


def history(conn: Connection, account_id: UUID, limit: int, before: int | None) -> dict:
    get_account(conn, account_id)
    rows = conn.execute(
        """
        SELECT e.id AS entry_id, e.amount, e.balance_after, e.created_at,
               t.id AS transaction_id, t.kind, t.description,
               (SELECT string_agg(a.name, ', ' ORDER BY a.name)
                  FROM entries o JOIN accounts a ON a.id = o.account_id
                 WHERE o.transaction_id = t.id AND o.account_id <> e.account_id) AS counterparty
          FROM entries e
          JOIN transactions t ON t.id = e.transaction_id
         WHERE e.account_id = %(account_id)s
           AND (%(before)s::bigint IS NULL OR e.id < %(before)s)
         ORDER BY e.id DESC
         LIMIT %(limit)s
        """,
        {"account_id": account_id, "before": before, "limit": limit},
    ).fetchall()
    next_cursor = rows[-1]["entry_id"] if len(rows) == limit else None
    return {"items": rows, "next_cursor": next_cursor}


# ---------------------------------------------------------------- auditoría


def reconcile(conn: Connection) -> dict:
    """Verifica las invariantes del sistema completo. Si ok=False, se perdió (o creó) plata."""
    total = conn.execute("SELECT COALESCE(SUM(balance), 0) AS total FROM accounts").fetchone()["total"]
    mismatched = conn.execute(
        """
        SELECT a.id, a.name, a.balance, COALESCE(SUM(e.amount), 0) AS ledger_balance
          FROM accounts a LEFT JOIN entries e ON e.account_id = a.id
         GROUP BY a.id
        HAVING a.balance <> COALESCE(SUM(e.amount), 0)
        """
    ).fetchall()
    unbalanced = conn.execute(
        "SELECT transaction_id, SUM(amount) AS total FROM entries GROUP BY transaction_id HAVING SUM(amount) <> 0"
    ).fetchall()
    negative = conn.execute(
        "SELECT id, name, balance FROM accounts WHERE kind <> 'system' AND balance < 0"
    ).fetchall()
    bad_pools = conn.execute(
        """
        SELECT p.id, p.name, p.status, a.balance
          FROM pools p JOIN accounts a ON a.id = p.account_id
         WHERE (p.status = 'open' AND a.balance > p.goal_amount)
            OR (p.status <> 'open' AND a.balance <> 0)
        """
    ).fetchall()
    money_in_system = conn.execute(
        "SELECT -balance AS total FROM accounts WHERE id = %s", (SYSTEM_ACCOUNT_ID,)
    ).fetchone()["total"]
    return {
        "ok": total == 0 and not (mismatched or unbalanced or negative or bad_pools),
        "sum_of_all_balances": total,
        "money_in_system": money_in_system,
        "accounts_not_matching_ledger": mismatched,
        "unbalanced_transactions": unbalanced,
        "negative_accounts": negative,
        "inconsistent_pools": bad_pools,
    }
