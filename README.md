# Ferment Station

Application web multi-tenant de gestion de production de fermentation (kéfir, infusions).

**Framework:** NiceGUI (Python 3.11+)
**Base de données:** PostgreSQL 16
**Déploiement:** OVH VPS — https://prod.symbiose-kefir.fr

## Fonctionnalités

- **Planning de production** — algorithme d'optimisation (égalisation des jours d'épuisement)
- **Intégration EasyBeer** — création automatique de brassins, planification conditionnement
- **Fiche de ramasse** — génération PDF/Excel pour les enlèvements
- **Fiche de production** — remplissage automatique du template Excel
- **Multi-tenancy** — isolation complète des données par organisation
- **Authentification** — PBKDF2-SHA256, sessions persistantes, protection brute-force

## Lancer en local

```bash
# 1. Installer les dépendances
pip install -r requirements.txt

# 2. Configurer l'environnement
cp ops/env.sample .env
# Éditer .env avec vos valeurs (DB, Brevo, EasyBeer, NICEGUI_SECRET)

# 3. Lancer les migrations DB
python scripts/app_bootstrap.py

# 4. Démarrer l'application
python app_nicegui.py
# → http://localhost:8502
```

## Structure du projet

```
app_nicegui.py          # Point d'entrée + middleware auth
ui/                     # Pages NiceGUI (auth, accueil, production, ramasse)
common/                 # Utilitaires partagés (auth, email, storage, EasyBeer)
core/                   # Logique métier / algorithmes (optimiseur)
db/                     # Couche base de données (SQLAlchemy)
config.yaml             # Constantes métier centralisées
```

## Déploiement (OVH VPS)

```bash
ssh ubuntu@92.222.229.87
cd /home/ubuntu/app && git pull && sudo systemctl restart ferment
```

Voir `CLAUDE.md` pour la documentation complète (architecture, conventions, API EasyBeer).
