# RUNBOOK — Ferment Station

Ce document explique comment maintenir, redéployer et dépanner l'application.

---

## 1. Infrastructure

- **App** : NiceGUI (Python 3.11+), service systemd `ferment`
- **Hébergement** : VPS OVH Ubuntu 24.04 (`92.222.229.87`), reverse proxy Caddy + HTTPS Let's Encrypt
- **URL** : https://prod.symbiose-kefir.fr
- **Base** : PostgreSQL 16 local (`whole-tomato-leopard`, user applicatif `shark`)
- **Emails** : Brevo (transactionnel) + fallback queue DB (`email_queue`)
- **IA** : Anthropic Claude (propositions commandes)
- **CI/CD** : GitHub Actions — push `main` → lint + pytest + deploy SSH

---

## 2. Lancer en local

Prérequis : Python 3.11+, `pip install -r requirements.txt`, PostgreSQL local, `.env` (copie `ops/env.sample`).

```bash
python3 scripts/app_bootstrap.py    # crée/migre les tables
python3 app_nicegui.py              # http://localhost:8502
```

---

## 3. Déploiement

Automatique via GitHub Actions : `git push origin main` → CI (lint + tests) → `git pull` + `systemctl restart ferment` sur le VPS.

Manuel (si besoin) :
```bash
ssh ubuntu@92.222.229.87
cd /home/ubuntu/app && git pull && sudo systemctl restart ferment
sudo journalctl -u ferment -f      # vérifier que ça démarre bien
```

Migration DB :
```bash
sudo cp /home/ubuntu/app/db/migrate.sql /tmp/migrate.sql
sudo -u postgres psql -d "whole-tomato-leopard" -f /tmp/migrate.sql
```

---

## 4. Backups & restauration (critique)

### 4.1 Fonctionnement

- **Script** : [`ops/backup-db.sh`](../ops/backup-db.sh) — pg_dump plain SQL + gzip dans `/backups/`
- **Cron** : quotidien à 03:00 UTC (`crontab -l` sous `ubuntu`)
- **Rotation** : conserve 30 jours, supprime les plus vieux
- **Log** : `/backups/backup.log`
- **Sanity check** : le script échoue si le dump pèse < 1000 octets

Vérifier que le cron tourne :
```bash
ssh ubuntu@92.222.229.87 'ls -lht /backups/*.sql.gz | head -5'
# On doit voir un fichier daté d'aujourd'hui ou d'hier.
```

### 4.2 Tester la restauration (drill ops)

Lancer à la main sur le VPS pour vérifier que le dernier backup est **réellement restaurable** :

```bash
ssh ubuntu@92.222.229.87 '/home/ubuntu/app/ops/restore-db.sh latest'
```

Le script restaure dans une DB temporaire (`ferment_restore_test_<timestamp>`), vérifie la présence des tables critiques (`tenants`, `users`, `audit_log`, `ramasse_history`, …), affiche les counts, puis drop la DB. Retour `0` = tout OK.

**Automatisé en timer systemd** : `backup-verify.timer` tourne tous les dimanches 04:15 UTC. Logs :
```bash
sudo journalctl -u backup-verify.service -n 50
```

### 4.3 Restauration réelle en cas de sinistre

**Scénario A — corruption DB, on a un backup sain** :

```bash
ssh ubuntu@92.222.229.87
sudo systemctl stop ferment     # bloquer les écritures
# Inspection : peut-on récupérer la DB en l'état ?
sudo -u postgres pg_dump whole-tomato-leopard > /tmp/emergency_dump.sql  # si encore lisible

# Restore du dernier backup sain dans la DB prod (DANGEREUX, écrase tout)
/home/ubuntu/app/ops/restore-db.sh latest --target whole-tomato-leopard
# Le script demande de taper "OUI" en majuscules pour confirmer.

sudo systemctl start ferment
curl -s https://prod.symbiose-kefir.fr/health
```

**Scénario B — le VPS est down / corruption FS, restauration ailleurs** :

1. Provisionner un nouveau VPS Ubuntu + PostgreSQL + venv Python.
2. Restaurer le `.env` depuis une sauvegarde externe chiffrée (secrets).
3. Copier un backup `.sql.gz` récent (via rsync depuis sauvegarde off-site).
4. `git clone` du repo dans `/home/ubuntu/app`.
5. `/home/ubuntu/app/ops/restore-db.sh <backup.sql.gz> --target whole-tomato-leopard`.
6. Redéployer les unit files systemd (`ferment.service`, `ramasse-purge.timer`, `email-retry.timer`, `backup-verify.timer`).
7. Basculer le DNS vers le nouveau VPS.

**Point d'attention critique** : aujourd'hui, les backups sont locaux uniquement (sur le même VPS que la DB). En cas de panne matérielle / perte complète du VPS, aucun backup récupérable. **À améliorer** : rsync quotidien vers un stockage off-site (S3, Backblaze B2, ou simplement un autre VPS).

### 4.4 Incident historique — leçon apprise

**2026-03-19 → 2026-04-19** : les backups ont échoué silencieusement pendant 1 mois. Cause : shebang corrompu (`#\!/usr/bin/env bash` au lieu de `#!/usr/bin/env bash`) dans l'ancien script `/home/ubuntu/backup_db.sh`, qui faisait que cron le lançait avec `/bin/sh` (dash) → rejet de `set -o pipefail` → exit immédiat sans créer de dump.

**Mesure corrective** : le cron pointe désormais sur `/home/ubuntu/app/ops/backup-db.sh` (versionné), et le timer `backup-verify.timer` lance un restore test hebdomadaire — un backup cassé sera détecté en 7 jours max, pas en 1 mois.

---

## 5. Timers ops installés

| Timer | Fréquence | But | Log |
|-------|-----------|-----|-----|
| `ramasse-purge.timer` | Daily 03:15 UTC | Hard-delete ramasses soft-deleted > 7j | `journalctl -u ramasse-purge.service` |
| `email-retry.timer` | Toutes les 10 min | Retry des emails en fallback queue | `journalctl -u email-retry.service` |
| `backup-verify.timer` | Sunday 04:15 UTC | Test restore du dernier backup | `journalctl -u backup-verify.service` |

État courant :
```bash
sudo systemctl list-timers '*.timer' --no-pager
```

---

## 6. Observabilité

- `/health` (JSON) : état db + disk + EasyBeer circuit breaker + cache entries
- `/metrics` (Prometheus text format) : gauges pour scraping externe
- **Logs app** : `journalctl -u ferment -f`
- **Logs audit** (qui a fait quoi) : page `/admin` (role=admin) + table `audit_log`
- **Logs backup** : `/backups/backup.log`

---

## 7. Troubleshooting rapide

**App down** :
```bash
sudo systemctl status ferment
sudo journalctl -u ferment -n 50
sudo systemctl restart ferment
```

**EasyBeer indisponible** : `/health` → `circuit_breaker: open`. Les pages affichent le mode dégradé automatiquement (import Accueil bloqué, message clair). Rien à faire côté serveur — le circuit se rouvre automatiquement après 60s de calme.

**Email non envoyé** : vérifier la queue DB :
```sql
SELECT status, COUNT(*) FROM email_queue GROUP BY status;
-- Force un retry manuel :
/home/ubuntu/app/venv/bin/python /home/ubuntu/app/scripts/retry_pending_emails.py
```

**Session corrompue / utilisateur bloqué** : vider le remember-me token.
```sql
DELETE FROM user_sessions WHERE user_id = (SELECT id FROM users WHERE email = 'X@Y.fr');
```

---

## 8. Contact

- Dev : nicolas@niko-drinks.fr
- Repo : https://github.com/symbiose-prod/Projet-prod
