-- Extensions nécessaires
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- pour gen_random_uuid()

-- =========================
-- Tables de base
-- =========================
CREATE TABLE IF NOT EXISTS tenants (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name       TEXT NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     UUID REFERENCES tenants(id) ON DELETE SET NULL,
  email         TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'user',
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS production_proposals (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   UUID REFERENCES tenants(id) ON DELETE CASCADE,
  created_by  UUID REFERENCES users(id) ON DELETE SET NULL,
  payload     JSONB NOT NULL,
  status      TEXT NOT NULL DEFAULT 'draft',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =========================
-- Fonctions utilitaires
-- =========================

-- Normalise les e-mails en minuscules pour garantir l’unicité réelle
CREATE OR REPLACE FUNCTION normalize_email_lower()
RETURNS trigger AS $$
BEGIN
  NEW.email := lower(NEW.email);
  RETURN NEW;
END $$ LANGUAGE plpgsql;

-- Met à jour automatiquement updated_at
CREATE OR REPLACE FUNCTION touch_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END $$ LANGUAGE plpgsql;

-- =========================
-- Triggers
-- =========================
DROP TRIGGER IF EXISTS trg_users_email_lower ON users;
CREATE TRIGGER trg_users_email_lower
BEFORE INSERT OR UPDATE ON users
FOR EACH ROW EXECUTE FUNCTION normalize_email_lower();

DROP TRIGGER IF EXISTS trg_pp_touch ON production_proposals;
CREATE TRIGGER trg_pp_touch
BEFORE UPDATE ON production_proposals
FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- =========================
-- Index & contraintes
-- =========================

-- Accélère les accès multi-tenant
CREATE INDEX IF NOT EXISTS idx_users_tenant      ON users(tenant_id);
CREATE INDEX IF NOT EXISTS idx_pp_tenant         ON production_proposals(tenant_id);
CREATE INDEX IF NOT EXISTS idx_pp_created_by     ON production_proposals(created_by);

-- Recherche performante sur JSONB
CREATE INDEX IF NOT EXISTS idx_pp_payload_gin    ON production_proposals USING GIN (payload);
-- Recherche par nom (_meta.name) à l’intérieur du JSON
CREATE INDEX IF NOT EXISTS idx_pp_meta_name      ON production_proposals ((payload->'_meta'->>'name'));

-- Recherche par e-mail (déjà normalisé en lowercase par trigger)
CREATE INDEX IF NOT EXISTS idx_users_email_lower ON users (lower(email));

-- Contrainte de rôle autorisé (idempotent)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'users_role_check'
  ) THEN
    ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('user','admin'));
  END IF;
END $$;

-- =========================
-- Password reset tokens
-- =========================
CREATE TABLE IF NOT EXISTS password_resets (
  id          BIGSERIAL PRIMARY KEY,
  user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash  TEXT NOT NULL,
  expires_at  TIMESTAMPTZ NOT NULL,
  used_at     TIMESTAMPTZ,
  request_ip  TEXT,
  request_ua  TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_password_resets_user    ON password_resets(user_id);
CREATE INDEX IF NOT EXISTS idx_password_resets_token   ON password_resets(token_hash);
CREATE INDEX IF NOT EXISTS idx_password_resets_expires ON password_resets(expires_at);

-- =========================
-- Sessions persistantes ("Se souvenir de moi")
-- =========================
CREATE TABLE IF NOT EXISTS user_sessions (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  token_hash  TEXT NOT NULL UNIQUE,
  expires_at  TIMESTAMPTZ NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_user_sessions_token   ON user_sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_user_sessions_user    ON user_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_user_sessions_expires ON user_sessions(expires_at);

-- =========================
-- Brute-force lockout (persiste au redémarrage)
-- =========================
CREATE TABLE IF NOT EXISTS login_failures (
  email       TEXT PRIMARY KEY,
  fail_count  INTEGER NOT NULL DEFAULT 0,
  last_fail   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_login_failures_last ON login_failures(last_fail);

-- =========================
-- Journal d'audit (traçabilité des actions métier)
-- =========================
CREATE TABLE IF NOT EXISTS audit_log (
  id          BIGSERIAL PRIMARY KEY,
  tenant_id   UUID REFERENCES tenants(id) ON DELETE SET NULL,
  user_email  VARCHAR(254),
  action      VARCHAR(50) NOT NULL,
  details     JSONB DEFAULT '{}',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_tenant_created ON audit_log(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action         ON audit_log(action, created_at DESC);

-- Index composite pour la recherche de propositions par tenant + statut
CREATE INDEX IF NOT EXISTS idx_pp_tenant_status ON production_proposals(tenant_id, status);

-- Index unique sur le nom de proposition par tenant (previent les race conditions rename)
CREATE UNIQUE INDEX IF NOT EXISTS idx_pp_unique_name_per_tenant
  ON production_proposals (tenant_id, (payload->'_meta'->>'name'))
  WHERE payload->'_meta'->>'name' IS NOT NULL;

-- Index fonctionnel pour les requetes lower() sur login_failures
CREATE INDEX IF NOT EXISTS idx_login_failures_email_lower ON login_failures(lower(email));

-- =========================
-- Configurations fournisseurs (contraintes de commande éditables)
-- =========================
CREATE TABLE IF NOT EXISTS supplier_configs (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  supplier    TEXT NOT NULL,
  config      JSONB NOT NULL DEFAULT '{}',
  updated_by  UUID REFERENCES users(id) ON DELETE SET NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Un seul enregistrement par fournisseur par tenant
CREATE UNIQUE INDEX IF NOT EXISTS idx_sc_tenant_supplier
  ON supplier_configs(tenant_id, supplier);

CREATE INDEX IF NOT EXISTS idx_sc_tenant ON supplier_configs(tenant_id);

DROP TRIGGER IF EXISTS trg_sc_touch ON supplier_configs;
CREATE TRIGGER trg_sc_touch
BEFORE UPDATE ON supplier_configs
FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- =========================
-- Synchronisation étiquettes (SaaS → Base Access)
-- =========================

-- File d'attente sync + audit trail
CREATE TABLE IF NOT EXISTS sync_operations (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  op_type       TEXT NOT NULL DEFAULT 'REPLACE_ALL',
  status        TEXT NOT NULL DEFAULT 'pending',
  payload       JSONB NOT NULL DEFAULT '[]'::jsonb,
  product_count INTEGER NOT NULL DEFAULT 0,
  triggered_by  TEXT NOT NULL DEFAULT 'scheduler',
  error_msg     TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  fetched_at    TIMESTAMPTZ,
  applied_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_sync_ops_tenant_status ON sync_operations(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_sync_ops_created       ON sync_operations(created_at DESC);

-- Clés API pour l'agent Windows
CREATE TABLE IF NOT EXISTS sync_api_keys (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  key_hash    TEXT NOT NULL UNIQUE,
  label       TEXT NOT NULL DEFAULT '',
  is_active   BOOLEAN NOT NULL DEFAULT TRUE,
  created_by  UUID REFERENCES users(id) ON DELETE SET NULL,
  last_used   TIMESTAMPTZ,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sync_api_keys_hash   ON sync_api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_sync_api_keys_tenant ON sync_api_keys(tenant_id);

-- Heure planifiée sync étiquettes (configurable par tenant)
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS sync_schedule_hour SMALLINT NOT NULL DEFAULT 5;
-- Défaut 5h du matin (avant la production)

-- =========================
-- Nomenclatures produits (BOM packaging par produit-format)
-- =========================
CREATE TABLE IF NOT EXISTS product_bom (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  id_produit    INTEGER NOT NULL,
  format_code   TEXT NOT NULL,
  product_label TEXT NOT NULL DEFAULT '',
  id_mp         INTEGER NOT NULL,
  mp_label      TEXT NOT NULL DEFAULT '',
  qty_per_unit  REAL NOT NULL DEFAULT 0,
  validated     BOOLEAN NOT NULL DEFAULT FALSE,
  source        TEXT NOT NULL DEFAULT 'manual',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_bom_tenant_product_mp
  ON product_bom(tenant_id, id_produit, format_code, id_mp);

CREATE INDEX IF NOT EXISTS idx_bom_tenant ON product_bom(tenant_id);
CREATE INDEX IF NOT EXISTS idx_bom_mp     ON product_bom(id_mp);

DROP TRIGGER IF EXISTS trg_bom_touch ON product_bom;
CREATE TRIGGER trg_bom_touch
BEFORE UPDATE ON product_bom
FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- =========================
-- Cache clients EasyBeer (sync nocturne)
-- =========================
CREATE TABLE IF NOT EXISTS client_cache (
  id              BIGSERIAL PRIMARY KEY,
  tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  id_client       INTEGER NOT NULL,
  nom             TEXT NOT NULL DEFAULT '',
  numero          TEXT NOT NULL DEFAULT '',
  type_libelle    TEXT NOT NULL DEFAULT '',
  type_parent     TEXT NOT NULL DEFAULT '',
  tournee         TEXT NOT NULL DEFAULT '',
  tags            TEXT[] NOT NULL DEFAULT '{}',
  actif           BOOLEAN NOT NULL DEFAULT TRUE,
  synced_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cc_tenant_client
  ON client_cache(tenant_id, id_client);
CREATE INDEX IF NOT EXISTS idx_cc_tenant
  ON client_cache(tenant_id);
CREATE INDEX IF NOT EXISTS idx_cc_tags
  ON client_cache USING GIN (tags);

-- Vue matérialisée des tags distincts (reconstruit à chaque sync)
CREATE TABLE IF NOT EXISTS client_tags_cache (
  id              BIGSERIAL PRIMARY KEY,
  tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  tag             TEXT NOT NULL,
  client_count    INTEGER NOT NULL DEFAULT 0,
  synced_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ctc_tenant_tag
  ON client_tags_cache(tenant_id, tag);

-- =========================
-- Historique des ramasses (fiches de ramasse envoyées)
-- =========================
CREATE TABLE IF NOT EXISTS ramasse_history (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  created_by      UUID REFERENCES users(id) ON DELETE SET NULL,
  date_ramasse    DATE NOT NULL,
  destinataire    TEXT NOT NULL,
  recipients      TEXT[] NOT NULL DEFAULT '{}',
  line_count      INTEGER NOT NULL DEFAULT 0,
  total_cartons   INTEGER NOT NULL DEFAULT 0,
  total_palettes  INTEGER NOT NULL DEFAULT 0,
  total_poids_kg  INTEGER NOT NULL DEFAULT 0,
  lines           JSONB NOT NULL DEFAULT '[]'::jsonb,
  packaging       JSONB DEFAULT '[]'::jsonb,
  pdf_bytes       BYTEA,
  brassin_ids     TEXT[] DEFAULT '{}',
  status          TEXT NOT NULL DEFAULT 'sent',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ramasse_tenant_date
  ON ramasse_history(tenant_id, date_ramasse DESC);
CREATE INDEX IF NOT EXISTS idx_ramasse_tenant_created
  ON ramasse_history(tenant_id, created_at DESC);

-- Édition / versioning / verrouillage chauffeur (ajout 2026-04)
ALTER TABLE ramasse_history
  ADD COLUMN IF NOT EXISTS version          INTEGER     NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS version_log      JSONB       NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS previous_lines   JSONB,
  ADD COLUMN IF NOT EXISTS driver_passed    BOOLEAN     NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS driver_passed_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS driver_passed_by UUID        REFERENCES users(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS updated_at       TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS idx_ramasse_tenant_driver_passed
  ON ramasse_history(tenant_id, driver_passed, date_ramasse DESC);

-- Soft-delete (ajout 2026-04-19) — permet récupération pendant 7 jours avant purge.
ALTER TABLE ramasse_history
  ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

-- Index partiel : seules les lignes vivantes (non supprimées) sont indexées,
-- les requêtes quotidiennes scannent donc uniquement les ramasses actives.
CREATE INDEX IF NOT EXISTS idx_ramasse_tenant_active
  ON ramasse_history(tenant_id, date_ramasse DESC)
  WHERE deleted_at IS NULL;

-- =========================
-- File d'attente emails (fallback Brevo)
-- =========================
-- Si l'envoi via l'API Brevo échoue (rate-limit, réseau, panne), l'email
-- est persisté ici avec status='pending' pour retry ultérieur par un
-- worker / cron (common.email_queue.retry_pending_emails).
CREATE TABLE IF NOT EXISTS email_queue (
  id           BIGSERIAL PRIMARY KEY,
  tenant_id    UUID REFERENCES tenants(id) ON DELETE CASCADE,
  to_emails    TEXT[] NOT NULL,
  cc_emails    TEXT[] DEFAULT '{}',
  subject      TEXT NOT NULL,
  html_body    TEXT NOT NULL,
  attachments  JSONB DEFAULT '[]'::jsonb,
  reply_to     TEXT,
  status       TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending','sent','failed')),
  attempts     INTEGER NOT NULL DEFAULT 0,
  last_error   TEXT,
  provider_msg_id TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  sent_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_email_queue_status_created
  ON email_queue(status, created_at)
  WHERE status = 'pending';

-- =========================
-- Row-Level Security (defense-in-depth) — ramasse_history
-- =========================
-- Active la RLS avec une policy "opt-in" qui filtre par tenant_id uniquement
-- si la session a positionné la variable app.current_tenant_id (via SET LOCAL
-- ou via set_config('app.current_tenant_id', ...)). Sans cette variable, la
-- policy laisse passer (backward-compat avec l'accès admin/owner actuel).
--
-- Activation full-enforcement (étape future, hors migration — nécessite un
-- rôle applicatif dédié non-owner) :
--   ALTER TABLE ramasse_history FORCE ROW LEVEL SECURITY;
--   GRANT SELECT,INSERT,UPDATE,DELETE ON ramasse_history TO app_role;
--   (l'app doit alors set_config('app.current_tenant_id', ...) systématiquement)
ALTER TABLE ramasse_history ENABLE ROW LEVEL SECURITY;

-- Drop-if-exists + create pour permettre la ré-exécution idempotente
DROP POLICY IF EXISTS tenant_isolation ON ramasse_history;
CREATE POLICY tenant_isolation ON ramasse_history
  USING (
    -- NULL ou vide → policy inactive (comportement actuel préservé)
    current_setting('app.current_tenant_id', true) IS NULL
    OR current_setting('app.current_tenant_id', true) = ''
    OR tenant_id = current_setting('app.current_tenant_id', true)::uuid
  )
  WITH CHECK (
    current_setting('app.current_tenant_id', true) IS NULL
    OR current_setting('app.current_tenant_id', true) = ''
    OR tenant_id = current_setting('app.current_tenant_id', true)::uuid
  );

-- =========================
-- Cache EasyBeer générique (JSONB)
-- =========================
CREATE TABLE IF NOT EXISTS eb_cache (
  id         BIGSERIAL PRIMARY KEY,
  tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  cache_key  TEXT NOT NULL,
  item_id    TEXT NOT NULL DEFAULT '',
  data       JSONB NOT NULL,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ebc_tenant_key_item
  ON eb_cache(tenant_id, cache_key, item_id);

-- Metadata de sync EasyBeer
CREATE TABLE IF NOT EXISTS eb_sync_meta (
  id              BIGSERIAL PRIMARY KEY,
  tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  cache_key       TEXT NOT NULL,
  last_sync_at    TIMESTAMPTZ,
  sync_duration_s REAL,
  item_count      INTEGER NOT NULL DEFAULT 0,
  error_count     INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_esm_tenant_key
  ON eb_sync_meta(tenant_id, cache_key);

-- =========================
-- Cache ventes mensuelles par goût (pour la page Prévisions)
-- =========================
-- Une ligne par (tenant, année, mois, goût canon). Évite les appels API
-- répétés sur l'historique : seuls les mois en cours sont rafraîchis.
CREATE TABLE IF NOT EXISTS monthly_sales (
  id          BIGSERIAL PRIMARY KEY,
  tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  year        SMALLINT NOT NULL,
  month       SMALLINT NOT NULL CHECK (month BETWEEN 1 AND 12),
  gout_canon  TEXT NOT NULL,
  volume_hl   REAL NOT NULL DEFAULT 0,
  fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ms_tenant_period_gout
  ON monthly_sales(tenant_id, year, month, gout_canon);

CREATE INDEX IF NOT EXISTS idx_ms_tenant_year
  ON monthly_sales(tenant_id, year);

-- =========================
-- Étiquettes palette : historique des étiquettes générées
-- (page /etiquettes-palette — pour ré-impression et audit trail)
-- =========================
CREATE TABLE IF NOT EXISTS etiquette_palette_history (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  user_email    TEXT,
  ean           TEXT NOT NULL,                -- GTIN colis (carton)
  lot           TEXT NOT NULL,
  ddm           DATE NOT NULL,
  fmt           TEXT NOT NULL,                -- ex: "6x33", "12x33", "6x75", "4x75"
  marque        TEXT NOT NULL,                -- "NIKO" | "SYMBIOSE"
  designation   TEXT,                         -- libellé produit nettoyé
  gout          TEXT,                         -- ex: "Gingembre"
  case_count    INTEGER NOT NULL,
  full_pallet   BOOLEAN NOT NULL DEFAULT false,
  n_copies      INTEGER NOT NULL DEFAULT 1,
  pcb           INTEGER NOT NULL DEFAULT 0,
  generated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_etiq_pal_tenant_date
  ON etiquette_palette_history(tenant_id, generated_at DESC);

-- Colonnes additionnelles ajoutées après création initiale (idempotent)
ALTER TABLE etiquette_palette_history
  ADD COLUMN IF NOT EXISTS gtin_uvc TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS code_interne TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS bio BOOLEAN NOT NULL DEFAULT true;

-- =========================
-- Permissions (user applicatif "shark")
-- =========================
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'shark') THEN
    GRANT ALL ON TABLE tenants, users, production_proposals,
                       password_resets, user_sessions, login_failures,
                       audit_log, supplier_configs,
                       sync_operations, sync_api_keys,
                       product_bom, ramasse_history,
                       client_cache, client_tags_cache,
                       eb_cache, eb_sync_meta,
                       email_queue, monthly_sales,
                       etiquette_palette_history TO shark;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO shark;
  END IF;
END $$;
