"""Verifica contra la app DESPLEGADA que cumple cada requisito del reto.

Uso:  python3 scripts/verify.py [http://localhost:8000]
Solo usa la librería estándar, no necesita instalar nada.
"""

import json
import sys
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/") + "/api"
results = []


def call(method, path, body=None, idem=None):
    headers = {"Content-Type": "application/json"}
    if idem is not None:
        headers["Idempotency-Key"] = idem
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def key():
    return str(uuid.uuid4())


def check(name, condition, detail=""):
    results.append(condition)
    print(f"  {'✅' if condition else '❌'} {name}" + (f"  ({detail})" if detail and not condition else ""))


def balance(account_id):
    return call("GET", f"/accounts/{account_id}")[1]["balance"]


def new_account(name):
    return call("POST", "/accounts", {"name": name})[1]["id"]


print("\n── Núcleo ──")
status, ana = call("POST", "/accounts", {"name": "Ana (verify)"})
check("01 Crear una cuenta", status == 201 and ana["balance"] == 0, ana)
ana = ana["id"]
beto = new_account("Beto (verify)")

status, _ = call("POST", f"/accounts/{ana}/deposits", {"amount": 100_000}, key())
check("02 Cargar saldo (simulado)", status == 201 and balance(ana) == 100_000)

status, _ = call("POST", "/transfers",
                 {"from_account_id": ana, "to_account_id": beto, "amount": 30_000, "description": "Cena cumple"}, key())
check("03 Transferir saldo", status == 201 and balance(ana) == 70_000 and balance(beto) == 30_000)

status, acc = call("GET", f"/accounts/{beto}")
check("04 Consultar saldo", status == 200 and acc["balance"] == 30_000)

status, hist = call("GET", f"/accounts/{ana}/history")
kinds = [(i["kind"], i["amount"]) for i in hist["items"]]
check("05 Ver historial (con contexto)", kinds == [("transfer", -30_000), ("deposit", 100_000)]
      and hist["items"][0]["description"] == "Cena cumple" and hist["items"][0]["counterparty"] == "Beto (verify)", kinds)

print("\n── Funcionalidad extra: vacas ──")
mama = new_account("Mamá (verify)")
caro = new_account("Caro (verify)")
call("POST", f"/accounts/{caro}/deposits", {"amount": 100_000}, key())
status, pool = call("POST", "/pools", {"name": "Mercado (verify)", "goal_amount": 100_000, "owner_id": ana, "beneficiary_id": mama})
check("Crear vaca", status == 201 and pool["status"] == "open")
pid = pool["id"]
call("POST", f"/pools/{pid}/contributions", {"account_id": ana, "amount": 60_000}, key())
call("POST", f"/pools/{pid}/contributions", {"account_id": caro, "amount": 40_000}, key())
pool = call("GET", f"/pools/{pid}")[1]
check("Aportes por persona y progreso", pool["balance"] == 100_000 and pool["remaining"] == 0
      and {c["amount"] for c in pool["contributions"]} == {60_000, 40_000})
status, _ = call("POST", f"/pools/{pid}/contributions", {"account_id": caro, "amount": 1}, key())
check("No se puede pasar de la meta", status == 422)
status, _ = call("POST", f"/pools/{pid}/release", {"requested_by": caro})
check("Solo el dueño la entrega", status == 403)
status, _ = call("POST", f"/pools/{pid}/release", {"requested_by": ana})
check("Entregar al beneficiario", status == 200 and balance(mama) == 100_000)
check("La mamá ve UN movimiento con contexto", len(call("GET", f"/accounts/{mama}/history")[1]["items"]) == 1)

pool2 = call("POST", "/pools", {"name": "Plan cancelado (verify)", "goal_amount": 50_000, "owner_id": ana, "beneficiary_id": mama})[1]["id"]
before = (balance(ana), balance(beto))
call("POST", f"/pools/{pool2}/contributions", {"account_id": ana, "amount": 5_000}, key())
call("POST", f"/pools/{pool2}/contributions", {"account_id": beto, "amount": 7_000}, key())
call("POST", f"/pools/{pool2}/cancel", {"requested_by": ana})
check("Cancelar devuelve exacto a cada uno", (balance(ana), balance(beto)) == before)

print("\n── Regla: no perder un peso ──")
rich, poor = new_account("Rico (verify)"), new_account("Pobre (verify)")
call("POST", f"/accounts/{rich}/deposits", {"amount": 10_000}, key())
status, err = call("POST", "/transfers", {"from_account_id": rich, "to_account_id": poor, "amount": 10_001}, key())
check("Saldo insuficiente no mueve nada", status == 409 and balance(rich) == 10_000 and balance(poor) == 0)

bad = [call("POST", f"/accounts/{rich}/deposits", {"amount": a}, key())[0] for a in (0, -5, 10.5, "100", 10**12)]
check("Montos inválidos rechazados (0, negativo, decimal, texto, gigante)", bad == [422] * 5, bad)

status, _ = call("POST", f"/accounts/{rich}/deposits", {"amount": 100})
check("Operaciones de plata exigen Idempotency-Key", status == 422)

k = key()
body = {"from_account_id": rich, "to_account_id": poor, "amount": 1_000}
first, retry = call("POST", "/transfers", body, k)[1], call("POST", "/transfers", body, k)[1]
check("Reintento con la misma clave no cobra doble", first["id"] == retry["id"] and balance(poor) == 1_000)
status, _ = call("POST", "/transfers", {**body, "amount": 2_000}, k)
check("Misma clave con otro monto se rechaza", status == 409)

# Concurrencia real por HTTP: 60 transferencias de $1.000 en paralelo con solo $9.000 disponibles.
with ThreadPoolExecutor(30) as ex:
    codes = list(ex.map(lambda _: call("POST", "/transfers", body, key())[0], range(60)))
check("60 transferencias simultáneas: solo pasan las que alcanzan",
      codes.count(201) == 9 and codes.count(409) == 51 and balance(rich) == 0, f"201={codes.count(201)}")

with ThreadPoolExecutor(20) as ex:
    same = list(ex.map(lambda _: call("POST", f"/accounts/{poor}/deposits", {"amount": 500}, "doble-clic-" + k), range(20)))
check("20 reintentos simultáneos de la misma operación = 1 movimiento",
      len({r[1]["id"] for r in same}) == 1 and balance(poor) == 10_500)

rec = call("GET", "/reconciliation")[1]
check("Auditoría global: el libro cuadra", rec["ok"], rec)

passed = sum(results)
print(f"\n{passed}/{len(results)} verificaciones OK\n")
sys.exit(0 if passed == len(results) else 1)
