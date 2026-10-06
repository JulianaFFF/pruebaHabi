# Billetera con vacas

Una billetera para mover plata entre personas (cargar, transferir, consultar saldo e historial) y,
encima del núcleo, **vacas**: fondos compartidos con meta. Sirven para el regalo de la mamá, el
mercado del mes, el arriendo de los roommates o la cena del cumpleaños.

**Por qué vacas:** el problema que plantea el reto es que el banco no entiende el contexto.
Cuando tres hermanos le mandan plata a la mamá, el banco ve tres transferencias sueltas. Con una
vaca, todos ven cuánto lleva cada uno y cuánto falta. La mamá recibe **un solo movimiento**:
"Vaca «Mercado de mamá» liberada". Si el plan se cae, a cada uno se le devuelve exactamente lo
suyo, sin que nadie tenga que cuadrar cuentas en un grupo de WhatsApp.

## Cómo correrlo

Requisitos: Docker con el plugin Compose (en Ubuntu: `sudo apt install docker-compose-v2`).

```bash
docker compose up -d --build
# -> http://localhost:8000 (app)  ·  http://localhost:8000/docs (API interactiva)

# Verificar los requisitos del reto contra la app desplegada (solo usa la librería estándar)
python3 scripts/verify.py
```

Para correr los tests (contra Postgres real, en la base `wallet_test` que crea `db/init.sql`):

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements-dev.txt
cd backend && ../.venv/bin/pytest -v
```

## Estructura

```
backend/app/schema.sql   tablas + reglas en la base de datos (CHECKs y triggers)
backend/app/ledger.py    núcleo contable: bloqueos, movimientos, idempotencia, auditoría
backend/app/pools.py     vacas, construidas sobre las primitivas del ledger
backend/app/main.py      API (FastAPI)
backend/tests/           tests de API, de vacas y de seguridad de la plata (concurrencia)
frontend/index.html      interfaz web (HTML + JS, sin build)
scripts/verify.py        verificación de requisitos contra la app desplegada
```

## API

| Método | Ruta | Qué hace |
|---|---|---|
| POST | `/api/accounts` | Crear cuenta `{name}` |
| GET | `/api/accounts/{id}` | Consultar saldo |
| POST | `/api/accounts/{id}/deposits` (*) | Cargar saldo (simulado) `{amount, description?}` |
| POST | `/api/transfers` (*) | Transferir `{from_account_id, to_account_id, amount, description?}` |
| GET | `/api/accounts/{id}/history?limit=&before=` | Historial paginado con contexto y contraparte |
| POST | `/api/pools` | Crear vaca `{name, goal_amount, owner_id, beneficiary_id}` |
| GET | `/api/pools/{id}` | Progreso y aportes por persona |
| POST | `/api/pools/{id}/contributions` (*) | Aportar `{account_id, amount}` |
| POST | `/api/pools/{id}/release` | El dueño entrega la vaca al beneficiario (solo si llegó a la meta) |
| POST | `/api/pools/{id}/cancel` | El dueño cancela y se le devuelve a cada uno lo suyo |
| GET | `/api/reconciliation` | Auditoría: ¿cuadra todo el sistema? |

(*) = requiere el header `Idempotency-Key`.

---

## 01 · Decisiones clave

- **FastAPI + PostgreSQL.** Python es el stack de HabiCapital. Elegí Postgres porque quería que
  la protección de la plata estuviera en transacciones ACID, bloqueos de fila y restricciones de
  la base de datos, no solo en el código de la aplicación. Uso SQL directo con psycopg, sin ORM,
  para que cada `FOR UPDATE` quede a la vista.
- **Libro de doble partida.** No guardo solo "saldos": cada operación es una `transaction` con
  dos o más `entries` que suman 0. Los depósitos salen de una cuenta `system` (el mundo exterior,
  la única que puede quedar negativa), así que **la suma de todos los saldos del sistema es
  siempre 0**. Eso hace que sea fácil de auditar.
- **Una vaca es una cuenta más.** No inventé un mecanismo aparte para mover plata a las vacas:
  aportar es mover plata hacia la cuenta de la vaca con las mismas primitivas del núcleo. Así las
  vacas heredan todas las garantías sin código nuevo de manejo de plata.
- **Liberar y cancelar una vaca son idempotentes por su estado.** Con la vaca bloqueada, solo
  la primera llamada la encuentra abierta; las demás reciben un 409. Por eso no necesitan
  Idempotency-Key.
- **Montos en pesos enteros (BIGINT).** Nada de float. La API rechaza `10.5`, `"100"` y `true`
  (pydantic en modo estricto).
- **Idempotency-Key obligatoria** en toda operación que mueve plata.
- **Historial inmutable.** Hay triggers que prohíben UPDATE/DELETE en `entries` y
  `transactions`. Un error se corrige con una transacción nueva, nunca editando el pasado.
- **Frontend en HTML y JS sin framework.** Su función es hacer la demo usable. Preferí invertir
  el tiempo en el backend, que es donde está el riesgo.

## 02 · Cómo sé que no se pierde un peso

| Qué puede salir mal | Cómo lo protejo | Evidencia |
|---|---|---|
| Dos transferencias simultáneas gastan el mismo saldo | `SELECT … FOR UPDATE` sobre las cuentas dentro de una transacción | `test_concurrent_transfers_never_overdraw`: 150 transferencias en paralelo de $1.000 desde $100.000 → exactamente 100 pasan |
| Deadlock entre A→B y B→A | Bloqueo siempre en el mismo orden (por id); la vaca siempre antes que las cuentas | `test_crossed_transfers_do_not_deadlock` (300 operaciones cruzadas) |
| Doble clic / la red reintenta | `Idempotency-Key` única en la BD; si se repite con otro contenido → 409 | `test_same_idempotency_key_in_parallel_moves_money_once` (20 reintentos simultáneos) |
| Un bug en mi código deja un saldo negativo | `CHECK (balance >= 0)` en la BD como última línea de defensa | `test_db_rejects_negative_balance_even_if_code_has_a_bug` |
| Plata que aparece o desaparece | Trigger diferido: al hacer COMMIT, cada transacción debe sumar 0 | `test_db_rejects_unbalanced_transaction` |
| Saldo guardado distinto del historial | Auditoría (`/api/reconciliation`): saldo = suma de entries, suma total = 0, vacas coherentes | Se corre automáticamente **después de cada test** |
| Aporte que llega mientras se libera la vaca | La fila de la vaca se bloquea al aportar, liberar y cancelar | `test_release_racing_with_contributions` |
| Montos inválidos (0, negativos, decimales, texto, gigantes) | Validación estricta + `CHECK amount <> 0` | `test_invalid_amounts_are_rejected` |
| Todo a la vez | — | `test_random_workload_preserves_all_money`: 400 operaciones aleatorias en paralelo |

Además, `scripts/verify.py` repite las pruebas clave **contra la app desplegada, por HTTP**. Por
ejemplo: 60 transferencias simultáneas desde una cuenta que solo alcanza para 9 → pasan
exactamente 9. También comprobé que, al reiniciar los contenedores, los saldos se mantienen y la
auditoría sigue cuadrando.

**¿Cómo sé que los tests sirven?** Rompí el código a propósito y verifiqué que fallaran:

- Quité el `FOR UPDATE`: los tests de concurrencia fallaron con `CheckViolation`. La restricción
  de la BD frenó el saldo negativo aunque el código tenía el bug. Ahí vi la defensa en
  profundidad funcionando.
- Bloqueé las cuentas en orden aleatorio: Postgres detectó un `DeadlockDetected` real.
- Desactivé la idempotencia: los tests de reintento detectaron el cobro doble.

## 03 · Qué dejé fuera y por qué

- **Autenticación.** Quien pide una acción se identifica con su id (`requested_by`,
  `from_account_id`). En producción saldría de un token. Lo dejé fuera para invertir el tiempo en
  la integridad de la plata.
- **Pasarela de pagos real y retiros.** El enunciado dice que la carga es simulada.
- **Múltiples monedas.** Solo COP.
- **Límites antifraude / KYC.** Solo puse un máximo de $1.000.000.000 por operación.
- **Migraciones de esquema.** El esquema se aplica al arrancar con SQL que se puede ejecutar
  varias veces. Para un proyecto vivo usaría Alembic.

## 04 · Qué haría distinto con más tiempo

- **Autenticación de verdad**, porque es clave para la seguridad del producto. Hoy cualquiera que
  conozca el id de una cuenta puede mover su plata.
- **Vacas recurrentes que se rearmen cada mes** (el arriendo, el mercado de la mamá), con una
  cuota sugerida por persona. Así se tendría un control real de quién ya pagó y quién no, mes a
  mes.
- **Idempotency-Keys con alcance por usuario y con expiración.** Hoy son globales y no vencen.
- **Pruebas de carga** para saber cuántas operaciones por segundo aguanta una sola cuenta muy
  usada.

## 05 · Qué NO sé

- **Cómo escalaría esto con una cuenta "caliente".** Si miles de personas aportan a la misma vaca
  al mismo tiempo, todas hacen fila por el mismo bloqueo. Sé que existen técnicas para eso
  (dividir el saldo en varias filas, procesar en cola), pero nunca las he implementado.
- **READ COMMITTED vs. SERIALIZABLE.** Usé el nivel de aislamiento por defecto de Postgres más
  bloqueos explícitos, y los tests de concurrencia pasan. Pero no sabría asegurar que no hay un
  caso que no probé donde SERIALIZABLE haría una diferencia.
- **Cómo se integra con plata real.** Si una pasarela de pagos confirma un depósito pero mi base
  de datos falla justo en ese momento, ¿cómo se reconcilia? Entiendo que el problema existe, pero
  no conozco los patrones que se usan en producción para resolverlo.
- **La regulación.** No sé qué exige la Superintendencia Financiera para un producto que guarda
  saldos de personas (depósitos de bajo monto, SARLAFT, límites por usuario).

## 06 · Supuestos

- Un usuario = una cuenta, en pesos colombianos enteros (sin centavos), porque en la práctica en
  Colombia no se mueven centavos.
- La plata entra al sistema desde una cuenta `system` que representa el mundo exterior; es la
  única que puede quedar negativa.
- Una vaca no puede pasarse de la meta: el aporte que la excede se rechaza y se informa cuánto
  falta. Así no queda plata "sobrante" sin dueño claro.
- Solo quien crea la vaca puede entregarla o cancelarla, y solo puede entregarla cuando se
  completa.
- Cualquier usuario puede aportar a cualquier vaca abierta (no hay invitaciones), y el dueño
  puede ser también el beneficiario (por ejemplo, el roommate que paga el arriendo).
- Si una operación falla (por ejemplo, por saldo insuficiente), la Idempotency-Key no queda
  "gastada": el cliente puede reintentar con la misma clave.
- Sin autenticación, se confía en el id que manda el cliente (ver sección 03).

## 07 · Cómo usé IA

Usé **Claude Code** dentro de VS Code durante todo el reto.

**La dinámica:** le pasé el PDF y le pedí ayuda para entender el reto y construirlo. Primero me
explicó qué pedían y qué podía salir mal con la plata (redondeo, carreras entre operaciones,
reintentos, deadlocks). Luego me planteó las decisiones que me tocaban a mí, y yo elegí:
**FastAPI + Postgres** (por ser el stack de HabiCapital y por las garantías de la base de datos),
**las vacas** como funcionalidad extra (entre varias opciones, porque me pareció menos obvia que
dividir una cuenta y más fiel a la idea de "contexto") y **un frontend web sencillo** para la
demo. Con eso, la IA escribió la mayor parte del código, los tests y el borrador de este README.
Yo [completa con lo que hiciste: leí `ledger.py` y `schema.sql` hasta poder explicar cada
bloqueo, desplegué con Docker, corrí la verificación, escribí y corregí secciones del README…].

**Dónde se equivocó o casi me hace equivocar:**
- **Supuso cosas de mi entorno.** Escribió los comandos asumiendo que yo tenía `docker compose`,
  y en mi máquina no estaba el plugin. Más adelante, al intentar detener el servidor viejo, usó un
  comando (`pkill -f`) cuyo patrón coincidía consigo mismo y terminó matando su propia terminal.
  Ninguno fue grave, pero me mostró que la IA no ve mi máquina: asume.
- **Todos los tests pasaron a la primera.** Eso podía darme una falsa confianza: un test que
  nunca ha fallado no demuestra nada. Por eso rompimos el código a propósito (sección 02) para
  confirmar que los tests realmente detectan los errores.
- **Un test podía fallar al azar.** La primera versión del test de liberación de vaca dependía del
  orden en que corrieran los hilos; hubo que hacerla determinista.

Mi conclusión es que la IA es muy rápida escribiendo, pero lo que decide si el sistema es
confiable es lo que uno verifica.

## 08 · Qué aprendí

- **Qué es un libro de doble partida** y por qué los sistemas de plata no guardan solo un número
  de saldo: si cada movimiento suma cero, auditar se vuelve una suma.
- **Que la concurrencia es donde se pierde la plata.** No conocía `SELECT … FOR UPDATE` ni sabía
  que bloquear en distinto orden produce deadlocks. Verlo fallar de verdad al romper el código me
  lo dejó mucho más claro que leerlo.
- **Qué es la idempotencia** y por qué un doble clic es un problema real cuando se trata de plata.
- **Lo que más me sorprendió:** al quitar el bloqueo, la regla `CHECK` de la base de datos igual
  impidió el saldo negativo. No confiar en una sola capa sí sirve.
- **Lo que me llevo:** con IA, escribir código es la parte fácil. La parte difícil es decidir qué
  construir, desconfiar de lo que "ya funciona" y demostrarlo con evidencia.
