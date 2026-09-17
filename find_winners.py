"""Phase 1 : discovery des tokens Solana "winners" (x5 ou plus).

Pipeline : collecte des pools -> deduplication par token -> pre-filtres sur
le payload -> memoire Supabase -> OHLCV 60 jours -> upsert de tous les
candidats analyses.

Les winners produits ici serviront, dans une phase ulterieure, a remonter
les wallets qui les ont achetes tot.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable

import geckoterminal as gt
import supabase_client as db
from config import (
    ANALYZED_TTL_DAYS,
    MAX_POOL_AGE_DAYS,
    MIN_LIQUIDITY_USD,
    MIN_POOL_AGE_DAYS,
    MIN_VOLUME_24H_USD,
    WINNER_MULTIPLE,
    diagnose_environment,
    setup_logging,
)

log = logging.getLogger("solana-agent")

# Objectif de volume : en dessous, le recoupement entre early buyers ne
# donne rien (constate sur ETH avec 7 winners).
WINNERS_TARGET = 50

NEW_POOLS_PAGES = 10
TRENDING_POOLS_PAGES = 5
TOP_POOLS_PAGES = 10

# Quote assets et LST : ils apparaissent en base_token sur certains pools
# mais ne sont jamais des winners recherches.
NOISE_MINTS = {
    "So11111111111111111111111111111111111111112": "wSOL",
    "So11111111111111111111111111111111111111111": "SOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn": "JitoSOL",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": "mSOL",
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1": "bSOL",
    "5oVNBeEEQvYi1cX3ir8Dx5n1P7pdxydbGF2X4TxVusJm": "INF",
}

# Filet de securite si un mint de la liste ci-dessus evolue ou manque.
NOISE_SYMBOLS = {"SOL", "WSOL", "USDC", "USDT", "JITOSOL", "MSOL", "BSOL", "INF"}


# ---------------------------------------------------------------------------
# Helpers de parsing
# ---------------------------------------------------------------------------


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _base_mint(pool: dict) -> str | None:
    """Mint du base_token, extrait de l'id relationnel 'solana_<mint>'."""
    token_id = (
        pool.get("relationships", {})
        .get("base_token", {})
        .get("data", {})
        .get("id")
    )
    if not isinstance(token_id, str) or "_" not in token_id:
        return None
    return token_id.split("_", 1)[1]


def _pool_age_days(created_at: str | None, now: datetime) -> float | None:
    if not created_at:
        return None
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (now - created).total_seconds() / 86400.0


def _symbol_from_pool_name(pool: dict) -> str:
    """'BONK / SOL 0.25%' -> 'BONK'. Evite un appel token supplementaire."""
    name = pool.get("attributes", {}).get("name") or ""
    return name.split("/")[0].strip() or "?"


def _index_tokens(included: Iterable[dict], index: dict[str, dict]) -> None:
    """Alimente mint -> attributs token depuis la section 'included'."""
    for item in included:
        if item.get("type") != "token":
            continue
        item_id = item.get("id", "")
        if "_" not in item_id:
            continue
        index.setdefault(item_id.split("_", 1)[1], item.get("attributes", {}))


# ---------------------------------------------------------------------------
# Etape a : collecte
# ---------------------------------------------------------------------------


def collect_pools() -> tuple[list[dict], dict[str, dict], int]:
    """Agrege new_pools, trending_pools et pools.

    Retourne (pools, index mint -> attributs token, nombre de pages perdues).
    """
    sources = (
        ("new_pools", gt.new_pools, NEW_POOLS_PAGES),
        ("trending_pools", gt.trending_pools, TRENDING_POOLS_PAGES),
        ("pools", gt.top_pools, TOP_POOLS_PAGES),
    )
    pools: list[dict] = []
    token_index: dict[str, dict] = {}
    losses = 0

    for label, fetch, pages in sources:
        collected = 0
        for page in range(1, pages + 1):
            result = fetch(page)
            if result is None:  # perte deja loguee cote client HTTP
                losses += 1
                continue
            batch, included = result
            if not batch:  # vraie fin de pagination
                break
            pools.extend(batch)
            _index_tokens(included, token_index)
            collected += len(batch)
        log.info("Collecte %-16s : %d pools", label, collected)

    if losses:
        log.warning("Collecte : %d page(s) perdue(s), couverture incomplete", losses)
    return pools, token_index, losses


# ---------------------------------------------------------------------------
# Etapes b, c, d : deduplication, bruit, pre-filtres
# ---------------------------------------------------------------------------


def build_candidates(pools: list[dict], token_index: dict[str, dict]) -> list[dict]:
    """Un candidat par token (le pool le plus liquide), pre-filtres appliques."""
    now = datetime.now(timezone.utc)
    best: dict[str, dict] = {}

    for pool in pools:
        mint = _base_mint(pool)
        if not mint or mint in NOISE_MINTS:
            continue

        attrs = pool.get("attributes", {})
        token_attrs = token_index.get(mint, {})
        symbol = (token_attrs.get("symbol") or _symbol_from_pool_name(pool)).strip()
        if symbol.upper() in NOISE_SYMBOLS:
            continue

        age = _pool_age_days(attrs.get("pool_created_at"), now)
        if age is None or not (MIN_POOL_AGE_DAYS <= age <= MAX_POOL_AGE_DAYS):
            continue

        liquidity = _to_float(attrs.get("reserve_in_usd"))
        if liquidity < MIN_LIQUIDITY_USD:
            continue

        volume_24h = _to_float((attrs.get("volume_usd") or {}).get("h24"))
        if volume_24h < MIN_VOLUME_24H_USD:
            continue

        pool_address = attrs.get("address") or (pool.get("id", "").split("_", 1)[-1])
        candidate = {
            "mint": mint,
            "symbol": symbol,
            "name": token_attrs.get("name") or attrs.get("name") or symbol,
            "pool_address": pool_address,
            "liquidity_usd": round(liquidity, 2),
            "volume_24h_usd": round(volume_24h, 2),
        }
        # Un token a souvent plusieurs pools : on garde le plus liquide, qui
        # porte l'historique de prix le plus representatif.
        previous = best.get(mint)
        if previous is None or liquidity > previous["liquidity_usd"]:
            best[mint] = candidate

    return list(best.values())


# ---------------------------------------------------------------------------
# Etapes g, h : performance
# ---------------------------------------------------------------------------


def analyze_performance(candidate: dict) -> dict | None:
    """Enrichit le candidat avec perf_x / peak_at / is_winner.

    Retourne None si l'OHLCV a ete PERDU (reseau) : dans ce cas on n'ecrit
    rien, pour que le token soit retente au prochain run au lieu d'etre
    enterre dans la memoire.
    """
    candles = gt.ohlcv_day(candidate["pool_address"], limit=60)
    if candles is None:
        return None

    row = dict(candidate)
    # [timestamp, open, high, low, close, volume], ordre decroissant cote API.
    ordered = sorted(
        (c for c in candles if isinstance(c, (list, tuple)) and len(c) >= 5),
        key=lambda c: _to_float(c[0]),
    )
    if len(ordered) < 3:
        row.update(perf_x=None, peak_at=None, is_winner=False,
                   rejected_reason="ohlcv_invalide")
        return row

    first_open = next((_to_float(c[1]) for c in ordered if _to_float(c[1]) > 0), 0.0)
    highs = [(_to_float(c[2]), _to_float(c[0])) for c in ordered]
    peak_price, peak_ts = max(highs, key=lambda item: item[0])
    if first_open <= 0 or peak_price <= 0:
        row.update(perf_x=None, peak_at=None, is_winner=False,
                   rejected_reason="ohlcv_invalide")
        return row

    perf_x = peak_price / first_open
    is_winner = perf_x >= WINNER_MULTIPLE
    row.update(
        perf_x=round(perf_x, 4),
        peak_at=datetime.fromtimestamp(peak_ts, tz=timezone.utc).isoformat(),
        is_winner=is_winner,
        # Pas de filtre sur le drawdown : sur Solana un vrai winner fait
        # -85% en routine sans cesser d'etre un winner.
        rejected_reason=None if is_winner else "perf_insuffisante",
    )
    return row


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def run() -> int:
    setup_logging()
    diagnose_environment()

    pools, token_index, _ = collect_pools()
    candidates = build_candidates(pools, token_index)

    known = db.fetch_recent_mints(ANALYZED_TTL_DAYS)
    fresh = [c for c in candidates if c["mint"] not in known]
    already = len(candidates) - len(fresh)

    log.info(
        "collectes %d | dedupliques %d | deja analyses %d | a analyser %d",
        len(pools), len(candidates), already, len(fresh),
    )
    if not fresh:
        log.info("Rien de nouveau a analyser.")
        return 0

    analyzed: list[dict] = []
    lost = 0
    analyzed_at = datetime.now(timezone.utc).isoformat()
    for candidate in fresh:
        row = analyze_performance(candidate)
        if row is None:
            lost += 1
            continue
        row["analyzed_at"] = analyzed_at
        analyzed.append(row)

    if lost:
        log.warning("OHLCV : %d token(s) perdus, retentes au prochain run", lost)

    # Winners ET non-winners sont ecrits : c'est ce qui alimente la memoire
    # et evite de re-analyser les memes tokens a chaque run.
    db.upsert_analyzed_tokens(analyzed)

    winners = sorted(
        (r for r in analyzed if r["is_winner"]),
        key=lambda r: r["perf_x"],
        reverse=True,
    )
    invalid = sum(1 for r in analyzed if r["rejected_reason"] == "ohlcv_invalide")
    calls, losses = gt.request_stats()

    log.info("=== Resume ===")
    log.info(
        "analyses %d | winners %d | ohlcv invalides %d | appels API %d "
        "| pertes %d",
        len(analyzed), len(winners), invalid, calls, losses,
    )
    for winner in winners:
        log.info(
            "  WINNER %-12s x%-7s %s  liq %s$",
            winner["symbol"][:12],
            round(winner["perf_x"], 2),
            winner["mint"],
            f"{winner['liquidity_usd']:,.0f}",
        )

    if len(winners) < WINNERS_TARGET:
        log.warning(
            "Objectif non atteint : %d winners sur %d vises. Relancer le run "
            "ou assouplir les seuils AJUSTABLES de config.py.",
            len(winners), WINNERS_TARGET,
        )
    return len(winners)


if __name__ == "__main__":
    run()
