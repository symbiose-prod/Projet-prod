# Agent d'impression Brother QL-1110NWBc

Petit script Python qui tourne sur la machine Windows toujours allumée
de l'entrepôt, et imprime les étiquettes palette envoyées par les opérateurs
depuis l'iPhone via la page `https://prod.symbiose-kefir.fr/etiquettes-palette`.

## Comment ça marche

```
iPhone ─── tap "Imprimer directement" ──► VPS Ferment Station
                                                │
                                                │  POST /api/print-jobs
                                                ▼
                                          DB (table print_jobs, status=pending)
                                                │
                                                │  signal asyncio
                                                ▼
                       (long-polling actif, ~50ms latence)
                                                │
Agent Windows ──► GET /api/print-jobs/next ◄────┘
                                │
                                │  reçoit {id, pdf_b64, filename, n_copies}
                                ▼
                       décode → PDF temp file
                                │
                                ▼
            ShellExecute("printto", pdf, "Brother QL-1110NWBc")
                                │
                                ▼
                     Driver Brother Windows ──► Imprimante
                                │
                                ▼
                  POST /api/print-jobs/{id}/done
```

## Setup (~10 min)

### 1. Installer le driver Brother

Télécharger depuis [brother.fr](https://support.brother.com/) le driver
de la **QL-1110NWBc** pour Windows 10 64-bit. Installer.

Vérifier que l'imprimante apparait dans **Paramètres → Imprimantes & scanners**.

Imprimer une page de test depuis Windows pour confirmer que le driver
fonctionne et que les étiquettes DK-11247 (103×164 mm) sont bien chargées.

### 2. Récupérer le token agent

Demander à l'admin (ou regarder dans le `.env` du VPS) la valeur de
`PRINT_AGENT_TOKEN`. C'est le token bearer partagé.

Si le token n'existe pas encore côté VPS, en générer un :
```bash
openssl rand -hex 32
```
Puis l'ajouter au `.env` du VPS sous `PRINT_AGENT_TOKEN=...` et `PRINT_AGENT_TENANT_ID=...`
(l'UUID du tenant Symbiose, à récupérer dans la table `tenants`).

### 3. Installer Python sur Windows

Télécharger [Python 3.11+ depuis python.org](https://www.python.org/downloads/windows/).
Cocher **« Add Python to PATH »** à l'installation.

Vérifier dans une invite de commandes :
```
python --version
```

### 4. Récupérer l'agent

Cloner le repo (ou télécharger juste le dossier `scripts/windows_print_agent/`) :
```cmd
git clone https://github.com/symbiose-prod/Projet-prod.git
cd Projet-prod\scripts\windows_print_agent
```

### 5. Installer les dépendances

```cmd
pip install -r requirements.txt
```

### 6. Configurer

Copier `.env.sample` en `.env` et remplir :

```
VPS_URL=https://prod.symbiose-kefir.fr
AGENT_TOKEN=<le token récupéré à l'étape 2>
PRINTER_NAME=Brother QL-1110NWBc
```

⚠ **PRINTER_NAME** doit être le **nom EXACT** affiché dans
`Paramètres → Imprimantes & scanners`. Si laissé vide, l'agent utilise
l'imprimante par défaut Windows — fonctionne aussi mais moins explicite.

### 7. Tester

Lancer l'agent dans une fenêtre de commande :
```cmd
python print_agent.py
```

Tu devrais voir :
```
2026-XX-XX HH:MM:SS [INFO] Print agent démarré — VPS=https://..., printer='Brother QL-1110NWBc'
```

Sur l'iPhone, ouvrir https://prod.symbiose-kefir.fr/etiquettes-palette,
scanner un carton, taper « Imprimer directement ». L'étiquette doit sortir
de la Brother en ~1 seconde. Tu verras dans la console Windows :
```
[INFO] Impression de etiquette_NIKO_12x33_Gingembre_08052027_126c.pdf sur 'Brother QL-1110NWBc' (×2)
[INFO] ✓ Job 42 imprimé
```

### 8. Démarrage automatique au boot Windows

Pour que l'agent redémarre tout seul après un reboot :

**Option simple — raccourci dans Démarrage :**

1. Win+R → `shell:startup` → ouvre le dossier Démarrage
2. Click droit → Nouveau → Raccourci
3. Cible : `pythonw C:\chemin\vers\print_agent.py`
   (`pythonw` au lieu de `python` = pas de fenêtre console)
4. Nom : « Brother Print Agent »

**Option propre — Tâche planifiée :**

1. Ouvrir « Planificateur de tâches »
2. Créer une tâche basique → Démarrage → `pythonw C:\chemin\vers\print_agent.py`
3. Cocher « Exécuter au démarrage de l'utilisateur connecté »
4. Cocher « Redémarrer en cas d'échec, toutes les 1 minute »

## Logs

L'agent écrit dans `print_agent.log` (à côté du script). Vérifier ce
fichier en cas de problème.

## Dépannage

| Symptôme | Cause probable | Fix |
|---|---|---|
| « Token invalide » au démarrage | AGENT_TOKEN ≠ PRINT_AGENT_TOKEN du VPS | Vérifier que les deux sont identiques |
| « Le VPS n'a pas configuré PRINT_AGENT_TOKEN » | Côté VPS, env var manquante | Demander à l'admin de poser la var |
| Pas d'erreur mais rien n'imprime | Mauvais PRINTER_NAME | Mettre le nom EXACT du Panneau de configuration |
| Impression coupée / mal alignée | Mauvaises étiquettes chargées | Vérifier que ce sont des DK-11247 (103×164 mm) |
| L'agent crash au reboot | Python pas dans PATH | Réinstaller Python avec « Add to PATH » coché |
| Logs montrent « Connexion VPS impossible » | Pas de réseau / VPS down | Vérifier que l'iPhone aussi voit le VPS |

## Maintenance

L'agent est sans état — on peut le tuer (Ctrl+C) ou le relancer à volonté.
Les jobs en cours « bloqués » côté VPS (status = `printing` mais l'agent
a crashé) sont automatiquement remis en `pending` après 5 minutes par
un watchdog côté serveur.

Pour voir l'historique d'impression côté VPS :
```sql
SELECT id, status, error_message, created_at, printed_at
FROM print_jobs
WHERE tenant_id = '<UUID Symbiose>'
ORDER BY created_at DESC LIMIT 20;
```
