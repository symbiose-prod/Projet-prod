# Contribuer à Projet-prod (backend Ferment Station)

Bienvenue 👋 Ce guide explique **comment contribuer sans rien casser**.
Lis-le en entier avant ta première PR — surtout les sections ⚠️.

## 🔑 Règle d'or

> **Tu ne pousses JAMAIS sur `main`. Tu ne touches JAMAIS à la production.**

Tout passe par une **Pull Request** relue et mergée par le mainteneur
(@nicolaspradignac-lang). Un push sur `main` déclenche un **déploiement
automatique en prod** (vraie base de données, données alimentaires
réglementaires) : c'est réservé au mainteneur.

## 🍴 Workflow : fork → branche → PR

On travaille en **fork** (tu n'as pas accès en écriture au repo principal).

1. **Forke** le repo (bouton *Fork* en haut à droite sur GitHub).
2. Clone ton fork :
   ```bash
   git clone git@github.com:<ton-compte>/Projet-prod.git
   cd Projet-prod
   git remote add upstream git@github.com:symbiose-prod/Projet-prod.git
   ```
3. Crée une branche par sujet (jamais de travail directement sur `main`) :
   ```bash
   git checkout -b feat/ma-fonctionnalite
   ```
4. Code, commit, push **sur ton fork**, puis ouvre une **PR vers
   `symbiose-prod/Projet-prod:main`**.
5. Garde ton fork à jour :
   ```bash
   git fetch upstream && git rebase upstream/main
   ```

Fais des **petites PR** ciblées (plus faciles et plus rapides à relire
qu'une grosse).

## 🧪 Avant CHAQUE push : lint + tests

La CI bloque le merge si ça échoue. Lance-les en local **avant** de pousser
(ça t'évite des allers-retours) :

```bash
ruff check .       # lint — doit être clean
pytest tests/      # tests — tout doit passer
```

Si tu ajoutes du code, **ajoute des tests**. Si tu corriges un bug, ajoute
un test qui échouait avant le fix.

## 🏗 Lancer en local

- Python **3.11**, environnement virtuel :
  ```bash
  python3.11 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  ```
- **Base de données** : utilise une **Postgres locale** (jamais la prod).
  Demande au mainteneur un jeu de données de test / le schéma (`db/migrate.sql`).
- **Variables d'environnement** : copie `.env.example` (si présent) vers
  `.env` et remplis avec des valeurs **locales**. Ne demande jamais les
  identifiants de prod.

## 🚦 Tester « en conditions réelles » → `staging`, pas prod

Il existe un environnement **staging** séparé (branche `staging`,
service `ferment-staging`). Si un changement doit être validé sur un
serveur live, c'est le mainteneur qui le déploie sur staging. **La prod
n'est jamais une cible de test.**

## ⚠️ Changements sensibles (relecture renforcée)

Ces fichiers/chemins demandent une attention particulière — préviens le
mainteneur dans la PR :

- **`db/migrate.sql`** — migrations DB. Règles strictes :
  - **idempotent** (`IF NOT EXISTS`, `DROP ... IF EXISTS` avant `ADD`),
  - **additif** : on ajoute des colonnes/tables, on ne supprime pas et on
    ne fait jamais de `DELETE`/`DROP TABLE`/`TRUNCATE` destructeur sur des
    données existantes,
  - testé sur une base locale d'abord.
- **`common/auth*.py`, `common/permissions.py`** — authentification, rôles,
  isolation multi-tenant. Une erreur ici = faille de sécurité.
- **`.github/workflows/`** — la CI/CD. Ne modifie pas ces fichiers sans
  validation explicite (ils ont accès aux secrets de déploiement).
- Tout ce qui touche au **scoping `tenant_id`** : chaque requête doit
  rester filtrée par tenant.

## 🔒 Secrets & sécurité

- **Ne commite jamais** de secret, mot de passe, clé, `.env`, token.
- Si tu crois avoir commité un secret par erreur : préviens **immédiatement**
  le mainteneur (il faudra le révoquer).
- Ne stocke pas de données réelles de prod sur ta machine.

## 📝 Convention de commits

Format court type *Conventional Commits* :

```
feat(scope): ajoute X
fix(scope): corrige Y
chore(scope): tâche d'entretien
docs(scope): documentation
test(scope): tests
```

Messages en français, à l'impératif, qui expliquent le **pourquoi** quand
ce n'est pas évident.

## ✅ Checklist avant d'ouvrir une PR

- [ ] `ruff check .` est clean
- [ ] `pytest tests/` passe
- [ ] J'ai ajouté/maj les tests concernés
- [ ] Aucun secret ni donnée prod dans le diff
- [ ] Les changements sensibles (migrations, auth) sont signalés dans la PR
- [ ] La PR est petite et fait une seule chose

Merci, et bon code ! 🍶
