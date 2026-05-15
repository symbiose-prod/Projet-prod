"""
pages/test_carton_counter.py
============================
PoC admin : interface de test pour le comptage automatique de cartons
via Claude Vision, avec collecte de la vérité terrain pour mesurer la
précision.

Workflow :
1. L'opérateur prend une photo du dessus d'une palette.
2. Claude Vision répond avec un nombre + confiance + description.
3. L'opérateur saisit le nombre RÉEL qu'il a compté manuellement.
4. L'écart est calculé et persisté ; les stats globales se mettent à
   jour en bas de page.

Après 15-20 essais, on a une vraie mesure (% exact, % à ±1, % à ±3,
erreur absolue moyenne) qui dit si Claude est assez fiable pour
remplacer la saisie manuelle dans /etiquettes-palette.
"""
from __future__ import annotations

import logging

from nicegui import app, ui

from common.services.carton_counter import (
    compute_eval_stats,
    list_recent_evals,
)
from pages.auth import require_auth
from pages.theme import COLORS, page_layout

_log = logging.getLogger("ferment.test_carton_counter")


@ui.page("/test-carton-counter")
def page_test_carton_counter():
    user = require_auth()
    if not user:
        return
    role = (app.storage.user.get("role") or "").lower()
    if role != "admin":
        with page_layout("Accès refusé", "block", "/test-carton-counter"):
            ui.label("Page réservée aux admins.").classes("text-negative q-pa-md")
        return

    tenant_id = user.get("tenant_id", "")

    with page_layout("PoC comptage cartons", "photo_camera", "/test-carton-counter"):
        ui.label(
            "Prends une photo du dessus d'une palette, puis saisis le "
            "nombre réel que tu comptes. Les stats en bas mesurent la "
            "fiabilité de Claude Vision en temps réel.",
        ).classes("text-body2").style(f"color: {COLORS['ink2']}")

        # ── État partagé entre les sections ──
        # current_eval contient l'eval_id renvoyé par /api/count-cartons-poc.
        # Il sert au POST /eval ultérieur (saisie du réel).
        state: dict = {"current_eval_id": None, "current_claude_count": None}

        # ── Bouton « Prendre une photo » ──
        with ui.row().classes("w-full justify-center q-mt-md q-mb-md"):
            ui.html(
                '<label '
                'style="display:inline-flex; align-items:center; gap:12px; '
                'padding:22px 36px; background:#15803D; color:white; '
                'border-radius:12px; cursor:pointer; font-size:20px; '
                'font-weight:600; user-select:none; position:relative; '
                'overflow:hidden; box-shadow:0 4px 12px rgba(21,128,61,0.3); '
                '-webkit-tap-highlight-color: rgba(255,255,255,0.2); '
                'touch-action: manipulation;">'
                '<span class="material-icons" style="font-size:32px;">photo_camera</span>'
                'Prendre une photo'
                '<input type="file" id="carton-count-input" '
                'accept="image/*" capture="environment" '
                'style="position:absolute; inset:0; opacity:0; cursor:pointer; '
                'width:100%; height:100%;">'
                '</label>',
            )

        # ── Sections : preview, résultat Claude, saisie réel ──
        preview_container = ui.row().classes("w-full justify-center q-mb-sm")
        result_container = ui.column().classes("w-full q-mt-md")
        eval_input_container = ui.column().classes("w-full q-mt-md")

        # ── Section stats + historique (refresh après chaque eval) ──
        stats_container = ui.column().classes("w-full q-mt-lg").style(
            f"border-top: 1px solid {COLORS['border']}; padding-top: 16px",
        )

        def _show_uploading(_e):
            preview_container.clear()
            result_container.clear()
            eval_input_container.clear()
            with result_container:
                with ui.row().classes(
                    "w-full items-center justify-center gap-3 q-pa-md",
                ):
                    ui.spinner("dots", size="lg", color="green-8")
                    ui.label("Analyse en cours… (1-3 sec)").classes(
                        "text-body1",
                    ).style(f"color: {COLORS['ink2']}")

        def _show_preview(e):
            data_url = e.args or ""
            if not data_url:
                return
            preview_container.clear()
            with preview_container:
                ui.image(data_url).style(
                    "max-width: 300px; max-height: 300px; "
                    "border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1)",
                )

        def _show_result(e):
            data = e.args or {}
            result_container.clear()
            eval_input_container.clear()
            with result_container:
                if data.get("error"):
                    with ui.card().classes("w-full q-pa-md").style(
                        "background: #FEF2F2; border: 1px solid #FCA5A5",
                    ):
                        ui.label("❌ Erreur").classes("text-subtitle1").style(
                            "color: #B91C1C; font-weight: 700",
                        )
                        ui.label(str(data.get("error"))).classes("text-body2").style(
                            "color: #7F1D1D",
                        )
                    return

                count = data.get("count")
                if count is None:
                    return
                conf = (data.get("confidence") or "low").lower()
                desc = data.get("description") or ""
                state["current_eval_id"] = data.get("eval_id")
                state["current_claude_count"] = int(count)

                color = {
                    "high":   "#15803D",
                    "medium": "#D97706",
                    "low":    "#B91C1C",
                }.get(conf, "#6B7280")
                conf_label = {
                    "high": "ÉLEVÉE", "medium": "MOYENNE", "low": "FAIBLE",
                }.get(conf, "?")

                with ui.card().classes("w-full q-pa-lg").props("flat bordered"):
                    with ui.column().classes("w-full items-center gap-0"):
                        ui.label("CLAUDE A COMPTÉ").classes("text-caption").style(
                            f"color: {COLORS['ink2']}; letter-spacing: 2px; "
                            "font-weight: 600",
                        )
                        ui.label(str(count)).style(
                            f"color: {color}; font-weight: 800; "
                            "font-size: 88px; line-height: 1; margin-top: 8px",
                        )
                        ui.label(f"Confiance : {conf_label}").classes(
                            "text-subtitle2 q-mt-sm",
                        ).style(f"color: {color}; font-weight: 600")
                        if desc:
                            ui.separator().classes("q-my-md")
                            ui.label(desc).classes("text-body2").style(
                                f"color: {COLORS['ink']}; font-style: italic; "
                                "text-align: center; max-width: 600px",
                            )

            _render_eval_input()

        def _render_eval_input():
            """Affiche le champ « Combien tu en comptes ? » + bouton."""
            eval_input_container.clear()
            if state.get("current_eval_id") is None:
                # DB en panne au moment du save_eval_attempt → pas
                # d'eval_id, donc pas de comparaison possible.
                with eval_input_container:
                    ui.label(
                        "⚠ Comparaison désactivée (eval non enregistrée).",
                    ).classes("text-caption").style(f"color: {COLORS['ink2']}")
                return
            with eval_input_container:
                with ui.card().classes("w-full q-pa-md").props("flat bordered").style(
                    "background: #F0F9FF",
                ):
                    ui.label(
                        "Maintenant, compte manuellement le nombre réel "
                        "de cartons sur la palette et saisis-le ici :",
                    ).classes("text-body2 q-mb-sm").style(
                        f"color: {COLORS['ink']}",
                    )
                    with ui.row().classes("w-full items-end gap-3"):
                        real_input = ui.number(
                            label="Nombre réel", min=0, max=999, step=1,
                            value=state.get("current_claude_count") or 0,
                        ).props("outlined dense").style("max-width: 160px")
                        validate_btn = ui.button(
                            "Enregistrer la comparaison",
                            icon="check",
                        ).props("color=blue-7 unelevated")

                    feedback_row = ui.row().classes("w-full q-mt-sm")

                    async def _do_save():
                        validate_btn.disable()
                        validate_btn.props("loading")
                        try:
                            rc = int(real_input.value or 0)
                        except (TypeError, ValueError):
                            ui.notify("Saisis un nombre entier.", type="warning")
                            validate_btn.enable()
                            validate_btn.props(remove="loading")
                            return
                        try:
                            resp = await ui.run_javascript(
                                f"""
                                fetch('/api/count-cartons-poc/eval', {{
                                    method: 'POST',
                                    headers: {{'Content-Type': 'application/json'}},
                                    body: JSON.stringify({{
                                        eval_id: {int(state['current_eval_id'])},
                                        real_count: {rc}
                                    }}),
                                }}).then(r => r.json())
                                """,
                                timeout=10.0,
                            )
                        except Exception as exc:
                            ui.notify(f"Erreur réseau : {exc}", type="negative")
                            validate_btn.enable()
                            validate_btn.props(remove="loading")
                            return
                        validate_btn.enable()
                        validate_btn.props(remove="loading")
                        if not isinstance(resp, dict) or not resp.get("ok"):
                            err = (resp or {}).get("error", "erreur inconnue")
                            ui.notify(f"Échec enregistrement : {err}",
                                      type="negative")
                            return

                        feedback_row.clear()
                        claude_count = int(state.get("current_claude_count") or 0)
                        err = abs(claude_count - rc)
                        if err == 0:
                            color, msg = "#15803D", f"✓ Match exact ! Claude = réel = {rc}"
                        elif err <= 1:
                            color, msg = "#D97706", f"⚠ Écart de 1 (Claude {claude_count}, réel {rc})"
                        else:
                            color, msg = "#B91C1C", f"❌ Écart de {err} (Claude {claude_count}, réel {rc})"
                        with feedback_row:
                            ui.label(msg).classes("text-subtitle2").style(
                                f"color: {color}; font-weight: 600",
                            )
                        # Reset état pour éviter double-validation
                        state["current_eval_id"] = None
                        _refresh_stats()

                    validate_btn.on_click(_do_save)

        def _render_stats():
            """Section stats + historique des 15 derniers essais."""
            stats_container.clear()
            with stats_container:
                try:
                    stats = compute_eval_stats(tenant_id)
                    recent = list_recent_evals(tenant_id, limit=15)
                except Exception as exc:
                    _log.warning("Échec stats/historique", exc_info=True)
                    ui.label(f"Erreur chargement stats : {exc}").classes(
                        "text-negative",
                    )
                    return

                ui.label("PRÉCISION CUMULÉE").classes("text-overline").style(
                    f"color: {COLORS['ink2']}; letter-spacing: 1px; "
                    "font-weight: 700",
                )
                if stats.total_with_real == 0:
                    ui.label(
                        "Aucune comparaison enregistrée pour l'instant. "
                        "Prends une photo, puis saisis le nombre réel.",
                    ).classes("text-body2 q-mt-xs").style(
                        f"color: {COLORS['ink2']}; font-style: italic",
                    )
                else:
                    with ui.row().classes("w-full gap-4 q-mt-sm"):
                        for label, value in [
                            ("Évals", str(stats.total_with_real)),
                            ("Exact", f"{stats.exact_match_pct:.0f}%"),
                            ("≤ ±1", f"{stats.within_one_pct:.0f}%"),
                            ("≤ ±3", f"{stats.within_three_pct:.0f}%"),
                            ("Erreur moy.", f"{stats.avg_abs_error:.1f}"),
                        ]:
                            with ui.card().classes(
                                "q-pa-sm flex-1 items-center",
                            ).props("flat bordered"):
                                ui.label(label).classes("text-caption").style(
                                    f"color: {COLORS['ink2']}",
                                )
                                ui.label(value).style(
                                    f"color: {COLORS['ink']}; font-weight: 700; "
                                    "font-size: 22px",
                                )

                # Historique
                if recent:
                    ui.label("DERNIERS ESSAIS").classes("text-overline q-mt-md").style(
                        f"color: {COLORS['ink2']}; letter-spacing: 1px; "
                        "font-weight: 700",
                    )
                    cols = [
                        {"name": "when", "label": "Quand", "field": "when",
                         "align": "left"},
                        {"name": "claude", "label": "Claude", "field": "claude",
                         "align": "right"},
                        {"name": "real", "label": "Réel", "field": "real",
                         "align": "right"},
                        {"name": "err", "label": "Écart", "field": "err",
                         "align": "right"},
                        {"name": "conf", "label": "Conf", "field": "conf",
                         "align": "center"},
                    ]
                    rows_ui = []
                    from common.ramasse import fmt_paris as _fmt_paris
                    for e in recent:
                        when = _fmt_paris(e.created_at, "%d/%m %H:%M") or "?"
                        if e.real_count is None:
                            real_str = "—"
                            err_str = "?"
                        else:
                            err = abs(e.claude_count - e.real_count)
                            real_str = str(e.real_count)
                            err_str = "✓" if err == 0 else f"±{err}"
                        rows_ui.append({
                            "id": e.id,
                            "when": when,
                            "claude": e.claude_count,
                            "real": real_str,
                            "err": err_str,
                            "conf": (e.claude_confidence or "?")[:3].upper(),
                        })
                    ui.table(
                        columns=cols, rows=rows_ui, row_key="id",
                        pagination={"rowsPerPage": 0},
                    ).classes("w-full q-mt-xs").props("flat bordered dense")

        def _refresh_stats():
            _render_stats()

        ui.on("carton_uploading", _show_uploading)
        ui.on("carton_preview", _show_preview)
        ui.on("carton_counted", _show_result)

        # JS : capture + resize + upload (identique à la version précédente)
        ui.add_body_html("""
<script>
(function() {
    if (window._fsCartonInputBound) return;
    window._fsCartonInputBound = true;

    async function _resizeImage(file, maxDim) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onerror = () => reject(new Error('FileReader'));
            reader.onload = () => {
                const img = new Image();
                img.onerror = () => reject(new Error('Image load'));
                img.onload = () => {
                    let w = img.naturalWidth, h = img.naturalHeight;
                    const scale = Math.min(1, maxDim / Math.max(w, h));
                    w = Math.round(w * scale); h = Math.round(h * scale);
                    const canvas = document.createElement('canvas');
                    canvas.width = w; canvas.height = h;
                    canvas.getContext('2d').drawImage(img, 0, 0, w, h);
                    canvas.toBlob(
                        (blob) => blob ? resolve(blob) : reject(new Error('toBlob')),
                        'image/jpeg', 0.85,
                    );
                };
                img.src = reader.result;
            };
            reader.readAsDataURL(file);
        });
    }

    async function _blobToDataUrl(blob) {
        return new Promise((resolve, reject) => {
            const r = new FileReader();
            r.onerror = () => reject(new Error('FileReader'));
            r.onload = () => resolve(r.result);
            r.readAsDataURL(blob);
        });
    }

    const wait = () => {
        const input = document.getElementById('carton-count-input');
        if (!input) { setTimeout(wait, 200); return; }
        input.addEventListener('change', async (e) => {
            const file = e.target.files && e.target.files[0];
            if (!file) return;
            try {
                let toUpload;
                try { toUpload = await _resizeImage(file, 1280); }
                catch (err) { toUpload = file; }
                try {
                    const dataUrl = await _blobToDataUrl(toUpload);
                    emitEvent('carton_preview', dataUrl);
                } catch (err) { /* noop */ }

                emitEvent('carton_uploading', toUpload.size);

                const formData = new FormData();
                formData.append('file', toUpload, 'photo.jpg');
                const resp = await fetch('/api/count-cartons-poc', {
                    method: 'POST', body: formData,
                });
                const data = await resp.json();
                emitEvent('carton_counted', data);
            } catch (err) {
                emitEvent('carton_counted', {error: String(err)});
            }
            e.target.value = '';
        });
    };
    wait();
})();
</script>
""")

        # Render initial des stats
        _render_stats()
