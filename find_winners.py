"""Phase 1 : discovery des tokens Solana "winners" (x5 ou plus).

Pipeline : collecte des pools -> deduplication par token -> pre-filtres sur
le payload -> memoire Supabase -> OHLCV 60 jours -> upsert de tous les
candidats analyses.

Les winners produits ici serviront, dans une phase ulterieure, a remonter
les wallets qui les ont achetes tot.
"""

from __future__ import annotations

import logging
import os
import statistics
from datetime import datetime, timezone
from functools import partial
from typing import Any, Callable, Iterable

import geckoterminal as gt
import supabase_client as db
from config import (
    ANALYZED_TTL_DAYS,
    MAX_POOL_AGE_DAYS,
    MIN_LIQUIDITY_USD,
    MIN_POOL_AGE_DAYS,
    MIN_REQUEST_INTERVAL_S,
    MIN_VOLUME_24H_USD,
    WINNER_MULTIPLE,
    diagnose_environment,
    setup_logging,
)

log = logging.getLogger("solana-agent")

# Topologie de collecte, arretee au run du 17/09 19:18 apres trois
# iterations de mesure. Seules subsistent les sources dont l'age median tombe
# dans la fenetre 7-60 j ou s'en approche.
#
# Sorties de la collecte, avec leur age median mesure :
#   new_pools             0,0 j - reviendra pour une logique d'accumulation
#   trending_5m           0,1 j
#   dex_meteora-damm-v2   0,3 j
#   dex_meteora-dbc       0,3 j
#   dex_pumpswap          0,3 j
#   pools_volume          0,4 j - le tri par volume n'y a rien change
#   dex_bags-fm           3,2 j
#   dex_orca            404,1 j
#   dex_boop-fun        504,0 j - 17 pools seulement
#   dex_heaven              n/a - 0 pool
TRENDING_POOLS_PAGES = 10
DEX_POOLS_PAGES = 10

# Trois durees conservees : 1h (49,9 j), 6h (21,0 j), 24h (21,0 j).
TRENDING_DURATIONS = ("1h", "6h", "24h")

VOLUME_SORT = "h24_volume_usd_desc"

# DEX conserves, tous en forme EXACTE tiree de la liste reelle renvoyee par
# /networks/solana/dexes. Ils sont malgre tout re-resolus a chaque run, et un
# id absent de la reponse est ignore avec un warning plutot que devine.
# Ages medians : meteora 41,7 j, raydium-clmm 15,9 j, raydium 5,3 j.
PREFERRED_DEXES = (
    "meteora",
    "raydium-clmm",
    "raydium",
)

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


def _to_float_or_none(value: Any) -> float | None:
    """Comme _to_float, mais distingue 'absent' de 'zero'.

    Un FDV manquant ecrit comme 0 fausserait la calibration ulterieure de la
    fenetre de mcap : on preserve le NULL.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def _parse_created_at(created_at: str | None) -> datetime | None:
    """'2024-01-15T12:00:00Z' -> datetime aware, ou None si illisible."""
    if not created_at:
        return None
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created


def _pool_age_days(created: datetime, now: datetime) -> float:
    return (now - created).total_seconds() / 86400.0


def _dex_id(pool: dict) -> str | None:
    """Identifiant du DEX ('raydium', 'pumpswap', 'meteora'...)."""
    dex_id = (
        pool.get("relationships", {})
        .get("dex", {})
        .get("data", {})
        .get("id")
    )
    return dex_id if isinstance(dex_id, str) and dex_id else None


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


SourceSpec = tuple[str, Callable[[int], "gt.PoolPage | None"], int]


def resolve_dexes() -> list[str]:
    """Identifiants de DEX reellement exposes par l'API, parmi les souhaites.

    Un seul appel. Chaque valeur retournee vient de /networks/solana/dexes :
    un DEX souhaite mais absent de la reponse est ignore, jamais devine ni
    remplace par un id approchant.
    """
    data = gt.dexes()
    if data is None:
        log.warning(
            "PERTE : /networks/%s/dexes indisponible, collecte par DEX "
            "desactivee pour ce run", "solana",
        )
        return []

    available = [d.get("id") for d in data if isinstance(d.get("id"), str)]
    log.info("DEX disponibles (%d) : %s", len(available), ", ".join(available))

    # Correspondance exacte, sans repli sur une variante : PREFERRED_DEXES ne
    # contient plus que des ids observes dans une reponse reelle. Un repli
    # substituerait silencieusement un autre DEX a celui qu'on veut mesurer.
    resolved: list[str] = []
    for wanted in PREFERRED_DEXES:
        if wanted in available:
            resolved.append(wanted)
        else:
            log.warning("DEX '%s' absent de la reponse, ignore", wanted)

    log.info("DEX retenus (%d) : %s", len(resolved), ", ".join(resolved) or "aucun")
    return resolved


def build_source_specs(dex_ids: list[str]) -> list[SourceSpec]:
    """(libelle, fonction de page, nombre de pages) par source de collecte."""
    specs: list[SourceSpec] = [
        (f"trending_{duration}",
         partial(gt.trending_pools, duration=duration),
         TRENDING_POOLS_PAGES)
        for duration in TRENDING_DURATIONS
    ]
    specs.extend(
        (f"dex_{dex}", partial(gt.dex_pools, dex, sort=VOLUME_SORT), DEX_POOLS_PAGES)
        for dex in dex_ids
    )
    return specs


def collect_pools() -> tuple[list[dict], dict[str, dict], int]:
    """Agrege toutes les sources de collecte.

    Retourne (pools, index mint -> attributs token, nombre de pages perdues).
    Une source qui echoue est loguee et n'interrompt pas les autres.
    """
    specs = build_source_specs(resolve_dexes())
    planned = sum(min(pages, gt.MAX_PAGE) for _, _, pages in specs)
    log.info(
        "Collecte : %d sources, %d appels au plus (~%.0f s de throttle)",
        len(specs), planned, planned * MIN_REQUEST_INTERVAL_S,
    )

    pools: list[dict] = []
    token_index: dict[str, dict] = {}
    losses = 0
    now = datetime.now(timezone.utc)

    for label, fetch, pages in specs:
        collected = 0
        source_losses = 0
        ages: list[float] = []
        for page in range(1, min(pages, gt.MAX_PAGE) + 1):
            result = fetch(page)
            if result is None:  # perte deja loguee cote client HTTP
                source_losses += 1
                continue
            batch, included = result
            if not batch:  # vraie fin de pagination
                break
            pools.extend(batch)
            _index_tokens(included, token_index)
            collected += len(batch)
            for pool in batch:
                created = _parse_created_at(
                    pool.get("attributes", {}).get("pool_created_at")
                )
                if created is not None:
                    ages.append(_pool_age_days(created, now))
        losses += source_losses
        # L'age median dit si la source vise la meme fenetre que nos seuils.
        median = f"{statistics.median(ages):.1f}".replace(".", ",") if ages else "n/a"
        log.info("%-16s : %d pools, age median %s j", label, collected, median)
        if source_losses and not collected:
            log.warning("PERTE : source %s sans aucun resultat exploitable", label)

    if losses:
        log.warning("Collecte : %d page(s) perdue(s), couverture incomplete", losses)
    return pools, token_index, losses


# ---------------------------------------------------------------------------
# Etapes b, c, d : deduplication, bruit, pre-filtres
# ---------------------------------------------------------------------------


# Motifs de rejet du pre-filtre, dans l'ordre d'affichage. Ils sont mutuellement
# exclusifs : collectes = somme(motifs hors deja_analyse) + dedupliques.
FUNNEL_REASONS = (
    "doublon_pool",
    "bruit",
    "age_trop_jeune",
    "age_trop_vieux",
    "liquidite_insuffisante",
    "volume_insuffisant",
    "payload_incomplet",
    "deja_analyse",
)


def new_funnel() -> dict[str, int]:
    return {reason: 0 for reason in FUNNEL_REASONS}


def format_funnel(funnel: dict[str, int]) -> str:
    return " | ".join(f"{reason} {funnel[reason]}" for reason in FUNNEL_REASONS)


def build_candidates(
    pools: list[dict], token_index: dict[str, dict]
) -> tuple[list[dict], dict[str, int]]:
    """Un candidat par token (le pool le plus liquide), pre-filtres appliques.

    Retourne (candidats, compteurs de rejet par motif). Les compteurs sont la
    seule facon de savoir quel filtre elimine quoi : sans eux, tout reglage de
    seuil serait de l'intuition.
    """
    now = datetime.now(timezone.utc)
    best: dict[str, dict] = {}
    funnel = new_funnel()

    for pool in pools:
        mint = _base_mint(pool)
        if not mint:
            funnel["payload_incomplet"] += 1
            continue
        if mint in NOISE_MINTS:
            funnel["bruit"] += 1
            continue

        attrs = pool.get("attributes", {})
        token_attrs = token_index.get(mint, {})
        symbol = (token_attrs.get("symbol") or _symbol_from_pool_name(pool)).strip()
        if symbol.upper() in NOISE_SYMBOLS:
            funnel["bruit"] += 1
            continue

        created = _parse_created_at(attrs.get("pool_created_at"))
        if created is None:
            funnel["payload_incomplet"] += 1
            continue
        age = _pool_age_days(created, now)
        # Ventiler jeune/vieux separement : c'est ce qui dira si la fenetre
        # d'age est mal placee ou si les endpoints collectent a cote.
        if age < MIN_POOL_AGE_DAYS:
            funnel["age_trop_jeune"] += 1
            continue
        if age > MAX_POOL_AGE_DAYS:
            funnel["age_trop_vieux"] += 1
            continue

        liquidity = _to_float(attrs.get("reserve_in_usd"))
        if liquidity < MIN_LIQUIDITY_USD:
            funnel["liquidite_insuffisante"] += 1
            continue

        volume_24h = _to_float((attrs.get("volume_usd") or {}).get("h24"))
        if volume_24h < MIN_VOLUME_24H_USD:
            funnel["volume_insuffisant"] += 1
            continue

        pool_address = attrs.get("address") or (pool.get("id", "").split("_", 1)[-1])
        candidate = {
            "mint": mint,
            "symbol": symbol,
            "name": token_attrs.get("name") or attrs.get("name") or symbol,
            "pool_address": pool_address,
            "dex": _dex_id(pool),
            "pool_created_at": created.isoformat(),
            "fdv_usd": _to_float_or_none(attrs.get("fdv_usd")),
            "liquidity_usd": round(liquidity, 2),
            "volume_24h_usd": round(volume_24h, 2),
        }
        # Un token a souvent plusieurs pools : on garde le plus liquide, qui
        # porte l'historique de prix le plus representatif.
        previous = best.get(mint)
        if previous is not None:
            funnel["doublon_pool"] += 1  # un pool du mint est ecarte, pas le token
        if previous is None or liquidity > previous["liquidity_usd"]:
            best[mint] = candidate

    return list(best.values()), funnel


# ---------------------------------------------------------------------------
# Etapes g, h : performance
# ---------------------------------------------------------------------------


def analyze_performance(candidate: dict) -> dict | None:
    """Enrichit le candidat avec perf_x, perf_x_launch, peak_at, is_winner.

    Deux metriques, ecrites cote a cote pour pouvoir comparer leur
    distribution sur donnees reelles :

      perf_x_launch = max(high) / premier open non nul
          Mesure historique. Pour un token lance sur bonding curve, le premier
          open est le prix de depart de la courbe, proche de zero : la valeur
          est mecaniquement enorme et ne discrimine rien.

      perf_x = max(high des bougies d'index >= 1) / close de la bougie d'index 0
          Ce qu'un acheteur entre a la fin du premier jour aurait pu faire.
          C'est elle qui determine is_winner.

    Retourne None si l'OHLCV a ete PERDU (reseau) : dans ce cas on n'ecrit
    rien, pour que le token soit retente au prochain run au lieu d'etre
    enterre dans la memoire.
    """
    candles = gt.ohlcv_day(candidate["pool_address"], limit=60)
    if candles is None:
        return None

    row = dict(candidate)
    row.update(perf_x=None, perf_x_launch=None, peak_at=None, is_winner=False)

    # [timestamp, open, high, low, close, volume], ordre decroissant cote API.
    ordered = sorted(
        (c for c in candles if isinstance(c, (list, tuple)) and len(c) >= 5),
        key=lambda c: _to_float(c[0]),
    )
    if len(ordered) < 3:
        row["rejected_reason"] = "ohlcv_insuffisant"
        return row

    # Metrique historique, calculee des qu'elle est calculable : elle sert de
    # point de comparaison meme quand perf_x ne l'est pas.
    first_open = next((_to_float(c[1]) for c in ordered if _to_float(c[1]) > 0), 0.0)
    peak_all = max(_to_float(c[2]) for c in ordered)
    if first_open > 0 and peak_all > 0:
        row["perf_x_launch"] = round(peak_all / first_open, 4)

    entry = _to_float(ordered[0][4])  # close du premier jour
    highs = [(_to_float(c[2]), _to_float(c[0])) for c in ordered[1:]]
    peak_price, peak_ts = max(highs, key=lambda item: item[0])
    if entry <= 0 or peak_price <= 0:
        row["rejected_reason"] = "ohlcv_invalide"
        return row

    perf_x = peak_price / entry
    is_winner = perf_x >= WINNER_MULTIPLE
    row.update(
        perf_x=round(perf_x, 4),
        # peak_at suit la metrique qui decide is_winner.
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
    candidates, funnel = build_candidates(pools, token_index)

    known = db.fetch_recent_mints(ANALYZED_TTL_DAYS)
    fresh = [c for c in candidates if c["mint"] not in known]
    funnel["deja_analyse"] = len(candidates) - len(fresh)

    log.info(
        "collectes %d | dedupliques %d | deja analyses %d | a analyser %d",
        len(pools), len(candidates), funnel["deja_analyse"], len(fresh),
    )
    log.info("filtrage : %s", format_funnel(funnel))
    # Les tokens rejetes en pre-filtre ne sont PAS ecrits en base : un token
    # ecarte aujourd'hui pour age < MIN_POOL_AGE_DAYS sera eligible dans
    # quelques jours, l'ecrire avec un TTL de 60 j l'enterrerait.
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

    # Ces colonnes sont nullables : un payload incomplet passerait l'upsert
    # sans erreur et la perte serait silencieuse. On l'annonce.
    missing_dex = sum(1 for r in analyzed if not r.get("dex"))
    missing_fdv = sum(1 for r in analyzed if r.get("fdv_usd") is None)
    if missing_dex or missing_fdv:
        log.warning(
            "Payload incomplet sur %d lignes : dex absent %d | fdv_usd absent %d",
            len(analyzed), missing_dex, missing_fdv,
        )

    # Winners ET non-winners sont ecrits : c'est ce qui alimente la memoire
    # et evite de re-analyser les memes tokens a chaque run.
    db.upsert_analyzed_tokens(analyzed)

    winners = sorted(
        (r for r in analyzed if r["is_winner"]),
        key=lambda r: r["perf_x"],
        reverse=True,
    )
    invalid = sum(
        1 for r in analyzed
        if r["rejected_reason"] in ("ohlcv_invalide", "ohlcv_insuffisant")
    )
    calls, losses = gt.request_stats()

    log.info("=== Resume ===")
    log.info(
        "analyses %d | winners %d | ohlcv invalides %d | appels API %d "
        "| pertes %d",
        len(analyzed), len(winners), invalid, calls, losses,
    )
    for winner in winners:
        launch = winner["perf_x_launch"]
        log.info(
            "  WINNER %-12s x%-8s (launch %s)  %s  liq %s$",
            winner["symbol"][:12],
            round(winner["perf_x"], 1),
            f"x{launch:.1f}" if launch is not None else "n/a",
            winner["mint"],
            f"{winner['liquidity_usd']:,.0f}",
        )

    # La base s'alimente par accumulation hebdomadaire via le TTL : le
    # compte d'un run isole n'est pas un objectif a atteindre.
    log.info("winners ce run : %d", len(winners))
    return len(winners)


if __name__ == "__main__":
    # Point d'entree direct. RUN_MODE est gere par main.py : si quelqu'un le
    # positionne en pensant changer de mode ici, on le dit plutot que de
    # lancer silencieusement le mauvais traitement.
    _mode = os.environ.get("RUN_MODE", "").strip().lower()
    if _mode and _mode != "winners":
        setup_logging()
        log.warning(
            "RUN_MODE=%s ignore : ce script ne lance que le pipeline winners. "
            "Utiliser 'python main.py' comme Start Command.", _mode,
        )
    run()
