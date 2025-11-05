# RUNBOOK — Ferment Station / Symbiose

Ce document explique comment maintenir, redéployer et dépanner l’application.

---

## 1️⃣ Contexte

- App : Streamlit multi-tenant (tenants → users → production_proposals)
- Hébergement : Kinsta App
- Base : PostgreSQL
- Emails : Brevo (transactionnel)
- Authentification : e-mail + mot de passe, réinitialisation par lien

---

## 2️⃣ Lancer en local

### Prérequis
- Python 3.11+
- `pip install -r requirements.txt`
- Un accès à la base (locale ou distante)
- Fichier `.env` (copie de `ops/env.sample`)

### Commande
```bash
streamlit run app.py
