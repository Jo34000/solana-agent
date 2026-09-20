"""Client HTTP pour l'API onchain de CoinGecko (ex-GeckoTerminal).

Regles non negociables :
  - un intervalle minimum entre deux appels (cle Demo partagee, 30 req/min) ;
  - retry a backoff exponentiel sur 429 et 5xx ;
  - en cas d'echec definitif : log "PERTE : ..." et None retourne. Jamais de
    liste vide silencieuse, qui ferait passer une perte de donnees pour un
    resultat legitime.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from config import (
    GECKOTERMINAL_BASE_URL,
    MAX_RETRIES,
    MIN_REQUEST_INTERVAL_S,
    NETWORK,
    coingecko_api_key,
)

log = logging.getLogger("solana-agent")

_last_call_at: float = 0.0
_request_count: int = 0
_loss_count: int = 0


def _throttle() -> None:
    """Bloque jusqu'a ce que MIN_REQUEST_INTERVAL_S soit ecoule."""
    global _last_call_at
    elapsed = time.monotonic() - _last_call_at
    if elapsed < MIN_REQUEST_INTERVAL_S:
        time.sleep(MIN_REQUEST_INTERVAL_S - elapsed)
    _last_call_at = time.monotonic()


def _headers() -> dict[str, str]:
    headers = {"accept": "application/json"}
    key = coingecko_api_key()
    if key:
        headers["x-cg-demo-api-key"] = key
    return headers


def request_stats() -> tuple[int, int]:
    """(appels effectues, pertes definitives) depuis le demarrage."""
    return _request_count, _loss_count


def get(endpoint: str, params: dict[str, Any] | None = None) -> dict | None:
    """Appelle un endpoint onchain et retourne le JSON, ou None si perdu."""
    global _request_count, _loss_count

    url = f"{GECKOTERMINAL_BASE_URL}{endpoint}"
    backoff = 2.0

    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
        _request_count += 1
        try:
            response = requests.get(
                url, params=params, headers=_headers(), timeout=30
            )
        except requests.RequestException as exc:
            log.warning(
                "%s : erreur reseau (%s), tentative %d/%d",
                endpoint, type(exc).__name__, attempt, MAX_RETRIES,
            )
        else:
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError:
                    log.warning(
                        "%s : JSON illisible, tentative %d/%d",
                        endpoint, attempt, MAX_RETRIES,
                    )
            elif response.status_code == 429 or response.status_code >= 500:
                log.warning(
                    "%s : HTTP %d, tentative %d/%d",
                    endpoint, response.status_code, attempt, MAX_RETRIES,
                )
            else:
                # 4xx autre que 429 : inutile de reessayer.
                log.error(
                    "PERTE : %s abandonne (HTTP %d, non recuperable)",
                    endpoint, response.status_code,
                )
                _loss_count += 1
                return None

        if attempt < MAX_RETRIES:
            time.sleep(backoff)
            backoff *= 2

    log.error("PERTE : %s abandonne apres %d tentatives", endpoint, MAX_RETRIES)
    _loss_count += 1
    return None


PoolPage = tuple[list[dict], list[dict]]

# La pagination au dela de la page 10 est reservee aux plans payants et
# renvoie une erreur : on refuse l'appel plutot que de le gaspiller.
MAX_PAGE = 10


def _pool_list(
    endpoint: str, page: int, extra: dict[str, Any] | None = None
) -> PoolPage | None:
    """(pools, tokens inclus) pour une page. None = perte, jamais [] muet.

    include=base_token ramene les symboles dans le meme appel : pas de
    requete supplementaire par token.
    """
    if not 1 <= page <= MAX_PAGE:
        raise ValueError(
            f"page={page} hors bornes : l'API n'accepte que 1..{MAX_PAGE} "
            "sur le plan Demo."
        )
    params: dict[str, Any] = {"page": page, "include": "base_token"}
    params.update(extra or {})
    payload = get(endpoint, params)
    if payload is None:
        return None
    data = payload.get("data")
    if not isinstance(data, list):
        log.error("PERTE : %s page %d - payload inattendu", endpoint, page)
        return None
    included = payload.get("included")
    return data, included if isinstance(included, list) else []


def new_pools(page: int = 1) -> PoolPage | None:
    """Pools les plus recemment crees. None = perte, [] = vraie page vide.

    Hors collecte depuis le run du 17/09 : age median 0,0 j, aucun pool ne
    peut atteindre la fenetre MIN_POOL_AGE_DAYS. Conserve pour une future
    logique d'accumulation.
    """
    return _pool_list(f"/networks/{NETWORK}/new_pools", page)


def trending_pools(page: int = 1, duration: str | None = None) -> PoolPage | None:
    """Pools en tendance. duration : 5m, 1h, 6h ou 24h (defaut API : 24h)."""
    extra = {"duration": duration} if duration else None
    return _pool_list(f"/networks/{NETWORK}/trending_pools", page, extra)


def top_pools(page: int = 1, sort: str | None = None) -> PoolPage | None:
    """Top pools du reseau. sort par defaut cote API : h24_tx_count_desc."""
    extra = {"sort": sort} if sort else None
    return _pool_list(f"/networks/{NETWORK}/pools", page, extra)


def token_pools(mint: str, page: int = 1) -> PoolPage | None:
    """Pools d'un token donne, pour retrouver le plus liquide."""
    return _pool_list(f"/networks/{NETWORK}/tokens/{mint}/pools", page)


def dex_pools(dex: str, page: int = 1, sort: str | None = None) -> PoolPage | None:
    """Top pools d'un DEX donne."""
    extra = {"sort": sort} if sort else None
    return _pool_list(f"/networks/{NETWORK}/dexes/{dex}/pools", page, extra)


def dexes(page: int = 1) -> list[dict] | None:
    """DEX disponibles sur le reseau. None = perte.

    Sert a resoudre les identifiants reellement exposes par l'API plutot
    que d'en coder en dur.
    """
    payload = get(f"/networks/{NETWORK}/dexes", {"page": page})
    if payload is None:
        return None
    data = payload.get("data")
    if not isinstance(data, list):
        log.error("PERTE : /networks/%s/dexes - payload inattendu", NETWORK)
        return None
    return data


def ohlcv(
    pool_address: str,
    timeframe: str = "day",
    limit: int = 60,
    before: int | None = None,
) -> list[list] | None:
    """Bougies d'un pool. timeframe : day, hour ou minute.

    before (timestamp epoch) remonte a une periode passee precise.
    """
    params: dict[str, Any] = {"limit": limit, "currency": "usd"}
    if before:
        params["before_timestamp"] = int(before)
    payload = get(
        f"/networks/{NETWORK}/pools/{pool_address}/ohlcv/{timeframe}", params
    )
    if payload is None:
        return None
    candles = (
        payload.get("data", {}).get("attributes", {}).get("ohlcv_list")
    )
    if not isinstance(candles, list):
        log.error("PERTE : ohlcv/%s %s - payload inattendu",
                  timeframe, pool_address)
        return None
    return candles


def ohlcv_day(pool_address: str, limit: int = 60) -> list[list] | None:
    """Bougies journalieres d'un pool. None = perte ou payload inattendu."""
    payload = get(
        f"/networks/{NETWORK}/pools/{pool_address}/ohlcv/day",
        {"limit": limit, "currency": "usd"},
    )
    if payload is None:
        return None
    candles = (
        payload.get("data", {})
        .get("attributes", {})
        .get("ohlcv_list")
    )
    if not isinstance(candles, list):
        log.error("PERTE : ohlcv/day %s - payload inattendu", pool_address)
        return None
    return candles
