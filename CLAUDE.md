# CLAUDE.md — Ferment Station

## Project Overview

Ferment Station is a multi-tenant NiceGUI web application for fermentation production management. It handles production planning, optimization, harvest sheet generation (PDF/Excel), EasyBeer ERP integration, and email notifications.

**Deployment:** OVH VPS (migré depuis Kinsta/Sevalla en février 2026)
**Serveur:** `vps-7ac853de.vps.ovh.net` — IP `92.222.229.87`
**URL:** https://prod.symbiose-kefir.fr
**Language:** Python 3.11+
**Framework:** NiceGUI (Quasar/Vue3 + FastAPI/Starlette)
**Database:** PostgreSQL 16 (local sur le VPS)
**Reverse proxy:** Caddy (HTTPS automatique via Let's Encrypt)
**Email:** Brevo API (compte Symbiose Kéfir — `hello@symbiose-kefir.fr`)

---

## Architecture

```
app_nicegui.py          # Entry point — NiceGUI, auth middleware, health check
ui/                     # NiceGUI pages (@ui.page decorators)
common/                 # Shared utilities (auth, email, storage, EasyBeer client)
core/                   # Business logic / algorithms (optimizer)
db/                     # Database layer (SQLAlchemy + psycopg2)
data/                   # Data files (Excel, CSV)
assets/                 # Templates, images, signatures
scripts/                # CLI utilities
tests/                  # Unit tests (pytest)
ops/                    # Ops config (env.sample)
docs/                   # RUNBOOK.md, DEPLOYMENT_NOTES.md, EasyBeer OpenAPI
```

### Pages (ui/)

| File | Route | Purpose |
|------|-------|---------|
| `ui/auth.py` | `/login`, `/reset/{token}` | Login, signup, password reset |
| `ui/accueil.py` | `/accueil` | Home — file upload, EasyBeer sync |
| `ui/production.py` | `/production` | Production planning + EasyBeer brassin creation |
| `ui/ramasse.py` | `/ramasse` | Harvest/collection sheet + BL PDF/Excel export |
| `ui/theme.py` | — | Design system, page layout, custom components |
| `ui/_production_calc.py` | — | Production computation (no UI, thread-safe) |
| `ui/_production_easybeer.py` | — | EasyBeer brassin creation section |

### Common Modules

| File | Purpose |
|------|---------|
| `common/auth.py` | User/tenant management, PBKDF2-SHA256 hashing, session tokens, brute-force lockout |
| `common/auth_reset.py` | Password reset token handling (Brevo email) |
| `common/data.py` | Config loading (`config.yaml`), file paths, business constants |
| `common/email.py` | Brevo HTTPS API integration (transactional email) |
| `common/storage.py` | DB-backed production proposal storage (JSONB) |
| `common/session_store.py` | DataFrame serialization with zlib compression |
| `common/ramasse.py` | Harvest sheet business logic (poids cartons, palettes) |
| `common/lot_fifo.py` | FIFO batch tracking for ingredient lots |
| `common/xlsx_fill/` | Excel/PDF generation package (fiche production, BL) |
| `common/easybeer/` | EasyBeer API client package (stocks, brassins, recipes, conditioning) |

### Core Modules

| File | Purpose |
|------|---------|
| `core/optimizer/planning.py` | Production planning algorithm (equalization + dichotomy) |
| `core/optimizer/parsing.py` | Stock format parsing (bouteilles/carton, volume) |
| `core/optimizer/losses.py` | Loss computation |
| `core/optimizer/flavors.py` | Flavor canonicalization |
| `core/optimizer/normalization.py` | Text normalization (accents, encodage) |
| `core/optimizer/excel_io.py` | Excel input reader with period detection |

---

## Database

PostgreSQL 16 with 6 tables:

- **tenants** — organization isolation (multi-tenancy)
- **users** — per-tenant accounts (email, PBKDF2-SHA256 password hash, role)
- **production_proposals** — saved production plans (JSONB payload)
- **password_resets** — one-time reset tokens with expiry
- **user_sessions** — remember-me tokens (30 days, hashed)
- **login_failures** — brute-force lockout tracking (persistent)

Schema: `db/migrate.sql`
Run migrations: `python scripts/app_bootstrap.py`

---

## Configuration

### config.yaml (business constants — version-controlled)

Toutes les constantes métier sont centralisées dans `config.yaml` :
- Configurations cuves (capacités, pertes)
- DDM, prix de référence
- Limites (max slots, fenêtre de données)

Voir `common/data.py` → `get_business_config()` pour le chargement avec valeurs par défaut.

### Environment Variables

Fichier `.env` à la racine (local) ou `/home/ubuntu/app/.env` (production).

```bash
# Database
DB_HOST, DB_PORT, DB_DATABASE, DB_USERNAME, DB_PASSWORD
DB_SSLMODE=disable          # local sur OVH

# Email (Brevo)
BREVO_API_KEY
EMAIL_SENDER, EMAIL_SENDER_NAME

# App
BASE_URL                    # https://prod.symbiose-kefir.fr
ENV                         # production | development
RESET_TTL_MINUTES           # default 60
NICEGUI_SECRET              # obligatoire, >= 32 chars
NICEGUI_PORT                # default 8502

# EasyBeer API
EASYBEER_API_USER
EASYBEER_API_PASS
EASYBEER_ID_BRASSERIE       # default 2013

# Production security
ALLOWED_TENANTS             # obligatoire en production (ex: "Symbiose Kéfir")
```

---

## Running Locally

```bash
pip install -r requirements.txt
python scripts/app_bootstrap.py   # run DB migrations
python app_nicegui.py             # starts on port 8502
```

L'app est accessible sur http://localhost:8502.

---

## Deployment (OVH VPS)

### Infrastructure

```
VPS OVH Ubuntu 24.04 LTS — 92.222.229.87
├── NiceGUI (ferment.service)    → 127.0.0.1:8502
├── PostgreSQL 16 (local)        → 127.0.0.1:5432
└── Caddy (reverse proxy HTTPS)  → ports 80/443
```

### Connexion SSH

```bash
ssh ubuntu@92.222.229.87
```

### Déployer une mise à jour

```bash
cd /home/ubuntu/app && git pull && sudo systemctl restart ferment
```

### Commandes utiles

```bash
# Statut de l'app
sudo systemctl status ferment

# Logs en temps réel
sudo journalctl -u ferment -f

# Redémarrer l'app
sudo systemctl restart ferment

# Statut Caddy
sudo systemctl status caddy

# Accéder à la base de données
sudo -u postgres psql -d "whole-tomato-leopard"
```

### Variables d'environnement

Fichier : `/home/ubuntu/app/.env`

```bash
DB_HOST=localhost
DB_PORT=5432
DB_DATABASE=whole-tomato-leopard
DB_USERNAME=shark
DB_SSLMODE=disable
BREVO_API_KEY=xkeysib-...
EMAIL_SENDER=hello@symbiose-kefir.fr
EMAIL_SENDER_NAME=Symbiose Kéfir
BASE_URL=https://prod.symbiose-kefir.fr
ENV=production
RESET_TTL_MINUTES=60
NICEGUI_SECRET=...
ALLOWED_TENANTS=Symbiose Kéfir
```

Après modification du `.env` :
```bash
sudo systemctl restart ferment
```

### Migrations DB

```bash
sudo cp /home/ubuntu/app/db/migrate.sql /tmp/migrate.sql
sudo -u postgres psql -d "whole-tomato-leopard" -f /tmp/migrate.sql
```

### Workflow de développement

```bash
# Créer une branche feature
git checkout -b feature/nom-de-la-feature

# Développer en local
python app_nicegui.py

# Pousser et déployer
git push origin main
ssh ubuntu@92.222.229.87 "cd /home/ubuntu/app && git pull && sudo systemctl restart ferment"
```

---

## Key Conventions

- **Authentication:** `AuthMiddleware` dans `app_nicegui.py` protège toutes les routes sauf `/login`, `/reset`, `/health` ; cookies HttpOnly + session DB
- **Multi-tenancy:** All data is scoped to `tenant_id`; never mix tenant data
- **DB connections:** `db/conn.py` — pool_size=10, recycle=30min, statement_timeout=60s
- **Business constants:** Centralisées dans `config.yaml` → `common/data.py:get_business_config()`
- **Excel templates:** Located in `assets/` — `Fiche_production.xlsx`, `Grande.xlsx` (7200L), `Petite.xlsx` (5200L), `BL_enlevements_Sofripa.xlsx`
- **Data files:** `data/production.xlsx` and `data/flavor_map.csv` are the main data sources
- **Email:** Use Brevo HTTPS API (`common/email.py`), never SMTP in production

---

## Easy Beer API

**Spec OpenAPI :** découpée par tag dans `docs/easybeer/` — voir `docs/easybeer/INDEX.md`
**Fichiers utiles pour ce projet :** `docs/easybeer/controleur-indicateur.json` + `docs/easybeer/controleur-stock.json`
**Base URL :** `https://api.easybeer.fr`
**Auth :** HTTP Basic (`EASYBEER_API_USER` / `EASYBEER_API_PASS`)
**ID brasserie :** `EASYBEER_ID_BRASSERIE` (valeur production : `2013`)
**Client centralisé :** `common/easybeer/` (package)

### Endpoints utilisés

| Méthode | Endpoint | Usage | Module |
|---------|----------|-------|--------|
| `POST` | `/indicateur/autonomie-stocks/export/excel` | Excel ventes+stock → page Accueil | `common/easybeer/stocks.py` |
| `POST` | `/indicateur/autonomie-stocks` | Autonomie JSON | `common/easybeer/stocks.py` |
| `GET` | `/matiere-premiere/all` | Liste matières premières | `common/easybeer/stocks.py` |
| `POST` | `/indicateur/synthese-consommations-mp` | Consommation MP | `common/easybeer/stocks.py` |
| `GET` | `/produit/all` | Liste produits | `common/easybeer/products.py` |
| `GET` | `/produit/{id}` | Détail produit (recette, étapes) | `common/easybeer/products.py` |
| `POST` | `/brassin` | Créer un brassin | `common/easybeer/brassins.py` |
| `GET` | `/brassin/{id}` | Détail brassin | `common/easybeer/brassins.py` |
| `POST` | `/brassin/{id}/fichier` | Upload fiche production | `common/easybeer/brassins.py` |
| `GET` | `/conditionnement/planification/matrice` | Matrice conditionnement | `common/easybeer/conditioning.py` |
| `POST` | `/conditionnement/planification` | Planifier conditionnement | `common/easybeer/conditioning.py` |
| `GET` | `/materiel/all` | Matériels (cuves) | `common/easybeer/stocks.py` |
| `GET` | `/entrepot/all` | Entrepôts | `common/easybeer/stocks.py` |

### Format payload (CRITIQUE)

Tous les endpoints `POST /indicateur/*` utilisent le schéma `ModeleIndicateur` :

```json
{
  "idBrasserie": 2013,
  "periode": {
    "dateDebut": "2026-01-22T00:00:00.000Z",
    "dateFin":   "2026-02-22T23:59:59.999Z",
    "type":      "PERIODE_LIBRE"
  }
}
```

> ⚠️ **Gotchas connus :**
> - `periode.type = "PERIODE_LIBRE"` est **obligatoire** — sans lui → 500
> - Les endpoints JSON `/indicateur/*` requièrent `?forceRefresh=false` en query param → 500 sinon
> - L'endpoint `/export/excel` n'a pas besoin de `forceRefresh`

### Schémas clés (réponses)

**`ModeleAutonomie`** (autonomie-stocks) :
```json
{
  "produits": [
    { "libelle": "Kéfir Original", "autonomie": 28.5, "quantiteVirtuelle": 1150, "volume": 4.0 }
  ]
}
```

**`ModeleMatierePremiere`** (matieres-premieres/all) :
```json
{
  "idMatierePremiere": 42, "libelle": "Carton 12×33cl",
  "quantiteVirtuelle": 1200.0, "seuilBas": 500.0,
  "type": { "code": "CONDITIONNEMENT" },
  "unite": { "symbole": "u" }
}
```

**`ModeleSyntheseConsoMP`** (synthese-consommations-mp) :
```json
{
  "syntheseConditionnement": {
    "elements": [
      { "idMatierePremiere": 42, "libelle": "Carton 12×33cl", "quantite": 1500.0 }
    ]
  }
}
```

### Calcul durée de stock composants
```
durée (jours) = quantiteVirtuelle / (quantite_consommée_sur_période / nb_jours_fenêtre)
```

---

## Dependencies (Key)

```
nicegui              # UI framework (Quasar/Vue3 + FastAPI)
pandas, numpy        # Data manipulation
openpyxl, xlrd       # Excel read/write
fpdf2                # PDF generation
pillow               # Image processing
requests             # HTTP client (EasyBeer API)
SQLAlchemy, psycopg2-binary   # Database
python-dotenv        # Environment loading
pyyaml               # Config parsing
```

Dev/Testing:
```
pytest, pytest-cov   # Unit tests + coverage
pip-audit            # Dependency vulnerability scanning
```

Full list: `requirements.txt`
