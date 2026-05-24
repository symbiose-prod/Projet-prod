# EasyBeer — payloads de référence pour les writes

Capturé via HAR export Chrome DevTools, brassin **KDF18052026** (id 259288),
2026-05-24. Source de vérité pour reconstruire les builders côté backend.

---

## Contexte

Le Sprint Outbox (cf. `common/services/production_sheet_eb_bind.py`) construit
des payloads pour pousser des events vers EasyBeer. Les payloads actuels sont
**incomplets** par rapport à ce qu'EB attend, et EB rejette silencieusement
(HTTP 200 + body vide) au lieu de retourner une erreur explicite.

Le 2026-05-23, le test E2E a confirmé que `brassin.terminer` et
`brassin.mise-en-bouteille` ne déclenchent **aucune action** côté EB malgré
le statut `sent` côté outbox (la PR#38 qui tolérait body vide a été reverte
parce qu'elle masquait justement ces rejets).

Ces JSON sont la référence pour reconstruire les builders.

---

## Endpoints capturés

### `POST /brassin/mise-en-bouteille`

- **Request** : `mise-en-bouteille.request.json` (56 KB)
- **Response succès** : `{"message": "", "map": {}}` (HTTP 200, 32 bytes)

**Top-level keys requises :**

| Clé | Type | Notes |
|-----|------|-------|
| `dateLimiteUtilisationOptimale` | ISO date | DDM = DateMiseEnBouteille + durabilité produit (365 j pour kéfir) |
| `produitsDerives` | list | Au moins 1 entrée = full ModeleProduit du brassin |
| `modeleBrassin` | dict (42 clés) | **Full ModeleBrassin** — pas un `{id: X}` |
| `modeleElevage` | dict (6 clés mais quasi vide) | Champ obligatoire même si pas d'élevage |
| `volumeRestant` | int | Volume restant à mettre en bouteille (en L) |
| `modelesStockProduitBouteille` | list | **Stock produit fini à CRÉER** (cf. ci-dessous) |
| `modelesStockProduitFutContenant` | list (vide pour kéfir bouteille) | Stock fûts à créer |
| `modelesStocksMiseEnBouteille` | list (~8 items) | **Stocks à DÉCRÉMENTER** (cf. ci-dessous) |
| `numeroLot` | string | Code brassin (ex. "KDF18052026") |
| `dateMiseEnBouteille` | ISO date | Date de l'opération |

**`modelesStocksMiseEnBouteille`** = ce qui est consommé en stock à la mise
en bouteille. Pour chaque format produit, EB attend 4 entrées :

```json
[
  {"type": "BOUTEILLE", "idStockBouteille": 111687, "quantite": 1194},
  {"type": "MP", "idMatierePremiere": 95498,  "quantite": 1194},  // capsules
  {"type": "MP", "idMatierePremiere": 95550,  "quantite": 199},   // cartons (1 carton = 6 bouteilles)
  {"type": "MP", "idMatierePremiere": 96105,  "quantite": 1194}   // étiquettes
]
```

**Source des IDs** : le brassin EB lui-même expose la liste prête à l'emploi
dans son champ `matieresPremieresConditionnement` (visible via
`get_brassin_detail(id)`). C'est l'équivalent de la BOM (Bill Of Materials)
du conditionnement, déjà résolue par EB en fonction du produit et du format.

**`modelesStockProduitBouteille`** = stock produit fini à créer. Structure
en arbre : un seul élément racine avec `libelle: "FERMENT STATION"` et
`modelesFils[]` listant les stocks bouteilles individuels (un par format).

---

### `POST /brassin/terminer`

- **Request** : `terminer.request.json` (37 KB)
- **Response succès** : **body vide** (HTTP 200, 7 bytes seulement à cause
  des headers Content-Length=0)

⚠️ Body vide = **succès** pour cet endpoint. Notre `_safe_json` doit
tolérer ce cas spécifique (cf. PR#38 reverte qui le faisait trop largement).

**Structure :** full ModeleBrassin envoyé tel quel.

**Points clés :**

| Clé | Notes |
|-----|-------|
| `idBrassin` (top-level) | ⚠️ PAS `id` — c'est `idBrassin` ! Bug actuel : on envoie `id` |
| `etat` | `{"code": "EN_COURS", ...}` ← UI envoie EN_COURS, c'est l'endpoint qui flippe en TERMINE côté serveur |
| `archive` | `false` (sauf si on archive en même temps) |
| `enCours` | `true` (UI ne change pas) |
| `termine` | `false` (UI ne change pas) |
| `volumeFinal` | Volume produit (en L) |
| `densiteInitiale`, `densiteFinale` | Mesures de fermentation (utile pour rapport, mais EB tolère probablement absent) |
| `ph` | Dernière mesure pH |
| `attenuationLimite` | Calcul EB à partir des densités |
| `rendementBrassin`, `rendementCentrifugeuse`, `rendementConditionnement` | KPI rendement |
| `commentaire` | HTML, peut contenir le récap (SSCC, mesures, incidents) |
| `dateFinFormulaire` | ISO date de fin |
| `numeroLot`, `dateMiseEnBouteille`, `dateLimiteUtilisationOptimale` | Cohérent avec mise-en-bouteille |
| `productions` | list — pré-rempli par la mise-en-bouteille précédente |
| `matieresPremieresConditionnement` | Idem |

**Stratégie d'implémentation** : `get_brassin_detail(id)` + overrides
ciblés (idBrassin, volumeFinal, densiteInitiale, densiteFinale, ph,
commentaire, attenuationLimite, rendement*, dateFinFormulaire, archive).

---

### `POST /brassin/deduction-stocks-conditionnement`

- **Request** : `deduction-stocks-conditionnement.request.json` (55 KB)
- **Response succès** : full ModeleBrassin updated (HTTP 200, 58 KB)

Endpoint que l'UI EB appelle **avant** `mise-en-bouteille` pour calculer
les stocks à décrémenter. Le résultat enrichit le brassin avec
`matieresPremieresConditionnement` mis à jour. Pourrait être utile si on
veut une approche en 2 étapes (calculer puis valider), sinon on pioche
directement dans `brassin.matieresPremieresConditionnement` qui est déjà
résolu côté EB.

---

## Plan de reconstruction

### Phase A — Fix `brassin.terminer` (le plus simple)

1. Lazy load : `brassin_full = get_brassin_detail(id)`
2. Overrides ciblés : `volumeFinal`, `densiteInitiale`, `densiteFinale`,
   `ph`, `commentaire`, `archive`, `dateFinFormulaire`
3. **Renommer `id` → `idBrassin`** dans le payload de queue
4. Merge `{**brassin_full, **overrides}`
5. POST `/brassin/terminer`
6. Tolérer body vide sur 2xx **uniquement pour cet endpoint** (whitelist)

### Phase B — Fix `brassin.mise-en-bouteille`

1. Lazy load : `brassin_full = get_brassin_detail(id)`
2. Récupérer `matieresPremieresConditionnement` du brassin
3. **Ajuster les quantités** selon le conditionnement réel de la fiche
   (notre `data.conditionnement_reel.items` × pcb pour les bouteilles,
   etc.)
4. Construire `modelesStocksMiseEnBouteille` depuis cette liste ajustée
5. Construire `modelesStockProduitBouteille` (arbre FERMENT STATION + fils)
6. Ajouter `produitsDerives`, `modeleElevage`, `volumeRestant`, dates
7. POST `/brassin/mise-en-bouteille`
8. Valider que la réponse contient `{"message": "", "map": {}}`

### Phase C — Tests E2E

Brassin sandbox, finalize fiche avec `brassin_termine=true`, vérifier sur
EB que le brassin passe en TERMINÉ avec stock créé.
