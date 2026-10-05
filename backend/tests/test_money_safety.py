"""Las pruebas que importan: concurrencia, idempotencia bajo carrera y defensas de la BD.

Corren contra Postgres real con hilos y conexiones independientes, porque los bugs de
plata casi siempre aparecen cuando dos cosas pasan al mismo tiempo.
"""

import random
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from app import ledger, pools
from app.errors import AppError, InsufficientFunds


def make_account(conn, name, amount=0):
    account_id = ledger.create_account(conn, name)["id"]
    if amount:
        ledger.deposit(conn, account_id, amount, None, str(uuid.uuid4()))
    return account_id


def run_parallel(pool, fn, n, workers=20):
    def worker(i):
        with pool.connection() as conn:
            try:
                fn(conn, i)
                return "ok"
            except InsufficientFunds:
                return "insufficient"

    with ThreadPoolExecutor(workers) as executor:
        return list(executor.map(worker, range(n)))


def test_concurrent_transfers_never_overdraw(pool, conn):
    """150 transferencias de $1.000 en paralelo desde una cuenta con $100.000."""
    ana = make_account(conn, "Ana", 100_000)
    beto = make_account(conn, "Beto")

    results = run_parallel(
        pool, lambda c, i: ledger.transfer(c, ana, beto, 1_000, None, f"t-{i}"), 150
    )

    assert results.count("ok") == 100
    assert results.count("insufficient") == 50
    assert ledger.get_account(conn, ana)["balance"] == 0
    assert ledger.get_account(conn, beto)["balance"] == 100_000


def test_crossed_transfers_do_not_deadlock(pool, conn):
    """A->B y B->A al mismo tiempo: sin orden de bloqueo esto produce deadlocks."""
    ana = make_account(conn, "Ana", 50_000)
    beto = make_account(conn, "Beto", 50_000)

    def op(c, i):
        src, dst = (ana, beto) if i % 2 else (beto, ana)
        ledger.transfer(c, src, dst, random.randint(1, 3_000), None, f"x-{i}")

    results = run_parallel(pool, op, 300)

    assert set(results) <= {"ok", "insufficient"}
    total = ledger.get_account(conn, ana)["balance"] + ledger.get_account(conn, beto)["balance"]
    assert total == 100_000


def test_same_idempotency_key_in_parallel_moves_money_once(pool, conn):
    """20 reintentos simultáneos de la misma operación (doble clic, red que reintenta)."""
    ana = make_account(conn, "Ana", 10_000)
    beto = make_account(conn, "Beto")
    tx_ids = []

    def op(c, _):
        tx_ids.append(ledger.transfer(c, ana, beto, 7_000, None, "misma-clave")["id"])

    assert run_parallel(pool, op, 20) == ["ok"] * 20
    assert len(set(tx_ids)) == 1
    assert ledger.get_account(conn, beto)["balance"] == 7_000


def test_concurrent_contributions_never_overfill_pool(pool, conn):
    owner = make_account(conn, "Ana", 1_000_000)
    pool_id = pools.create_pool(conn, "Arriendo", 100_000, owner, owner)["id"]
    friends = [make_account(conn, f"Amigo {i}", 50_000) for i in range(10)]

    def op(c, i):
        try:
            pools.contribute(c, pool_id, friends[i % 10], 7_000, f"p-{i}")
        except AppError as exc:
            if exc.code != "invalid_operation":
                raise

    run_parallel(pool, op, 40)
    detail = pools.get_pool(conn, pool_id)
    assert detail["balance"] <= 100_000
    assert detail["balance"] == sum(c["amount"] for c in detail["contributions"])


def test_release_racing_with_contributions(pool, conn):
    """Aportes y liberación compiten: la plata termina toda en el beneficiario o en los aportantes."""
    owner = make_account(conn, "Ana", 100_000)
    mama = make_account(conn, "Mamá")
    pool_id = pools.create_pool(conn, "Regalo", 100_000, owner, mama)["id"]
    pools.contribute(conn, pool_id, owner, 99_000, "base")
    friend = make_account(conn, "Beto", 100_000)

    def op(c, i):
        try:
            if i % 5 == 0:
                pools.release(c, pool_id, owner)
            else:
                pools.contribute(c, pool_id, friend, 1_000, f"r-{i}")
        except AppError as exc:
            if exc.code not in ("invalid_operation", "pool_not_open"):
                raise

    run_parallel(pool, op, 50)
    if pools.get_pool(conn, pool_id)["status"] == "open":
        # Todas las liberaciones llegaron antes que el último aporte: liberar ahora.
        pools.release(conn, pool_id, owner)
    assert pools.get_pool(conn, pool_id)["status"] == "released"
    assert ledger.get_account(conn, mama)["balance"] == 100_000
    assert ledger.get_account(conn, friend)["balance"] == 99_000


def test_random_workload_preserves_all_money(pool, conn):
    """Carga aleatoria mezclando todo. La auditoría (fixture clean_db) valida al final."""
    people = [make_account(conn, f"P{i}", 200_000) for i in range(6)]
    vacas = [pools.create_pool(conn, f"V{i}", 150_000, people[i], people[-1 - i])["id"] for i in range(3)]
    rng = random.Random(42)
    plan = [(rng.random(), rng.choice(people), rng.choice(people), rng.choice(vacas), rng.randint(1, 60_000))
            for _ in range(400)]

    def op(c, i):
        r, a, b, vaca, amount = plan[i]
        try:
            if r < 0.6 and a != b:
                ledger.transfer(c, a, b, amount, None, f"w-{i}")
            elif r < 0.9:
                pools.contribute(c, vaca, a, amount, f"w-{i}")
            else:
                pools.cancel(c, vaca, pools.get_pool(c, vaca)["owner_id"])
        except AppError as exc:
            if exc.code not in ("invalid_operation", "pool_not_open", "insufficient_funds"):
                raise

    run_parallel(pool, op, len(plan))
    report = ledger.reconcile(conn)
    assert report["ok"], report
    assert report["money_in_system"] == 6 * 200_000


# ---------------------------------------------------------------- defensas de la propia BD


def test_db_rejects_unbalanced_transaction(conn):
    ana = make_account(conn, "Ana")
    with pytest.raises(psycopg.errors.RaiseException, match="unbalanced"):
        with conn.transaction():
            tx = ledger.new_transaction(conn, "deposit", "plata de la nada")
            conn.execute(
                "UPDATE accounts SET balance = balance + 1000 WHERE id = %s", (ana,)
            )
            conn.execute(
                "INSERT INTO entries (transaction_id, account_id, amount, balance_after) VALUES (%s, %s, 1000, 1000)",
                (tx, ana),
            )
    assert ledger.get_account(conn, ana)["balance"] == 0


def test_db_rejects_negative_balance_even_if_code_has_a_bug(conn):
    ana = make_account(conn, "Ana", 100)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE accounts SET balance = balance - 101 WHERE id = %s", (ana,))


def test_history_is_append_only(conn):
    make_account(conn, "Ana", 100)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        conn.execute("UPDATE entries SET amount = 1000000")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        conn.execute("DELETE FROM transactions")
