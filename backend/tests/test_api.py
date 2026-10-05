"""Comportamiento del núcleo a través de la API HTTP."""

import uuid

import pytest

from tests.conftest import key


def new_account(client, name="Ana", balance=0):
    account = client.post("/api/accounts", json={"name": name}).json()
    if balance:
        r = client.post(f"/api/accounts/{account['id']}/deposits", json={"amount": balance}, headers=key())
        assert r.status_code == 201
    return account["id"]


def balance(client, account_id):
    return client.get(f"/api/accounts/{account_id}").json()["balance"]


def test_create_account_starts_at_zero(client):
    r = client.post("/api/accounts", json={"name": "  Ana  "})
    assert r.status_code == 201
    assert r.json()["name"] == "Ana"
    assert r.json()["balance"] == 0


def test_deposit_transfer_and_history(client):
    ana = new_account(client, "Ana", 100_000)
    beto = new_account(client, "Beto")

    r = client.post(
        "/api/transfers",
        json={"from_account_id": ana, "to_account_id": beto, "amount": 30_000, "description": "Cena cumple"},
        headers=key(),
    )
    assert r.status_code == 201
    assert balance(client, ana) == 70_000
    assert balance(client, beto) == 30_000

    items = client.get(f"/api/accounts/{ana}/history").json()["items"]
    assert [(i["kind"], i["amount"], i["balance_after"]) for i in items] == [
        ("transfer", -30_000, 70_000),
        ("deposit", 100_000, 100_000),
    ]
    assert items[0]["description"] == "Cena cumple"
    assert items[0]["counterparty"] == "Beto"


def test_history_pagination(client):
    ana = new_account(client, "Ana")
    for _ in range(5):
        client.post(f"/api/accounts/{ana}/deposits", json={"amount": 1}, headers=key())
    first = client.get(f"/api/accounts/{ana}/history?limit=3").json()
    second = client.get(f"/api/accounts/{ana}/history?limit=3&before={first['next_cursor']}").json()
    assert len(first["items"]) == 3 and len(second["items"]) == 2
    assert second["next_cursor"] is None


def test_insufficient_funds_changes_nothing(client):
    ana = new_account(client, "Ana", 10_000)
    beto = new_account(client, "Beto")
    r = client.post(
        "/api/transfers",
        json={"from_account_id": ana, "to_account_id": beto, "amount": 10_001},
        headers=key(),
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "insufficient_funds"
    assert balance(client, ana) == 10_000
    assert balance(client, beto) == 0


@pytest.mark.parametrize("amount", [0, -100, 10.5, 100.0, "100", True, None, 10**12])
def test_invalid_amounts_are_rejected(client, amount):
    ana = new_account(client, "Ana")
    r = client.post(f"/api/accounts/{ana}/deposits", json={"amount": amount}, headers=key())
    assert r.status_code == 422
    assert balance(client, ana) == 0


def test_cannot_transfer_to_self(client):
    ana = new_account(client, "Ana", 1_000)
    r = client.post(
        "/api/transfers", json={"from_account_id": ana, "to_account_id": ana, "amount": 1}, headers=key()
    )
    assert r.status_code == 422


def test_unknown_account(client):
    ana = new_account(client, "Ana", 1_000)
    r = client.post(
        "/api/transfers",
        json={"from_account_id": ana, "to_account_id": str(uuid.uuid4()), "amount": 1},
        headers=key(),
    )
    assert r.status_code == 404
    assert balance(client, ana) == 1_000


def test_money_operations_require_idempotency_key(client):
    ana = new_account(client, "Ana")
    r = client.post(f"/api/accounts/{ana}/deposits", json={"amount": 100})
    assert r.status_code == 422


def test_retry_with_same_key_does_not_duplicate(client):
    ana = new_account(client, "Ana", 50_000)
    beto = new_account(client, "Beto")
    headers = key()
    body = {"from_account_id": ana, "to_account_id": beto, "amount": 20_000}

    first = client.post("/api/transfers", json=body, headers=headers).json()
    retry = client.post("/api/transfers", json=body, headers=headers).json()

    assert first["id"] == retry["id"]
    assert retry["replayed"] is True
    assert balance(client, ana) == 30_000
    assert balance(client, beto) == 20_000


def test_same_key_different_request_is_rejected(client):
    ana = new_account(client, "Ana", 50_000)
    beto = new_account(client, "Beto")
    headers = key()
    client.post("/api/transfers", json={"from_account_id": ana, "to_account_id": beto, "amount": 1}, headers=headers)
    r = client.post("/api/transfers", json={"from_account_id": ana, "to_account_id": beto, "amount": 2}, headers=headers)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_key_reused"
    assert balance(client, beto) == 1


def test_failed_operation_does_not_burn_the_key(client):
    """Si falla por saldo, el cliente puede cargar plata y reintentar con la misma clave."""
    ana = new_account(client, "Ana")
    beto = new_account(client, "Beto")
    headers = key()
    body = {"from_account_id": ana, "to_account_id": beto, "amount": 5_000}
    assert client.post("/api/transfers", json=body, headers=headers).status_code == 409
    client.post(f"/api/accounts/{ana}/deposits", json={"amount": 5_000}, headers=key())
    assert client.post("/api/transfers", json=body, headers=headers).status_code == 201
    assert balance(client, beto) == 5_000
