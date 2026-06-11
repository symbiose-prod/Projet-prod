#!/usr/bin/env bash
# .claude/hooks/pre-push-verify.sh
#
# PreToolUse hook — lance les guards d'architecture avant chaque `git push`.
# Bloque le push si une règle est enfreinte (même principe que le CI, mais
# en local pour éviter un round-trip CI rouge).
#
# Format attendu : stdin = JSON du tool call Claude Code. Exit 0 = autorise,
# exit 2 = bloque avec message sur stderr.
#
# Wiring : référencé depuis .claude/settings.json "hooks.PreToolUse".

set -euo pipefail

# Lire le JSON du tool call depuis stdin
INPUT=$(cat)

# On ne déclenche QUE sur les commandes "git push" — les autres Bash passent
# sans interception.
if ! echo "$INPUT" | grep -qE '"command"\s*:\s*"[^"]*git push'; then
    exit 0
fi

# Localiser la racine du projet. On se fie à CLAUDE_PROJECT_DIR qui est posé
# par Claude Code ; fallback sur le cwd si absent.
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$PROJECT_ROOT"

# Interpréteur : le venv du projet s'il existe (pytest y est installé),
# sinon python3 système — évite un faux blocage quand pytest n'est pas global.
PYTHON="python3"
if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    PYTHON="$PROJECT_ROOT/.venv/bin/python"
fi

# Les guards d'architecture eux-mêmes (4 tests, ~100 ms).
echo "→ pre-push: running architecture guards..." >&2
if ! "$PYTHON" -m pytest tests/test_architecture_layers.py -q 2>&1 | tee /tmp/pre-push-output.txt; then
    echo "" >&2
    echo "✗ Architecture guards failed — push bloqué." >&2
    echo "  Corrige les violations (voir output ci-dessus) avant de repousser." >&2
    exit 2
fi

echo "✓ Architecture guards OK, push autorisé." >&2
exit 0
