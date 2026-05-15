"""
pages/test_douchette.py
=======================
PoC admin : tester l'intégration d'une douchette code-barre Bluetooth
(BCST-72, Honeywell, etc.) avec Ferment Station.

Workflow attendu :
1. L'opérateur appaire sa douchette à l'iPad via Réglages → Bluetooth
   (la douchette apparaît comme « Keyboard »).
2. Sur cette page, le curseur est placé dans le grand champ central.
3. L'opérateur scanne un code-barres → les caractères tapés par la
   douchette atterrissent dans le champ, et un Enter final déclenche le
   décodage.
4. Le système affiche le type détecté (SSCC, EAN-13, GS1-128, URL…) +
   les AIs parsés (pour les GS1-128) + un historique des 10 derniers
   scans.

Sert à valider qu'une nouvelle douchette est compatible avec le format
GS1-128 / FNC1 utilisé par les étiquettes palette **avant** de
l'intégrer dans /chargement-camion ou /etiquettes-palette.

Wake Lock API activé : l'iPad ne se met plus en veille tant que la
page est ouverte (utile en test, indispensable en chargement réel).
"""
from __future__ import annotations

import logging

from nicegui import app, ui

from pages.auth import require_auth
from pages.theme import COLORS, install_wake_lock, page_layout

_log = logging.getLogger("ferment.test_douchette")


# Exemples à scanner sans étiquette physique. La génération du code
# se fait côté navigateur (JsBarcode pour 1D, qrcode-generator pour
# 2D) pour éviter la dépendance Ghostscript côté serveur.
#
# ``js_format`` :
#   - 'CODE128' : Code128 brut. On lui passe la chaîne avec parenthèses
#     pour qu'elles soient visibles sous le code-barres (HRI). Le
#     parser Ferment Station détecte le format HRI parenthèses sans
#     ambiguïté.
#   - 'EAN13' : standard EAN-13 (13 digits).
#   - 'QR' : QR code via qrcode-generator.
_SAMPLE_CODES = [
    {
        "title": "SSCC palette seul (AI 00)",
        "js_format": "CODE128",
        "data": "(00)337700144200000005",
        "note": "Reproduit le scan AI 00 d'une palette logistique nue.",
    },
    {
        "title": "Étiquette palette complète (SSCC + GTIN + DDM + Lot + Count)",
        "js_format": "CODE128",
        "data": "(00)337700144200000005(02)23770014427049(15)270508(10)L080527(37)126",
        "note": "Format exact d'une étiquette palette Ferment Station.",
    },
    {
        "title": "EAN-13 carton (style supermarché)",
        "js_format": "EAN13",
        "data": "3770014427250",
        "note": "Format d'un carton individuel — pas une palette.",
    },
    {
        "title": "QR code URL (test 2D)",
        "js_format": "QR",
        "data": "https://prod.symbiose-kefir.fr/test-douchette",
        "note": "Valide que la BCST-72 lit aussi les codes 2D.",
    },
]


@ui.page("/test-douchette")
def page_test_douchette():
    user = require_auth()
    if not user:
        return
    role = (app.storage.user.get("role") or "").lower()
    if role != "admin":
        with page_layout("Accès refusé", "block", "/test-douchette"):
            ui.label("Page réservée aux admins.").classes("text-negative q-pa-md")
        return

    with page_layout("Test douchette code-barre", "qr_code_scanner", "/test-douchette"):
        ui.label(
            "Appaire ta douchette à l'iPad via Réglages → Bluetooth, "
            "puis place le curseur dans le champ ci-dessous et scanne. "
            "Le contenu sera décodé en temps réel.",
        ).classes("text-body2").style(f"color: {COLORS['ink2']}")

        # ── Champ de capture + bouton clear ──
        with ui.row().classes("w-full items-end gap-3 q-mt-md"):
            scan_input = ui.input(
                label="Scan ici (champ focus auto)",
                placeholder="Le résultat de la douchette apparaîtra ici…",
            ).classes("flex-1").props(
                "outlined autofocus clearable",
            ).style("font-family: monospace; font-size: 16px")
            ui.button(
                "Effacer", icon="clear",
                on_click=lambda: (scan_input.set_value(""), result_card.clear()),
            ).props("outline color=grey-7")

        # ── Résultat du dernier scan ──
        result_card = ui.column().classes("w-full q-mt-md")

        # ── Historique ──
        history_container = ui.column().classes("w-full q-mt-md")

        # State : 10 derniers scans
        history: list[dict] = []

        def _render_result(data: dict):
            result_card.clear()
            with result_card:
                _render_decoded_card(data)

        def _append_history(data: dict):
            history.insert(0, data)
            del history[10:]
            history_container.clear()
            with history_container:
                if not history:
                    return
                ui.label("HISTORIQUE (10 derniers)").classes("text-overline").style(
                    f"color: {COLORS['ink2']}; letter-spacing: 1px; "
                    "font-weight: 700",
                )
                for entry in history:
                    with ui.row().classes(
                        "w-full items-center gap-3 q-pa-sm",
                    ).style(
                        f"border-top: 1px solid {COLORS['border']}",
                    ):
                        ui.badge(
                            (entry.get("type") or "?").upper(),
                            color=_color_for_type(entry.get("type")),
                        ).props("outline")
                        ui.label(
                            entry.get("summary") or "(vide)",
                        ).classes("flex-1 text-body2").style(
                            f"color: {COLORS['ink']}",
                        )
                        ui.label(
                            (entry.get("raw") or "")[:80],
                        ).classes("text-caption").style(
                            f"color: {COLORS['ink2']}; font-family: monospace",
                        )

        async def _decode_scan_async(raw: str):
            if not raw:
                return
            try:
                resp = await ui.run_javascript(
                    f"""
                    fetch('/api/test-douchette-decode', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{raw: {repr(raw)}}}),
                    }}).then(r => r.json())
                    """,
                    timeout=10.0,
                )
            except Exception as exc:
                _log.exception("Decode échec")
                ui.notify(f"Erreur décodage : {exc}", type="negative")
                return
            if not isinstance(resp, dict) or resp.get("error"):
                err = (resp or {}).get("error", "erreur inconnue")
                ui.notify(f"Erreur : {err}", type="negative")
                return
            _render_result(resp)
            _append_history(resp)

        def _on_scan_enter(_e=None):
            raw = scan_input.value or ""
            if raw:
                # Capture puis vide le champ pour le scan suivant
                scan_input.set_value("")

                async def _run():
                    await _decode_scan_async(raw)

                ui.timer(0.01, _run, once=True)

        # Déclenche le décodage quand l'utilisateur valide (Enter de la
        # douchette OU touche Entrée au clavier).
        scan_input.on("keydown.enter", _on_scan_enter)

        # ── Exemples GS1-128 affichés à scanner ──
        ui.separator().classes("q-my-lg")
        ui.label("EXEMPLES À SCANNER DEPUIS UN ÉCRAN").classes(
            "text-overline",
        ).style(
            f"color: {COLORS['ink2']}; letter-spacing: 1px; font-weight: 700",
        )
        ui.label(
            "Affiche cette page sur un écran (Mac, ordinateur portable) "
            "et scanne les codes-barres depuis l'iPad. Permet de tester "
            "sans étiquettes palette physiques.",
        ).classes("text-body2 q-mb-md").style(f"color: {COLORS['ink2']}")

        # Pour chaque exemple : on émet un placeholder DOM avec un id
        # unique, et un payload JSON. Le script en bas de page lit les
        # data-attributes et appelle JsBarcode / qrcode-generator pour
        # remplir le placeholder (rendu côté navigateur — pas de
        # dépendance Ghostscript côté serveur).
        import html
        for idx, sample in enumerate(_SAMPLE_CODES):
            placeholder_id = f"fs-sample-bc-{idx}"
            with ui.card().classes("w-full q-pa-md q-mb-md").props(
                "flat bordered",
            ):
                with ui.column().classes("w-full gap-1"):
                    ui.label(sample["title"]).classes("text-subtitle1").style(
                        f"color: {COLORS['ink']}; font-weight: 600",
                    )
                    ui.label(sample["note"]).classes("text-caption").style(
                        f"color: {COLORS['ink2']}; font-style: italic",
                    )
                    ui.label(sample["data"]).classes("text-caption").style(
                        f"color: {COLORS['ink2']}; font-family: monospace; "
                        "word-break: break-all",
                    )
                    # Placeholder qui sera rempli par le JS en bas de page.
                    # On encode data + format dans des data-attributes pour
                    # éviter toute injection / problème de quoting.
                    ui.html(
                        f'<div id="{placeholder_id}" '
                        f'class="fs-sample-barcode" '
                        f'data-format="{html.escape(sample["js_format"])}" '
                        f'data-payload="{html.escape(sample["data"])}" '
                        f'style="background: white; padding: 12px; '
                        f'border-radius: 8px; min-height: 80px; '
                        f'display: flex; align-items: center; '
                        f'justify-content: center;">'
                        '<span style="color:#9CA3AF;font-style:italic">'
                        'Génération…</span></div>',
                    )

        # Inclusion des libs JS (JsBarcode pour 1D, qrcode-generator pour
        # 2D) + script qui parcourt les placeholders et fait le rendu.
        # Libs servies en local depuis /static/vendor/ (vs CDN) — évite
        # tout blocage CSP / réseau côté iPad.
        ui.add_body_html("""
<script src="/static/vendor/JsBarcode.all.min.js"></script>
<script src="/static/vendor/qrcode.min.js"></script>
<script>
(function() {
    function _renderBarcodes() {
        const els = document.querySelectorAll('.fs-sample-barcode');
        els.forEach((el) => {
            if (el.dataset.rendered === '1') return;
            const format = el.dataset.format;
            const payload = el.dataset.payload;
            try {
                if (format === 'QR') {
                    if (typeof qrcode === 'undefined') return;
                    const qr = qrcode(0, 'M');
                    qr.addData(payload);
                    qr.make();
                    el.innerHTML = qr.createImgTag(6, 12);
                } else {
                    if (typeof JsBarcode === 'undefined') return;
                    // Crée un SVG inline, plus net pour le scan sur écran.
                    el.innerHTML = '<svg></svg>';
                    JsBarcode(el.querySelector('svg'), payload, {
                        format: format,
                        width: 2.5,
                        height: 100,
                        displayValue: true,
                        fontSize: 14,
                        margin: 12,
                    });
                }
                el.dataset.rendered = '1';
            } catch (err) {
                el.innerHTML = '<span style="color:#B91C1C">' +
                    'Erreur rendu : ' + (err.message || err) + '</span>';
            }
        });
    }
    // Attend que les libs CDN soient chargées (parfois retardé).
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _renderBarcodes);
    } else {
        _renderBarcodes();
    }
    // Re-essai après 500ms au cas où les libs n'étaient pas prêtes.
    setTimeout(_renderBarcodes, 500);
    setTimeout(_renderBarcodes, 1500);
})();
</script>
""")

        # Wake Lock : iPad reste allumé tant que la page est ouverte.
        install_wake_lock()


# ─── Rendu d'une carte de résultat décodé ───────────────────────────────────

def _color_for_type(type_str: str | None) -> str:
    return {
        "sscc": "green-8",
        "gs1_128_hri": "green-8",
        "gs1_128_raw": "green-7",
        "ean13": "blue-7",
        "gtin14": "blue-7",
        "url": "purple-7",
        "text": "grey-7",
        "empty": "grey-5",
    }.get(type_str or "", "orange-8")


def _render_decoded_card(data: dict) -> None:
    """Affiche le résultat structuré d'un scan."""
    type_str = data.get("type") or "unknown"
    summary = data.get("summary") or ""
    raw = data.get("raw") or ""
    normalized = data.get("normalized") or ""
    ais = data.get("ais") or {}
    sscc = data.get("sscc") or ""
    color = _color_for_type(type_str)

    with ui.card().classes("w-full q-pa-md").props("flat bordered"):
        with ui.row().classes("w-full items-center gap-3"):
            ui.icon("check_circle", size="lg").style("color: #15803D")
            ui.label(summary).classes("text-h6 flex-1").style(
                f"color: {COLORS['ink']}; font-weight: 600",
            )
            ui.badge(type_str.upper(), color=color).props("outline").style(
                "font-size: 12px",
            )

        ui.separator().classes("q-my-sm")

        with ui.column().classes("w-full gap-1"):
            _kv("Brut reçu", raw)
            if normalized != raw:
                _kv("Normalisé (FNC1 → |)", normalized)
            if sscc:
                _kv("SSCC extrait", sscc)
            if ais:
                ui.label("AIs parsés :").classes("text-caption q-mt-xs").style(
                    f"color: {COLORS['ink2']}; font-weight: 600",
                )
                for ai, val in ais.items():
                    label = _AI_FRIENDLY.get(ai, f"AI {ai}")
                    _kv(f"  ({ai}) {label}", val)


def _kv(label: str, value: str) -> None:
    with ui.row().classes("w-full gap-2"):
        ui.label(f"{label} :").classes("text-caption").style(
            f"color: {COLORS['ink2']}; min-width: 200px",
        )
        ui.label(value).classes("text-body2").style(
            f"color: {COLORS['ink']}; font-family: monospace; "
            "word-break: break-all",
        )


_AI_FRIENDLY: dict[str, str] = {
    "00": "SSCC palette",
    "01": "GTIN colis",
    "02": "GTIN contenu",
    "10": "Lot",
    "11": "Date production",
    "13": "Date emballage",
    "15": "DDM",
    "17": "Date expiration",
    "21": "N° de série",
    "30": "Quantité",
    "37": "Nombre d'unités",
    "240": "Réf. supplémentaire",
    "241": "Réf. client",
}
