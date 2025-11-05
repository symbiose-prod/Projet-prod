# Schéma PostgreSQL — Ferment Station

Ce fichier décrit l’état attendu de la base, basé sur `db/migrate.sql` **et** sur ce qui est visible dans la base actuelle.

---

## 1. tenants

```sql
CREATE TABLE tenants (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name       TEXT NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE users (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     UUID REFERENCES tenants(id) ON DELETE SET NULL,
  email         TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'user',
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE production_proposals (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   UUID REFERENCES tenants(id) ON DELETE CASCADE,
  created_by  UUID REFERENCES users(id) ON DELETE SET NULL,
  payload     JSONB NOT NULL,
  status      TEXT NOT NULL DEFAULT 'draft',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE password_resets (
  id          BIGSERIAL PRIMARY KEY,
  user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash  TEXT NOT NULL,
  expires_at  TIMESTAMPTZ NOT NULL,
  used_at     TIMESTAMPTZ,
  request_ip  TEXT,
  request_ua  TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_password_resets_user  ON password_resets(user_id);
CREATE INDEX idx_password_resets_token ON password_resets(token_hash);

CREATE OR REPLACE FUNCTION normalize_email_lower() ...
CREATE TRIGGER trg_users_email_lower BEFORE INSERT OR UPDATE ON users
  FOR EACH ROW EXECUTE FUNCTION normalize_email_lower();

CREATE OR REPLACE FUNCTION touch_updated_at() ...
CREATE TRIGGER trg_pp_touch BEFORE UPDATE ON production_proposals
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
