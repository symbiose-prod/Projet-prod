```markdown
# 🧭 GUIDE TECHNIQUE – Application **Symbiose**
## Ferment Station – 2025

---

## 🎯 Objectif du projet

**Symbiose** est une application interne de gestion de la production et des ramasses pour l’entreprise **Ferment Station** (producteur de boissons fermentées).  
Développée avec **Streamlit** et hébergée sur **Kinsta**, elle fonctionne comme un **SaaS multi-tenant** permettant de gérer :
- les propositions de production (recettes, stocks, contraintes),
- les demandes de ramasse et envois automatiques d’e-mails,
- la centralisation des données par tenant (client ou site de production).

L’application est conçue pour être simple à maintenir, sécurisée, et extensible.

---

## 🧩 Architecture générale

### 🏗 Technologies principales
- **Frontend / Backend :** [Streamlit](https://streamlit.io)
- **Langage :** Python 3.11+
- **Base de données :** PostgreSQL (managée via Kinsta)
- **Hébergement :** Kinsta App Platform
- **E-mails :** Brevo (ex-Sendinblue)
- **PDF :** ReportLab (génération automatique)
- **Contrôle de version :** GitHub (`https://github.com/symbiose-prod/Projet-prod`)

---

## 📂 Structure du dépôt GitHub

```

Projet-prod/
├─ .streamlit/
│ └─ secrets.toml
├─ assets/
│ ├─ signature/
│ │ ├─ logo_symbiose.png
│ │ └─ NIKO_Logo.png
│ └─ BL_enlevements_Sofripa.xlsx
├─ common/
│ ├─ auth.py
│ ├─ auth_reset.py
│ ├─ email.py
│ ├─ session.py
│ ├─ storage.py
│ └─ design.py
├─ core/
│ ├─ optimizer.py
│ └─ utils.py
├─ db/
│ ├─ conn.py
│ └─ migrate.sql
├─ pages/
│ ├─ 01_Accueil.py
│ ├─ 02_Production.py
│ ├─ 03_Fiche_de_ramasse.py
│ ├─ 04_Paramètres.py
│ ├─ 05_Profile.py
│ └─ 06_Reset_password.py
├─ app.py
├─ Procfile
└─ requirements.txt
```

---

## 🗄️ Base de données (PostgreSQL)

### Schéma principal (simplifié)

#### Table `tenants`
| Colonne | Type | Description |
|----------|------|-------------|
| id | uuid | Identifiant du tenant |
| name | text | Nom du tenant |
| created_at | timestamp | Date de création |

#### Table `users`
| Colonne | Type | Description |
|----------|------|-------------|
| id | uuid | Identifiant utilisateur |
| tenant_id | uuid | FK vers `tenants` |
| email | text | E-mail unique |
| password_hash | text | Hash du mot de passe |
| role | text | “user” ou “admin” |
| is_active | bool | Statut du compte |
| created_at | timestamp | Création du compte |

#### Table `production_proposals`
| Colonne | Type | Description |
|----------|------|-------------|
| id | uuid | ID de la proposition |
| tenant_id | uuid | FK vers tenant |
| created_by | uuid | FK vers user |
| payload | jsonb | Données brutes (recette, stock, contraintes, etc.) |
| status | text | État (draft, validated, sent, etc.) |
| created_at | timestamp | Date de création |
| updated_at | timestamp | Dernière mise à jour |

#### Table `password_resets`
| Colonne | Type | Description |
|----------|------|-------------|
| user_id | uuid | FK vers user |
| token_hash | text | Hash du token de reset |
| expires_at | timestamp | Expiration du lien |
| used_at | timestamp | Date d’utilisation |
| request_ip | text | Adresse IP de demande |
| request_ua | text | User-Agent |
| created_at | timestamp | Date de création |

---

## 🔐 Authentification & gestion utilisateur

- Authentification classique **e-mail + mot de passe**
- Chaque utilisateur appartient à un **tenant**
- Les données sont **filtrées par tenant_id**
- Fonctionnalités :
  - Création de compte (`create_user`)
  - Connexion (`authenticate`)
  - Gestion de session (`session.py`)
  - Réinitialisation de mot de passe sécurisée :
    - Génération d’un token signé (table `password_resets`)
    - Envoi automatique du lien via e-mail
    - Lien temporaire avec expiration (par défaut : 1 h)

---

## 📧 Envoi d’e-mails (via Brevo)

Géré par `common/email.py`

### Fonctionnement :
1. Lorsqu’une fiche de ramasse est validée, le script génère un **PDF** à partir du modèle `BL_enlevements_Sofripa.xlsx`.
2. Le PDF est enregistré temporairement.
3. Un e-mail est envoyé via **Brevo API**, contenant :
   - Objet personnalisé
   - Corps HTML (template intégré)
   - Signature (logos Symbiose + Niko)
   - Pièce jointe (PDF)
4. Les réponses à ces e-mails sont redirigées vers `station.ferment@gmail.com`.

**Expéditeur actuel :**
```

[station.ferment@10112311.brevosend.com](mailto:station.ferment@10112311.brevosend.com)

````

---

## ☁️ Déploiement et hébergement sur Kinsta

### Composants :
- Application Streamlit : hébergée sur **Kinsta App**
- Base de données PostgreSQL : hébergée sur **Kinsta Database**
- Domaine : `prod.symbiose-kefir.fr`

### Variables d’environnement principales :

| Nom | Description |
|-----|--------------|
| `BASE_URL` | URL publique (https://prod.symbiose-kefir.fr) |
| `DB_HOST` | Hôte PostgreSQL |
| `DB_PORT` | Port PostgreSQL |
| `DB_DATABASE` | Nom de la base |
| `DB_USERNAME` | Utilisateur PostgreSQL |
| `DB_PASSWORD` | Mot de passe PostgreSQL |
| `DB_SSLMODE` | Mode SSL (`require`) |
| `EMAIL_PROVIDER` | `brevo` |
| `EMAIL_SENDER` | Adresse d’envoi |
| `EMAIL_SENDER_NAME` | “Symbiose App” |
| `EMAIL_REPLY_TO` | Adresse de réponse |
| `EMAIL_RECIPIENTS` | Destinataires par défaut |
| `BRAVO_API_KEY` | Clé API Brevo |
| `ENV` | `production` |
| `GH_TOKEN` | Token GitHub (si synchro automatisée) |

---

## 🚀 RUNBOOK – Redéployer l’app sur Kinsta

1. **Commit et push** les changements sur la branche `main` de GitHub.
2. Aller sur le **Dashboard Kinsta → Applications → Symbiose**.
3. Cliquer sur **“Deploy now”**.
4. Kinsta reconstruit automatiquement le conteneur :
   - Installe les dépendances de `requirements.txt`
   - Exécute `streamlit run app.py`
5. Vérifier le déploiement :
   - L’URL `https://prod.symbiose-kefir.fr` doit s’afficher
   - Le cadenas 🔒 doit apparaître (SSL actif)

**Durée moyenne :** 1–3 min par déploiement.

---

## 🧰 Maintenance & améliorations possibles

### 🔧 Modifier le design (UI)
- Les composants visuels communs sont dans `common/design.py`
- Tu peux ajouter :
  - des couleurs cohérentes via `st.markdown` CSS inline
  - des icônes Streamlit (`st.icon`, `st.columns`, etc.)

### 🧩 Ajouter une nouvelle page
1. Créer un fichier `pages/07_Nom_de_la_page.py`
2. Utiliser la structure :
   ```python
   from common.session import require_login
   user = require_login()

   import streamlit as st
   st.title("Titre de la nouvelle page")
````

3. La page apparaîtra automatiquement dans le menu Streamlit.

### 🧮 Modifier la logique de production

* Le cœur de l’optimisation est dans `core/optimizer.py`
* Le code peut être étendu pour :

  * intégrer de nouvelles contraintes (ex : stock, co-production)
  * améliorer les calculs d’autonomie
  * ajouter des filtres selon les ventes

### 🧾 Adapter la fiche de ramasse

* Modèle : `assets/BL_enlevements_Sofripa.xlsx`
* Le PDF est généré automatiquement depuis ce fichier → modifier le contenu ou le design Excel pour changer le rendu final.

---

## 🧱 Sécurité et bonnes pratiques

* Toujours utiliser un **mot de passe fort** pour la base PostgreSQL.
* Ne jamais commit le fichier `.streamlit/secrets.toml` ni les tokens.
* Ne pas stocker de secrets dans le code.
* Tester les e-mails Brevo sur une **boîte de test** avant envoi réel.
* Effectuer un **redéploiement manuel** après toute modification du code ou des variables d’environnement.

---

## 📚 Contacts et références

**Entreprise :** Ferment Station
**Projet :** Symbiose (gestion production & ramasses)
**Hébergement :** Kinsta App + Database
**E-mails :** Brevo (Sendinblue)
**Dépôt GitHub :** [symbiose-prod/Projet-prod](https://github.com/symbiose-prod/Projet-prod)

---

## ✅ En résumé

| Élément              | Statut                 | Lieu                             |
| -------------------- | ---------------------- | -------------------------------- |
| Authentification     | Fonctionnelle          | `common/auth.py`                 |
| Reset Password       | Fonctionnel            | `common/auth_reset.py`           |
| Envois e-mails + PDF | Automatisés            | `common/email.py`                |
| Multi-tenant         | Implémenté             | Tables `tenants`, `users`        |
| Domaine sécurisé     | ✅ HTTPS actif          | `https://prod.symbiose-kefir.fr` |
| Déploiement          | Automatique via Kinsta | App Platform                     |

---

> **Rédigé par :** Chloé
> **Date :** Novembre 2025
> **Projet :** Symbiose – Ferment Station

---
