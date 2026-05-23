#!/usr/bin/env bash
# scripts/refresh_eb_swagger.sh
# ============================
# Télécharge le Swagger officiel d'Easybeer (api.easybeer.fr/v2/api-docs)
# et le découpe par controller dans docs/easybeer/*.json.
#
# Usage :
#   make refresh-eb-swagger
#   # ou directement :
#   bash scripts/refresh_eb_swagger.sh
#
# Prérequis :
#   - jq installé (brew install jq / apt-get install jq)
#   - curl installé (toujours)
#   - Variables EASYBEER_API_USER + EASYBEER_API_PASS dans l'env (cf. .env)
#
# Fichiers produits :
#   docs/easybeer-api.swagger.json         # swagger global (1+ MB)
#   docs/easybeer/controleur-XXX.json      # un fichier par controller
#   docs/easybeer/INDEX.md                 # index des fichiers (régénéré)

set -euo pipefail

# ─── Couleurs (skip si pas de TTY) ────────────────────────────────────────
if [[ -t 1 ]]; then
    GREEN='\033[0;32m'
    RED='\033[0;31m'
    YELLOW='\033[0;33m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    GREEN=''; RED=''; YELLOW=''; BOLD=''; NC=''
fi

# ─── Pré-checks ───────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Charger .env si présent (sans écraser les vars existantes)
if [[ -f .env && -z "${EASYBEER_API_USER:-}" ]]; then
    # shellcheck disable=SC2046
    export $(grep -E '^EASYBEER_API_(USER|PASS)=' .env | xargs -0 2>/dev/null || true)
    # Fallback : parse ligne par ligne (compat sed/grep stricts)
    while IFS='=' read -r key val; do
        [[ "$key" =~ ^EASYBEER_API_(USER|PASS)$ ]] && export "$key=$val"
    done < <(grep -E '^EASYBEER_API_(USER|PASS)=' .env || true)
fi

if [[ -z "${EASYBEER_API_USER:-}" || -z "${EASYBEER_API_PASS:-}" ]]; then
    echo -e "${RED}✗ EASYBEER_API_USER / EASYBEER_API_PASS manquantes${NC}"
    echo "  Définir dans .env ou en variables d'environnement avant de lancer."
    exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
    echo -e "${RED}✗ jq non installé${NC}"
    echo "  macOS  : brew install jq"
    echo "  Ubuntu : sudo apt-get install jq"
    exit 1
fi

# ─── 1. Télécharger le swagger global ─────────────────────────────────────
echo -e "${BOLD}1/3 Téléchargement du Swagger Easybeer…${NC}"

GLOBAL_OUT="docs/easybeer-api.swagger.json"
TMP_OUT="$(mktemp)"
trap 'rm -f "$TMP_OUT"' EXIT

HTTP_CODE=$(curl -sS -o "$TMP_OUT" -w "%{http_code}" \
    -u "$EASYBEER_API_USER:$EASYBEER_API_PASS" \
    "https://api.easybeer.fr/v2/api-docs" \
    --max-time 60)

if [[ "$HTTP_CODE" != "200" ]]; then
    echo -e "${RED}✗ HTTP $HTTP_CODE — téléchargement échoué${NC}"
    head -c 500 "$TMP_OUT"
    exit 1
fi

# Vérifier que c'est bien du JSON valide
if ! jq empty "$TMP_OUT" 2>/dev/null; then
    echo -e "${RED}✗ Réponse non-JSON :${NC}"
    head -c 500 "$TMP_OUT"
    exit 1
fi

SIZE=$(wc -c < "$TMP_OUT" | tr -d ' ')
VERSION=$(jq -r '.info.version // "?"' "$TMP_OUT")
PATHS_COUNT=$(jq '.paths | length' "$TMP_OUT")
POST_COUNT=$(jq '[.paths[] | to_entries[] | select(.key=="post")] | length' "$TMP_OUT")

mkdir -p docs
mv "$TMP_OUT" "$GLOBAL_OUT"
trap - EXIT

echo -e "  ${GREEN}✓${NC} $GLOBAL_OUT ($SIZE octets, EB v$VERSION, $PATHS_COUNT endpoints, $POST_COUNT POST)"

# ─── 2. Split par controller ──────────────────────────────────────────────
echo -e "${BOLD}2/3 Split par controller…${NC}"

mkdir -p docs/easybeer

# Liste des tags présents dans le swagger
mapfile -t TAGS < <(jq -r '.tags[].name' "$GLOBAL_OUT" | sort -u)

# Nettoyage des anciens fichiers controleur-*.json
rm -f docs/easybeer/controleur-*.json

for TAG in "${TAGS[@]}"; do
    OUT="docs/easybeer/${TAG}.json"
    # Pour chaque tag, on génère un mini-swagger autonome avec seulement les
    # paths qui ont ce tag. Préserve info/host/basePath/securityDefinitions.
    jq --arg tag "$TAG" '
        . as $root |
        {
            swagger: $root.swagger,
            info: $root.info,
            host: $root.host,
            basePath: $root.basePath,
            schemes: $root.schemes,
            consumes: $root.consumes,
            produces: $root.produces,
            tags: ($root.tags | map(select(.name == $tag))),
            paths: (
                $root.paths
                | to_entries
                | map(select(
                    [.value | to_entries[] | select(.value.tags // [] | contains([$tag]))] | length > 0
                ))
                | from_entries
            ),
            securityDefinitions: $root.securityDefinitions
        }
    ' "$GLOBAL_OUT" > "$OUT"

    N=$(jq '.paths | length' "$OUT")
    if [[ "$N" -gt 0 ]]; then
        printf "  ${GREEN}✓${NC} %-45s %d endpoints\n" "$OUT" "$N"
    else
        rm -f "$OUT"
    fi
done

# ─── 3. Régénérer INDEX.md ────────────────────────────────────────────────
echo -e "${BOLD}3/3 Régénération de INDEX.md…${NC}"

INDEX="docs/easybeer/INDEX.md"
TOTAL_ENDPOINTS=$(jq '.paths | length' "$GLOBAL_OUT")
TOTAL_POST=$(jq '[.paths[] | to_entries[] | select(.key=="post")] | length' "$GLOBAL_OUT")
TOTAL_GET=$(jq '[.paths[] | to_entries[] | select(.key=="get")] | length' "$GLOBAL_OUT")

{
cat <<EOF
# Easy Beer API — Index des fichiers

Spec Swagger 2.0 découpée en fichiers par tag.

**Régénéré automatiquement** par \`scripts/refresh_eb_swagger.sh\` (ou \`make refresh-eb-swagger\`).
**Dernière mise à jour :** $(date -u +"%Y-%m-%d %H:%M UTC")

**Base URL :** \`https://api.easybeer.fr\`
**Auth :** HTTP Basic (\`EASYBEER_API_USER\` / \`EASYBEER_API_PASS\`)
**ID brasserie :** \`EASYBEER_ID_BRASSERIE\` (\`2013\` en production)

**Stats globales :** $TOTAL_ENDPOINTS endpoints ($TOTAL_POST POST, $TOTAL_GET GET) — EB API v$VERSION

## Fichiers générés

| Fichier | Taille | Endpoints |
|---------|-------:|----------:|
EOF

for f in docs/easybeer/controleur-*.json; do
    [[ -f "$f" ]] || continue
    name="$(basename "$f")"
    size_h="$(wc -c < "$f" | awk '{ if ($1<1024) print $1"B"; else if ($1<1048576) printf "%.0fK\n", $1/1024; else printf "%.1fM\n", $1/1048576 }')"
    n="$(jq '.paths | length' "$f")"
    printf "| \`%s\` | %s | %d |\n" "$name" "$size_h" "$n"
done

cat <<EOF

## Comment utiliser

\`\`\`bash
# Voir tous les endpoints d'un controller :
jq -r '.paths | keys[]' docs/easybeer/controleur-brassin.json

# Voir la méthode + opId d'un endpoint :
jq '.paths["/brassin/mise-en-bouteille"].post' docs/easybeer/controleur-brassin.json

# Voir le schéma d'un modèle :
jq '.definitions.ModeleStockProduit' docs/easybeer-api.swagger.json
\`\`\`

## Mise à jour

Pour rafraîchir cet index et tous les fichiers :

\`\`\`bash
make refresh-eb-swagger
\`\`\`

EB évolue régulièrement (+59 endpoints en 3 mois entre fév 2026 et mai 2026).
Il est recommandé de rafraîchir mensuellement, ou avant chaque sprint qui
ajoute des appels EB nouveaux.
EOF
} > "$INDEX"

echo -e "  ${GREEN}✓${NC} $INDEX"
echo
echo -e "${GREEN}${BOLD}✅ Swagger refresh terminé.${NC}"
echo -e "   Diff avec git pour voir les changements depuis la dernière exécution :"
echo -e "   ${YELLOW}git diff --stat docs/easybeer/${NC}"
