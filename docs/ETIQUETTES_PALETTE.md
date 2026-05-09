# Étiquettes palette — Documentation

**Statut :** vivant. Mis à jour 2026-05-09.
**Public :** développeur (humain ou Claude Code) qui doit modifier, déboguer ou étendre cette feature.

---

## TL;DR — En 30 secondes

L'opérateur scanne le code-barres d'un carton (étiquette imprimée par le Domino) avec la caméra de son iPad/iPhone. Le serveur :

1. Décode le **GS1-128** (zxing-cpp) → extrait `EAN colis + Lot + DLUO`.
2. Cherche le produit dans la **matrice codes-barres EasyBeer** (cache 24 h) → récupère `marque + format + PCB + désignation + goût`.
3. Tout est pré-rempli côté UI. L'opérateur saisit la quantité de caisses.
4. Génère un **PDF d'étiquette palette** (102×152 mm, format Dymo 5XL) avec un nouveau **GS1-128** au format `(02)(15)(10)(37)` — `treepoem` + `Ghostscript`.
5. Imprime via **AirPrint** depuis l'iPad.

Tout l'historique est persisté pour réimpression et audit.

---

## Routes & fichiers

### Page

| Route | Fichier | Auth |
|---|---|---|
| `GET /etiquettes-palette` | [pages/etiquettes_palette.py](../pages/etiquettes_palette.py) | session NiceGUI |
| `POST /api/scan-barcode` | [app_nicegui.py](../app_nicegui.py) (`_api_scan_barcode`) | session NiceGUI |

### Services & helpers

| Fichier | Rôle |
|---|---|
| [common/services/etiquette_palette_service.py](../common/services/etiquette_palette_service.py) | Logique pure (parsing GS1, lookup EB, classify, save/list/purge historique) — sans NiceGUI |
| [common/etiquette_palette_pdf.py](../common/etiquette_palette_pdf.py) | Rendu PDF via `fpdf2` + code-barres via `treepoem` |
| [pages/etiquettes_palette.py](../pages/etiquettes_palette.py) | Page NiceGUI (943 lignes — voir refacto possible plus bas) |

### Tests

| Fichier | Couverture |
|---|---|
| [tests/test_etiquette_palette_service.py](../tests/test_etiquette_palette_service.py) | Logique pure (60 tests) : `compute_case_count`, `build_gs1_128_payload`, `classify_bottle_type`, `extract_label_gout`, `find_entry_by_ean`, `load_label_data_from_sync`, etc. |
| [tests/test_etiquette_palette_e2e.py](../tests/test_etiquette_palette_e2e.py) | Pipeline bout-en-bout (24 tests) : génération GS1 → décodage → PDF, save/list/purge historique avec mock DB, réimpression identique au PDF initial |

**Lancer les tests :** `python3 -m pytest tests/test_etiquette_palette_*.py -q`

---

## Flow utilisateur

```
1.  ┌──────────────────────────────────┐
    │  Scanner un carton               │  ← bouton hero (caméra iOS native)
    └────────────────┬─────────────────┘
                     │ photo HD redimensionnée canvas 1280px JPEG 85 %
                     │ POST /api/scan-barcode (multipart)
                     ▼
2.  ┌──────────────────────────────────┐
    │  Serveur                         │
    │  zxing-cpp → (01)EAN(15)DDM(10)L │  ← parse GS1-128
    │  lookup matrice CB EasyBeer      │  ← cache L2 DB 24 h
    │  → designation + format + ...    │
    └────────────────┬─────────────────┘
                     │ JSON {ean, lot, ddm, product?}
                     │ emitEvent('barcode_scanned', data)
                     ▼
3.  ┌──────────────────────────────────┐
    │  Récap auto-rempli :             │
    │  Photo, Désignation, EAN, Lot,   │
    │  DDM, Format, PCB                │
    └────────────────┬─────────────────┘
                     │ opérateur saisit "palette pleine" + quantité
                     ▼
4.  ┌──────────────────────────────────┐
    │  Bouton "Générer PDF"            │
    │  - Validations (DDM, qty>0)      │
    │  - build_etiquette_palette_pdf   │
    │  - save_label_history (audit)    │
    │  - purge_old_label_history       │
    └────────────────┬─────────────────┘
                     │ PDF téléchargé
                     │ + bouton "Scanner le suivant" affiché
                     ▼
5.  AirPrint → Dymo 5XL Wireless
```

---

## Format GS1-128

### Étiquette **carton** (entrée scan)

```
(01)<GTIN-14 du carton>(15)<DLUO YYMMDD>(10)<lot>
```

Exemple décodé : `(01)03770014427250(15)270511(10)110527`
- `01` = GTIN colis (les 14 digits avec `0` indicateur logistique padding gauche)
- `15` = DLUO (Date Limite d'Utilisation Optimale, AKA DDM/Best Before, format YYMMDD)
- `10` = numéro de lot (variable, max 20 chars alphanumériques)

### Étiquette **palette** (sortie PDF)

```
(02)<GTIN-14 des articles contenus>(15)<DLUO>(10)<lot>(37)<count>
```

Exemple : `(02)03770014427250(15)270511(10)110527(37)96`
- `02` = GTIN des **articles contenus** (= les cartons sur la palette)
- `37` = nombre de cartons (variable jusqu'à 8 digits, on padd à 3)

> **Source** : `common/services/etiquette_palette_service.py:build_gs1_128_payload`. L'asymétrie 01 (entrée) ↔ 02 (sortie) suit la norme GS1 : AI 01 sur unité-consommateur, AI 02 sur unité logistique non commercialisée seule. Voir [docs/etiquette_modele_GS1_France_2015.pdf](#) (manuel pratique GS1 — récupéré une fois lors du dev).

### Encoding & dépendances

- **Génération** (PDF côté serveur) : `treepoem` (wrapper BWIPP via PostScript) — supporte FNC1 strict GS1-128. Nécessite **Ghostscript** (`apt install ghostscript`).
- **Décodage** (image scannée) : `zxing-cpp` (binding C++ ZXing) — robuste, supporte GS1-128, ITF-14, EAN-13, Code 128, QR. Pas de dépendance système.

> **Pourquoi pas `python-barcode` ?** Ne supporte pas FNC1 → impossible de générer un GS1-128 standard. Initialement utilisé puis remplacé.
>
> **Pourquoi pas `html5-qrcode` ou `BarcodeDetector` côté JS ?** Trop fragile sur l'iPhone (résolution caméra basse, focus inégal sur GS1-128 long). On capture une photo HD via `<input capture="environment">` et on décode côté serveur — beaucoup plus fiable.

---

## Données

### Source primaire : matrice codes-barres EasyBeer

`common.easybeer.conditioning.get_code_barre_matrice()` → cache L2 DB 24 h. Format :

```json
{
  "produits": [
    {
      "codesBarres": [
        {
          "code": "23770014427018",
          "modeleProduit": {"idProduit": 42},
          "modeleContenant": {"contenance": 0.33},
          "modeleLot": {"libelle": "Carton de 6"}
        }
      ]
    }
  ]
}
```

Parsé par `common.ramasse.parse_barcode_matrix` puis enrichi par `lookup_product_by_ean` (service étiquette palette) avec :
- libellé via `common.easybeer.products.get_all_products()`
- marque via `determine_brand_from_label()` (« niko » dans le libellé → NIKO, sinon SYMBIOSE)
- bottle_type via `classify_bottle_type()` (33cl / 75cl SAFT / 75cl Eau gazeuse — règles métier internes)
- goût via `extract_label_gout()` (extrait le goût du libellé : `KÉFIR PAMPLEMOUSSE ROSE` → `Pamplemousse Rose`)

### Table `etiquette_palette_history`

Migration : [db/migrate.sql](../db/migrate.sql) (idempotente, déployée auto via CI).

```sql
CREATE TABLE etiquette_palette_history (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  user_email    TEXT,
  ean           TEXT NOT NULL,           -- GTIN colis (carton)
  lot           TEXT NOT NULL,
  ddm           DATE NOT NULL,
  fmt           TEXT NOT NULL,           -- "6x33", "12x33", "6x75", "4x75"
  marque        TEXT NOT NULL,           -- "NIKO" | "SYMBIOSE"
  designation   TEXT,
  gout          TEXT,
  case_count    INTEGER NOT NULL,
  full_pallet   BOOLEAN NOT NULL DEFAULT false,
  n_copies      INTEGER NOT NULL DEFAULT 1,
  pcb           INTEGER NOT NULL DEFAULT 0,
  gtin_uvc      TEXT NOT NULL DEFAULT '',  -- EAN bouteille
  code_interne  TEXT NOT NULL DEFAULT '',  -- ex: "SK-KDF-PECHE-75"
  bio           BOOLEAN NOT NULL DEFAULT true,
  generated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_etiq_pal_tenant_date ON etiquette_palette_history(tenant_id, generated_at DESC);
```

Service :
- `save_label_history(...)` : INSERT (fire-and-forget, ne propage pas les erreurs)
- `list_recent_labels(tenant_id, limit=20)` : SELECT ordonné par `generated_at DESC`
- `purge_old_label_history(tenant_id, keep=500)` : DELETE NOT IN (top N) — appelé après chaque save pour borner la table

### Mapping goût → photo

`assets/image_map.csv` : 2 colonnes `canonical,filename`. Lu par `get_product_image_url()`. Photos servies via `/assets/<file>` (route statique exposée dans `app_nicegui.py`).

---

## Configuration métier

### Layout palette (config.yaml)

```yaml
business:
  palette_layouts:
    "12x33": { layers: 7, per_layer: 18 }   # 126 cartons/palette
    "6x33":  { layers: 7, per_layer: 36 }   # 252
    "6x75":  { layers: 4, per_layer: 24,    # 96 (Verallia)
               overrides: { niko: { layers: 4, per_layer: 21 } } }  # 84 (NIKO SAFT)
    "4x75":  { layers: 4, per_layer: 28 }   # 112
```

Lu par `common.data.get_palette_layouts_config()`. Utilisé par `common.ramasse.get_palette_layout(fmt, label)` pour le calcul `compute_case_count`.

### Variables d'env (rappel)

- `EASYBEER_API_USER` / `EASYBEER_API_PASS` : crédentials EasyBeer (matrice CB)
- `EASYBEER_ID_BRASSERIE` : `2013` en prod
- `BASE_URL` : `https://prod.symbiose-kefir.fr` (utilisé par fetch côté JS pour les URLs absolues)

---

## Comment ajouter / modifier

### Ajouter un goût (avec photo produit)

1. Mettre la photo dans `assets/<CODE>.jpg` (4-5 lettres, ex: `BERG.jpg` pour Bergamote)
2. Ajouter une ligne dans `assets/image_map.csv` :
   ```csv
   Bergamote,BERG.jpg
   ```
3. Vérifier que `extract_label_gout` retourne bien `"Bergamote"` pour un libellé EasyBeer typique (sinon ajuster les regex de strip)

### Ajouter un format de carton (ex: 4×33)

1. Ajouter dans `config.yaml` → `business.palette_layouts."4x33"` avec `layers` et `per_layer`
2. Vérifier `classify_bottle_type` : c'est probablement BOTTLE_33 si volume = 33, déjà OK
3. Tester avec un brassin EasyBeer qui sort du 4×33 → matrice CB EB doit le contenir

### Modifier le layout du PDF

→ [common/etiquette_palette_pdf.py](../common/etiquette_palette_pdf.py) `build_etiquette_palette_pdf()`. Format papier `_LABEL_WIDTH_MM × _LABEL_HEIGHT_MM` (102×152 mm Dymo 5XL).

Pour preview rapide :
```bash
python3 -m common.etiquette_palette_pdf
qlmanage -t -s 800 -o /tmp /tmp/etiquette_palette_sample.pdf
open /tmp/etiquette_palette_sample.pdf.png
```

### Changer les règles de marque/bottle_type

`classify_bottle_type` dans le service ; `determine_brand_from_label` dans `common.easybeer.products`. Tester impérativement les changements via `tests/test_etiquette_palette_service.py:TestClassifyBottleType` + `TestExtractLabelGout`.

---

## Déploiement

CI/CD : `.github/workflows/ci.yml`. Sur push main :
1. Lint (ruff) + tests (pytest)
2. SSH au VPS
3. `git pull && apt install ghostscript && venv/bin/pip install -r requirements.txt`
4. **Migration DB auto** : `psql -d "whole-tomato-leopard" -f /tmp/migrate.sql` (idempotent grâce aux `IF NOT EXISTS` et `ALTER … ADD COLUMN IF NOT EXISTS`)
5. Health-check post-install : `import treepoem` + `import zxingcpp`
6. `systemctl restart ferment`

Si jamais la migration ajoute une nouvelle colonne, **toujours** utiliser `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` pour rester idempotent côté CI.

### Dépendances système (sur le VPS)

- **ghostscript** (`apt install ghostscript`) — requis par treepoem (génération GS1-128)
- (zxing-cpp est en wheel pip, pas de dep système)

---

## Gotchas connus

| Symptôme | Cause | Fix |
|---|---|---|
| `No module named 'barcode'` côté prod | python-barcode pas installé dans le venv ferment | Le workflow CI installe maintenant via `/home/ubuntu/app/venv/bin/pip` (pas system pip). On utilise `treepoem` + `zxing-cpp`, pas `python-barcode`. |
| `'facingMode' should be string or object with exact as key` | `html5-qrcode` (déprécié dans ce projet) | Plus utilisé. On capture via `<input capture="environment">` natif. |
| Bouton "Prendre une photo" inactif sur Chrome iOS | `input.click()` programmatique bloqué quand l'input est `display:none` | Pattern utilisé : `<input>` imbriqué DANS un `<label>` avec `position:absolute; opacity:0; inset:0`. |
| EAN décodé mais auto-sélection pas déclenchée | `dispatchEvent('input')` sur input caché ne traverse pas Vue/Quasar | On utilise `emitEvent('barcode_scanned', data)` (canal WebSocket NiceGUI) + `ui.on('barcode_scanned', handler)` côté Python. |
| Photo trop grosse, latence 4G | iPhone HEIC/HD = 3-5 MB | Resize côté client via canvas → 1280 px max, JPEG 85 % → ~300 KB. |
| Code-barres pas reconnu sur la photo | Cas rare : flou, reflets, code abîmé | Bouton "Saisir l'EAN à la main" en fallback. |
| Le récap garde les anciennes infos après "Scanner le suivant" | `_reset_for_next_scan()` n'est pas appelé | Bug à corriger — vérifier que le bouton appelle bien `_reset_for_next_scan()`. |
| Migration DB pas appliquée en prod | Le step `psql -f migrate.sql` du workflow CI a échoué | Vérifier les logs du run GitHub Actions ; rerun le workflow ou exécuter manuellement via SSH (cf. CLAUDE.md section Migrations). |

---

## Architecture (rappel des couches)

```
pages/etiquettes_palette.py      ← UI NiceGUI (cascade marque/bouteille/goût + scan)
        │
        ▼  (jamais l'inverse)
common/services/etiquette_palette_service.py   ← logique pure (sans NiceGUI)
        │                                       LabelEntry, HistoryEntry, classify_*
        ▼
common/easybeer/conditioning.py     common/easybeer/products.py     db/conn.py
(matrice CB)                        (libellés, determine_brand)     (history)
```

Règles **enforced** par `tests/test_architecture_layers.py` :
- `common/services/` ne peut **pas** importer `nicegui`
- `common/easybeer/` ne peut **pas** importer `common/services/`
- `pages/X.py` ne peut **pas** importer `pages/Y.py` (sauf `theme`/`auth`)

---

## Pistes d'évolution

- **SSCC** (numéro de palette unique GS1, 18 digits) — manuel GS1 le marque obligatoire pour étiquette palette logistique. Pas implémenté car nécessite le préfixe entreprise GS1 de Symbiose.
- **Refactor `_render_form` en classe** : 940 lignes dans cette fonction, beaucoup de closures. Un `EtiquettesPaletteForm` avec `state` encapsulé permettrait de splitter en méthodes propres. Risque : nombreux callbacks à repasser → tests E2E indispensables.
- **Multi-scan en série** : actuellement un bouton "Scanner le suivant" reset le state ; un mode "scan continu" (auto-impression dès qu'un scan matche en palette pleine) pourrait économiser 2-3 taps par carton. Pas demandé pour l'instant.
- **Impression directe** : remplacer le détour PDF → AirPrint par une impression directe sur le Dymo via IPP ou agent local. Permettrait du multi-scan vraiment fluide.
- **Bio par produit** : actuellement `bio=True` hardcodé. Si on ajoute du non-bio, lookup à étendre.

---

## Cheat-sheet « je débugge »

| Question | Où regarder |
|---|---|
| Mon scan retourne "EAN introuvable" | `_log.info("Scan barcode: ean=… product=…")` dans le journald ferment. Si `product=(non trouvé EB)` → matrice CB EasyBeer ne contient pas cet EAN. |
| Le PDF se génère mais le code-barres est blanc | `treepoem` ne trouve pas Ghostscript : `gs -version` sur le VPS. Réinstaller via `apt install ghostscript`. |
| L'auto-sélection ne déclenche rien | F12 console : vérifier que `emitEvent('barcode_scanned', data)` est bien envoyé. Côté Python, vérifier que `ui.on('barcode_scanned', …)` est bien attaché. |
| L'historique est vide | Vérifier la table `etiquette_palette_history` (ligne par génération). Si table inexistante → migration pas appliquée. |
| Le PDF n'a pas le logo | Vérifier `assets/signature/logo_symbiose.png` ou `NIKO_Logo.png` existent. Le PDF échoue silencieusement si manquant. |
