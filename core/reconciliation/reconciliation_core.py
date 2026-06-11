"""
reconciliation_core.py — LE CŒUR de la réconciliation (la "recette").

Ce module ne lit AUCUN fichier et n'écrit AUCUN Excel.
On lui donne :
  - des lignes de facture transporteur (LigneFacture)
  - des commandes Easy Beer (Commande), indexées par numéro
…et il rend un résultat structuré (appariements, écarts, statuts, KPIs).

But : cette logique se réutilise telle quelle, qu'on l'alimente par des
fichiers (aujourd'hui) ou par des API Easy Beer / Pennylane (demain), et
qu'on l'affiche dans un Excel ou dans l'app Ferment Station.
"""
import datetime
import logging
import re
import unicodedata
from dataclasses import dataclass, field

_log = logging.getLogger("ferment.reconciliation_core")

# ---- Paramètres métier -------------------------------------------------------
# Deux régimes de numérotation coexistent entre le N° pièce SOFRIPA et le N°
# commande Easy Beer (voir _resoudre_commande) :
#   - « +4000 » (historique) : N° pièce = N° commande + 4000   (commandes ≤ 3190 → pièces 4100-7190)
#   - « direct »             : N° pièce = N° commande           (commandes ≥ 7191)
OFFSET = 4000          # offset du régime historique
SEUIL_PCT_DEFAUT = 0.25  # au-delà, l'écart de poids est "notable" (trop gros pour l'emballage)

# ---- Statuts possibles d'une ligne ------------------------------------------
STATUT_OK = "OK"
STATUT_NEGATIF = "À vérifier (négatif)"      # écart négatif = physiquement impossible
STATUT_NOTABLE = "Écart notable"             # écart > seuil
STATUT_PALETTE = "Palette (pas de poids)"    # ligne facturée sans poids (forfait palette)
STATUT_PAS_POIDS_EB = "Pas de poids EB"      # Easy Beer n'a pas de poids


# ============================ Structures de données ===========================
@dataclass
class LigneFacture:
    """Une ligne de la facture transporteur (ex : SOFRIPA)."""
    exp_date: str | None = None   # date d'expédition (jj/mm/aa)
    ot: str | None = None         # N° OT (ordre de transport)
    client: str | None = None     # destinataire imprimé sur la facture
    piece: str | None = None      # N° pièce (0000xxxx) — sert à l'appariement
    poids: float | None = None    # poids BRUT facturé (kg) : produit + emballage + palette
    montant: float | None = None  # coût de transport de la ligne (€)


@dataclass
class Commande:
    """Une commande Easy Beer (les champs utiles à la réconciliation)."""
    numero: int
    client: str | None = None
    poids: float | None = None    # poids NET produit (kg)
    ht: float | None = None       # total HT de la commande (€)
    tournee: str | None = None
    brut: dict = field(default_factory=dict)  # toutes les colonnes Easy Beer (pour l'export)


@dataclass
class LigneReconciliee:
    """Le résultat d'un appariement facture ↔ commande, avec tous les calculs."""
    numero: int
    client: str | None
    ot: str | None
    piece: str | None
    poids_eb: float | None
    poids_sofripa: float | None
    ecart_kg: float | None
    ecart_pct: float | None
    cout_transport: float | None
    montant_ht: float | None
    transport_sur_ht: float | None
    eur_par_kg: float | None
    statut: str
    commande: Commande | None = None  # référence vers la commande (colonnes brutes)


@dataclass
class GroupeEnseigne:
    enseigne: str
    nb_livraisons: int = 0
    poids_sofripa: float = 0.0
    ecart_kg: float = 0.0
    cout_transport: float = 0.0
    montant_ht: float = 0.0

    @property
    def eur_par_kg(self):
        return (self.cout_transport / self.poids_sofripa) if self.poids_sofripa else None

    @property
    def transport_sur_ht(self):
        return (self.cout_transport / self.montant_ht) if self.montant_ht else None


@dataclass
class Resultat:
    """Tout ce que produit le cœur."""
    lignes: list                      # list[LigneReconciliee] (appariées)
    sans_piece: list                  # list[LigneFacture] non rapprochables
    internes: list                    # list[LigneFacture] transferts internes (exclus)
    par_enseigne: list                # list[GroupeEnseigne], triés par coût décroissant
    kpis: dict                        # indicateurs globaux
    # Suggestions pour les lignes sans pièce (alignées sur sans_piece, Commande|None).
    # ⚠️ SUPPOSITIONS à vérifier à la main — jamais utilisées dans les calculs/KPIs.
    sans_piece_suggestions: list = field(default_factory=list)


# ============================ Logique du cœur =================================
def _est_interne(client: str | None) -> bool:
    """Un transfert interne = enlèvement de bouteilles Symbiose (DEST = SYMBIOSE KEFIR…)."""
    return (client or "").startswith("SYMBIOSE KEFIR")


def _statut(poids_eb, poids_sof, ecart_kg, ecart_pct, seuil_pct) -> str:
    """Réplique en Python la règle qui était dans les formules Excel."""
    if poids_sof is None:
        return STATUT_PALETTE
    if poids_eb in (None, 0):
        return STATUT_PAS_POIDS_EB
    if ecart_kg is not None and ecart_kg < 0:
        return STATUT_NEGATIF
    if ecart_pct is not None and ecart_pct > seuil_pct:
        return STATUT_NOTABLE
    return STATUT_OK


def _enseigne_de(facture: LigneFacture, commande: Commande) -> str:
    """Regroupement : la tournée Easy Beer si dispo, sinon le client facture nettoyé."""
    if commande.tournee:
        return commande.tournee
    c = facture.client or ""
    m = re.search(r'\((\d{2})\)', c)   # retire le "(59)" etc. du client facture
    return (c[:m.start()].strip() if m else c.strip())


def _norm_client(s) -> set:
    """Normalise un nom de client en jeu de tokens (minuscules, sans accents,
    sans les parenthèses « (94) », « (Magasin 78) »…). Sert au départage par client."""
    s = re.sub(r"\(.*?\)", " ", s or "")                       # retire (94), (Magasin 78)…
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))   # enlève les accents
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    return {t for t in s.split() if len(t) > 2}


def _resoudre_commande(piece, client, poids, commandes_par_num, offset):
    """Résout la commande Easy Beer correspondant à un N° pièce SOFRIPA.

    Règle « candidats existants » (robuste, sans seuil ni date codés en dur) :
    on calcule les deux candidats possibles et on garde celui (ou ceux) qui
    existe(nt) réellement parmi les commandes :
      - cand « +4000 » = pièce − 4000   (régime historique)
      - cand « direct » = pièce          (régime nouveau, série 7191+)

    Sur les données actuelles, le trou de numérotation 3191-7190 (côté commande)
    garantit qu'au plus UN candidat existe → aucune ambiguïté possible.

    ⚠️ Le régime « direct » (7191+) est DÉDUIT : il repose sur la connaissance
    métier + la continuité de numérotation (l'ancien régime s'arrête pile à la
    commande 3190 → pièce 7190, le nouveau démarre à la commande 7191). Il n'a
    PAS encore pu être observé sur facture (1re facture de la série attendue en
    juin) — à CONFIRMER sur la 1re facture de juin.

    Retourne (num_commande, Commande) ou (None, None) si non résolu.
    """
    try:
        p = int(piece)
    except (TypeError, ValueError):
        return None, None

    candidats = []  # liste de (num, Commande), régime +4000 d'abord puis direct
    for num in (p - offset, p):
        cmd = commandes_par_num.get(num)
        if cmd is not None and all(num != n for n, _ in candidats):
            candidats.append((num, cmd))

    if not candidats:
        return None, None
    if len(candidats) == 1:
        return candidats[0]

    # Collision (ne survient pas sur les données actuelles : le trou 3191-7190
    # est vide). Filet de sécurité futur : départage par client (principal) puis
    # poids (secondaire), avec un avertissement loggé pour inspection.
    fac_tokens = _norm_client(client)

    def _cle(item):
        _num, cmd = item
        score_client = len(fac_tokens & _norm_client(cmd.client))
        ecart_poids = (
            abs(poids - cmd.poids)
            if (poids is not None and cmd.poids is not None)
            else float("inf")
        )
        return (score_client, -ecart_poids)

    candidats.sort(key=_cle, reverse=True)
    _log.warning(
        "N° pièce %s ambigu (candidats commandes %s) — départage par recoupement → %s",
        p, [n for n, _ in candidats], candidats[0][0],
    )
    return candidats[0]


def _parse_date_fr(v):
    """'17/04/26', '17/04/2026', '17/04/2026 à 09:00' ou datetime -> date. Sinon None."""
    if v is None:
        return None
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    m = re.match(r"(\d{2})/(\d{2})/(\d{2,4})", str(v).strip())
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    try:
        return datetime.date(y, mo, d)
    except ValueError:
        return None


def _suggerer_commande(facture, commandes_par_num, fenetre_jours=3):
    """Suggestion de commande EB pour une ligne de facture SANS pièce exploitable.

    Croisement d'indices, comme l'ancien outil Excel (« Suggestion Easy Beer
    (à vérifier) ») :
      1. client (obligatoire) — tokens normalisés communs entre le client
         facture et le client commande ;
      2. date — expédition SOFRIPA vs livraison réelle EB (± fenetre_jours,
         si les deux dates sont connues) ;
      3. poids — départage final (le plus proche gagne).

    ⚠️ Retourne une SUPPOSITION (Commande) ou None — à vérifier à la main,
    jamais intégrée aux appariements ni aux KPIs.
    """
    fac_tokens = _norm_client(facture.client)
    if not fac_tokens:
        return None
    d_fac = _parse_date_fr(facture.exp_date)
    best, best_key, best_ecart_j = None, None, None
    for cmd in commandes_par_num.values():
        score = len(fac_tokens & _norm_client(cmd.client))
        if score == 0:
            continue
        d_cmd = _parse_date_fr(
            cmd.brut.get("Date de livr. réelle") or cmd.brut.get("Date de livr. prévue")
        )
        # La date ne sert qu'à DÉPARTAGER (un match de nom fort gagne toujours,
        # même si la date est lointaine — cas « INTERMARCHE Lepic »).
        ecart_j = abs((d_fac - d_cmd).days) if (d_fac and d_cmd) else 999
        d_poids = abs((facture.poids or 0) - (cmd.poids or 0))
        key = (score, -ecart_j, -d_poids)
        if best_key is None or key > best_key:
            best, best_key, best_ecart_j = cmd, key, ecart_j
    # Garde-fou : un match faible (1 seul mot commun) n'est suggéré que si la
    # date colle aussi — sinon c'est du bruit, on préfère « aucune piste ».
    if best_key is not None and best_key[0] <= 1 and best_ecart_j > fenetre_jours:
        return None
    return best


def reconcilier(factures, commandes_par_num, seuil_pct=SEUIL_PCT_DEFAUT, offset=OFFSET) -> Resultat:
    """
    factures            : iterable[LigneFacture]
    commandes_par_num   : dict[int, Commande]
    """
    lignes, sans_piece, internes = [], [], []

    for f in factures:
        if _est_interne(f.client):
            internes.append(f)
            continue
        if not f.piece:
            sans_piece.append(f)
            continue
        num, cmd = _resoudre_commande(f.piece, f.client, f.poids, commandes_par_num, offset)
        if cmd is None:
            sans_piece.append(f)   # pièce présente mais aucune commande en face (ou non résolue)
            continue

        poids_eb = cmd.poids
        poids_sof = f.poids
        ecart_kg = (poids_sof - poids_eb) if (poids_sof is not None and poids_eb is not None) else None
        ecart_pct = (ecart_kg / poids_eb) if (ecart_kg is not None and poids_eb) else None
        cout = f.montant
        ht = cmd.ht
        transport_sur_ht = (cout / ht) if (cout is not None and ht) else None
        eur_par_kg = (cout / poids_sof) if (cout is not None and poids_sof) else None

        lignes.append(LigneReconciliee(
            numero=num, client=cmd.client, ot=f.ot, piece=f.piece,
            poids_eb=poids_eb, poids_sofripa=poids_sof,
            ecart_kg=ecart_kg, ecart_pct=ecart_pct,
            cout_transport=cout, montant_ht=ht,
            transport_sur_ht=transport_sur_ht, eur_par_kg=eur_par_kg,
            statut=_statut(poids_eb, poids_sof, ecart_kg, ecart_pct, seuil_pct),
            commande=cmd,
        ))

    # ---- Synthèse par enseigne ----
    groupes = {}
    for L in lignes:
        key = _enseigne_de(LigneFacture(client=L.client, ot=L.ot, piece=L.piece,
                                        poids=L.poids_sofripa, montant=L.cout_transport),
                           L.commande or Commande(numero=L.numero, client=L.client))
        g = groupes.setdefault(key, GroupeEnseigne(enseigne=key))
        g.nb_livraisons += 1
        g.cout_transport += L.cout_transport or 0
        g.montant_ht += L.montant_ht or 0
        if L.poids_sofripa:
            g.poids_sofripa += L.poids_sofripa
        if L.poids_sofripa and L.poids_eb:
            g.ecart_kg += (L.poids_sofripa - L.poids_eb)
    par_enseigne = sorted(groupes.values(), key=lambda g: -g.cout_transport)

    # ---- KPIs globaux ----
    comparables = [L for L in lignes if L.poids_sofripa and L.poids_eb]
    poids_sof_total = sum(L.poids_sofripa for L in comparables)
    poids_eb_total = sum(L.poids_eb for L in comparables)
    cout_total = sum((L.cout_transport or 0) for L in lignes)
    ht_total = sum((L.montant_ht or 0) for L in lignes)
    sof_pos = [L for L in lignes if L.poids_sofripa]
    cout_sof_pos = sum((L.cout_transport or 0) for L in sof_pos)
    poids_sof_pos = sum(L.poids_sofripa for L in sof_pos)

    kpis = {
        "livraisons_appariees": len(lignes),
        "lignes_sans_piece": len(sans_piece),
        "transferts_internes": len(internes),
        "poids_sofripa_comparable_kg": round(poids_sof_total, 2),
        "poids_easybeer_comparable_kg": round(poids_eb_total, 2),
        "ecart_poids_total_kg": round(poids_sof_total - poids_eb_total, 2),
        "lignes_a_verifier_negatif": sum(1 for L in lignes if L.statut == STATUT_NEGATIF),
        "ecarts_notables": sum(1 for L in lignes if L.statut == STATUT_NOTABLE),
        "cout_transport_total_eur": round(cout_total, 2),
        "montant_ht_total_eur": round(ht_total, 2),
        "part_transport_dans_ht": round(cout_total / ht_total, 4) if ht_total else None,
        "cout_moyen_eur_par_kg": round(cout_sof_pos / poids_sof_pos, 3) if poids_sof_pos else None,
    }

    return Resultat(lignes=lignes, sans_piece=sans_piece, internes=internes,
                    par_enseigne=par_enseigne, kpis=kpis,
                    sans_piece_suggestions=[
                        _suggerer_commande(f, commandes_par_num) for f in sans_piece
                    ])
