"""
common/services/
================
Couche "services" (domaine métier) entre la couche transport/API
(``common/easybeer``, ``db``) et la couche UI (``pages/``).

Règles :
- Aucune dépendance vers ``nicegui`` ou ``pages/`` (sauf TYPE_CHECKING).
- Tous les appels réseau passent par ``common/easybeer`` ou ``db.conn``.
- Les services peuvent se composer entre eux.
- Testables unitairement avec mocks légers (pas de setup NiceGUI).

Modules :
- stocks_service — calcul autonomie stocks contenants + BOM + propositions commande.
"""
