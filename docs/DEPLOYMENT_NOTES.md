
---

### ☁️ FICHIER 4 — `docs/DEPLOYMENT_NOTES.md`

> 📍Chemin : `Projet-prod/docs/DEPLOYMENT_NOTES.md`

```markdown
# DEPLOYMENT NOTES — Kinsta

## 🔹 1. Source
- Repo : https://github.com/symbiose-prod/Projet-prod
- Branch : `main`

---

## 🔹 2. Procfile
À la racine :

---

## 🔹 3. Variables d’environnement à saisir dans Kinsta
Voir `ops/env.sample`.

Variables minimales :
- BASE_URL
- DB_HOST / DB_PORT / DB_DATABASE / DB_USERNAME / DB_PASSWORD / DB_SSLMODE
- BREVO_API_KEY
- EMAIL_SENDER / EMAIL_SENDER_NAME

⚠️ Ne pas confondre `BREVO_API_KEY` avec `BRAVO_API_KEY`.

---

## 🔹 4. Build & lancement
- Kinsta clone le repo GitHub
- Installe les dépendances (`requirements.txt`)
- Exécute la commande du Procfile

---

## 🔹 5. Tests après déploiement
1. Page d’accueil Streamlit s’affiche
2. Connexion OK
3. Reset de mot de passe OK
4. Envoi de fiche de ramasse OK
5. Accès DB vérifié (lecture / écriture)

---

## 🔹 6. Versioning
Créer un tag pour chaque version stable :
```bash
git tag v1.0.0
git push origin v1.0.0
