# Plan de migration Streamlit → NiceGUI

## Principe

Migrer les 6 pages Streamlit vers NiceGUI en **réutilisant 100% de la logique métier** existante (`common/`, `core/`, `db/`). Seule la couche UI change.

---

## Structure cible

```
app_nicegui.py              # Point d'entrée NiceGUI (remplace app.py)
ui/
├── theme.py                # Couleurs, layout partagé, composants réutilisables
├── auth.py                 # Pages login / signup / reset password
├── accueil.py              # Page Accueil (upload fichier)
├── production.py           # Page Production
├── optimisation.py         # Page Optimisation
└── ramasse.py              # Page Fiche de ramasse
common/                     # INCHANGÉ — logique métier
core/                       # INCHANGÉ
db/                         # INCHANGÉ
assets/                     # INCHANGÉ
```

---

## Charte couleurs Ferment Station

```python
COLORS = {
    "bg":       "#F7F4EF",   # Fond crème chaud
    "ink":      "#2D2A26",   # Texte brun foncé
    "green":    "#2F7D5A",   # Vert Symbiose — couleur primaire
    "sage":     "#8BAA8B",   # Vert sauge — secondaire
    "lemon":    "#EEDC5B",   # Jaune citron — accents/highlights
    "card":     "#FFFFFF",   # Fond cartes
    "orange":   "#F57C00",   # Orange Niko — accent secondaire
    "error":    "#D32F2F",   # Rouge erreur
    "success":  "#2E7D32",   # Vert succès
}
```

---

## Étapes de migration (par ordre)

### Étape 1 — Fondations (`ui/theme.py` + `app_nicegui.py`)

- Layout partagé : header avec logo + nom utilisateur, drawer navigation, footer
- Middleware auth : vérifie `app.storage.user` sur chaque route protégée
- Composants réutilisables : `kpi_card()`, `section_title()`, `page_layout()` context manager
- Thème Quasar avec les couleurs Ferment Station
- Port 8501, `show=False`, config systemd

### Étape 2 — Auth (`ui/auth.py`)

- Login / signup / mot de passe oublié
- Réutilise `common/auth.py` (PBKDF2, multi-tenant)
- `app.storage.user` pour la session (persiste côté serveur)
- Redirect vers `/` après login

### Étape 3 — Fiche de ramasse (`ui/ramasse.py`) — page prioritaire

- AG Grid éditable (cartons, date ramasse souhaitée)
- Colonnes calculées : palettes et poids (valueGetter JS côté client)
- KPI cards avec icônes
- Sélection brassins (multiselect avec chips)
- Boutons PDF + Email
- Réutilise : `common/ramasse.py`, `common/easybeer.py`, `common/xlsx_fill.py`, `common/email.py`

### Étape 4 — Accueil (`ui/accueil.py`)

- Upload fichier Excel (drag & drop natif NiceGUI)
- Affichage données production.xlsx
- Réutilise : `common/data.py`, EasyBeer export Excel

### Étape 5 — Production (`ui/production.py`)

- Planning production avec tableaux
- Création brassins EasyBeer
- Réutilise : `common/proposals.py`, `common/easybeer.py`

### Étape 6 — Optimisation (`ui/optimisation.py`)

- Optimisation pertes
- Réutilise : `core/` algorithmes

---

## Déploiement

- Même VPS OVH, même Caddy, même PostgreSQL
- Nouveau service systemd `ferment-ng.service` sur port 8503 pendant la migration
- Bascule finale : rediriger Caddy vers le nouveau port
- Rollback facile : revenir sur Streamlit si problème

---

## Ce qui ne change PAS

- `common/` — toute la logique métier (easybeer, ramasse, auth, email, xlsx_fill…)
- `core/` — algorithmes d'optimisation
- `db/` — schéma PostgreSQL, connexions, migrations
- `data/` — fichiers Excel, CSV
- `assets/` — templates, logos, signatures
- Variables d'environnement (.env)
