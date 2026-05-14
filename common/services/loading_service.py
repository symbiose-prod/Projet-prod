"""
common/services/loading_service.py
==================================
Service métier : chargement camion par scan SSCC palette.

Workflow :
  1. L'opérateur scanne le SSCC palette d'une palette en attente.
  2. ``lookup_sscc()`` résout les infos (produit, lot, DDM, cartons) et
     vérifie qu'elle n'est pas déjà chargée sur une autre ramasse.
  3. La palette est ajoutée à un "panier" UI (côté front).
  4. Une fois toutes les palettes scannées, ``commit_loading()`` :
     - INSERT les palettes dans ``palette_loadings`` avec le ramasse_id
     - Crée ou met-à-jour la ramasse (réutilise ``save_ramasse`` /
       ``update_ramasse`` de common.ramasse_history)
     - L'appelant gère l'email + PDF + download

Sans NiceGUI — utilisable depuis CLI / tests.
"""
from __future__ import annotations

import datetime as _dt
import io
import logging
import re
import warnings
from dataclasses import dataclass

from db.conn import run_sql

_log = logging.getLogger("ferment.services.loading")


# ─── Modèles typés ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PaletteInfo:
    """Infos consolidées d'une palette identifiée par son SSCC."""
    sscc: str                       # 18 digits
    gtin_palette: str               # GTIN colis (carton)
    lot: str
    ddm: _dt.date | None
    case_count: int                 # nb cartons sur la palette
    designation: str                # libellé produit (ex: "Kéfir Pêche")
    fmt: str                        # ex: "6x75"
    marque: str
    gout: str
    pcb: int
    gtin_uvc: str
    generated_at: _dt.datetime      # quand la palette a été étiquetée


@dataclass(frozen=True)
class LookupResult:
    """Résultat de la résolution d'un SSCC scanné."""
    status: str                     # 'ok' | 'unknown' | 'already_loaded' | 'inconsistent'
    palette: PaletteInfo | None = None
    # Si already_loaded : id de la ramasse qui détient déjà cette palette
    existing_ramasse_id: str | None = None
    existing_scanned_at: _dt.datetime | None = None
    error_message: str = ""


# ─── Lookup principal : scan SSCC → résolution ──────────────────────────────

_SSCC_DIGIT_RE = re.compile(r"\D+")


def _normalize_sscc(raw: str) -> str:
    """Nettoie une chaîne SSCC : retire (00) AI prefix + espaces + tirets."""
    s = _SSCC_DIGIT_RE.sub("", raw or "")
    # Si on a 20 digits (AI 00 + SSCC 18), prendre les 18 derniers
    if len(s) == 20 and s.startswith("00"):
        s = s[2:]
    return s


def lookup_sscc(sscc_raw: str, tenant_id: str) -> LookupResult:
    """Cherche une palette par SSCC et vérifie son statut de chargement.

    Args:
        sscc_raw: SSCC tel que scanné — peut contenir des séparateurs ou
                  le préfixe AI ``(00)``. Sera normalisé.
        tenant_id: scope tenant.

    Returns:
        LookupResult avec status :
          - 'ok' : palette trouvée, libre, infos remplies
          - 'unknown' : SSCC absent de sscc_log (cas 4.1)
          - 'already_loaded' : palette déjà liée à une ramasse (cas 4.3)
          - 'inconsistent' : sscc_log existe mais etiquette_palette_history
            manquante — anomalie DB
    """
    sscc = _normalize_sscc(sscc_raw)
    if len(sscc) != 18 or not sscc.isdigit():
        return LookupResult(
            status="unknown",
            error_message=f"SSCC invalide ({len(sscc)} digits, attendu 18)",
        )

    rows = run_sql(
        """SELECT
              sl.sscc, sl.gtin_palette, sl.lot, sl.ddm,
              sl.case_count, sl.generated_at, sl.voided_at,
              eph.designation, eph.fmt, eph.marque, eph.gout,
              eph.pcb, eph.gtin_uvc,
              pl.ramasse_id AS pl_ramasse_id,
              pl.scanned_at AS pl_scanned_at
           FROM sscc_log sl
           LEFT JOIN etiquette_palette_history eph
                  ON eph.sscc = sl.sscc AND eph.tenant_id = sl.tenant_id
           LEFT JOIN palette_loadings pl
                  ON pl.sscc = sl.sscc
                 AND pl.tenant_id = sl.tenant_id
                 AND pl.unlinked_at IS NULL
           WHERE sl.sscc = :sscc AND sl.tenant_id = :t
           LIMIT 1""",
        {"sscc": sscc, "t": tenant_id},
    ) or []

    if not rows:
        return LookupResult(
            status="unknown",
            error_message="SSCC inconnu dans le journal — palette pas générée ou problème DB",
        )

    r = rows[0]

    # Si la palette a été annulée (fantôme), on traite comme inconnu pour
    # éviter de l'inclure dans le chargement.
    if r.get("voided_at"):
        return LookupResult(
            status="unknown",
            error_message=(
                "Cette palette a été annulée (étiquette fantôme) — "
                "ne pas la charger."
            ),
        )

    # Cas 4.3 — palette déjà chargée
    if r.get("pl_ramasse_id"):
        return LookupResult(
            status="already_loaded",
            existing_ramasse_id=str(r["pl_ramasse_id"]),
            existing_scanned_at=r.get("pl_scanned_at"),
            error_message="Palette déjà chargée sur une autre ramasse",
        )

    # Cas anomalie : sscc_log présent mais pas etiquette_palette_history
    if not r.get("designation"):
        return LookupResult(
            status="inconsistent",
            error_message=(
                "SSCC connu mais infos produit manquantes dans l'historique — "
                "anomalie DB, à investiguer"
            ),
        )

    ddm = r.get("ddm")
    ddm_date = ddm if isinstance(ddm, _dt.date) or ddm is None \
        else _dt.date.fromisoformat(str(ddm)[:10])

    palette = PaletteInfo(
        sscc=str(r["sscc"]),
        gtin_palette=str(r.get("gtin_palette") or ""),
        lot=str(r.get("lot") or ""),
        ddm=ddm_date,
        case_count=int(r.get("case_count") or 0),
        designation=str(r.get("designation") or ""),
        fmt=str(r.get("fmt") or ""),
        marque=str(r.get("marque") or ""),
        gout=str(r.get("gout") or ""),
        pcb=int(r.get("pcb") or 0),
        gtin_uvc=str(r.get("gtin_uvc") or ""),
        generated_at=r["generated_at"],
    )
    return LookupResult(status="ok", palette=palette)


def lookup_sscc_from_image(image_bytes: bytes, tenant_id: str) -> LookupResult:
    """Décode une image (caméra iPhone), extrait l'AI 00 (SSCC) et appelle
    ``lookup_sscc``. Retourne 'unknown' si pas de SSCC trouvé.
    """
    try:
        import zxingcpp
        from PIL import Image
    except ImportError as exc:
        _log.error("zxing-cpp ou Pillow indisponible : %s", exc)
        return LookupResult(status="unknown", error_message="Décodeur indisponible")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(image_bytes))
            img.load()
            if img.mode not in ("L", "RGB"):
                img = img.convert("RGB")
            results = zxingcpp.read_barcodes(img)
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        return LookupResult(
            status="unknown",
            error_message="Image trop grosse — rejetée",
        )
    except Exception:
        _log.exception("Erreur décodage image scan SSCC")
        return LookupResult(status="unknown", error_message="Décodage image échoué")

    if not results:
        return LookupResult(status="unknown", error_message="Aucun code-barres détecté")

    # On cherche l'AI 00 (SSCC) dans les barcodes décodés. Si on trouve
    # plusieurs codes-barres, on prend le premier qui contient AI 00.
    sscc_pattern = re.compile(r"\(00\)(\d{18})")
    for r in results:
        text = (r.text or "").strip()
        m = sscc_pattern.search(text)
        if m:
            return lookup_sscc(m.group(1), tenant_id)

    # Pas d'AI 00 trouvé — peut-être un GS1-128 produit (AI 02) scanné par
    # erreur, ou un EAN-13 simple. On retourne unknown.
    return LookupResult(
        status="unknown",
        error_message="Pas de SSCC (AI 00) trouvé dans le code-barres scanné",
    )


# ─── Vérifs sur une liste (panier UI) ───────────────────────────────────────

def aggregate_palettes_to_lines(
    palettes: list[PaletteInfo],
    carton_weight_fn=None,
) -> list[dict]:
    """Agrège une liste de palettes en lignes de ramasse au format historique.

    Une ligne = un (designation, fmt). Les cartons / palettes / poids sont
    sommés sur toutes les palettes du même produit.

    ``carton_weight_fn(fmt, designation)`` est injectable pour le test ;
    si None, on importe ``common.ramasse.get_carton_weight`` à la volée.

    Format de sortie aligné sur ``build_lines_payload`` (ramasse_service).
    """
    if carton_weight_fn is None:
        from common.ramasse import get_carton_weight as _gcw
        carton_weight_fn = _gcw

    # Group by (designation, fmt) — case-insensitive sur designation pour
    # éviter les doublons si la même palette est représentée 2× avec une
    # casse différente (improbable mais défensif).
    groups: dict[tuple[str, str], list[PaletteInfo]] = {}
    for p in palettes:
        key = (p.designation.strip().lower(), p.fmt.strip().lower())
        groups.setdefault(key, []).append(p)

    out: list[dict] = []
    for (desig_lower, fmt_lower), items in groups.items():
        # Représentant pour les libellés (premier de la liste)
        head = items[0]
        total_cartons = sum(p.case_count for p in items)
        nb_palettes = len(items)
        # Poids = cartons × poids_carton + N × poids_palette_vide (25 kg)
        carton_w = carton_weight_fn(head.fmt, head.designation) or 0.0
        from common.ramasse import PALETTE_EMPTY_WEIGHT
        poids = round(total_cartons * carton_w + nb_palettes * PALETTE_EMPTY_WEIGHT)
        # ref unique pour la ligne (réutilise gtin_palette + fmt)
        ref = re.sub(r"\D+", "", head.gtin_palette)[-6:] or head.fmt
        # ddm = la plus proche parmi les palettes (worst case pour le client)
        valid_ddms = [p.ddm for p in items if p.ddm is not None]
        ddm_str = min(valid_ddms).strftime("%d/%m/%Y") if valid_ddms else ""
        out.append({
            "ref": ref,
            "produit": f"{head.designation} {head.fmt}".strip(),
            "ddm": ddm_str,
            "cartons": int(total_cartons),
            "palettes": int(nb_palettes),
            "poids": int(poids),
        })
    # Tri stable pour rendu reproductible
    out.sort(key=lambda r: (r["produit"], r["ref"]))
    return out


# ─── Commit : INSERT palette_loadings + lien ramasse ────────────────────────

def link_palettes_to_ramasse(
    tenant_id: str,
    *,
    sscc_list: list[str],
    ramasse_id: str,
    user_email: str = "",
) -> tuple[int, list[str]]:
    """INSERT les palettes dans palette_loadings + leur attribue ramasse_id.

    Si une palette est DÉJÀ liée (UNIQUE constraint), elle est skip
    silencieusement et ajoutée à la liste retournée des conflits.

    Args:
        sscc_list: SSCC normalisés (18 digits, validés).
        ramasse_id: UUID de la ramasse créée/mise-à-jour.

    Returns:
        (nb_inserted, sscc_conflicts)
    """
    if not sscc_list:
        return (0, [])
    inserted = 0
    conflicts: list[str] = []
    for sscc in sscc_list:
        try:
            # ON CONFLICT cible l'index unique partiel `unlinked_at IS NULL` :
            # une palette unlinkée pourra être ré-INSÉRÉE pour un nouveau lien.
            rows = run_sql(
                """INSERT INTO palette_loadings
                       (tenant_id, sscc, ramasse_id, scanned_by)
                   VALUES (:t, :sscc, :rid, :u)
                   ON CONFLICT (sscc) WHERE unlinked_at IS NULL DO NOTHING
                   RETURNING id""",
                {
                    "t": tenant_id, "sscc": sscc,
                    "rid": ramasse_id, "u": user_email or "",
                },
            )
            if rows:
                inserted += 1
            else:
                conflicts.append(sscc)
        except Exception:
            _log.exception("Échec INSERT palette_loadings sscc=%s", sscc)
            conflicts.append(sscc)
    return (inserted, conflicts)


def list_unscanned_recent_palettes(
    tenant_id: str,
    *,
    days: int = 7,
    limit: int = 50,
) -> list[PaletteInfo]:
    """Liste les palettes étiquetées récemment mais pas encore chargées.

    Utile pour rappel visuel : "tu n'as pas oublié ces palettes-là ?"
    """
    rows = run_sql(
        """SELECT
              sl.sscc, sl.gtin_palette, sl.lot, sl.ddm,
              sl.case_count, sl.generated_at,
              eph.designation, eph.fmt, eph.marque, eph.gout,
              eph.pcb, eph.gtin_uvc
           FROM sscc_log sl
           LEFT JOIN etiquette_palette_history eph
                  ON eph.sscc = sl.sscc AND eph.tenant_id = sl.tenant_id
           LEFT JOIN palette_loadings pl
                  ON pl.sscc = sl.sscc
                 AND pl.tenant_id = sl.tenant_id
                 AND pl.unlinked_at IS NULL
           WHERE sl.tenant_id = :t
             AND sl.generated_at > now() - (:d * INTERVAL '1 day')
             AND sl.voided_at IS NULL
             AND pl.id IS NULL
             AND eph.designation IS NOT NULL
           ORDER BY sl.generated_at DESC
           LIMIT :lim""",
        {"t": tenant_id, "d": int(days), "lim": int(limit)},
    ) or []

    out: list[PaletteInfo] = []
    for r in rows:
        try:
            ddm = r.get("ddm")
            ddm_date = ddm if isinstance(ddm, _dt.date) or ddm is None \
                else _dt.date.fromisoformat(str(ddm)[:10])
            out.append(PaletteInfo(
                sscc=str(r["sscc"]),
                gtin_palette=str(r.get("gtin_palette") or ""),
                lot=str(r.get("lot") or ""),
                ddm=ddm_date,
                case_count=int(r.get("case_count") or 0),
                designation=str(r.get("designation") or ""),
                fmt=str(r.get("fmt") or ""),
                marque=str(r.get("marque") or ""),
                gout=str(r.get("gout") or ""),
                pcb=int(r.get("pcb") or 0),
                gtin_uvc=str(r.get("gtin_uvc") or ""),
                generated_at=r["generated_at"],
            ))
        except (KeyError, TypeError, ValueError):
            _log.warning("Ligne unscanned invalide ignorée : %r", r, exc_info=True)
    return out


# ─── Création manuelle d'une palette (cas 4.1 — SSCC inconnu) ───────────────

def create_palette_manually(
    tenant_id: str,
    *,
    sscc: str,
    user_email: str,
    gtin_palette: str,
    lot: str,
    ddm: _dt.date,
    case_count: int,
    designation: str = "",
    fmt: str = "",
    marque: str = "",
    gout: str = "",
    pcb: int = 0,
    gtin_uvc: str = "",
) -> bool:
    """Récupération : enregistre rétroactivement une palette dont le SSCC
    a été imprimé mais pas tracé en DB (anomalie rare).

    Crée 2 entrées miroir :
      - sscc_log : pour la traçabilité
      - etiquette_palette_history : pour la résolution future

    Idempotent : si le SSCC existe déjà dans sscc_log, no-op.
    """
    sscc_clean = _normalize_sscc(sscc)
    if len(sscc_clean) != 18:
        raise ValueError(f"SSCC invalide : {sscc!r}")

    # Vérifier qu'on ne re-crée pas par erreur une palette existante
    existing = run_sql(
        "SELECT 1 FROM sscc_log WHERE sscc = :s AND tenant_id = :t",
        {"s": sscc_clean, "t": tenant_id},
    )
    if existing:
        _log.warning("create_palette_manually : SSCC %s déjà connu, no-op", sscc_clean)
        return False

    try:
        run_sql(
            """INSERT INTO sscc_log
                   (sscc, tenant_id, user_email, gtin_palette, lot, ddm, case_count)
               VALUES (:s, :t, :u, :g, :l, :d, :c)""",
            {
                "s": sscc_clean, "t": tenant_id, "u": user_email or "",
                "g": gtin_palette or "", "l": lot or "",
                "d": ddm, "c": int(case_count or 0),
            },
        )
    except Exception:
        _log.exception("Échec INSERT sscc_log (récupération manuelle)")
        return False

    # Mirror dans etiquette_palette_history pour permettre les lookups
    # standards (le service de chargement fait un JOIN dessus).
    try:
        run_sql(
            """INSERT INTO etiquette_palette_history
                   (tenant_id, user_email, ean, lot, ddm, fmt, marque,
                    designation, gout, case_count, full_pallet, n_copies,
                    pcb, gtin_uvc, code_interne, bio, sscc)
               VALUES (:t, :u, :ean, :lot, :ddm, :fmt, :m, :des, :g,
                       :cc, :fp, :n, :pcb, :uvc, :ci, :bio, :sscc)""",
            {
                "t": tenant_id, "u": user_email or "",
                "ean": gtin_palette or "", "lot": lot or "",
                "ddm": ddm, "fmt": fmt or "", "m": marque or "",
                "des": designation or "", "g": gout or "",
                "cc": int(case_count or 0), "fp": False, "n": 1,
                "pcb": int(pcb or 0), "uvc": gtin_uvc or "",
                "ci": "", "bio": True, "sscc": sscc_clean,
            },
        )
    except Exception:
        _log.exception(
            "Échec INSERT etiquette_palette_history (récupération manuelle) — "
            "sscc_log créé mais lookup ne trouvera pas le produit",
        )
        return False

    _log.warning(
        "Palette récupérée manuellement : sscc=%s, user=%s, gtin=%s, lot=%s, cc=%d",
        sscc_clean, user_email, gtin_palette, lot, case_count,
    )
    return True


# ─── Source de vérité : reconstruction des lignes depuis palette_loadings ───

def list_linked_palettes(
    ramasse_id: str,
    tenant_id: str,
) -> list[PaletteInfo]:
    """Liste les palettes actuellement liées à une ramasse.

    Filtre :
    - ``palette_loadings.unlinked_at IS NULL`` (les liaisons annulées
      ne comptent pas pour le BL).
    - ``sscc_log.voided_at IS NULL`` (les SSCC annulés ne comptent pas
      non plus, même s'ils ont une liaison historique).
    - ``etiquette_palette_history.designation IS NOT NULL`` (anomalie
      DB : on n'inclut pas une palette dont le libellé est manquant).

    Tri par ``scanned_at`` croissant — affichage UI stable.
    Sert à la fois à reconstruire les lignes (agrégation) et à proposer
    le déliage palette par palette dans l'UI.
    """
    rows = run_sql(
        """SELECT
              sl.sscc, sl.gtin_palette, sl.lot, sl.ddm,
              sl.case_count, sl.generated_at,
              eph.designation, eph.fmt, eph.marque, eph.gout,
              eph.pcb, eph.gtin_uvc
           FROM palette_loadings pl
           JOIN sscc_log sl
                ON sl.sscc = pl.sscc AND sl.tenant_id = pl.tenant_id
           LEFT JOIN etiquette_palette_history eph
                ON eph.sscc = pl.sscc AND eph.tenant_id = pl.tenant_id
           WHERE pl.ramasse_id    = :rid
             AND pl.tenant_id     = :t
             AND pl.unlinked_at  IS NULL
             AND sl.voided_at    IS NULL
             AND eph.designation IS NOT NULL
           ORDER BY pl.scanned_at""",
        {"rid": ramasse_id, "t": tenant_id},
    ) or []

    palettes: list[PaletteInfo] = []
    for r in rows:
        try:
            ddm = r.get("ddm")
            ddm_date = ddm if isinstance(ddm, _dt.date) or ddm is None \
                else _dt.date.fromisoformat(str(ddm)[:10])
            palettes.append(PaletteInfo(
                sscc=str(r["sscc"]),
                gtin_palette=str(r.get("gtin_palette") or ""),
                lot=str(r.get("lot") or ""),
                ddm=ddm_date,
                case_count=int(r.get("case_count") or 0),
                designation=str(r.get("designation") or ""),
                fmt=str(r.get("fmt") or ""),
                marque=str(r.get("marque") or ""),
                gout=str(r.get("gout") or ""),
                pcb=int(r.get("pcb") or 0),
                gtin_uvc=str(r.get("gtin_uvc") or ""),
                generated_at=r["generated_at"],
            ))
        except (KeyError, TypeError, ValueError):
            _log.warning(
                "Palette liée ignorée (données invalides) ramasse=%s : %r",
                ramasse_id, r, exc_info=True,
            )
    return palettes


def rebuild_lines_from_palettes(
    ramasse_id: str,
    tenant_id: str,
    *,
    carton_weight_fn=None,
) -> tuple[list[dict], int, int, int]:
    """Reconstruit les lignes d'une ramasse depuis ses palettes actives.

    C'est la source de vérité unique : le BL d'une ramasse = l'agrégation
    des palettes encore liées (``unlinked_at IS NULL``). Aucun merge JSON,
    aucune addition applicative — l'état physique du camion (les SSCC
    réellement liés) dicte le BL.

    Args:
        ramasse_id: UUID de la ramasse cible.
        tenant_id:  scope tenant.
        carton_weight_fn: injectable pour les tests
            (signature ``(fmt, designation) -> float``).

    Returns:
        ``(lines, total_cartons, total_palettes, total_poids_kg)`` —
        prêt à être persisté dans ``ramasse_history`` via
        ``save_ramasse`` / ``update_ramasse``.
    """
    palettes = list_linked_palettes(ramasse_id, tenant_id)
    lines = aggregate_palettes_to_lines(palettes, carton_weight_fn=carton_weight_fn)
    total_cartons  = sum(int(line["cartons"])  for line in lines)
    total_palettes = sum(int(line["palettes"]) for line in lines)
    total_poids    = sum(int(line["poids"])    for line in lines)
    return (lines, total_cartons, total_palettes, total_poids)


def unlink_palette(
    tenant_id: str,
    *,
    sscc: str,
    ramasse_id: str,
    reason: str,
    user_email: str = "",
) -> bool:
    """Délie une palette d'une ramasse (soft-unlink).

    La ligne ``palette_loadings`` n'est jamais hard-deletée : on patche
    ``unlinked_at``, ``unlinked_by``, ``unlinked_reason``. La palette
    redevient « non chargée » (réapparaît dans
    :func:`list_unscanned_recent_palettes`), peut être ré-liée à une
    autre ramasse (l'index unique partiel ne contraint que les rows
    actives), et son SSCC reste valide (pas d'annulation).

    Distinct de :func:`common.services.sscc_service.void_sscc` : un void
    annule la palette physique elle-même (étiquette pas imprimée,
    doublon). Un unlink dit juste « cette palette ne fait plus partie
    de cette ramasse » — typiquement palette pas prête à temps,
    palette cassée au chargement, ou erreur de scan.

    Args:
        sscc: SSCC normalisé (18 digits).
        ramasse_id: UUID de la ramasse — sert de garde-fou (on n'unlink
            pas une palette qui serait liée à une autre ramasse).
        reason: justification métier saisie par l'opérateur (≤ 500 chars).
        user_email: pour audit.

    Returns:
        ``True`` si une liaison active a été marquée unlinked, ``False``
        si la palette n'était pas liée à cette ramasse (ou déjà unlinked).
    """
    sscc_clean = _normalize_sscc(sscc)
    if len(sscc_clean) != 18:
        _log.warning("unlink_palette: SSCC invalide %r", sscc)
        return False
    reason_clean = (reason or "").strip()[:500] or "Sans raison précisée"
    try:
        rows = run_sql(
            """UPDATE palette_loadings
                  SET unlinked_at     = now(),
                      unlinked_by     = :u,
                      unlinked_reason = :r
                WHERE sscc        = :sscc
                  AND tenant_id   = :t
                  AND ramasse_id  = :rid
                  AND unlinked_at IS NULL
               RETURNING id""",
            {
                "sscc": sscc_clean, "t": tenant_id,
                "rid": ramasse_id, "u": user_email or "",
                "r": reason_clean,
            },
        )
    except Exception:
        _log.exception(
            "Échec unlink palette_loadings sscc=%s ramasse=%s",
            sscc_clean, ramasse_id,
        )
        return False
    if rows:
        _log.info(
            "Palette %s déliée de ramasse %s par %s — raison: %s",
            sscc_clean, ramasse_id, user_email or "?", reason_clean,
        )
        return True
    return False
