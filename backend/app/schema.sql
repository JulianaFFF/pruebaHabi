-- Esquema de la billetera. Se puede ejecutar varias veces sin romper nada.
--
-- Idea central: libro contable de doble partida.
--   * accounts: el saldo actual de cada cuenta (una "foto" rápida de consultar).
--   * transactions: cada operación de negocio (depósito, transferencia, aporte a vaca...).
--   * entries: los movimientos. Cada transacción tiene >= 2 entries y su suma SIEMPRE es 0.
-- La plata no se crea ni se destruye: solo se mueve. Los depósitos salen de una cuenta
-- "system" (el mundo exterior), que es la única que puede quedar negativa.

CREATE TABLE IF NOT EXISTS accounts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL CHECK (length(trim(name)) > 0),
    kind        TEXT NOT NULL CHECK (kind IN ('user', 'pool', 'system')),
    -- Montos en pesos enteros (BIGINT). Nunca float.
    balance     BIGINT NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Última línea de defensa: aunque el código tenga un bug, la BD no deja saldos negativos.
    CONSTRAINT non_negative_balance CHECK (kind = 'system' OR balance >= 0)
);

CREATE TABLE IF NOT EXISTS transactions (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind             TEXT NOT NULL CHECK (kind IN
                        ('deposit', 'transfer', 'pool_contribution', 'pool_release', 'pool_refund')),
    description      TEXT,
    -- La clave de idempotencia la manda el cliente; si reintenta, no se cobra dos veces.
    idempotency_key  TEXT UNIQUE,
    request_hash     TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS entries (
    id              BIGSERIAL PRIMARY KEY,
    transaction_id  UUID NOT NULL REFERENCES transactions(id),
    account_id      UUID NOT NULL REFERENCES accounts(id),
    amount          BIGINT NOT NULL CHECK (amount <> 0),
    balance_after   BIGINT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (transaction_id, account_id)
);
CREATE INDEX IF NOT EXISTS entries_account_idx ON entries (account_id, id DESC);

-- Vacas: un fondo compartido con meta. El dinero de la vaca vive en su propia cuenta (kind='pool').
CREATE TABLE IF NOT EXISTS pools (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID NOT NULL UNIQUE REFERENCES accounts(id),
    name            TEXT NOT NULL CHECK (length(trim(name)) > 0),
    goal_amount     BIGINT NOT NULL CHECK (goal_amount > 0),
    owner_id        UUID NOT NULL REFERENCES accounts(id),
    beneficiary_id  UUID NOT NULL REFERENCES accounts(id),
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'released', 'cancelled')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at       TIMESTAMPTZ
);

-- Regla de doble partida a nivel de BD: al hacer COMMIT, las entries de cada
-- transacción deben sumar 0. Es DEFERRED para poder insertar las patas una por una.
CREATE OR REPLACE FUNCTION check_transaction_balanced() RETURNS trigger AS $$
BEGIN
    IF (SELECT COALESCE(SUM(amount), 0) FROM entries WHERE transaction_id = NEW.transaction_id) <> 0 THEN
        RAISE EXCEPTION 'transaction % is unbalanced', NEW.transaction_id;
    END IF;
    RETURN NULL;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS entries_balanced ON entries;
CREATE CONSTRAINT TRIGGER entries_balanced
    AFTER INSERT ON entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION check_transaction_balanced();

-- El historial es inmutable: los errores se corrigen con una transacción nueva, nunca editando.
CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS entries_append_only ON entries;
CREATE TRIGGER entries_append_only
    BEFORE UPDATE OR DELETE ON entries
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

DROP TRIGGER IF EXISTS transactions_append_only ON transactions;
CREATE TRIGGER transactions_append_only
    BEFORE UPDATE OR DELETE ON transactions
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
