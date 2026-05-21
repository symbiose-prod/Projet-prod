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

from common.services.realtime import broadcast as _rt_broadcast
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


def lookup_sscc_batch(
    sscc_list: list[str], tenant_id: str,
) -> dict[str, PaletteInfo]:
    """Lookup batch de plusieurs SSCC en un seul aller-retour DB.

    Utilisé par ``send_previsionnel`` au J1 pour générer le PDF BL
    provisoire et agréger les lignes à partir d'une liste de SSCC, SANS
    passer par ``palette_loadings`` (les palettes ne sont pas encore
    liées à la ramasse à ce stade — workflow J1 informatif uniquement).

    Args:
        sscc_list: liste de SSCC bruts (peuvent contenir préfixes AI ou
                   séparateurs — sera normalisée par SSCC).
        tenant_id: scope tenant strict.

    Returns:
        dict ``{sscc_normalisé: PaletteInfo}``. Les SSCC inconnus,
        annulés (voided), ou avec etiquette_palette_history manquante
        sont silencieusement omis du dict. L'appelant compare donc
        ``set(sscc_list) - set(result.keys())`` pour détecter les
        SSCC "fantômes" si besoin.
    """
    if not sscc_list:
        return {}
    # Normalise + dédoublonne
    normalized = sorted({
        _normalize_sscc(s) for s in sscc_list
        if _normalize_sscc(s) and len(_normalize_sscc(s)) == 18
    })
    if not normalized:
        return {}

    rows = run_sql(
        """SELECT
              sl.sscc, sl.gtin_palette, sl.lot, sl.ddm,
              sl.case_count, sl.generated_at, sl.voided_at,
              eph.designation, eph.fmt, eph.marque, eph.gout,
              eph.pcb, eph.gtin_uvc
           FROM sscc_log sl
           LEFT JOIN etiquette_palette_history eph
                  ON eph.sscc = sl.sscc AND eph.tenant_id = sl.tenant_id
           WHERE sl.sscc = ANY(:ssccs)
             AND sl.tenant_id = :t""",
        {"ssccs": normalized, "t": tenant_id},
    ) or []

    out: dict[str, PaletteInfo] = {}
    for r in rows:
        if r.get("voided_at"):
            continue  # palette fantôme — ne pas inclure dans le BL
        if not r.get("designation"):
            continue  # etiquette_palette_history manquante (anomalie)
        ddm = r.get("ddm")
        ddm_date = ddm if isinstance(ddm, _dt.date) or ddm is None \
            else _dt.date.fromisoformat(str(ddm)[:10])
        sscc = str(r["sscc"])
        out[sscc] = PaletteInfo(
            sscc=sscc,
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
    return out


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
    linked_sscc: list[str] = []
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
                linked_sscc.append(sscc)
            else:
                conflicts.append(sscc)
        except Exception:
            _log.exception("Échec INSERT palette_loadings sscc=%s", sscc)
            conflicts.append(sscc)
    # Broadcast un event par palette effectivement liée. Les subscribers SSE
    # (web ramasse + iOS) animeront le déplacement CF → camion.
    for sscc in linked_sscc:
        _rt_broadcast(tenant_id, {
            "type": "palette_linked",
            "ramasse_id": ramasse_id,
            "sscc": sscc,
            "scanned_by": user_email or "",
        })
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


# ─── Stock chambre froide ───────────────────────────────────────────────────

def list_palettes_in_cold_room(tenant_id: str) -> list[PaletteInfo]:
    """Liste toutes les palettes actuellement en chambre froide.

    Une palette est « en CF » quand :
    - elle a été étiquetée (``sscc_log`` + ``etiquette_palette_history``),
    - elle n'a pas été annulée (``voided_at IS NULL``),
    - elle n'est pas liée à une ramasse active (``palette_loadings`` actif
      = ``unlinked_at IS NULL``).

    Pas de filtre temporel : une palette étiquetée il y a 2 semaines et
    jamais chargée reste candidate (cas typique : production excédentaire
    en attente d'une ramasse plus volumineuse).

    Tri ascendant par ``generated_at`` — FIFO côté UI (les plus anciennes
    en haut pour priorisation par DDM).

    Sert au workflow de la « demande de ramasse provisoire » : à la
    place de demander à l'opérateur de scanner les palettes à inclure,
    on prend automatiquement tout le stock CF.
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
             AND sl.voided_at IS NULL
             AND pl.id IS NULL
             AND eph.designation IS NOT NULL
           ORDER BY sl.generated_at ASC""",
        {"t": tenant_id},
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
            _log.warning("Ligne CF invalide ignorée : %r", r, exc_info=True)
    return out


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


# ─── Orchestration bout-en-bout : prévisionnel + finalize (mobile + futur DRY web) ──

def _build_df_for_pdf(lines: list[dict]):
    """Construit le DataFrame attendu par ``build_bl_enlevements_pdf``.

    Extrait de ``pages/chargement_camion.py:_build_df_for_pdf`` pour
    permettre la réutilisation depuis ``send_previsionnel`` / ``finalize_loading``
    sans dépendance NiceGUI.

    Les noms de colonnes ``Nb cartons`` / ``Nb palettes`` matchent exactement
    ceux lus par ``bl_pdf.py`` (cf. ``r.get("Nb cartons", ...)``). Sans cette
    correspondance, le PDF tombait sur le fallback 0 et affichait des
    colonnes ``0, 0, 0, ...`` pour TOUS les BLs définitifs — bug rédhibitoire
    côté logisticien Sofripa.
    """
    import pandas as pd
    return pd.DataFrame([
        {
            "Référence": line.get("ref", ""),
            "Produit": line.get("produit", ""),
            "DDM": line.get("ddm", ""),
            "Nb cartons": int(line.get("cartons") or 0),
            "Nb palettes": int(line.get("palettes") or 0),
            "Poids (kg)": int(line.get("poids") or 0),
        }
        for line in lines
    ])


def _resolve_destinataire(name: str) -> dict | None:
    """Cherche un destinataire dans ``data/destinataires.json`` par nom.

    Retourne le dict complet (``name``, ``address_lines``, ``email_recipients``,
    ``packaging_items``) ou ``None`` si inconnu.
    """
    from common.ramasse import load_destinataires
    for d in load_destinataires():
        if d.get("name") == name:
            return d
    return None


def normalize_packaging_payload(items: list[dict] | None) -> list[dict]:
    """Normalise un payload packaging (mobile/web) en ``[{label, qty, unit}]``.

    Filtre les entrées invalides (label vide, qty <= 0). Coerce ``qty`` en int.
    Format identique à ``ramasse.build_packaging_summary`` mais accepte plus
    de variantes en entrée (tolérant aux clés manquantes).
    """
    out: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        try:
            qty = int(item.get("qty") or 0)
        except (ValueError, TypeError):
            qty = 0
        if not label or qty <= 0:
            continue
        out.append({
            "label": label,
            "qty": qty,
            "unit": str(item.get("unit") or "palette"),
        })
    return out


def send_previsionnel(
    tenant_id: str,
    *,
    user_id: str,
    user_email: str,
    destinataire: str,
    date_ramasse: _dt.date,
    sscc_list: list[str],
    packaging: list[dict] | None = None,
) -> dict:
    """Crée + envoie un BL prévisionnel pour un destinataire.

    Orchestre toute la séquence métier en une seule appel (utilisable depuis
    mobile_v1 ou un futur refactor de ``pages/chargement_camion.py``) :

    1. Résout le destinataire (recipients + address_lines) depuis
       ``data/destinataires.json``.
    2. ``save_ramasse`` placeholder vide en status ``previsionnel`` —
       fait remonter le verrou métier "1 ramasse active par dest" en
       ``ValueError`` si déjà active.
    3. ``link_palettes_to_ramasse`` — INSERT palette_loadings.
    4. ``rebuild_lines_from_palettes`` — agrégation par produit.
    5. ``build_bl_enlevements_pdf`` (kind="previsionnel").
    6. ``finalize_ramasse_lines`` — patch lignes/totaux/PDF en DB.
    7. ``send_html_with_pdf`` — envoi email au logisticien + créateur.

    Si l'envoi email plante, on log mais on retourne ``email_sent=False`` —
    la ramasse est créée en DB, l'opérateur peut la renvoyer manuellement.

    Args:
        sscc_list: SSCC à inclure dans le prévisionnel. Si vide, la ramasse
            est créée sans palettes (cas exceptionnel — rare en pratique).
        packaging: ``[{label, qty, unit}, ...]`` — emballages à ramener.

    Returns:
        ``{"id", "total_palettes", "total_cartons", "total_poids_kg",
           "inserted", "conflicts", "email_sent", "recipients"}``.

    Raises:
        ValueError: destinataire inconnu, sans emails, ou ramasse active
            déjà ouverte pour ce destinataire (verrou métier).
    """
    from common.email import send_html_with_pdf
    from common.ramasse_history import finalize_ramasse_lines, save_ramasse
    from common.services.ramasse_service import (
        build_email_body,
        build_email_subject,
    )
    from common.xlsx_fill.bl_pdf import build_bl_enlevements_pdf

    dest_obj = _resolve_destinataire(destinataire)
    if dest_obj is None:
        raise ValueError(f"Destinataire inconnu : {destinataire}")

    recipients = list(dest_obj.get("email_recipients", []) or [])
    if user_email and user_email not in recipients:
        recipients.append(user_email)
    if not recipients:
        raise ValueError(f"Aucun email configuré pour {destinataire}")

    address_lines = dest_obj.get("address_lines", []) or []
    packaging_clean = normalize_packaging_payload(packaging)

    # 1. Placeholder ramasse vide → verrou métier remonte en ValueError
    ramasse_id = save_ramasse(
        date_ramasse=date_ramasse,
        destinataire=destinataire,
        recipients=recipients,
        lines=[],
        total_cartons=0,
        total_palettes=0,
        total_poids_kg=0,
        packaging=packaging_clean,
        status="previsionnel",
        tenant_id=tenant_id,
        user_id=user_id,
    )

    # 2. Lookup batch des palettes annoncées (SANS lien DB — workflow refondu
    #    2026-05 : le prévisionnel J1 est purement informatif. Les palettes
    #    restent en CF côté palette_loadings, ne sont liées qu'au scan J2.
    #    Les SSCC inconnus ou annulés sont silencieusement omis du résultat.
    palette_map = lookup_sscc_batch(sscc_list or [], tenant_id)
    palettes_prevues = list(palette_map.values())
    # SSCC effectivement valides — snapshot persisté pour diff au J2.
    valid_sscc = sorted(palette_map.keys())
    # Conflits "shape backward-compat" pour le retour : SSCC qui n'ont pas
    # été trouvés (sscc_log absent, palette annulée, etc.).
    conflicts = sorted(set(sscc_list or []) - set(valid_sscc))
    inserted = len(valid_sscc)

    # 3. Agrégation directe depuis PaletteInfo (sans lecture palette_loadings)
    total_cartons = sum(p.case_count for p in palettes_prevues)
    total_palettes = len(palettes_prevues)
    lines = aggregate_palettes_to_lines(palettes_prevues)
    # Poids = somme des poids estimés par ligne (cohérent avec aggregate)
    total_poids = sum(int(line.get("poids") or 0) for line in lines)

    # 4. Génération PDF BL provisoire
    df_lines = _build_df_for_pdf(lines)
    pdf_bytes = build_bl_enlevements_pdf(
        date_creation=_dt.date.today(),
        date_ramasse=date_ramasse,
        destinataire_title=destinataire,
        destinataire_lines=address_lines,
        df_lines=df_lines,
        packaging_lines=packaging_clean,
        kind="previsionnel",
    )

    # 5. Finalize en DB (atomic patch lignes + totaux + PDF + snapshot SSCC).
    #    Le champ previsionnel_sscc_list permettra au finalize J2 de
    #    calculer le diff prévu vs réellement scanné.
    finalize_ramasse_lines(
        ramasse_id,
        lines=lines,
        total_cartons=total_cartons,
        total_palettes=total_palettes,
        total_poids_kg=total_poids,
        pdf_bytes=pdf_bytes,
        packaging=packaging_clean,
        tenant_id=tenant_id,
        previsionnel_sscc_list=valid_sscc,
    )

    # 5. Envoi email — best-effort (la ramasse reste en DB si l'email plante)
    subject = build_email_subject(date_ramasse, kind="previsionnel")
    body = build_email_body(
        date_ramasse,
        total_palettes=total_palettes,
        total_cartons=total_cartons,
        packaging_lines=packaging_clean,
        kind="previsionnel",
    )
    fname = f"BL_Provisoire_{date_ramasse:%Y%m%d}.pdf"
    email_sent = False
    try:
        send_html_with_pdf(
            to_email=recipients,
            subject=subject,
            html_body=body,
            attachments=[(fname, pdf_bytes)],
        )
        email_sent = True
    except Exception:
        _log.exception(
            "Échec envoi email prévisionnel ramasse=%s dest=%s",
            ramasse_id, destinataire,
        )

    # Broadcast création — les autres sessions web/iOS rafraîchissent la
    # liste des ramasses actives sans polling.
    _rt_broadcast(tenant_id, {
        "type": "loading_created",
        "ramasse_id": ramasse_id,
        "destinataire": destinataire,
        "date_ramasse": date_ramasse.isoformat(),
        "total_palettes": total_palettes,
        "total_cartons": total_cartons,
        "created_by": user_email or "",
    })

    return {
        "id": ramasse_id,
        "total_palettes": total_palettes,
        "total_cartons": total_cartons,
        "total_poids_kg": total_poids,
        "inserted": inserted,
        "conflicts": conflicts,
        "email_sent": email_sent,
        "recipients": recipients,
    }


def finalize_loading(
    tenant_id: str,
    *,
    ramasse_id: str,
    user_email: str,
) -> tuple[dict, bytes]:
    """Finalise une ramasse ``previsionnel`` → ``definitif`` (BL réel).

    Appelé au moment du chargement physique (J2) une fois toutes les palettes
    scannées sur le camion. Recharge les lignes depuis ``palette_loadings``
    (= ce qui est réellement chargé), génère le BL définitif avec diff vs
    prévisionnel, envoie l'email rectificatif, et retourne le PDF pour
    download immédiat (le chauffeur l'imprime).

    Orchestration :
    1. Charge la ramasse (doit exister + être ``previsionnel``).
    2. Snapshot ``previous_lines`` (= ce qui était dans le prévisionnel) →
       servira au diff JAUNE/BLEU dans le PDF.
    3. ``rebuild_lines_from_palettes`` (source de vérité = palette_loadings).
    4. ``build_bl_enlevements_pdf`` (kind="definitif" + previous_lines).
    5. ``update_ramasse(target_status="definitif")`` — transition de statut
       + remplace lignes/totaux/PDF.
    6. ``send_html_with_pdf`` — envoi email rectificatif (best-effort).

    Returns:
        Tuple ``(info_dict, pdf_bytes)`` :
        - ``info_dict`` = ``{"id", "total_palettes", ..., "email_sent",
          "recipients", "version"}``
        - ``pdf_bytes`` = PDF binaire pour download immédiat (chauffeur)

    Raises:
        ValueError: ramasse introuvable, déjà ``definitif``, ou autre
            transition refusée.
    """
    from common.email import send_html_with_pdf
    from common.ramasse_history import get_ramasse, update_ramasse
    from common.services.ramasse_service import (
        build_email_body,
        build_email_subject,
    )
    from common.xlsx_fill.bl_pdf import build_bl_enlevements_pdf

    current = get_ramasse(ramasse_id, tenant_id)
    if current is None:
        raise ValueError("Ramasse introuvable")
    if current.get("status") != "previsionnel":
        raise ValueError(
            f"Seules les ramasses 'previsionnel' peuvent être finalisées "
            f"(statut actuel : {current.get('status')})",
        )

    destinataire = current.get("destinataire") or ""
    dest_obj = _resolve_destinataire(destinataire)
    address_lines = (dest_obj or {}).get("address_lines", []) or []

    # Recipients : on relit destinataires.json (en cas d'évolution config)
    # + ajout du créateur du finalize si pas déjà dedans.
    recipients = list((dest_obj or {}).get("email_recipients", []) or [])
    existing_recipients = current.get("recipients") or []
    for r in existing_recipients:
        if r and r not in recipients:
            recipients.append(r)
    if user_email and user_email not in recipients:
        recipients.append(user_email)

    previous_lines = current.get("lines") or []
    packaging = current.get("packaging") or []
    date_ramasse = current.get("date_ramasse")
    next_version = int(current.get("version") or 1) + 1

    # Rebuild lignes depuis palette_loadings (= chargement physique réel)
    lines, total_cartons, total_palettes, total_poids = rebuild_lines_from_palettes(
        ramasse_id, tenant_id,
    )

    df_lines = _build_df_for_pdf(lines)
    pdf_bytes = build_bl_enlevements_pdf(
        date_creation=_dt.date.today(),
        date_ramasse=date_ramasse,
        destinataire_title=destinataire,
        destinataire_lines=address_lines,
        df_lines=df_lines,
        packaging_lines=packaging,
        previous_lines=previous_lines,
        version=next_version,
        kind="definitif",
    )

    result = update_ramasse(
        ramasse_id,
        date_ramasse=date_ramasse,
        destinataire=destinataire,
        recipients=recipients,
        lines=lines,
        total_cartons=total_cartons,
        total_palettes=total_palettes,
        total_poids_kg=total_poids,
        packaging=packaging,
        pdf_bytes=pdf_bytes,
        target_status="definitif",
        tenant_id=tenant_id,
    )
    if result is None:
        raise ValueError(
            "Transition refusée (ramasse verrouillée par chauffeur ou "
            "transition de statut invalide)",
        )

    # Email rectificatif — best-effort
    subject = build_email_subject(date_ramasse, kind="definitif")
    body = build_email_body(
        date_ramasse,
        total_palettes=total_palettes,
        total_cartons=total_cartons,
        packaging_lines=packaging,
        kind="definitif",
    )
    fname = f"BL_Definitif_{date_ramasse:%Y%m%d}.pdf"
    email_sent = False
    try:
        send_html_with_pdf(
            to_email=recipients,
            subject=subject,
            html_body=body,
            attachments=[(fname, pdf_bytes)],
        )
        email_sent = True
    except Exception:
        _log.exception(
            "Échec envoi email définitif ramasse=%s dest=%s",
            ramasse_id, destinataire,
        )

    info = {
        "id": ramasse_id,
        "total_palettes": total_palettes,
        "total_cartons": total_cartons,
        "total_poids_kg": total_poids,
        "email_sent": email_sent,
        "recipients": recipients,
        "version": next_version,
    }

    # Broadcast finalisation — clôt la ramasse côté UI : on enlève la
    # bannière "ramasse en cours" et on bascule l'historique.
    _rt_broadcast(tenant_id, {
        "type": "loading_finalized",
        "ramasse_id": ramasse_id,
        "total_palettes": total_palettes,
        "total_cartons": total_cartons,
        "version": next_version,
        "finalized_by": user_email or "",
    })

    return (info, pdf_bytes)


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
        _rt_broadcast(tenant_id, {
            "type": "palette_unlinked",
            "ramasse_id": ramasse_id,
            "sscc": sscc_clean,
            "reason": reason_clean,
            "unlinked_by": user_email or "",
        })
        return True
    return False
