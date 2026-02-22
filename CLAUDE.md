# CLAUDE.md — Ferment Station

## Project Overview

Ferment Station is a multi-tenant Streamlit web application for fermentation production management. It handles production planning, optimization, harvest sheet generation (PDF/Excel), purchase management, and email notifications.

**Deployment:** OVH VPS (migré depuis Kinsta/Sevalla en février 2026)
**Serveur:** `vps-7ac853de.vps.ovh.net` — IP `92.222.229.87`
**URL:** https://prod.symbiose-kefir.fr
**Language:** Python 3.11+
**Framework:** Streamlit
**Database:** PostgreSQL 16 (local sur le VPS)
**Reverse proxy:** Caddy (HTTPS automatique via Let's Encrypt)
**Email:** Brevo (compte Symbiose Kéfir — `hello@symbiose-kefir.fr`)

---

## Architecture

```
app.py                  # Entry point — auth routing
pages/                  # Streamlit pages (numbered for ordering)
common/                 # Shared utilities
core/                   # Business logic / algorithms
db/                     # Database layer
data/                   # Data files (Excel, CSV)
assets/                 # Templates, images, signatures
scripts/                # CLI utilities
ops/                    # Ops config (env.sample)
docs/                   # RUNBOOK.md, DEPLOYMENT_NOTES.md
```

### Pages

| File | Purpose |
|------|---------|
| `00_Auth.py` | Login / signup / password reset |
| `01_Accueil.py` | Home — file upload |
| `02_Production.py` | Production planning |
| `03_Optimisation.py` | Loss optimization |
| `04_Fiche_de_ramasse.py` | Harvest/collection sheet + PDF export |
| `05_Achats_conditionnements.py` | Purchases & packaging |
| `06_Reset_password.py` | Password reset flow |
| `98_Run_Migration.py` | Manual DB migration runner |
| `99_Debug.py` | Debug utilities |

### Common Modules

| File | Purpose |
|------|---------|
| `auth.py` | User/tenant management, PBKDF2 hashing |
| `session.py` | Session state, auth guards, sidebar nav |
| `design.py` | UI theme and custom components |
| `data.py` | Config and file path management |
| `email.py` | Brevo email API integration |
| `storage.py` | DB-backed snapshot storage |
| `proposals.py` | Production proposal logic |
| `auth_reset.py` | Password reset token handling |
| `xlsx_fill.py` | Excel template filling (largest file, 909 lines) |

---

## Database

PostgreSQL with 4 tables:

- **tenants** — organization isolation
- **users** — per-tenant accounts (email, PBKDF2-SHA256 password hash, role)
- **production_proposals** — saved production plans (JSONB payload)
- **password_resets** — one-time reset tokens with expiry

Schema: `db/migrate.sql`
Run migrations: `python scripts/app_bootstrap.py`
Or via UI: page `98_Run_Migration`

---

## Environment Variables

See `ops/env.sample` for full list. Key variables:

```bash
# Database
DB_HOST, DB_PORT, DB_DATABASE, DB_USERNAME, DB_PASSWORD
DB_SSLMODE        # auto-detected for Kinsta internal vs public endpoints

# Email (Brevo)
BREVO_API_KEY
EMAIL_SENDER, EMAIL_SENDER_NAME

# App
BASE_URL
ENV               # production | development
RESET_TTL_MINUTES # default 60

# GitHub integration
GH_REPO, GH_BRANCH, GH_TOKEN
```

Local secrets go in `.streamlit/secrets.toml`.

---

## Running Locally

```bash
pip install -r requirements.txt
python scripts/app_bootstrap.py   # run DB migrations
streamlit run app.py              # starts on port 8501
```

---

## Deployment (OVH VPS)

### Infrastructure

```
VPS OVH Ubuntu 24.04 LTS — 92.222.229.87
├── Streamlit (ferment.service)   → 127.0.0.1:8501
├── PostgreSQL 16 (local)         → 127.0.0.1:5432
└── Caddy (reverse proxy HTTPS)   → ports 80/443
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
streamlit run app.py

# Pousser et déployer
git push origin main
ssh ubuntu@92.222.229.87 "cd /home/ubuntu/app && git pull && sudo systemctl restart ferment"
```

---

## Key Conventions

- **Authentication:** All pages check `is_authenticated()` from `common/session.py`; unauthenticated users are redirected to `00_Auth.py`
- **Multi-tenancy:** All data is scoped to `tenant_id`; never mix tenant data
- **DB connections:** `db/conn.py` — sur OVH, `DB_SSLMODE=disable` car PostgreSQL est local
- **Excel templates:** Located in `assets/` — `Grande.xlsx` (7000L), `Petite.xlsx` (5000L), `BL_enlevements_Sofripa.xlsx`
- **Data files:** `data/production.xlsx` and `data/flavor_map.csv` are the main data sources
- **Email:** Use Brevo API (not SMTP directly in production); SMTP config in secrets is for local dev only

---

## Easy Beer API

**Spec OpenAPI :** découpée par tag dans `docs/easybeer/` — voir `docs/easybeer/INDEX.md`
**Fichiers utiles pour ce projet :** `docs/easybeer/controleur-indicateur.json` + `docs/easybeer/controleur-stock.json`
**Base URL :** `https://api.easybeer.fr`
**Auth :** HTTP Basic (`EASYBEER_API_USER` / `EASYBEER_API_PASS`)
**ID brasserie :** `EASYBEER_ID_BRASSERIE` (valeur production : `2013`)
**Client centralisé :** `common/easybeer.py`

### Endpoints utilisés

| Méthode | Endpoint | Usage | Fichier |
|---------|----------|-------|---------|
| `POST` | `/indicateur/autonomie-stocks/export/excel` | Excel ventes+stock → page Accueil | `01_Accueil.py` |
| `POST` | `/indicateur/autonomie-stocks` | JSON autonomie (jours de stock) produits finis | `05_Achats_conditionnements.py` |
| `GET`  | `/stock/matieres-premieres/all` | Stock tous composants (MP, conditionnements) | `05_Achats_conditionnements.py` |
| `POST` | `/indicateur/synthese-consommations-mp` | Consommation MP sur période | `05_Achats_conditionnements.py` |

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
streamlit
sqlalchemy, psycopg2-binary
pandas, numpy, openpyxl, xlrd
reportlab, fpdf2, pdfplumber
Pillow
PyGithub
sib-api-v3-sdk    # Brevo
pyyaml
```

Full list: `requirements.txt`
