.PHONY: test lint fix typecheck audit ci

## Lancer les tests
test:
	python3 -m pytest tests/ -v --tb=short

## Vérifier le style (ruff)
lint:
	ruff check .

## Corriger automatiquement les erreurs de style
fix:
	ruff check --fix .

## Vérification des types (mypy) — modules business + DB
typecheck:
	python3 -m mypy common/data.py common/ramasse.py common/brassin_builder.py common/email.py db/conn.py

## Audit de sécurité des dépendances
audit:
	pip-audit -r requirements.txt

## Pipeline CI locale (lint + typecheck + tests)
ci: lint typecheck test
