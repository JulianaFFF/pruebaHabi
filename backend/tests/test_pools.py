"""Vacas: el escenario de los hermanos que le juntan plata a la mamá."""

from tests.conftest import key
from tests.test_api import balance, new_account


def setup_pool(client, goal=300_000):
    mama = new_account(client, "Mamá")
    hermanos = [new_account(client, n, 500_000) for n in ("Ana", "Beto", "Caro")]
    pool = client.post(
        "/api/pools",
        json={"name": "Mercado de mamá", "goal_amount": goal, "owner_id": hermanos[0], "beneficiary_id": mama},
    ).json()
    return pool["id"], mama, hermanos


def contribute(client, pool_id, account_id, amount, headers=None):
    return client.post(
        f"/api/pools/{pool_id}/contributions",
        json={"account_id": account_id, "amount": amount},
        headers=headers or key(),
    )


def test_full_pool_lifecycle(client):
    pool_id, mama, (ana, beto, caro) = setup_pool(client)
    for who in (ana, beto, caro):
        assert contribute(client, pool_id, who, 100_000).status_code == 201

    pool = client.get(f"/api/pools/{pool_id}").json()
    assert pool["balance"] == 300_000 and pool["remaining"] == 0
    assert {c["account_name"]: c["amount"] for c in pool["contributions"]} == {
        "Ana": 100_000, "Beto": 100_000, "Caro": 100_000,
    }

    r = client.post(f"/api/pools/{pool_id}/release", json={"requested_by": ana})
    assert r.status_code == 200
    assert r.json()["status"] == "released" and r.json()["balance"] == 0
    assert balance(client, mama) == 300_000

    # La mamá ve UN movimiento con contexto, no tres transferencias sueltas.
    items = client.get(f"/api/accounts/{mama}/history").json()["items"]
    assert len(items) == 1
    assert items[0]["description"] == "Vaca «Mercado de mamá» liberada"


def test_contribution_cannot_exceed_goal(client):
    pool_id, _, (ana, beto, _) = setup_pool(client)
    contribute(client, pool_id, ana, 250_000)
    r = contribute(client, pool_id, beto, 60_000)
    assert r.status_code == 422
    assert balance(client, beto) == 500_000


def test_contribution_without_funds(client):
    pool_id, mama, _ = setup_pool(client)
    r = contribute(client, pool_id, mama, 1_000)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "insufficient_funds"


def test_only_owner_can_release_and_only_when_goal_reached(client):
    pool_id, _, (ana, beto, _) = setup_pool(client)
    contribute(client, pool_id, beto, 100_000)
    assert client.post(f"/api/pools/{pool_id}/release", json={"requested_by": beto}).status_code == 403
    assert client.post(f"/api/pools/{pool_id}/release", json={"requested_by": ana}).status_code == 422


def test_cancel_refunds_everyone_exactly(client):
    pool_id, mama, (ana, beto, caro) = setup_pool(client)
    contribute(client, pool_id, ana, 50_000)
    contribute(client, pool_id, beto, 70_000)
    contribute(client, pool_id, beto, 30_000)

    r = client.post(f"/api/pools/{pool_id}/cancel", json={"requested_by": ana})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert [balance(client, a) for a in (ana, beto, caro, mama)] == [500_000, 500_000, 500_000, 0]


def test_closed_pool_rejects_everything(client):
    pool_id, _, (ana, beto, _) = setup_pool(client, goal=100_000)
    contribute(client, pool_id, ana, 100_000)
    client.post(f"/api/pools/{pool_id}/release", json={"requested_by": ana})

    assert contribute(client, pool_id, beto, 1).status_code == 409
    assert client.post(f"/api/pools/{pool_id}/release", json={"requested_by": ana}).status_code == 409
    assert client.post(f"/api/pools/{pool_id}/cancel", json={"requested_by": ana}).status_code == 409


def test_cannot_transfer_directly_into_a_pool(client):
    """Si se pudiera, la vaca tendría plata que no es de ningún aporte y la devolución no cuadraría."""
    pool_id, _, (ana, _, _) = setup_pool(client)
    pool_account = client.get(f"/api/pools/{pool_id}").json()["account_id"]
    r = client.post(
        "/api/transfers",
        json={"from_account_id": ana, "to_account_id": pool_account, "amount": 1},
        headers=key(),
    )
    assert r.status_code == 422
