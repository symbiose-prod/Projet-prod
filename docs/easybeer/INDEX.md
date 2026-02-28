# Easy Beer API — Index des fichiers

Spec OpenAPI 2.4MB découpée en **35 fichiers par tag** + un fichier de schémas.

**Base URL :** `https://api.easybeer.fr`
**Auth :** HTTP Basic (`EASYBEER_API_USER` / `EASYBEER_API_PASS`)
**ID brasserie :** `EASYBEER_ID_BRASSERIE` (`2013` en production)

---

## Fichiers utilisés dans ce projet

| Fichier | Taille | Endpoints | Usage |
|---------|--------|-----------|-------|
| `controleur-indicateur.json` | 116KB | 82 | Autonomie stocks, synthèse conso MP |
| `controleur-stock.json` | 235KB | 162 | Stock matières premières |

### Endpoints actifs (voir CLAUDE.md pour les payloads)

```
POST /indicateur/autonomie-stocks/export/excel    → Excel ventes+stock (page 01_Accueil)
```

---

## Tous les fichiers disponibles

| Fichier | Taille | Endpoints |
|---------|--------|-----------|
| `controleur-action.json` | 2KB | 1 |
| `controleur-brassage-evenement.json` | 4KB | 3 |
| `controleur-brasserie.json` | 19KB | 19 |
| `controleur-brassin.json` | 94KB | 73 |
| `controleur-caisse.json` | 11KB | 10 |
| `controleur-commande.json` | 183KB | 143 |
| `controleur-comptabilite.json` | 69KB | 50 |
| `controleur-crm.json` | 11KB | 8 |
| `controleur-dashboard.json` | 14KB | 12 |
| `controleur-document.json` | 6KB | 3 |
| `controleur-douane.json` | 31KB | 21 |
| `controleur-formation.json` | 14KB | 11 |
| `controleur-fourniture.json` | 55KB | 41 |
| `controleur-fut.json` | 64KB | 50 |
| `controleur-hey-billy.json` | 8KB | 6 |
| `controleur-indicateur.json` | 116KB | 82 |
| `controleur-lavage.json` | 10KB | 8 |
| `controleur-location.json` | 46KB | 35 |
| `controleur-marketplace.json` | 9KB | 8 |
| `controleur-notification.json` | 9KB | 7 |
| `controleur-ordonnanceur.json` | 2KB | 2 |
| `controleur-parametres.json` | 414KB | 334 |
| `controleur-partenaire.json` | 10KB | 8 |
| `controleur-point-vente.json` | 29KB | 20 |
| `controleur-preference.json` | 53KB | 45 |
| `controleur-prestashop.json` | 8KB | 7 |
| `controleur-profil.json` | 12KB | 10 |
| `controleur-publipostage.json` | 14KB | 11 |
| `controleur-referentiel.json` | 115KB | 114 |
| `controleur-shopify.json` | 8KB | 7 |
| `controleur-sidely.json` | 4KB | 3 |
| `controleur-stock.json` | 235KB | 162 |
| `controleur-support.json` | 3KB | 3 |
| `controleur-tracabilite.json` | 19KB | 14 |
| `controleur-woo-commerce.json` | 9KB | 8 |
| `schemas.json` | 656KB | 678 schémas partagés |

> Les fichiers par tag ne contiennent pas les schémas. Charger `schemas.json` uniquement si besoin du détail des types de réponse.
