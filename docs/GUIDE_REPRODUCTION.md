# Guide de reproduction — Application de gestion de production

Ce document explique comment reproduire une application web multi-tenant de gestion de production (type Ferment Station) à partir de zéro. Il décrit les choix techniques, l'architecture et les étapes d'installation, sans divulguer de secrets (clés API, mots de passe, noms de domaine privés, identifiants clients).

---

## 1. Stack technique

### Choix du langage et framework
- **Python 3.11+** — écosystème mature, pandas/openpyxl pour manipuler Excel, génération PDF simple
- **NiceGUI** — framework tout-en-un qui sert à la fois l'UI (Quasar/Vue3) et l'API (FastAPI/Starlette) depuis un seul process Python
  - Pas besoin de split front/back, idéal pour une app métier mono-équipe
  - Alternative : Streamlit (moins flexible), Flask + React (plus complexe)

### Base de données
- **PostgreSQL 16** — relationnel + support JSONB pour payloads flexibles (plans de production sauvegardés, audit)
- **SQLAlchemy + psycopg2** côté Python (pool de connexions, timeouts configurables)

### Reverse proxy & HTTPS
- **Caddy** — HTTPS automatique via Let's Encrypt, config ultra-simple (2 lignes)
- Alternative : Nginx + certbot (plus verbeux)

### Hébergement
- **VPS Linux (Ubuntu 24.04 LTS)** chez un hébergeur au choix (OVH, Hetzner, Scaleway…)
- App gérée par **systemd** (redémarrage auto, logs via `journalctl`)

### Intégrations externes
- **API ERP métier** (dans notre cas EasyBeer, adaptable à tout ERP REST) — client HTTP centralisé avec retry
- **Brevo / Sendgrid / autre** — email transactionnel via API HTTPS (jamais SMTP en prod)
- **Claude API (Anthropic)** — génération assistée de contenus (emails, résumés)

### Dépendances Python clés
```
nicegui
pandas, numpy, openpyxl, xlrd
fpdf2, pypdf, pillow
requests, tenacity        # HTTP + retry exponentiel
SQLAlchemy, psycopg2-binary
python-dotenv, pyyaml, python-dateutil
anthropic                 # optionnel — IA
ruff, pytest, pip-audit   # dev
```

---

## 2. Architecture en couches

L'app respecte une séparation stricte **UI / domaine / accès données**, vérifiée par des tests automatiques.

```
app_main.py               # Entry point : serveur NiceGUI, auth middleware
pages/                    # UI NiceGUI (décorateurs @ui.page) — pas de logique métier
common/services/          # Logique métier pure — no UI, thread-safe, testable
common/                   # Utilitaires partagés (auth, email, storage, client ERP)
core/                     # Algorithmes (optimiseur, parsing)
db/                       # Couche d'accès données (migrations + conn pool)
data/                     # Fichiers de données (Excel, CSV)
assets/                   # Templates, images
scripts/                  # Outils CLI
tests/                    # Tests unitaires
```

### Règle d'or : le sens de dépendance
- `pages/` importe `services/` et `common/` — jamais l'inverse
- `services/` n'importe **jamais** `nicegui` (pas de `ui.notify`, pas de `app.storage`)
- `db/` ne connaît ni NiceGUI ni le métier

Mettre en place un test d'architecture (simple scan AST) qui bloque à la CI si ces règles sont violées. Exemple : un test qui vérifie qu'aucun fichier de `common/services/` n'importe `nicegui`.

---

## 3. Multi-tenancy

### Principe
Toutes les données sont **scopées par `tenant_id`**. Une organisation = un tenant. Les utilisateurs appartiennent à un tenant et ne voient que les données de ce tenant.

### Schéma DB minimal
```sql
tenants(id, name, created_at)
users(id, tenant_id FK, email, password_hash, role)
-- Toute table métier a une colonne tenant_id NOT NULL + index
production_data(id, tenant_id FK, payload JSONB, ...)
```

### Règles
- Chaque requête DB filtre sur `tenant_id` — helper central pour éviter les oublis
- Les sessions portent le `tenant_id` de l'utilisateur connecté
- Variable d'env `ALLOWED_TENANTS` pour whitelister les tenants en prod (defense en profondeur)

---

## 4. Authentification & sécurité

### Stack
- Mots de passe hashés **PBKDF2-SHA256** (stdlib Python, pas besoin de bcrypt)
- Sessions stockées en DB (table `user_sessions`), cookie HttpOnly + Secure + SameSite
- Middleware NiceGUI qui protège toutes les routes sauf `/login`, `/reset/*`, `/health`
- **Lockout anti-bruteforce** : table `login_failures` avec seuils progressifs (5/10/15 échecs)
- **Reset password** via token à usage unique envoyé par email (TTL 60 min)

### Secrets
- Secret NiceGUI (signature cookies) : >= 32 chars aléatoires, en variable d'env
- Toutes les clés API en `.env` (jamais commit)
- `.env.sample` dans le repo pour documenter les variables attendues

---

## 5. Configuration

### Séparer constantes métier vs secrets

**`config.yaml`** (commité) — constantes métier :
```yaml
business:
  max_saved_proposals: 6
  default_window_days: 60
  # paramètres de calcul, seuils, prix de référence...

suppliers:
  - name: "Fournisseur A"
    lead_time_days: 14
    min_order_palettes: 10
  # ...
```

**`.env`** (jamais commité) — secrets et infra :
```
DB_HOST, DB_PORT, DB_DATABASE, DB_USERNAME, DB_PASSWORD
API_KEY_XXX
EMAIL_API_KEY
BASE_URL
NICEGUI_SECRET
```

Loader centralisé dans `common/data.py` qui merge defaults + YAML + overrides DB si besoin.

---

## 6. Intégration ERP externe

### Pattern client API centralisé
Créer un package `common/erp/` avec un fichier par domaine (stocks, produits, fournisseurs…) :

```
common/erp/
  __init__.py
  client.py          # auth HTTP Basic, retry via tenacity, timeouts
  stocks.py          # fonctions métier : get_autonomie(), list_matieres_premieres()
  products.py
  suppliers.py
  models.py          # dataclasses typées (from_dict défensif)
```

### Bonnes pratiques apprises
- **Retry exponentiel** (tenacity) sur 5xx et timeouts
- **Timeout HTTP** explicite (10-30s), jamais infini
- **Dataclasses typées** pour parser les réponses avec `from_dict` défensif (tolère les champs manquants)
- **Documenter les gotchas** de l'API tierce dans un fichier dédié (format payload, query params obligatoires…)
- **Découper l'OpenAPI** par tag si la spec est énorme — éviter de charger le fichier complet en contexte

---

## 7. Génération de documents (PDF / Excel)

- **Excel** : `openpyxl` pour lire/écrire, avec **templates** dans `assets/` (fichiers .xlsx préparés à la main, on remplit juste les cellules)
- **PDF** :
  - `fpdf2` pour générer from scratch (factures, feuilles de route)
  - `pypdf` pour manipuler des PDFs existants (merge, split, overlays signatures)
- Séparer dans un sous-package `common/xlsx_fill/` : un fichier par type de document.

---

## 8. Déploiement

### Serveur
```
VPS Ubuntu 24.04
├── App Python (systemd service)  → 127.0.0.1:PORT
├── PostgreSQL 16 (local)         → 127.0.0.1:5432
└── Caddy (HTTPS reverse proxy)   → ports 80/443
```

### Service systemd (`/etc/systemd/system/app.service`)
```ini
[Unit]
Description=Mon app
After=network.target postgresql.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/app
EnvironmentFile=/home/ubuntu/app/.env
ExecStart=/usr/bin/python3 app_main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### Caddyfile (`/etc/caddy/Caddyfile`)
```
mon-domaine.example.com {
    reverse_proxy 127.0.0.1:8502
}
```
HTTPS auto, rien à faire de plus.

### CI/CD GitHub Actions
Workflow sur `push main` :
1. **Lint** (`ruff check`)
2. **Tests** (`pytest`)
3. **Deploy** via SSH : `git pull && sudo systemctl restart app`

Secrets GitHub : clé SSH de déploiement, host, user.

---

## 9. Observabilité & ops

- **Logs** : `sudo journalctl -u app -f` (systemd centralise)
- **Health check** : endpoint `/health` qui répond 200 si DB OK — utile pour monitoring externe (UptimeRobot, BetterStack…)
- **Audit log** : table `audit_log` INSERT fire-and-forget à chaque action sensible (création, modification, suppression)
- **Backup DB** : `pg_dump` quotidien via cron → stockage distant (S3, Backblaze, rsync…)

Documenter dans un **RUNBOOK** les procédures : restart, rollback, restore backup, diagnostic panne courante.

---

## 10. Workflow de développement

### Outils
- **Ruff** pour lint + format (remplace flake8 + black)
- **pytest** pour les tests, avec `pytest-cov` pour la couverture
- **pip-audit** dans la CI pour détecter les vulnérabilités de dépendances

### Slash commands / skills (si vous utilisez un assistant IA type Claude Code)
Automatiser les tâches répétées via des commandes projet :
- Migrer un endpoint vers un helper commun
- Extraire de la logique métier d'une page vers un service
- Ajouter une dataclass typée à partir d'un schéma JSON

### Hook pre-push
Script local qui lance les tests d'architecture avant chaque `git push` — bloque les régressions de couches.

---

## 11. Étapes concrètes pour reproduire

1. **Louer un VPS** (Ubuntu 24.04, ≥ 2 Go RAM, ~5€/mois suffit pour démarrer)
2. **Installer les prérequis** :
   ```bash
   sudo apt update && sudo apt install -y python3 python3-pip postgresql caddy git
   ```
3. **Créer la DB** :
   ```bash
   sudo -u postgres createuser myapp
   sudo -u postgres createdb myapp_db -O myapp
   ```
4. **Cloner le repo**, créer `.env` à partir de `.env.sample`
5. **Installer les deps** : `pip install -r requirements.txt`
6. **Lancer les migrations** : `python scripts/app_bootstrap.py`
7. **Créer le service systemd** (cf. section 8)
8. **Configurer Caddy** avec votre domaine (pointer le DNS vers l'IP du VPS avant)
9. **Lancer** : `sudo systemctl enable --now app caddy`
10. **Configurer GitHub Actions** pour le déploiement auto

---

## 12. Ce qu'il faut prévoir en plus

- **Tests** : au moins les algos métier critiques (ici l'optimiseur de planning) — pas besoin de 100% de couverture, cibler les parties à forte valeur
- **Documentation** : un `CLAUDE.md` / `CONTRIBUTING.md` à la racine qui explique la structure et les conventions à un nouveau contributeur (humain ou IA)
- **Migrations DB versionnées** : un fichier `migrate.sql` ou Alembic, exécutable de manière idempotente
- **Rate limiting** sur les endpoints sensibles (login, reset) si l'app est exposée publiquement
- **CSP headers** via Caddy pour durcir le frontend

---

## Résumé : ce qui rend cette archi solide

- **Un seul process Python** (NiceGUI) → simplicité opérationnelle
- **Couches strictes** vérifiées par la CI → maintenabilité long terme
- **Multi-tenancy par `tenant_id`** → prêt à accueillir plusieurs clients
- **Config YAML vs secrets .env** → pas de leaks dans Git
- **Déploiement automatisé** (push = deploy) → pas de bottleneck humain
- **Intégrations externes encapsulées** (package par API tierce) → isolable et testable

Pour toute question spécifique à l'implémentation, se référer à l'`ARCHITECTURE.md` et au `RUNBOOK.md` du projet.
