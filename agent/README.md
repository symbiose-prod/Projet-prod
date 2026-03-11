# Agent Sync Étiquettes — Ferment Station

Agent Python pour synchroniser les données de production du SaaS vers la base Microsoft Access utilisée par le logiciel d'impression d'étiquettes.

## Prérequis

- Windows 11 Pro
- Python 3.10+
- Microsoft Access Database Engine (driver ODBC)

## Installation

```powershell
# 1. Cloner ou copier le dossier agent/
cd C:\FermentStation\agent

# 2. Installer les dépendances
pip install -r requirements.txt

# 3. Configurer
copy config.ini.example config.ini
# Éditer config.ini avec les bons paramètres (URL, clé API, chemin .mdb)

# 4. Tester en mode console
python agent.py --once

# 5. Lancer en mode continu
python agent.py
```

## Configuration (config.ini)

| Section | Clé | Description |
|---------|-----|-------------|
| `[server]` | `url` | URL du SaaS (https://prod.symbiose-kefir.fr) |
| `[server]` | `api_key` | Clé API générée depuis le SaaS |
| `[local]` | `mdb_path` | Chemin absolu du fichier .mdb |
| `[local]` | `table_name` | Nom de la table Access (défaut: Produits) |
| `[local]` | `poll_interval` | Intervalle de polling en secondes (défaut: 300) |
| `[logging]` | `level` | Niveau de log: DEBUG, INFO, WARNING, ERROR |

## Fonctionnement

1. L'agent interroge le SaaS toutes les 5 minutes (`GET /api/sync/pending`)
2. S'il y a une opération en attente, il récupère la liste des produits
3. Il remplace tout le contenu de la table Access (DELETE + INSERT atomique)
4. Il confirme au SaaS (`POST /api/sync/ack`)

## Logs

Les logs sont écrits dans `logs/sync_agent.log` (rotation 5 MB, 3 fichiers).
