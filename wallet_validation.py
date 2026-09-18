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
    VALIDATION_MAX_RUG_RATE,
    VALIDATION_MAX_TX,
    VALIDATION_MIN_TOKENS,
    VALIDATION_MIN_WIN_RATE,
    VALIDATION_MIN_WINNERS,
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
    """Pool le plus liquide + bougies. None = mint non mesurable.

    Mis en cache pour la duree du run : plusieurs wallets achetent les
    memes tokens, et chaque mint coute deux appels GeckoTerminal.
    """
    if mint in _token_cache:
        return _token_cache[mint]

    result = gt.token_pools(mint)
    if result is None:  # PERTE deja loguee, ne pas memoriser
        return None
    pools, _ = result

    pool = _most_liquid_pool(pools) if pools else None
    if pool is None:
        _token_cache[mint] = None
        return None

    attributes = pool.get("attributes") or {}
    liquidity = _to_float(attributes.get("reserve_in_usd"))
    if liquidity < MIN_POOL_LIQUIDITY_USD:
        # Pool trop mince pour que l'OHLCV soit exploitable : le token est
        # ignore et ne comptera pas dans tokens_evaluated.
        _token_cache[mint] = None
        return None

    pool_address = attributes.get("address") or pool.get("id", "").split("_", 1)[-1]
    candles = gt.ohlcv_day(pool_address, limit=OHLCV_DAYS)
    if candles is None:  # PERTE, ne pas memoriser
        return None

    ordered = sorted(
        (c for c in candles if isinstance(c, (list, tuple)) and len(c) >= 5),
        key=lambda c: _to_float(c[0]),
    )
    if len(ordered) < 2:
        _token_cache[mint] = None
        return None

    volume_24h = _to_float((attributes.get("volume_usd") or {}).get("h24"))
    data = {
        "mint": mint,
        "pool_address": pool_address,
        "liquidity_usd": liquidity,
        "volume_24h_usd": volume_24h,
        "candles": ordered,
        # La liquidite est deja >= MIN_POOL_LIQUIDITY_USD a ce stade : seul
        # un volume 24h nul peut encore signaler un token mort.
        "is_rug": liquidity < MIN_POOL_LIQUIDITY_USD or volume_24h <= 0,
    }
    _token_cache[mint] = data
    return data


def performance_since(data: dict, bought_at: float) -> float | None:
    """max(high APRES l'achat) / prix a l'achat, cappe. None si non mesurable."""
    candles = data["candles"]
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


def validate_wallet(wallet: str) -> dict | None:
    """Verdict d'un wallet. None = PERTE : pas de validated_at.

    Retourne {"bot": True} quand le wallet depasse VALIDATION_MAX_TX.
    """
    detailed = helius.transactions_for_address_detailed(
        wallet, VALIDATION_TX_LIMIT, sort_order="desc"
    )
    if detailed is None:
        return None
    transactions, result = detailed

    total = total_transactions(result)
    if total is not None and total > VALIDATION_MAX_TX:
        return {"bot": True, "total_tx": total}

    usable = [
        item for item in transactions
        if isinstance(item, dict) and item.get("err") is None
        and isinstance(item.get("signature"), str)
    ]
    if not usable:
        return build_verdict(wallet, [], 0)

    enriched = helius.enrich_signatures([item["signature"] for item in usable])
    if enriched is None:
        return None

    # La voie A est interrogee en ordre DESCENDANT (historique recent) : on
    # remet les transactions en ordre chronologique pour que "premier achat"
    # designe bien la plus ancienne entree de la fenetre.
    chronological = sorted(
        enriched,
        key=lambda t: _to_float(t.get("timestamp")),
    )
    purchases = extract_purchases(chronological, wallet)

    perfs: list[float] = []
    rugs = 0
    for purchase in purchases.values():
        data = fetch_token_data(purchase["mint"])
        if data is None:  # non mesurable : ne compte pas
            continue
        perf = performance_since(data, purchase["bought_at"])
        if perf is None:  # OHLCV inexploitable pour cette date
            continue
        perfs.append(perf)
        if data["is_rug"]:
            rugs += 1

    verdict = build_verdict(wallet, perfs, rugs)
    verdict["_bought"] = len(purchases)
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

    per_wallet = 1 + math.ceil(VALIDATION_TX_LIMIT / helius.ENRICH_BATCH_SIZE)
    log.info(
        "Budget : %d wallets x %d appels Helius = %d (~%.1f min), plus 2 "
        "appels CoinGecko par mint distinct (~%.0f s chacun)",
        len(candidates), per_wallet, len(candidates) * per_wallet,
        len(candidates) * per_wallet * helius.MIN_REQUEST_INTERVAL_S / 60,
        2 * gt.MIN_REQUEST_INTERVAL_S,
    )

    verdicts: list[dict] = []
    bots = 0
    lost: list[str] = []
    no_total = 0

    for candidate in candidates:
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
        if verdict["tokens_evaluated"] == 0 and bought == 0:
            no_total += 1
        log.info(
            "%s... : %d achats | %d mesures | win %s | mediane %s | rug %s | %s",
            wallet[:8], bought, verdict["tokens_evaluated"],
            verdict["win_rate"], verdict["median_perf"], verdict["rug_rate"],
            verdict["activation_reason"],
        )
        verdicts.append(verdict)

    if lost:
        log.warning(
            "PERTE : %d wallet(s) non backtestes, sans validated_at, rejoues "
            "au prochain run", len(lost),
        )

    db.update_wallet_validation(verdicts)

    valid = [v for v in verdicts if v["activation_reason"] == "backtest_valide"]
    thin = [v for v in verdicts if v["activation_reason"] == "historique_insuffisant"]
    helius_calls, helius_losses = helius.request_stats()
    gecko_calls, gecko_losses = gt.request_stats()

    log.info("=== Resume ===")
    log.info(
        "candidats %d | backtestes %d | valides %d | historique insuffisant "
        "%d | exclus bot %d",
        len(candidates), len(verdicts), len(valid), len(thin), bots,
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
