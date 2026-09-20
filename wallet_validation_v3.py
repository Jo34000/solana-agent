"""Backtest v3 : PnL REALISE en SOL, sans aucune donnee de prix.

Le run v2 du 19/09 a corrige le prix d'entree mais laisse trois defauts
qui rendaient la mesure inexploitable :

  1. Biais de survie. Les 404 GeckoTerminal amputaient les echantillons, et
     tous les wallets au-dessus de 50 % de win rate etaient exactement ceux
     dont l'echantillon etait ampute : AkQ4bcEV 12 tokens sur 30 -> 75 %,
     EqQpvukm 20 sur 38 -> 75 %. Parmi les 24 wallets mesures sur 30
     tokens, le meilleur win rate tombait a 40 %.
  2. median_raw_perf aberrant : x3483, x99. Signature d'un prix d'entree
     calcule sur un montant SOL derisoire.
  3. 1280 receptions ecartees faute de contrepartie SOL contre 889 tokens
     classes : plus de la moitie des receptions n'etaient pas reconnues.

Et surtout la metrique elle-meme etait mauvaise : "pic apres achat / prix
d'entree" mesure ce que le wallet AURAIT gagne en vendant au sommet exact,
pas ce qu'il a gagne.

Or les ventes sont deja dans les donnees collectees : meme signature,
jambe token sortante, jambe SOL entrante — le symetrique exact de
l'achat. Ce module mesure donc le PnL realise en SOL.

Consequence directe : aucun appel GeckoTerminal, aucune conversion USD,
aucun cap. Les trois defauts disparaissent avec la dependance aux prix.
"""

from __future__ import annotations

import logging
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

import helius
import supabase_client as db
from config import (
    SMART_WALLETS_TABLE,
    SOL_MINTS,
    V2_MAX_AGE_DAYS,
    V2_MIN_AGE_DAYS,
    V3_MAX_PAGES,
    V3_MAX_TOKENS,
    V3_MIN_SOL_PER_BUY,
    VALIDATION_MIN_WINNERS,
    diagnose_environment,
    force_remeasure,
    setup_logging,
)

log = logging.getLogger("solana-agent")

TRANSFERS_LIMIT = 100  # plafond impose par l'API

# Part des tokens revendus a partir de laquelle la position est consideree
# comme fermee. 100 % exact est irrealiste : frais, poussieres, arrondis.
CLOSED_RATIO = 0.95

AMOUNT_KEYS = ("uiAmount", "tokenAmount", "amount")
_amount_key_usage: Counter = Counter()


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _amount(line: dict) -> float:
    for key in AMOUNT_KEYS:
        if key in line:
            value = _to_float(line.get(key))
            if value:
                _amount_key_usage[key] += 1
                return value
    _amount_key_usage["absent"] += 1
    return 0.0


def _line_time(line: dict) -> float:
    return _to_float(line.get("timestamp") or line.get("blockTime"))


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 4) if values else None


# ---------------------------------------------------------------------------
# Detection : achat et vente sont symetriques
# ---------------------------------------------------------------------------


def classify_group(lines: list[dict], wallet: str) -> dict | None:
    """Achat ou vente decrits par un groupe partageant une signature.

    ACHAT : jambe SOL SORTANTE du wallet + jambe d'un autre mint ENTRANTE.
    VENTE : jambe du mint SORTANTE du wallet + jambe SOL ENTRANTE.

    Tout le reste (transfert simple, swap token a token, airdrop) rend
    None : ce n'est ni un achat ni une vente.
    """
    sol_out = sol_in = 0.0
    token_in: dict | None = None
    token_out: dict | None = None

    for line in lines:
        mint = line.get("mint")
        if not isinstance(mint, str):
            continue
        if mint in SOL_MINTS:
            if line.get("fromUserAccount") == wallet:
                sol_out += _amount(line)
            elif line.get("toUserAccount") == wallet:
                sol_in += _amount(line)
        elif line.get("toUserAccount") == wallet and token_in is None:
            token_in = line
        elif line.get("fromUserAccount") == wallet and token_out is None:
            token_out = line

    if token_in is not None and sol_out > 0:
        tokens = _amount(token_in)
        if tokens > 0:
            return {
                "side": "buy",
                "mint": token_in.get("mint"),
                "at": _line_time(token_in),
                "sol": sol_out,
                "tokens": tokens,
                "signature": token_in.get("signature"),
            }

    if token_out is not None and sol_in > 0:
        tokens = _amount(token_out)
        if tokens > 0:
            return {
                "side": "sell",
                "mint": token_out.get("mint"),
                "at": _line_time(token_out),
                "sol": sol_in,
                "tokens": tokens,
                "signature": token_out.get("signature"),
            }

    return None


# ---------------------------------------------------------------------------
# Collecte
# ---------------------------------------------------------------------------


def oldest_activity_days(wallet: str) -> float | None:
    """Anciennete de la plus ancienne activite indexee. None = PERTE."""
    result = helius.transfers_by_address(wallet, 1, sort_order="asc")
    if result is None:
        return None
    items, _ = result
    stamps = [
        _line_time(item) for item in items
        if isinstance(item, dict) and _line_time(item) > 0
    ]
    if not stamps:
        return None
    now = datetime.now(timezone.utc).timestamp()
    return (now - min(stamps)) / 86400


def collect_events(wallet: str) -> tuple[list[dict], dict] | None:
    """Tous les achats et ventes du wallet, en ordre chronologique.

    None = PERTE. Contrairement a v2, la collecte NE s'arrete PAS a la fin
    de la fenetre de maturite : les ventes d'un achat mature lui sont
    posterieures par construction, il faut donc aller jusqu'au bout de
    l'historique disponible.
    """
    events: list[dict] = []
    pending: dict[str, list[dict]] = {}
    token: str | None = None
    pages = lines_read = signatures = 0
    dust = 0
    stop_reason = "limite_pages"

    def absorb(groups: dict[str, list[dict]]) -> None:
        nonlocal signatures, dust
        for lines in groups.values():
            signatures += 1
            event = classify_group(lines, wallet)
            if event is None:
                continue
            # Un achat poussiere met un denominateur derisoire au coeur du
            # calcul : il est compte a part, jamais mesure.
            if event["side"] == "buy" and event["sol"] < V3_MIN_SOL_PER_BUY:
                dust += 1
                continue
            events.append(event)

    while pages < V3_MAX_PAGES:
        result = helius.transfers_by_address(
            wallet, TRANSFERS_LIMIT, sort_order="asc", page_token=token
        )
        if result is None:  # PERTE deja loguee cote client
            return None
        items, payload = result
        pages += 1
        lines_read += len(items)

        if not items:
            stop_reason = "historique_epuise"
            break

        for line in items:
            if isinstance(line, dict) and isinstance(line.get("signature"), str):
                pending.setdefault(line["signature"], []).append(line)

        # Le groupe de la derniere ligne peut se poursuivre page suivante :
        # le scinder ferait perdre sa jambe SOL et l'achat avec.
        last_signature = None
        for line in reversed(items):
            if isinstance(line, dict) and isinstance(line.get("signature"), str):
                last_signature = line["signature"]
                break
        carried = pending.pop(last_signature, None) if last_signature else None
        absorb(pending)
        pending = {last_signature: carried} if carried else {}

        token = helius.pagination_token(payload)
        if not token:
            stop_reason = "historique_epuise"
            break

    absorb(pending)
    events.sort(key=lambda e: e["at"])
    stats = {
        "pages": pages,
        "lines": lines_read,
        "signatures": signatures,
        "events": len(events),
        "dust": dust,
        "stop_reason": stop_reason,
    }
    return events, stats


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


def build_positions(events: list[dict]) -> list[dict]:
    """Une position par mint dont le PREMIER achat est mature.

    Les achats les plus anciens de la fenetre sont retenus en priorite :
    ce sont ceux qui ont eu le plus de temps pour etre revendus, donc les
    plus susceptibles de donner une position fermee.
    """
    now = datetime.now(timezone.utc).timestamp()
    newest_ts = now - V2_MIN_AGE_DAYS * 86400
    oldest_ts = now - V2_MAX_AGE_DAYS * 86400

    by_mint: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        mint = event.get("mint")
        if isinstance(mint, str):
            by_mint[mint].append(event)

    positions: list[dict] = []
    for mint, mint_events in by_mint.items():
        buys = [e for e in mint_events if e["side"] == "buy"]
        if not buys:
            continue
        first_buy = buys[0]
        if not (oldest_ts <= first_buy["at"] <= newest_ts):
            continue

        # La position court a partir de son ouverture : tout ce qui la
        # precede appartient a un cycle anterieur.
        opened_at = first_buy["at"]
        sol_in = sum(e["sol"] for e in mint_events
                     if e["side"] == "buy" and e["at"] >= opened_at)
        tokens_bought = sum(e["tokens"] for e in mint_events
                            if e["side"] == "buy" and e["at"] >= opened_at)
        sells = [e for e in mint_events
                 if e["side"] == "sell" and e["at"] >= opened_at]
        sol_out = sum(e["sol"] for e in sells)
        tokens_sold = sum(e["tokens"] for e in sells)

        if sol_in <= 0 or tokens_bought <= 0:
            continue

        positions.append({
            "mint": mint,
            "opened_at": opened_at,
            "sol_invested": sol_in,
            "sol_returned": sol_out,
            "tokens_bought": tokens_bought,
            "tokens_sold": tokens_sold,
            "pnl_x": sol_out / sol_in,
            "closed": tokens_sold >= CLOSED_RATIO * tokens_bought,
        })

    positions.sort(key=lambda p: p["opened_at"])
    return positions[:V3_MAX_TOKENS]


def build_metrics(wallet: str, positions: list[dict]) -> dict:
    """Metriques sur les positions FERMEES uniquement.

    Une position encore ouverte a une valeur inconnue sans prix : la
    compter reviendrait a inventer un resultat.
    """
    closed = [p for p in positions if p["closed"]]
    opened = len(positions) - len(closed)

    pnls = [p["pnl_x"] for p in closed]
    winners = [p for p in pnls if p > 1.0]
    losers = [p for p in pnls if p <= 1.0]
    invested = sum(p["sol_invested"] for p in closed)
    returned = sum(p["sol_returned"] for p in closed)

    return {
        "wallet": wallet,
        "positions_fermees": len(closed),
        "positions_ouvertes": opened,
        "win_rate_reel": round(len(winners) / len(pnls), 4) if pnls else None,
        "median_pnl_x": _median(pnls),
        "median_gagnant_x": _median(winners),
        "median_perdant_x": _median(losers),
        "sol_investi": round(invested, 9) if closed else None,
        "sol_recupere": round(returned, 9) if closed else None,
        "pnl_global_x": round(returned / invested, 4) if invested > 0 else None,
        "active": False,
        "activation_reason": "mesure_v3",
        "validated_at": datetime.now(timezone.utc).isoformat(),
    }


def backtest_wallet(wallet: str) -> dict | None:
    """Metriques d'un wallet. None = PERTE : pas de validated_at."""
    collected = collect_events(wallet)
    if collected is None:
        return None
    events, stats = collected
    positions = build_positions(events)
    metrics = build_metrics(wallet, positions)
    metrics["_stats"] = stats
    return metrics


# ---------------------------------------------------------------------------


MEASURED_REASON = "mesure_v3"


def fetch_candidates() -> list[str]:
    """Wallets winners_count >= VALIDATION_MIN_WINNERS. Lecture seule.

    Les wallets deja mesures par ce mode sont ignores : un redemarrage de
    conteneur Railway relance le mode en place et rejouerait la mesure pour
    rien. FORCE_REMEASURE=true est la seule facon de la refaire.

    Le tri se fait en Python et non dans la requete : un .neq sur
    activation_reason exclurait aussi les lignes NULL, c'est-a-dire les
    wallets jamais mesures.
    """
    response = (
        db.get_client()
        .table(SMART_WALLETS_TABLE)
        .select("wallet, activation_reason")
        .gte("winners_count", VALIDATION_MIN_WINNERS)
        .limit(5000)
        .execute()
    )
    rows = [r for r in (response.data or [])
            if isinstance(r.get("wallet"), str)]
    log.info("Supabase : %d wallets winners_count >= %d",
             len(rows), VALIDATION_MIN_WINNERS)

    if force_remeasure():
        log.warning(
            "FORCE_REMEASURE actif : les %d wallets sont (re)mesures, "
            "y compris ceux deja traites", len(rows),
        )
        return [r["wallet"] for r in rows]

    fresh = [r for r in rows if r.get("activation_reason") != MEASURED_REASON]
    skipped = len(rows) - len(fresh)
    if skipped:
        log.info(
            "%d wallet(s) deja mesures (%s) ignores. FORCE_REMEASURE=true "
            "pour les refaire.", skipped, MEASURED_REASON,
        )
    return [r["wallet"] for r in fresh]


def _histogram(title: str, values: list[float], edges: list[float],
               unit: str = "") -> None:
    if not values:
        return
    log.info("%s (%d wallets) :", title, len(values))
    for low, high in zip(edges, edges[1:] + [float("inf")]):
        count = sum(1 for v in values if low <= v < high)
        if not count:
            continue
        share = 100 * count / len(values)
        label = (f"{low:g}-{high:g}{unit}" if high != float("inf")
                 else f">{low:g}{unit}")
        log.info("  %-12s : %3d (%5.1f%%) %s", label, count, share,
                 "#" * int(share / 2))


def run() -> int:
    setup_logging()
    diagnose_environment()
    helius.api_key()

    candidates = fetch_candidates()
    if not candidates:
        log.info("Aucun candidat.")
        return 0

    log.info("Selection : 1 appel par candidat, %d appels", len(candidates))
    retained: list[str] = []
    lost_selection = 0
    for wallet in candidates:
        age = oldest_activity_days(wallet)
        if age is None:
            lost_selection += 1
            continue
        if V2_MIN_AGE_DAYS <= age <= V2_MAX_AGE_DAYS:
            retained.append(wallet)

    log.info(
        "retenus %d / %d (fenetre %d-%d j) | non datables %d | budget de "
        "collecte : au plus %d x %d = %d appels",
        len(retained), len(candidates), V2_MIN_AGE_DAYS, V2_MAX_AGE_DAYS,
        lost_selection, len(retained), V3_MAX_PAGES,
        len(retained) * V3_MAX_PAGES,
    )
    if not retained:
        log.info("Aucun wallet dans la fenetre.")
        return 0

    results: list[dict] = []
    lost: list[str] = []
    totals = Counter()
    capped = 0

    for wallet in retained:
        metrics = backtest_wallet(wallet)
        if metrics is None:
            lost.append(wallet)
            continue
        stats = metrics.pop("_stats")
        totals["pages"] += stats["pages"]
        totals["lines"] += stats["lines"]
        totals["signatures"] += stats["signatures"]
        totals["events"] += stats["events"]
        totals["dust"] += stats["dust"]
        totals["fermees"] += metrics["positions_fermees"]
        totals["ouvertes"] += metrics["positions_ouvertes"]
        if stats["stop_reason"] == "limite_pages":
            capped += 1

        log.info(
            "%s... : %d pages, %d lignes, %d signatures -> %d evenements "
            "(%s) | fermees %d / ouvertes %d | win reel %s | pnl global %s "
            "| gagnants %s | perdants %s",
            wallet[:8], stats["pages"], stats["lines"], stats["signatures"],
            stats["events"], stats["stop_reason"],
            metrics["positions_fermees"], metrics["positions_ouvertes"],
            metrics["win_rate_reel"], metrics["pnl_global_x"],
            metrics["median_gagnant_x"], metrics["median_perdant_x"],
        )
        db.update_wallet_validation([metrics])
        results.append(metrics)

    if lost:
        log.warning(
            "PERTE : %d wallet(s) non backtestes, sans validated_at", len(lost)
        )

    helius_calls, helius_losses = helius.request_stats()
    measured = [m for m in results if m["positions_fermees"]]

    log.info("=== Resume v3 ===")
    log.info(
        "candidats %d | retenus %d | backtestes %d | avec positions fermees "
        "%d | PERTE %d | non datables %d",
        len(candidates), len(retained), len(results), len(measured),
        len(lost), lost_selection,
    )
    log.info(
        "positions : fermees %d | ouvertes %d | achats poussiere ecartes %d "
        "(< %s SOL)",
        totals["fermees"], totals["ouvertes"], totals["dust"],
        V3_MIN_SOL_PER_BUY,
    )
    log.info(
        "collecte : %d pages, %d lignes, %d signatures, %d evenements | "
        "%d wallet(s) plafonnes a %d pages",
        totals["pages"], totals["lines"], totals["signatures"],
        totals["events"], capped, V3_MAX_PAGES,
    )
    if _amount_key_usage:
        log.info("champ de montant utilise : %s", dict(_amount_key_usage))
    log.info("appels Helius %d (pertes %d)", helius_calls, helius_losses)

    _histogram(
        "distribution des pnl_global_x",
        [m["pnl_global_x"] for m in measured if m["pnl_global_x"] is not None],
        [0, 0.5, 1, 2, 5, 10], "x",
    )
    _histogram(
        "distribution des win_rate_reel",
        [m["win_rate_reel"] * 100 for m in measured
         if m["win_rate_reel"] is not None],
        [0, 10, 20, 30, 40, 50, 60, 70, 80, 90], "%",
    )

    top = sorted(
        (m for m in measured if m["pnl_global_x"] is not None),
        key=lambda m: -m["pnl_global_x"],
    )[:15]
    for metrics in top:
        log.info(
            "  %s  fermees %-3d ouvertes %-3d  win %5.1f%%  pnl global x%-7s "
            "median x%-7s gagnants x%-7s perdants x%-7s  %.3f -> %.3f SOL",
            metrics["wallet"], metrics["positions_fermees"],
            metrics["positions_ouvertes"],
            (metrics["win_rate_reel"] or 0) * 100, metrics["pnl_global_x"],
            metrics["median_pnl_x"], metrics["median_gagnant_x"],
            metrics["median_perdant_x"], metrics["sol_investi"] or 0,
            metrics["sol_recupere"] or 0,
        )

    log.info(
        "Aucun wallet active : active=false, activation_reason=mesure_v3. "
        "Les positions ouvertes sont comptees mais exclues du calcul."
    )
    return len(measured)


if __name__ == "__main__":
    run()
