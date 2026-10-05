# Billetera con vacas

> ⚠️ BORRADOR. Las secciones marcadas con ✍️ las tengo que escribir yo, con mi voz y mi experiencia.
> El resto lo reviso y lo reescribo con mis palabras antes de entregar.

Una billetera para mover plata entre personas (cargar, transferir, consultar saldo e historial) y,
encima del núcleo, **vacas**: fondos compartidos con meta. Sirven para el regalo de la mamá, el
mercado del mes, el arriendo de los roommates o la cena del cumpleaños.

**Por qué vacas:** el problema que plantea el reto es que el banco no entiende el contexto.
Cuando tres hermanos le mandan plata a la mamá, el banco ve tres transferencias sueltas. Con una
vaca, todos ven cuánto lleva cada uno y cuánto falta. La mamá recibe **un solo movimiento**:
"Vaca «Mercado de mamá» liberada". Si el plan se cae, a cada uno se le devuelve exactamente lo
suyo, sin que nadie tenga que cuadrar cuentas en un grupo de WhatsApp.

## Cómo correrlo

```bash
# Opción A: todo con Docker (necesita el plugin compose: sudo apt install docker-compose-v2)
docker compose up --build
# -> http://localhost:8000 (app)  ·  http://localhost:8000/docs (API interactiva)

# Opción B: Postgres en Docker y la API local
docker run -d --name wallet-db -e POSTGRES_USER=wallet -e POSTGRES_PASSWORD=wallet \
  -e POSTGRES_DB=wallet -p 5432:5432 -v "$PWD/db/init.sql:/docker-entrypoint-initdb.d/init.sql:ro" postgres:16
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements-dev.txt
cd backend && ../.venv/bin/uvicorn app.main:app --reload

# Tests (contra Postgres real, base wallet_test)
cd backend && ../.venv/bin/pytest -v
```

## API

| Método | Ruta | Qué hace |
|---|---|---|
| POST | `/api/accounts` | Crear cuenta `{name}` |
| GET | `/api/accounts/{id}` | Consultar saldo |
| POST | `/api/accounts/{id}/deposits` 🔑 | Cargar saldo (simulado) `{amount, description?}` |
| POST | `/api/transfers` 🔑 | Transferir `{from_account_id, to_account_id, amount, description?}` |
| GET | `/api/accounts/{id}/history?limit=&before=` | Historial paginado con contexto y contraparte |
| POST | `/api/pools` | Crear vaca `{name, goal_amount, owner_id, beneficiary_id}` |
| GET | `/api/pools/{id}` | Progreso y aportes por persona |
| POST | `/api/pools/{id}/contributions` 🔑 | Aportar `{account_id, amount}` |
| POST | `/api/pools/{id}/release` | El dueño entrega la vaca al beneficiario (solo si llegó a la meta) |
| POST | `/api/pools/{id}/cancel` | El dueño cancela y se le devuelve a cada uno lo suyo |
| GET | `/api/reconciliation` | Auditoría: ¿cuadra todo el sistema? |

🔑 = requiere el header `Idempotency-Key`.

---

## 01 · Decisiones clave

- **FastAPI + PostgreSQL.** Python es el stack de HabiCapital. Elegí Postgres porque la
  protección de la plata la quería en transacciones ACID, bloqueos de fila y restricciones de la
  base de datos, no solo en código de aplicación. SQL directo con psycopg, sin ORM, para que
  cada `FOR UPDATE` quede a la vista.
- **Libro de doble partida.** No guardo solo "saldos": cada operación es una `transaction` con
  dos o más `entries` que suman 0. Los depósitos salen de una cuenta `system` (el mundo exterior,
  la única que puede quedar negativa), así que **la suma de todos los saldos del sistema es
  siempre 0**. Eso es fácil de auditar.
- **Una vaca es una cuenta más.** No inventé un mecanismo aparte para mover plata a las vacas:
  aportar es mover plata hacia la cuenta de la vaca con las mismas primitivas del núcleo. Las
  vacas heredan todas las garantías sin código nuevo de manejo de plata.
- **Montos en pesos enteros (BIGINT).** Nada de float. La API rechaza `10.5`, `"100"` y `true`
  (pydantic en modo estricto).
- **Idempotency-Key obligatoria** en toda operación que mueve plata.
- **Historial inmutable.** Hay triggers que prohíben UPDATE/DELETE en `entries` y
  `transactions`. Un error se corrige con una transacción nueva, nunca editando el pasado.
- ✍️ *(agrega o quita según lo que tú realmente decidiste y por qué)*

## 02 · Cómo sé que no se pierde un peso

| Qué puede salir mal | Cómo lo protejo | Evidencia |
|---|---|---|
| Dos transferencias simultáneas gastan el mismo saldo | `SELECT … FOR UPDATE` sobre las cuentas dentro de una transacción | `test_concurrent_transfers_never_overdraw`: 150 transferencias en paralelo de $1.000 desde $100.000 → exactamente 100 pasan |
| Deadlock entre A→B y B→A | Bloqueo siempre en el mismo orden (por id); la vaca siempre antes que las cuentas | `test_crossed_transfers_do_not_deadlock` (300 operaciones cruzadas) |
| Doble clic / la red reintenta | `Idempotency-Key` única en la BD; si se repite con otro contenido → 409 | `test_same_idempotency_key_in_parallel_moves_money_once` (20 reintentos simultáneos) |
| Un bug en mi código deja un saldo negativo | `CHECK (balance >= 0)` en la BD como última línea de defensa | `test_db_rejects_negative_balance_even_if_code_has_a_bug` |
| Plata que aparece o desaparece | Trigger diferido: al hacer COMMIT, cada transacción debe sumar 0 | `test_db_rejects_unbalanced_transaction` |
| Saldo cacheado distinto del historial | Auditoría (`/api/reconciliation`): saldo = suma de entries, suma total = 0, vacas coherentes | Se corre automáticamente **después de cada test** |
| Aporte que llega mientras se libera la vaca | La fila de la vaca se bloquea en aportar, liberar y cancelar | `test_release_racing_with_contributions` |
| Montos inválidos (0, negativos, decimales, gigantes) | Validación estricta + `CHECK amount <> 0` | `test_invalid_amounts_are_rejected` |
| Todo a la vez | — | `test_random_workload_preserves_all_money`: 400 operaciones aleatorias en paralelo |

**¿Cómo sé que los tests sirven?** Rompí el código a propósito y verifiqué que fallaran:

- Quité el `FOR UPDATE`: los tests de concurrencia fallaron con `CheckViolation`. La restricción
  de la BD frenó el saldo negativo aunque el código tenía el bug. Ahí vi la defensa en
  profundidad funcionando.
- Bloqueé las cuentas en orden aleatorio: Postgres detectó un `DeadlockDetected` real.
- Desactivé la idempotencia: los tests de reintento detectaron el cobro doble.

## 03 · Qué dejé fuera y por qué

- **Autenticación.** Quien pide una acción se identifica con su id (`requested_by`,
  `from_account_id`). En producción saldría del token. Lo dejé fuera para invertir el tiempo en
  la integridad de la plata.
- **Pasarela de pagos real y retiros.** El enunciado dice que la carga es simulada.
- **Múltiples monedas.** Solo COP.
- **Límites antifraude / KYC.** Solo puse un máximo de $1.000.000.000 por operación.
- ✍️ *(revisa y completa)*

## 04 · Qué haría distinto con más tiempo

✍️ Ideas para pensar: autenticación de verdad, vacas recurrentes (que se rearmen cada mes),
invitaciones con cuota sugerida por persona, migraciones con Alembic, conciliación programada
con alertas, idempotency keys con alcance por usuario y expiración, tests de carga, frontend
en TypeScript.

## 05 · Qué NO sé

✍️ Sé honesto. Algunas preguntas para pensarlo: ¿Cómo escalaría esto con muchas escrituras sobre
la misma cuenta (una cuenta "caliente")? ¿Cómo funciona la regulación de depósitos de bajo
monto en Colombia? ¿Qué pasa con READ COMMITTED frente a SERIALIZABLE en casos que no probé?
¿Cómo se concilia contra un banco real?

## 06 · Supuestos

- Un usuario = una cuenta, en pesos colombianos enteros (sin centavos).
- Una vaca no puede pasarse de la meta: el aporte que la excede se rechaza y se informa cuánto falta.
- Solo quien crea la vaca puede entregarla o cancelarla, y solo puede entregarla al completarse.
- Si una operación falla, la Idempotency-Key no queda "gastada": el cliente puede reintentar.
- ✍️ *(revisa y completa)*

## 07 · Cómo usé IA

✍️ **Escríbelo tú.** Qué herramientas usaste (Claude Code), en qué etapas, qué le pedías y qué
decidías tú (por ejemplo: el stack, la funcionalidad de vacas, qué riesgos priorizar). Cuenta al
menos una vez en que la IA se equivocó o casi te hizo equivocar. Algunos casos reales de esta
sesión que puedes verificar y usar si te sirven:
- La IA asumió que `docker compose` estaba instalado; en mi máquina no estaba el plugin.
- Los tests pasaron todos a la primera, y eso no prueba nada. Por eso rompí el código a propósito
  (sección 02) para confirmar que de verdad detectan errores.
- Una primera versión del test de liberación de la vaca podía fallar al azar según el orden de
  los hilos; hubo que hacerla determinista.

## 08 · Qué aprendí

✍️ **Escríbelo tú.**
