"""
pages/test_carton_counter.py
============================
PoC admin : interface de test pour le comptage automatique de cartons
via Claude Vision.

L'opérateur (ou admin pendant le test) tape sur « Prendre une photo »,
la caméra iOS s'ouvre, la photo est resizée à 1280px puis envoyée à
``/api/count-cartons-poc``. Le résultat (nb cartons + confiance +
description) s'affiche en gros sur l'écran.

But du PoC : récolter ~20 photos de palettes réelles et mesurer la
fiabilité avant de décider d'intégrer dans /etiquettes-palette pour
remplacer la saisie manuelle des « extras sur le dessus ».
"""
from __future__ import annotations

import logging

from nicegui import app, ui

from pages.auth import require_auth
from pages.theme import COLORS, page_layout

_log = logging.getLogger("ferment.test_carton_counter")


@ui.page("/test-carton-counter")
def page_test_carton_counter():
    user = require_auth()
    if not user:
        return
    # Garde-fou admin (l'auth middleware bloque déjà via ADMIN_ONLY_PATHS,
    # double sécurité ici au cas où la liste évolue).
    role = (app.storage.user.get("role") or "").lower()
    if role != "admin":
        with page_layout("Accès refusé", "block", "/test-carton-counter"):
            ui.label("Page réservée aux admins.").classes("text-negative q-pa-md")
        return

    with page_layout("PoC comptage cartons", "photo_camera", "/test-carton-counter"):
        ui.label(
            "Prends une photo du dessus d'une palette de cartons. "
            "Claude Vision compte les cartons visibles et indique sa "
            "confiance. Sert à mesurer la fiabilité avant intégration "
            "dans /etiquettes-palette.",
        ).classes("text-body2").style(f"color: {COLORS['ink2']}")

        # ── Bouton « Prendre une photo » (caméra iOS native) ──
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

        # ── État UI : preview, loader, résultat ──
        preview_container = ui.row().classes("w-full justify-center q-mb-sm")
        result_container = ui.column().classes("w-full q-mt-md")

        def _show_uploading(_e):
            preview_container.clear()
            result_container.clear()
            with result_container:
                with ui.row().classes("w-full items-center justify-center gap-3 q-pa-md"):
                    ui.spinner("dots", size="lg", color="green-8")
                    ui.label("Analyse en cours… (1-3 sec)").classes("text-body1").style(
                        f"color: {COLORS['ink2']}",
                    )

        def _show_preview(e):
            """Affiche un thumbnail de la photo envoyée (b64 data URL)."""
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
                color = {
                    "high":   "#15803D",  # vert
                    "medium": "#D97706",  # orange
                    "low":    "#B91C1C",  # rouge
                }.get(conf, "#6B7280")
                conf_label = {
                    "high":   "ÉLEVÉE",
                    "medium": "MOYENNE",
                    "low":    "FAIBLE",
                }.get(conf, "?")

                with ui.card().classes("w-full q-pa-lg").props("flat bordered"):
                    with ui.column().classes("w-full items-center gap-0"):
                        ui.label("CARTONS DÉTECTÉS").classes("text-caption").style(
                            f"color: {COLORS['ink2']}; letter-spacing: 2px; "
                            "font-weight: 600",
                        )
                        ui.label(str(count)).style(
                            f"color: {color}; font-weight: 800; "
                            "font-size: 96px; line-height: 1; margin-top: 8px",
                        )
                        ui.label(f"Confiance : {conf_label}").classes(
                            "text-subtitle2 q-mt-sm",
                        ).style(f"color: {color}; font-weight: 600")
                        ui.separator().classes("q-my-md")
                        ui.label(desc).classes("text-body2").style(
                            f"color: {COLORS['ink']}; font-style: italic; "
                            "text-align: center; max-width: 600px",
                        )

        ui.on("carton_uploading", _show_uploading)
        ui.on("carton_preview", _show_preview)
        ui.on("carton_counted", _show_result)

        # ── JS : capture file + resize + upload + emitEvent ──
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
                try {
                    toUpload = await _resizeImage(file, 1280);
                } catch (err) {
                    toUpload = file;
                }
                // Preview pour que le user voie ce qu'il a envoyé
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
