"""Phase 3 : backtest de validation des wallets candidats.

sol_early_buys ne contient QUE des winners : un wallet vu sur dix d'entre
eux peut etre un excellent trader comme un sniper qui achete tous les
lancements, ses pertes etant invisibles par construction. Le backtest
reconstitue son historique d'achats REEL, mesure ce qu'est devenu chaque
token achete, et n'active que ceux qui performent.

Deux sources :
  - Helius, voies A puis C, pour l'historique d'achats du wallet ;
  - GeckoTerminal, pools puis OHLCV, pour la performance de chaque mint.

Les donnees par mint sont mises en cache pour le run : plusieurs wallets
achetent les memes tokens.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import geckoterminal as gt
import helius
import supabase_client as db
from config import (
    MIN_POOL_LIQUIDITY_USD,
    PERF_CAP,
    VALIDATION_MAX_PAGES,
    VALIDATION_MAX_RUG_RATE,
    VALIDATION_MAX_TOKENS_PER_WALLET,
    VALIDATION_MAX_TX,
    VALIDATION_MIN_TOKEN_AGE_DAYS,
    VALIDATION_MIN_TOKENS,
    VALIDATION_MIN_WIN_RATE,
    VALIDATION_MIN_WINNERS,
    VALIDATION_TARGET_AGE_DAYS,
    VALIDATION_TX_LIMIT,
    VALIDATION_WIN_MULTIPLE,
    diagnose_environment,
    setup_logging,
)
# Reutilise la liste de bruit de la phase 1 sans la dupliquer. Seuls les
# mints servent ici : tokenTransfers ne porte pas de symbole.
from find_winners import NOISE_MINTS

log = logging.getLogger("solana-agent")

# Profondeur d'historique de prix demandee par token. Un achat plus ancien
# que cette fenetre ne peut pas etre price : le token est alors ignore.
OHLCV_DAYS = 180

# Cles ou Helius pourrait exposer un nombre total de transactions. Aucune
# n'est garantie : la sonde n'a vu que 'data'.
TOTAL_TX_KEYS = ("total", "count", "totalCount", "total_count", "totalResults")

# mint -> donnees de marche du run, ou None si le mint est inmesurable.
_token_cache: dict[str, dict | None] = {}


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Historique d'achats d'un wallet
# ---------------------------------------------------------------------------


def total_transactions(result: dict) -> int | None:
    """Nombre total de transactions, si Helius l'expose. None sinon."""
    for key in TOTAL_TX_KEYS:
        value = result.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return None


def extract_purchases(transactions: list[dict], wallet: str) -> dict[str, dict]:
    """Premier achat par mint, hors bruit.

    Les transactions doivent etre fournies dans l'ordre chronologique : le
    "premier" achat en depend. Une vente dans la meme transaction n'annule
    pas une reception, comme en phase 2.
    """
    purchases: dict[str, dict] = {}

    for transaction in transactions:
        transfers = transaction.get("tokenTransfers")
        if not isinstance(transfers, list):
            continue
        timestamp = transaction.get("timestamp")
        if not isinstance(timestamp, (int, float)) or timestamp <= 0:
            continue

        for item in transfers:
            if not isinstance(item, dict):
                continue
            mint = item.get("mint")
            if not isinstance(mint, str) or mint in NOISE_MINTS:
                continue
            if item.get("toUserAccount") != wallet:
                continue
            purchases.setdefault(mint, {
                "mint": mint,
                "bought_at": float(timestamp),
                "signature": transaction.get("signature"),
            })

    return purchases


# ---------------------------------------------------------------------------
# Performance d'un mint
# ---------------------------------------------------------------------------


def _most_liquid_pool(pools: list[dict]) -> dict | None:
    best = None
    best_liquidity = -1.0
    for pool in pools:
        attributes = pool.get("attributes") or {}
        liquidity = _to_float(attributes.get("reserve_in_usd"))
        if liquidity > best_liquidity:
            best, best_liquidity = pool, liquidity
    return best


def fetch_token_data(mint: str) -> dict | None:
    """Etat de marche d'un mint. None = PERTE reseau (non memorisee).

    Trois etats, testes dans cet ordre :
      mort   : aucun pool. Aucun appel OHLCV, un seul appel au lieu de deux.
      rug    : pool sous MIN_POOL_LIQUIDITY_USD. L'OHLCV est quand meme
               tente, pour mesurer la perf atteinte avant la chute.
      vivant : pool au-dessus du seuil.

    Les trois sont mis en cache pour la duree du run, tokens morts compris.
    """
    if mint in _token_cache:
        return _token_cache[mint]

    result = gt.token_pools(mint)
    if result is None:  # PERTE deja loguee, ne pas memoriser
        return None
    pools, _ = result

    pool = _most_liquid_pool(pools) if pools else None
    if pool is None:
        # Aucun pool : le token est mort. C'est une perte seche, elle doit
        # compter dans le backtest.
        data = {"mint": mint, "status": "mort", "candles": None, "is_rug": True}
        _token_cache[mint] = data
        return data

    attributes = pool.get("attributes") or {}
    liquidity = _to_float(attributes.get("reserve_in_usd"))
    volume_24h = _to_float((attributes.get("volume_usd") or {}).get("h24"))
    is_rug = liquidity < MIN_POOL_LIQUIDITY_USD or volume_24h <= 0
    status = "rug" if is_rug else "vivant"

    pool_address = attributes.get("address") or pool.get("id", "").split("_", 1)[-1]
    candles = gt.ohlcv_day(pool_address, limit=OHLCV_DAYS)
    if candles is None:  # PERTE, ne pas memoriser
        return None

    ordered = sorted(
        (c for c in candles if isinstance(c, (list, tuple)) and len(c) >= 5),
        key=lambda c: _to_float(c[0]),
    )
    data = {
        "mint": mint,
        "status": status,
        "pool_address": pool_address,
        "liquidity_usd": liquidity,
        "volume_24h_usd": volume_24h,
        "candles": ordered if len(ordered) >= 2 else None,
        "is_rug": is_rug,
    }
    _token_cache[mint] = data
    return data


def performance_since(data: dict, bought_at: float) -> float | None:
    """max(high APRES l'achat) / prix a l'achat, cappe. None si non mesurable."""
    candles = data.get("candles")
    if not candles:
        return None

    entry_index = None
    for index, candle in enumerate(candles):
        if _to_float(candle[0]) <= bought_at:
            entry_index = index
        else:
            break
    if entry_index is None:  # achat anterieur a la fenetre OHLCV
        return None

    entry_price = _to_float(candles[entry_index][4])  # close du jour d'achat
    if entry_price <= 0:
        return None

    later = candles[entry_index + 1:]
    if not later:  # achat trop recent, rien apres
        return None

    peak = max(_to_float(candle[2]) for candle in later)
    if peak <= 0:
        return None
    return min(peak / entry_price, PERF_CAP)


def evaluate_purchase(data: dict, bought_at: float) -> tuple[float | None, str]:
    """(performance, issue). perf None = token non mesurable, non compte.

    Un token mort ou rugge est TOUJOURS compte, a perf 0 si son historique
    de prix est indisponible : c'est la perte qu'on cherche a mesurer. Seul
    un token VIVANT dont l'OHLCV est inexploitable sort du decompte.
    """
    if data["status"] == "mort":
        return 0.0, "mort"

    perf = performance_since(data, bought_at)
    if data["status"] == "rug":
        return (perf if perf is not None else 0.0), "rug"

    if perf is None:
        return None, "non_mesurable"
    return perf, "vivant"


# ---------------------------------------------------------------------------
# Verdict par wallet
# ---------------------------------------------------------------------------


def build_verdict(wallet: str, perfs: list[float], rugs: int) -> dict:
    """Metriques et activation. perfs est deja cappe a PERF_CAP."""
    evaluated = len(perfs)
    validated_at = datetime.now(timezone.utc).isoformat()

    if evaluated == 0:
        win_rate = rug_rate = median_perf = None
    else:
        win_rate = round(
            sum(1 for p in perfs if p >= VALIDATION_WIN_MULTIPLE) / evaluated, 4
        )
        rug_rate = round(rugs / evaluated, 4)
        median_perf = round(statistics.median(perfs), 4)

    if evaluated < VALIDATION_MIN_TOKENS:
        # Pas de verdict invente sur un ou deux tokens.
        active, reason = False, "historique_insuffisant"
    elif win_rate >= VALIDATION_MIN_WIN_RATE and rug_rate <= VALIDATION_MAX_RUG_RATE:
        active, reason = True, "backtest_valide"
    else:
        active, reason = False, "backtest_echoue"

    return {
        "wallet": wallet,
        "tokens_evaluated": evaluated,
        "win_rate": win_rate,
        "median_perf": median_perf,
        "rug_rate": rug_rate,
        "active": active,
        "activation_reason": reason,
        "validated_at": validated_at,
    }


def collect_transactions(
    wallet: str,
) -> tuple[list[dict], int, str, dict] | None:
    """Pagine la voie A jusqu'a la profondeur visee.

    Retourne (transactions, pages lues, motif d'arret, result de la 1re
    page). None = PERTE.

    Mesure du 18/09 : 500 transactions couvrent 3 heures chez un wallet tres
    actif. Une seule page ne peut donc jamais atteindre la fenetre 7-60 j ou
    se trouvent les winners.
    """
    target_ts = (
        datetime.now(timezone.utc).timestamp()
        - VALIDATION_TARGET_AGE_DAYS * 86400
    )
    collected: list[dict] = []
    token: str | None = None
    pages = 0
    first_result: dict = {}

    while pages < VALIDATION_MAX_PAGES:
        detailed = helius.transactions_for_address_detailed(
            wallet, VALIDATION_TX_LIMIT, sort_order="desc", page_token=token
        )
        if detailed is None:  # PERTE deja loguee cote client
            return None
        items, result = detailed
        pages += 1
        if pages == 1:
            first_result = result
        collected.extend(items)

        if not items:
            return collected, pages, "historique_epuise", first_result

        last = _to_float(items[-1].get("blockTime"))
        if last and last <= target_ts:
            return collected, pages, "profondeur_atteinte", first_result

        token = helius.pagination_token(result)
        if not token:
            return collected, pages, "historique_epuise", first_result

    return collected, pages, "limite_pages", first_result


def validate_wallet(wallet: str) -> dict | None:
    """Verdict d'un wallet. None = PERTE : pas de validated_at.

    Retourne {"bot": True} quand le wallet depasse VALIDATION_MAX_TX.
    """
    collected = collect_transactions(wallet)
    if collected is None:
        return None
    transactions, pages, stop_reason, first_result = collected

    total = total_transactions(first_result)
    if total is not None and total > VALIDATION_MAX_TX:
        return {"bot": True, "total_tx": total}

    now_ts = datetime.now(timezone.utc).timestamp()
    block_times = [
        _to_float(item.get("blockTime")) for item in transactions
        if isinstance(item, dict) and _to_float(item.get("blockTime")) > 0
    ]
    coverage = (
        ((now_ts - max(block_times)) / 86400, (now_ts - min(block_times)) / 86400)
        if block_times else (0.0, 0.0)
    )

    pagination = {
        "pages": pages,
        "transactions": len(transactions),
        "coverage": coverage,
        "stop_reason": stop_reason,
    }

    # Fenetre de maturite : entre VALIDATION_MIN_TOKEN_AGE_DAYS et
    # VALIDATION_TARGET_AGE_DAYS. Filtrer ICI, sur le blockTime que la voie A
    # porte deja, evite d'enrichir des transactions qu'on ecarterait ensuite.
    newest_ts = now_ts - VALIDATION_MIN_TOKEN_AGE_DAYS * 86400
    oldest_ts = now_ts - VALIDATION_TARGET_AGE_DAYS * 86400
    in_window = [
        item for item in transactions
        if isinstance(item, dict) and item.get("err") is None
        and isinstance(item.get("signature"), str)
        and oldest_ts <= _to_float(item.get("blockTime")) <= newest_ts
    ]
    pagination["in_window"] = len(in_window)

    if not in_window:
        verdict = build_verdict(wallet, [], 0)
        verdict["activation_reason"] = "historique_trop_recent"
        verdict.update(_bought=0, _mature=0, _sampled=0, _ages=(),
                       _counts=Counter(), _pagination=pagination)
        return verdict

    enriched = helius.enrich_signatures([item["signature"] for item in in_window])
    if enriched is None:
        return None

    # La voie A est interrogee en ordre DESCENDANT : on remet les
    # transactions en ordre chronologique pour que "premier achat" designe
    # bien la plus ancienne entree de la fenetre.
    chronological = sorted(enriched, key=lambda t: _to_float(t.get("timestamp")))
    purchases = extract_purchases(chronological, wallet)

    # Second filtre de maturite, sur le timestamp de la voie C cette fois :
    # c'est lui qui fait foi pour la date d'achat.
    cutoff = now_ts - VALIDATION_MIN_TOKEN_AGE_DAYS * 86400
    mature = [p for p in purchases.values() if p["bought_at"] <= cutoff]

    # Echantillon des plus RECENTS parmi les matures. Jamais un tri sur la
    # performance : cela biaiserait mecaniquement le win rate.
    sample = sorted(
        mature, key=lambda p: p["bought_at"], reverse=True
    )[:VALIDATION_MAX_TOKENS_PER_WALLET]
    ages = sorted((now_ts - p["bought_at"]) / 86400 for p in sample)

    if len(mature) < VALIDATION_MIN_TOKENS:
        # Verdict rendu sans aucun appel de marche : il n'y a rien a mesurer.
        verdict = build_verdict(wallet, [], 0)
        verdict["activation_reason"] = "historique_trop_recent"
        verdict.update(_bought=len(purchases), _mature=len(mature), _sampled=0,
                       _ages=(), _counts=Counter(), _pagination=pagination)
        return verdict

    perfs: list[float] = []
    rugs = 0
    counts: Counter = Counter()
    for purchase in sample:
        data = fetch_token_data(purchase["mint"])
        if data is None:  # PERTE reseau sur ce mint, deja loguee
            counts["non_mesurable"] += 1
            continue
        perf, outcome = evaluate_purchase(data, purchase["bought_at"])
        counts[outcome] += 1
        if perf is None:
            continue
        perfs.append(perf)
        if outcome in ("mort", "rug"):
            rugs += 1

    verdict = build_verdict(wallet, perfs, rugs)
    verdict.update(
        _bought=len(purchases), _mature=len(mature), _sampled=len(sample),
        _ages=(ages[0], ages[-1]) if ages else (),
        _counts=counts, _pagination=pagination,
    )
    return verdict


# ---------------------------------------------------------------------------


def _distribution(verdicts: list[dict]) -> None:
    """Repartition des win rates par tranche de 10 points."""
    buckets = Counter()
    for verdict in verdicts:
        rate = verdict.get("win_rate")
        if rate is None:
            continue
        bucket = min(int(math.floor(rate * 10)) * 10, 90)
        buckets[bucket] += 1
    if not buckets:
        return
    log.info("distribution des win rates :")
    for low in range(0, 100, 10):
        count = buckets.get(low, 0)
        if count:
            log.info("  %3d-%3d%% : %-3d %s", low, low + 9, count, "#" * count)


def run() -> int:
    setup_logging()
    diagnose_environment()
    helius.api_key()

    candidates = db.fetch_wallets_to_validate(VALIDATION_MIN_WINNERS)
    if not candidates:
        log.info("Aucun wallet a backtester.")
        return 0

    # La voie A est desormais paginee : le cout haut depend du nombre de
    # pages, la voie C ne porte plus que la fenetre de maturite.
    max_pages_total = len(candidates) * VALIDATION_MAX_PAGES
    log.info(
        "Budget : voie A au plus %d x %d = %d appels (~%.0f min). Voie C : "
        "seules les transactions de %d a %d j sont enrichies, par lots de %d. "
        "CoinGecko : au plus %d tokens, 1 a 2 appels par mint distinct, le "
        "cache absorbant les doublons",
        len(candidates), VALIDATION_MAX_PAGES, max_pages_total,
        max_pages_total * helius.MIN_REQUEST_INTERVAL_S / 60,
        VALIDATION_MIN_TOKEN_AGE_DAYS, VALIDATION_TARGET_AGE_DAYS,
        helius.ENRICH_BATCH_SIZE,
        len(candidates) * VALIDATION_MAX_TOKENS_PER_WALLET,
    )

    verdicts: list[dict] = []
    bots = 0
    lost: list[str] = []
    totals: Counter = Counter()
    pages_read = 0
    capped = 0

    for index, candidate in enumerate(candidates, start=1):
        wallet = candidate.get("wallet")
        if not isinstance(wallet, str) or not wallet:
            continue

        verdict = validate_wallet(wallet)
        if verdict is None:  # PERTE deja loguee cote client
            lost.append(wallet)
            continue
        if verdict.get("bot"):
            bots += 1
            log.info("%s... : exclu bot/MEV (%d tx)", wallet[:8], verdict["total_tx"])
            continue

        bought = verdict.pop("_bought", 0)
        mature = verdict.pop("_mature", 0)
        sampled = verdict.pop("_sampled", 0)
        ages = verdict.pop("_ages", ())
        counts = verdict.pop("_counts", Counter())
        pagination = verdict.pop("_pagination", {})
        totals.update(counts)

        if pagination:
            oldest, newest = pagination["coverage"]
            log.info(
                "%s... : %d pages, %d tx, couverture %.0f a %.0f j "
                "(%d dans la fenetre)",
                wallet[:8], pagination["pages"], pagination["transactions"],
                newest, oldest, pagination["in_window"],
            )
            if pagination["stop_reason"] == "limite_pages":
                # Limite atteinte, pas un succes : la profondeur visee n'est
                # pas couverte et des achats matures manquent peut-etre.
                log.warning(
                    "  %s... : LIMITE de %d pages atteinte, profondeur %d j "
                    "NON atteinte (couverture %.0f j)",
                    wallet[:8], VALIDATION_MAX_PAGES,
                    VALIDATION_TARGET_AGE_DAYS, oldest,
                )
            pages_read += pagination["pages"]
            if pagination["stop_reason"] == "limite_pages":
                capped += 1

        # Cette ligne est ce qui permet de verifier que la fenetre mesuree
        # est la bonne : un echantillon trop jeune ne mesure rien.
        window = f" (achats de {ages[0]:.0f} a {ages[1]:.0f} j)" if ages else ""
        log.info(
            "%s... : %d achats, %d matures, %d echantillonnes%s | %d mesures "
            "(morts %d / rugs %d / vivants %d, non mesurables %d) | win %s "
            "| mediane %s | rug %s | %s",
            wallet[:8], bought, mature, sampled, window,
            verdict["tokens_evaluated"],
            counts["mort"], counts["rug"], counts["vivant"],
            counts["non_mesurable"],
            verdict["win_rate"], verdict["median_perf"], verdict["rug_rate"],
            verdict["activation_reason"],
        )
        verdicts.append(verdict)

        # Ecriture au fil de l'eau : un arret du service ne doit pas faire
        # rejouer les wallets deja backtestes.
        db.update_wallet_validation([verdict])

        if index % 10 == 0:
            valid_so_far = sum(
                1 for v in verdicts if v["activation_reason"] == "backtest_valide"
            )
            log.info(
                "wallet %d/%d | valides %d | cache %d mints | appels "
                "CoinGecko %d",
                index, len(candidates), valid_so_far, len(_token_cache),
                gt.request_stats()[0],
            )

    if lost:
        log.warning(
            "PERTE : %d wallet(s) non backtestes, sans validated_at, rejoues "
            "au prochain run", len(lost),
        )

    valid = [v for v in verdicts if v["activation_reason"] == "backtest_valide"]
    thin = [v for v in verdicts if v["activation_reason"] == "historique_insuffisant"]
    too_recent = [
        v for v in verdicts if v["activation_reason"] == "historique_trop_recent"
    ]
    helius_calls, helius_losses = helius.request_stats()
    gecko_calls, gecko_losses = gt.request_stats()

    log.info("=== Resume ===")
    log.info(
        "candidats %d | backtestes %d | valides %d | historique insuffisant "
        "%d | trop recent %d | exclus bot %d",
        len(candidates), len(verdicts), len(valid), len(thin),
        len(too_recent), bots,
    )
    log.info(
        "pagination : %d pages lues au total | %d wallet(s) plafonnes a %d "
        "pages sans atteindre %d j",
        pages_read, capped, VALIDATION_MAX_PAGES, VALIDATION_TARGET_AGE_DAYS,
    )
    log.info(
        "tokens : morts %d | rugs %d | vivants %d | non mesurables %d",
        totals["mort"], totals["rug"], totals["vivant"], totals["non_mesurable"],
    )
    log.info(
        "mints distincts %d | appels Helius %d (pertes %d) | CoinGecko %d "
        "(pertes %d)",
        len(_token_cache), helius_calls, helius_losses, gecko_calls, gecko_losses,
    )
    _distribution(verdicts)

    top = sorted(
        valid,
        key=lambda v: (-(v["win_rate"] or 0), -(v["median_perf"] or 0)),
    )[:15]
    for verdict in top:
        log.info(
            "  VALIDE %s  tokens %-3d  win %5.1f%%  mediane x%-6s rug %5.1f%%",
            verdict["wallet"], verdict["tokens_evaluated"],
            (verdict["win_rate"] or 0) * 100, verdict["median_perf"],
            (verdict["rug_rate"] or 0) * 100,
        )
    return len(valid)


if __name__ == "__main__":
    run()
