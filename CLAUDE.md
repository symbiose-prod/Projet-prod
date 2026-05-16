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

## Claude Code — documents et automatismes clés

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — couches (transport / domaine / UI), règles, patterns, checklists "nouvel endpoint" / "nouveau service" / "nouvelle page".
- **[docs/RUNBOOK.md](docs/RUNBOOK.md)** — ops + backup/restore + troubleshooting.
- **[docs/ETIQUETTES_PALETTE.md](docs/ETIQUETTES_PALETTE.md)** — feature étiquettes palette (scan iPhone/iPad → décodage GS1-128 → PDF AirPrint) : flow, format GS1, gotchas, dépendances Ghostscript/treepoem/zxing-cpp, table historique.
- **[tests/test_architecture_layers.py](tests/test_architecture_layers.py)** — 4 guards CI qui bloquent les régressions de couches (lancé à chaque `pytest`).
- **Slash commands projet** (`.claude/commands/`) — workflows répétables :
  - `/project:migrate-endpoint <fonction>` — migrer un endpoint EB vers `execute_endpoint`.
  - `/project:extract-service <path:fonction>` — extraire de la logique métier depuis une page.
  - `/project:add-typed-model <nom>` — ajouter une dataclass typée avec `from_dict` défensif.
- **Hook pre-push** (`.claude/hooks/pre-push-verify.sh`, wiré via `.claude/settings.json`) — lance les guards d'architecture avant chaque `git push`, bloque en cas de violation.

---

## Architecture

```
app_nicegui.py          # Entry point — NiceGUI, auth middleware, health check
pages/                  # NiceGUI pages (@ui.page decorators)
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

### Pages (pages/)

| File | Route | Purpose |
|------|-------|---------|
| `pages/auth.py` | `/login`, `/reset/{token}` | Login, signup, password reset |
| `pages/accueil.py` | `/accueil` | Home — file upload, EasyBeer sync |
| `pages/production.py` | `/production` | Production planning + EasyBeer brassin creation |
| `pages/etiquettes_palette.py` | `/etiquettes-palette` | Scan carton iPhone/iPad → PDF étiquette palette (GS1-128). La génération du PDF passe par `etiquette_palette_service.generate_and_save_palette_label()` — fonction partagée avec l'app iOS mobile. Voir [docs/ETIQUETTES_PALETTE.md](docs/ETIQUETTES_PALETTE.md) et section "Mobile API" plus bas. |
| `pages/chargement_camion.py` | `/chargement-camion` | Scan SSCC palette → BL prévisionnel / définitif (workflow ramasse unique) |
| `pages/historique_ramasses.py` | `/historique-ramasses` | Historique complet + corbeille + export CSV |
| `pages/stocks.py` | `/stocks` | Stock autonomy by supplier, order suggestions |
| `pages/ressources.py` | `/ressources` | Supplier ordering constraints editor (lead time, min pallets) |
| `pages/theme.py` | — | Design system, page layout, custom components |
| `common/services/production_service.py` | — | Service domaine : calculs production (optimiseur, split cuves) — no UI, thread-safe |
| `common/services/etiquette_palette_service.py` | — | Service domaine étiquettes palette : parsing GS1-128, lookup matrice EB, classify, save/list/purge historique |
| `pages/_production_easybeer.py` | — | EasyBeer brassin creation section |
| `common/services/stocks_service.py` | — | Service domaine : autonomie stocks + BOM + propositions commande (no UI) |

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
| `common/audit.py` | Audit trail fire-and-forget (INSERT to `audit_log` table) |
| `common/supplier_config.py` | CRUD config fournisseurs (DB overrides merged on `config.yaml`) |
| `common/brassin_builder.py` | Brassin code generation, payload building, recipe scaling |
| `common/ai.py` | Claude/Anthropic client for supplier order email generation |
| `common/xlsx_fill/` | Excel/PDF generation package (fiche production, BL, bon de commande) |
| `common/etiquette_palette_pdf.py` | Génération PDF étiquette palette 102×152 mm (GS1-128 via `treepoem` + Ghostscript, layout fpdf2 inspiré du modèle PPTX interne) |
| `common/easybeer/` | EasyBeer API client package (stocks, brassins, recipes, conditioning, suppliers, history) |
| `common/mobile_auth.py` | Auth Bearer pour l'app iOS — table `mobile_api_tokens` (TTL 90j, SHA-256, révocation) |
| `common/mobile_v1.py` | Routes `/api/v1/*` pour l'app iOS — adaptateur HTTP qui délègue aux services (voir tableau dédié ci-dessous) |

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

PostgreSQL 16 with 9 tables principales :

- **tenants** — organization isolation (multi-tenancy)
- **users** — per-tenant accounts (email, PBKDF2-SHA256 password hash, role)
- **production_proposals** — saved production plans (JSONB payload)
- **password_resets** — one-time reset tokens with expiry
- **user_sessions** — remember-me tokens (30 days, hashed)
- **login_failures** — brute-force lockout tracking (persistent)
- **audit_log** — action audit trail (tenant_id, user_email, action, details JSONB)
- **supplier_configs** — editable supplier ordering constraints per tenant (JSONB, UNIQUE per tenant+supplier)
- **etiquette_palette_history** — audit + réimpression des étiquettes palette générées (purge auto à 500 par tenant)

Schema: `db/migrate.sql`
Run migrations: `python scripts/app_bootstrap.py`

---

## Configuration

### config.yaml (business constants — version-controlled)

Toutes les constantes métier sont centralisées dans `config.yaml` :

- **data_files** : chemins vers `data/production.xlsx`, `data/flavor_map.csv`, `assets/`
- **business** :
  - Configurations cuves (7200L : perte 800L ; 5200L : perte 400L)
  - DDM par défaut (365 jours), prix de référence (400 €/hL)
  - Limites (max 6 proposals sauvegardées, fenêtre par défaut 60 jours)
- **stocks** : configuration des fournisseurs (11 entrées) avec pour chacun :
  - Groupe (Contenants, MP, Emballages), délai de livraison (jours)
  - Minimum de commande (palettes, kg, unités), références palette
  - Exemples : Verallia (14j, 10 palettes min), Cristalco (21j, 4 palettes de 900kg), AWK (42j, 200k capsules)
- **security** : longueur min mot de passe (10), seuils lockout (5/10/15 échecs)

Voir `common/data.py` → `get_business_config()` / `get_stocks_config()` pour le chargement avec valeurs par défaut.

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

# Agent imprimante Brother QL-1110NWBc (étiquettes palette)
# Si non défini, l'API d'agent répond 503. Le même token doit être posé
# dans le .env de l'agent Windows (scripts/windows_print_agent/.env).
PRINT_AGENT_TOKEN           # bearer token (openssl rand -hex 32)
PRINT_AGENT_TENANT_ID       # UUID tenant Symbiose (table tenants)
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

Le déploiement est **automatique via GitHub Actions** :
`push main` → lint (ruff) → tests (pytest) → deploy SSH sur le VPS.

Pour un déploiement manuel :
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
# Développer en local
python app_nicegui.py

# Pousser — le CI lint + test + deploy automatiquement
git push origin main
```

---

## Key Conventions

- **Authentication:** `AuthMiddleware` dans `app_nicegui.py` protège toutes les routes sauf `/login`, `/reset`, `/health` ; cookies HttpOnly + session DB
- **Multi-tenancy:** All data is scoped to `tenant_id`; never mix tenant data
- **DB connections:** `db/conn.py` — pool_size=10, recycle=30min, statement_timeout=60s
- **Business constants:** Centralisées dans `config.yaml` → `common/data.py:get_business_config()`
- **Excel templates:** Located in `assets/` — `Fiche_production.xlsx`, `Grande.xlsx` (7200L), `Petite.xlsx` (5200L), `BL_enlevements_Sofripa.xlsx`
- **Data files:** `data/production.xlsx`, `data/flavor_map.csv`, `data/regles_cuves.csv` (tank ruler interpolation), `data/destinataires.json` (harvest pickup recipients)
- **Email:** Use Brevo HTTPS API (`common/email.py`), never SMTP in production
- **AI:** Claude API via `common/ai.py` for supplier order email generation (Anthropic SDK)

---

## Mobile API (app iOS Ferment station)

App native iOS dans `/Users/nicolaspradignac/Documents/Ferment station/` (Swift/SwiftUI 26+) qui se branche sur ce backend via des endpoints REST `/api/v1/*`. Auth Bearer token, distincte des cookies session NiceGUI du web.

### Architecture en 1 schéma

```
iPhone (SwiftUI)                  VPS OVH (Python NiceGUI)
─────────────────                 ──────────────────────────────────────────────
CameraScannerView                 app_nicegui.py
  └─ AVFoundation                   ├── PUBLIC_PATHS = {..., "/api/v1/"}
       décode GS1-128 →             └── _register_mobile_v1_routes(app)
                                          │
APIClient.swift          ──HTTP──►  common/mobile_v1.py  (adapteur HTTP)
  Bearer <token>                          │  parse body, vérif Bearer, format JSON
                                          ▼
                                    common/services/
                                      etiquette_palette_service.py
                                        - parse_gs1_to_entry
                                        - lookup_product_by_ean
                                        - generate_and_save_palette_label  ←┐
                                        - count_today_and_month             │
                                        - list_today_labels                 │  partagé
                                        - list_recent_labels                │  avec web
                                        - set_label_archived                │  (pages/
                                        - get_history_entry                 │  etiquettes
                                      sscc_service.py                       │  _palette.py)
                                        - generate_sscc                    ─┘
                                        - list_sscc_log
                                      mobile_auth.py
                                        - create_mobile_token / verify / revoke
                                          │
                                          ▼
                                    PostgreSQL (mobile_api_tokens, sscc_log,
                                                etiquette_palette_history, ...)
```

### Endpoints `/api/v1/*` (voir `common/mobile_v1.py`)

| Méthode | Route | Auth | Rôle |
|---|---|---|---|
| POST | `/api/v1/auth/login` | — | email+password → token Bearer + infos user |
| POST | `/api/v1/auth/logout` | Bearer | révoque le token courant |
| POST | `/api/v1/decode-gs1` | Bearer | décode string GS1-128 + lookup produit + image + layout palette |
| POST | `/api/v1/print-palette` | Bearer | génère SSCC + PDF étiquette palette + audit |
| POST | `/api/v1/labels/{id}/archive` | Bearer | toggle archive (réversible, tenant-scoped) |
| POST | `/api/v1/labels/{id}/reprint` | Bearer | régénère le PDF d'une étiquette historisée |
| GET | `/api/v1/today-labels` | Bearer | étiquettes du jour (archivées incluses) |
| GET | `/api/v1/home-summary` | Bearer | compteurs jour/mois + 20 derniers scans |
| GET | `/api/v1/sscc-log` | Bearer + admin | journal SSCC complet (filtres date/lot) |

### Code de génération unifié web + mobile

⚠️ **`etiquette_palette_service.generate_and_save_palette_label()` est l'unique source de vérité** pour le pipeline `SSCC → PDF → audit history → purge`. Appelée à 2 endroits :
- `pages/etiquettes_palette.py:_do_generate` (web — passe tous les champs produit depuis `LabelEntry`)
- `common/mobile_v1.py:_v1_print_palette` (mobile — passe juste ean/lot/ddm, le service fait `lookup_product_by_ean`)

Toute évolution du flux (nouveau champ context, gestion `bio`, nouveau format Domino…) doit se faire UNIQUEMENT dans cette fonction. Pas de divergence possible.

### Auth Bearer mobile

- Token créé via `POST /api/v1/auth/login` (90 jours TTL par défaut, hash SHA-256 en DB)
- Storé côté iOS dans le **Keychain iOS** (`KeychainStorage.swift`)
- À chaque requête : header `Authorization: Bearer <token>`
- Vérifié par `mobile_auth.verify_mobile_token` (resolution token → user dict)
- Révoqué via `POST /api/v1/auth/logout` ou via colonne `revoked_at` côté DB
- Le préfixe `/api/v1/` est listé dans `PUBLIC_PATHS` côté `app_nicegui.py` pour bypasser le middleware d'auth web

### Ajouter une route mobile en 5 étapes

1. **Endpoint Python** dans `common/mobile_v1.py` :
   - Définir `async def _v1_xxx(request: Request)`
   - Premier appel : `user = await _resolve_mobile_user(request)` + `if user is None: return _unauthorized()`
   - Déléguer la logique à un service (PAS de SQL inline)
2. **Enregistrer la route** dans `register_routes(app)` du même fichier
3. **Côté iOS** : ajouter une méthode dans `APIClient.swift` (suivre le pattern des existantes)
4. **Côté iOS** : ajouter un modèle `Codable` dans `Models.swift` pour la réponse
5. **CLAUDE.md** : compléter le tableau d'endpoints ci-dessus + ce diagramme si l'architecture change

### Multi-tenant

Tous les endpoints `/api/v1/*` filtrent obligatoirement par `user["tenant_id"]` (du token). Les services qui prennent un `tenant_id` paramètre l'utilisent dans toutes leurs queries. Pas de fuite cross-tenant possible **tant qu'on ne contourne pas** ce pattern.

### Tests à ajouter (priorité)

Actuellement aucun test ne couvre les modules mobile :
- `tests/test_mobile_auth.py` — create/verify/revoke/expired token
- `tests/test_gs1_parsers.py` — `parse_gs1_raw` (format iOS AVFoundation `]C1` + FNC1), `parse_gs1_to_entry` 3 formats
- `tests/test_mobile_api_v1.py` (FastAPI TestClient) — isolation tenant cross-tenant, login 200/401/400
- `tests/test_set_label_archived.py` — toggle/force/cross-tenant

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
| `GET` | `/stock/matieres-premieres/numero-lot/liste/{id}` | Lots MP pour FIFO | `common/easybeer/stocks.py` |
| `POST` | `/indicateur/synthese-consommations-mp` | Consommation MP | `common/easybeer/stocks.py` |
| `GET` | `/produit/all` | Liste produits | `common/easybeer/products.py` |
| `GET` | `/parametres/produit/edition/{id}` | Détail produit (recette, aromatisation) | `common/easybeer/products.py` |
| `POST` | `/brassin/enregistrer` | Créer un brassin | `common/easybeer/brassins.py` |
| `GET` | `/brassin/{id}` | Détail brassin | `common/easybeer/brassins.py` |
| `POST` | `/brassin/upload/{id}` | Upload fiche production | `common/easybeer/brassins.py` |
| `GET` | `/brassin/planification-conditionnement/matrice` | Matrice conditionnement | `common/easybeer/conditioning.py` |
| `POST` | `/conditionnement/planification` | Planifier conditionnement | `common/easybeer/conditioning.py` |
| `GET` | `/parametres/code-barre/matrice` | Matrice codes-barres | `common/easybeer/conditioning.py` |
| `GET` | `/materiel/all` | Matériels (cuves) | `common/easybeer/stocks.py` |
| `GET` | `/entrepot/all` | Entrepôts | `common/easybeer/stocks.py` |
| `GET` | `/fournisseur/all` | Liste fournisseurs | `common/easybeer/suppliers.py` |
| `GET` | `/fournisseur/{id}` | Détail fournisseur (contact, adresse) | `common/easybeer/suppliers.py` |
| `GET` | `/client/all` | Liste clients (paginé) | `common/easybeer/clients.py` |
| `GET` | `/stock/contenant/mouvement/liste/{id}` | Historique mouvements contenants | `common/easybeer/history.py` |
| `GET` | `/stock/matieres-premieres/entree/liste/{id}` | Historique entrées MP | `common/easybeer/history.py` |

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
pypdf                # PDF manipulation (merge, split)
pillow               # Image processing
requests             # HTTP client (EasyBeer API)
tenacity             # Retry with exponential backoff (Brevo, EasyBeer)
SQLAlchemy, psycopg2-binary   # Database
python-dotenv        # Environment loading
python-dateutil      # Date parsing
pyyaml               # Config parsing
anthropic            # Claude AI SDK (supplier order email generation)
treepoem             # GS1-128 (FNC1) via BWIPP — étiquettes palette
zxing-cpp            # Décodage codes-barres serveur — scan iPad/iPhone
```

Dev/Testing:
```
ruff                 # Python linter/formatter
pytest, pytest-cov   # Unit tests + coverage
pip-audit            # Dependency vulnerability scanning
```

**Dépendance système requise (VPS prod) :** `apt install ghostscript` — utilisé par `treepoem` pour générer le GS1-128 via PostScript. Le workflow CI/CD le réinstalle à chaque déploiement.

Full list: `requirements.txt`
