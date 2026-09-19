"""Backtest v2 : prix d'entree REEL, sur les wallets a historique court.

Trois constats de la sonde du 19/09 07:17 fondent ce module :

  1. limit plafonne a 100, et une ligne n'est pas une transaction : 100
     lignes = 57 signatures distinctes. Le cout par transaction tombe a
     0,175 credit contre 0,20 — un gain de 12 %, pas d'un facteur 10.
  2. La jambe SOL EST presente dans getTransfersByAddress, sous le mint
     So1111...1111 (dernier caractere 1). Le prix d'entree reel est donc
     calculable SANS la voie C.
  3. Anciennete des 117 candidats : mediane 89 j, max 921 j. La collecte
     ascendante n'est economique que pour les 30 wallets dont l'historique
     commence deja dans la fenetre 10-45 j.

Ce run MESURE, il n'active personne : les seuils seront choisis apres
avoir vu la distribution.
"""

from __future__ import annotations

import logging
import statistics
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import geckoterminal as gt
import helius
import supabase_client as db
from config import (
    PERF_CAP,
    SMART_WALLETS_TABLE,
    SOL_MINTS,
    V2_MAX_AGE_DAYS,
    V2_MAX_PAGES,
    V2_MAX_TOKENS,
    V2_MIN_AGE_DAYS,
    VALIDATION_MIN_WINNERS,
    VALIDATION_WIN_MULTIPLE,
    diagnose_environment,
    setup_logging,
)
# Reutilise le classement mort / rug / vivant et son cache par mint, sans
# le modifier.
from wallet_validation import fetch_token_data

log = logging.getLogger("solana-agent")

TRANSFERS_LIMIT = 100  # plafond impose par l'API
WSOL_MINT = "So11111111111111111111111111111111111111112"

# Cles candidates pour le montant d'une jambe. uiAmount est le nom observe ;
# les deux autres sont un repli, et l'usage reel est compte puis logue une
# fois dans le resume plutot qu'a chaque ligne.
AMOUNT_KEYS = ("uiAmount", "tokenAmount", "amount")
_amount_key_usage: Counter = Counter()

# Prix du SOL en USD, bougies journalieres du pool de reference. Charge une
# seule fois pour tout le run.
_sol_candles: list[list] | None = None


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
# Prix du SOL en USD
# ---------------------------------------------------------------------------


def load_sol_prices() -> bool:
    """Bougies journalieres du SOL, via son pool le plus liquide.

    Les prix OHLCV sont en USD et le prix d'entree est calcule en SOL : il
    faut convertir avant de diviser. Le pool retenu doit avoir WSOL en
    BASE token, sans quoi l'OHLCV donnerait le prix de l'autre jeton.
    """
    global _sol_candles

    result = gt.token_pools(WSOL_MINT)
    if result is None:
        log.error("PERTE : pools du SOL indisponibles, conversion impossible")
        return False
    pools, _ = result

    best = None
    best_liquidity = -1.0
    fallback = None
    fallback_liquidity = -1.0
    for pool in pools:
        attributes = pool.get("attributes") or {}
        liquidity = _to_float(attributes.get("reserve_in_usd"))
        base_id = (
            pool.get("relationships", {}).get("base_token", {})
            .get("data", {}).get("id", "")
        )
        if base_id.endswith(WSOL_MINT):
            if liquidity > best_liquidity:
                best, best_liquidity = pool, liquidity
        elif liquidity > fallback_liquidity:
            fallback, fallback_liquidity = pool, liquidity

    if best is None:
        log.warning(
            "Aucun pool avec le SOL en base token : repli sur le plus "
            "liquide, le prix de reference est a verifier"
        )
        best = fallback
    if best is None:
        log.error("PERTE : aucun pool SOL exploitable")
        return False

    attributes = best.get("attributes") or {}
    address = attributes.get("address") or best.get("id", "").split("_", 1)[-1]
    candles = gt.ohlcv_day(address, limit=180)
    if candles is None:
        log.error("PERTE : OHLCV du pool SOL indisponible")
        return False

    _sol_candles = sorted(
        (c for c in candles if isinstance(c, (list, tuple)) and len(c) >= 5),
        key=lambda c: _to_float(c[0]),
    )
    if not _sol_candles:
        log.error("PERTE : OHLCV du pool SOL vide")
        return False

    first, last = _sol_candles[0], _sol_candles[-1]
    log.info(
        "Prix de reference SOL : pool %s (%s), %d bougies | %s -> %.2f $ | "
        "%s -> %.2f $",
        address, attributes.get("name", "?"), len(_sol_candles),
        datetime.fromtimestamp(_to_float(first[0]), tz=timezone.utc).date(),
        _to_float(first[4]),
        datetime.fromtimestamp(_to_float(last[0]), tz=timezone.utc).date(),
        _to_float(last[4]),
    )
    return True


def sol_price_at(moment: float) -> float | None:
    """Prix du SOL en USD a une date. None si hors de la fenetre chargee."""
    if not _sol_candles:
        return None
    price = None
    for candle in _sol_candles:
        if _to_float(candle[0]) <= moment:
            price = _to_float(candle[4])
        else:
            break
    return price if price and price > 0 else None


# ---------------------------------------------------------------------------
# Detection des achats
# ---------------------------------------------------------------------------


def purchase_from_group(lines: list[dict], wallet: str) -> dict | None:
    """Achat decrit par un groupe de lignes partageant une signature.

    Un achat suppose DEUX jambes : du SOL sortant du wallet et un token
    entrant. Une reception sans SOL sortant est un airdrop, une migration
    ou un simple transfert — pas un achat.

    Retourne None si ce n'est pas un achat, ou un dict avec
    _reason="reception_sans_contrepartie" quand un token entre sans
    contrepartie, pour que le cas soit compte a part.
    """
    sol_out = 0.0
    incoming: dict | None = None

    for line in lines:
        mint = line.get("mint")
        if not isinstance(mint, str):
            continue
        if mint in SOL_MINTS:
            if line.get("fromUserAccount") == wallet:
                sol_out += _amount(line)
        elif line.get("toUserAccount") == wallet and incoming is None:
            incoming = line

    if incoming is None:
        return None

    tokens_in = _amount(incoming)
    if tokens_in <= 0:
        return None
    if sol_out <= 0:
        return {"_reason": "reception_sans_contrepartie",
                "mint": incoming.get("mint")}

    return {
        "mint": incoming.get("mint"),
        "signature": incoming.get("signature"),
        "bought_at": _line_time(incoming),
        "sol_spent": sol_out,
        "tokens_received": tokens_in,
        "entry_price_sol": sol_out / tokens_in,
    }


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


def collect_purchases(wallet: str) -> tuple[dict, dict] | None:
    """Achats matures d'un wallet, par pagination ascendante. None = PERTE.

    Les lignes sont regroupees PAR SIGNATURE au fil des pages. Le groupe
    de la derniere ligne d'une page reste en attente : il peut se
    poursuivre sur la page suivante.
    """
    now = datetime.now(timezone.utc).timestamp()
    newest_ts = now - V2_MIN_AGE_DAYS * 86400
    oldest_ts = now - V2_MAX_AGE_DAYS * 86400

    purchases: dict[str, dict] = {}
    pending: dict[str, list[dict]] = {}
    token: str | None = None
    pages = lines_read = 0
    signatures_seen = 0
    no_counterpart = 0
    stop_reason = "limite_pages"

    def absorb(groups: dict[str, list[dict]]) -> None:
        nonlocal signatures_seen, no_counterpart
        for lines in groups.values():
            signatures_seen += 1
            purchase = purchase_from_group(lines, wallet)
            if purchase is None:
                continue
            if purchase.get("_reason") == "reception_sans_contrepartie":
                no_counterpart += 1
                continue
            mint = purchase["mint"]
            moment = purchase["bought_at"]
            if not isinstance(mint, str) or mint in purchases:
                continue
            if oldest_ts <= moment <= newest_ts:
                purchases[mint] = purchase

    while pages < V2_MAX_PAGES:
        result = helius.transfers_by_address(
            wallet, TRANSFERS_LIMIT, sort_order="asc", page_token=token
        )
        if result is None:  # PERTE deja loguee cote client
            return None
        items, payload = result
        pages += 1
        lines_read += len(items)

        if not items:
            absorb(pending)
            pending = {}
            stop_reason = "historique_epuise"
            break

        for line in items:
            if not isinstance(line, dict):
                continue
            signature = line.get("signature")
            if isinstance(signature, str):
                pending.setdefault(signature, []).append(line)

        # Le groupe de la derniere ligne peut continuer page suivante.
        last_signature = None
        for line in reversed(items):
            if isinstance(line, dict) and isinstance(line.get("signature"), str):
                last_signature = line["signature"]
                break
        carried = pending.pop(last_signature, None) if last_signature else None
        absorb(pending)
        pending = {last_signature: carried} if carried else {}

        if len(purchases) >= V2_MAX_TOKENS:
            stop_reason = "echantillon_complet"
            break

        # Ordre ascendant : on part du plus ancien et on remonte le temps.
        # On sort de la fenetre quand les lignes deviennent trop RECENTES,
        # donc sur V2_MIN_AGE_DAYS.
        newest_line = max((_line_time(i) for i in items if isinstance(i, dict)),
                          default=0.0)
        if newest_line and newest_line > newest_ts:
            absorb(pending)
            pending = {}
            stop_reason = "fenetre_depassee"
            break

        token = helius.pagination_token(payload)
        if not token:
            absorb(pending)
            pending = {}
            stop_reason = "historique_epuise"
            break

    absorb(pending)
    stats = {
        "pages": pages,
        "lines": lines_read,
        "signatures": signatures_seen,
        "purchases": len(purchases),
        "no_counterpart": no_counterpart,
        "stop_reason": stop_reason,
    }
    return purchases, stats


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------


def peak_after(candles: list[list], moment: float) -> float | None:
    """max(high) des bougies STRICTEMENT posterieures a l'achat.

    La bougie contenant l'achat est exclue : l'utiliser reviendrait a
    connaitre le haut du jour au moment ou l'on achete.
    """
    later = [c for c in candles if _to_float(c[0]) > moment]
    if not later:
        return None
    peak = max(_to_float(c[2]) for c in later)
    return peak if peak > 0 else None


def measure(purchase: dict) -> tuple[float | None, float | None, str]:
    """(perf cappee, perf brute, issue) pour un achat."""
    data = fetch_token_data(purchase["mint"])
    if data is None:  # PERTE reseau, deja loguee
        return None, None, "non_mesurable"

    sol_usd = sol_price_at(purchase["bought_at"])
    if sol_usd is None:
        return None, None, "non_mesurable"
    entry_usd = purchase["entry_price_sol"] * sol_usd
    if entry_usd <= 0:
        return None, None, "non_mesurable"

    if data["status"] == "mort":
        return 0.0, 0.0, "mort"

    peak = peak_after(data.get("candles") or [], purchase["bought_at"])
    if data["status"] == "rug":
        raw = (peak / entry_usd) if peak else 0.0
        return min(raw, PERF_CAP), raw, "rug"

    if peak is None:
        return None, None, "non_mesurable"
    raw = peak / entry_usd
    return min(raw, PERF_CAP), raw, "vivant"


def build_metrics(wallet: str, perfs: list[float], raws: list[float],
                  rugs: int) -> dict:
    """Metriques du wallet. Aucune activation : ce run mesure."""
    evaluated = len(perfs)
    winners = [p for p in perfs if p >= VALIDATION_WIN_MULTIPLE]
    losers = [p for p in perfs if p < VALIDATION_WIN_MULTIPLE]

    return {
        "wallet": wallet,
        "tokens_evaluated": evaluated,
        "win_rate": round(len(winners) / evaluated, 4) if evaluated else None,
        "rug_rate": round(rugs / evaluated, 4) if evaluated else None,
        "median_perf": _median(perfs),
        "median_raw_perf": _median(raws),
        # Gagnants et perdants SEPAREMENT : un win rate de 20 % avec des
        # gagnants a x10 n'est pas un echec, et une mediane globale le
        # masque completement.
        "median_winner_x": _median(winners),
        "median_loser_x": _median(losers),
        "active": False,
        "activation_reason": "mesure_v2",
        "validated_at": datetime.now(timezone.utc).isoformat(),
    }


def backtest_wallet(wallet: str) -> dict | None:
    """Metriques d'un wallet. None = PERTE : pas de validated_at."""
    collected = collect_purchases(wallet)
    if collected is None:
        return None
    purchases, stats = collected

    perfs: list[float] = []
    raws: list[float] = []
    rugs = 0
    counts: Counter = Counter()
    for purchase in list(purchases.values())[:V2_MAX_TOKENS]:
        perf, raw, outcome = measure(purchase)
        counts[outcome] += 1
        if perf is None:
            continue
        perfs.append(perf)
        raws.append(raw if raw is not None else perf)
        if outcome in ("mort", "rug"):
            rugs += 1

    metrics = build_metrics(wallet, perfs, raws, rugs)
    metrics["_stats"] = stats
    metrics["_counts"] = counts
    return metrics


# ---------------------------------------------------------------------------


def fetch_candidates() -> list[str]:
    """Wallets winners_count >= VALIDATION_MIN_WINNERS. Lecture seule."""
    response = (
        db.get_client()
        .table(SMART_WALLETS_TABLE)
        .select("wallet")
        .gte("winners_count", VALIDATION_MIN_WINNERS)
        .limit(5000)
        .execute()
    )
    rows = response.data or []
    log.info("Supabase : %d wallets winners_count >= %d",
             len(rows), VALIDATION_MIN_WINNERS)
    return [r["wallet"] for r in rows if isinstance(r.get("wallet"), str)]


def _histogram(title: str, values: list[float], edges: list[float],
               unit: str = "") -> None:
    if not values:
        return
    log.info("%s :", title)
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

    if not load_sol_prices():
        log.error("Prix du SOL indisponible : le backtest v2 ne peut pas "
                  "convertir les prix d'entree, arret.")
        return 0

    candidates = fetch_candidates()
    if not candidates:
        log.info("Aucun candidat.")
        return 0

    log.info(
        "Selection : 1 appel par candidat pour dater sa plus ancienne "
        "activite, %d appels", len(candidates),
    )
    retained: list[str] = []
    ages: list[float] = []
    lost_selection = 0
    for wallet in candidates:
        age = oldest_activity_days(wallet)
        if age is None:
            lost_selection += 1
            continue
        ages.append(age)
        if V2_MIN_AGE_DAYS <= age <= V2_MAX_AGE_DAYS:
            retained.append(wallet)

    log.info(
        "retenus %d / %d candidats (fenetre %d-%d j) | non datables %d",
        len(retained), len(candidates), V2_MIN_AGE_DAYS, V2_MAX_AGE_DAYS,
        lost_selection,
    )
    if not retained:
        log.info("Aucun wallet dans la fenetre, rien a backtester.")
        return 0

    results: list[dict] = []
    lost: list[str] = []
    totals: Counter = Counter()
    pages_total = 0

    for wallet in retained:
        metrics = backtest_wallet(wallet)
        if metrics is None:
            lost.append(wallet)
            continue
        stats = metrics.pop("_stats")
        counts = metrics.pop("_counts")
        totals.update(counts)
        totals["reception_sans_contrepartie"] += stats["no_counterpart"]
        pages_total += stats["pages"]

        log.info(
            "%s... : %d pages, %d lignes, %d signatures -> %d achats (%s) | "
            "%d mesures | win %s | mediane gagnants %s | mediane perdants %s",
            wallet[:8], stats["pages"], stats["lines"], stats["signatures"],
            stats["purchases"], stats["stop_reason"],
            metrics["tokens_evaluated"], metrics["win_rate"],
            metrics["median_winner_x"], metrics["median_loser_x"],
        )
        db.update_wallet_validation([metrics])
        results.append(metrics)

    if lost:
        log.warning(
            "PERTE : %d wallet(s) non backtestes, sans validated_at", len(lost)
        )

    helius_calls, helius_losses = helius.request_stats()
    gecko_calls, gecko_losses = gt.request_stats()
    measured = [m for m in results if m["tokens_evaluated"]]

    log.info("=== Resume v2 ===")
    log.info(
        "candidats %d | retenus %d | backtestes %d | avec mesures %d | "
        "PERTE %d | non datables %d",
        len(candidates), len(retained), len(results), len(measured),
        len(lost), lost_selection,
    )
    log.info(
        "tokens : morts %d | rugs %d | vivants %d | non mesurables %d | "
        "receptions sans contrepartie %d",
        totals["mort"], totals["rug"], totals["vivant"],
        totals["non_mesurable"], totals["reception_sans_contrepartie"],
    )
    if _amount_key_usage:
        log.info("champ de montant utilise : %s", dict(_amount_key_usage))
    log.info(
        "pages lues %d | appels Helius %d (pertes %d) | CoinGecko %d "
        "(pertes %d)",
        pages_total, helius_calls, helius_losses, gecko_calls, gecko_losses,
    )

    _histogram(
        "distribution des win rates",
        [m["win_rate"] * 100 for m in measured if m["win_rate"] is not None],
        [0, 10, 20, 30, 40, 50, 60, 70, 80, 90], "%",
    )
    _histogram(
        "distribution des medianes de gagnants",
        [m["median_winner_x"] for m in measured
         if m["median_winner_x"] is not None],
        [2, 3, 5, 10, 20], "x",
    )

    top = sorted(
        (m for m in measured if m["median_winner_x"] is not None),
        key=lambda m: -m["median_winner_x"],
    )[:15]
    for metrics in top:
        log.info(
            "  %s  tokens %-3d  win %5.1f%%  gagnants x%-7s perdants x%-7s "
            "rug %5.1f%%",
            metrics["wallet"], metrics["tokens_evaluated"],
            (metrics["win_rate"] or 0) * 100, metrics["median_winner_x"],
            metrics["median_loser_x"], (metrics["rug_rate"] or 0) * 100,
        )

    log.info(
        "Aucun wallet active : active=false, activation_reason=mesure_v2. "
        "Les seuils seront choisis sur cette distribution."
    )
    return len(measured)


if __name__ == "__main__":
    run()
