"""Client HTTP Helius.

Deux voies, arretees apres la sonde du 18/09 :
  A. getTransactionsForAddress (JSON-RPC) pour les SIGNATURES, en ordre
     ascendant. C'est la seule voie qui remonte les transactions les plus
     anciennes d'une adresse.
  C. POST /v0/transactions (Enhanced Transactions) pour l'ENRICHISSEMENT
     d'une liste de signatures.

Pas de parsing de meta/transaction brut : transactionDetails="full" les
expose mais la voie C donne deja tokenTransfers et nativeTransfers.

Regles identiques au client CoinGecko :
  - throttle DEDIE, independant de celui de CoinGecko ;
  - retry a backoff exponentiel sur 429 et 5xx ;
  - en cas d'echec definitif : log "PERTE : ..." et None retourne. Jamais
    de liste vide silencieuse, qui ferait passer une perte pour un token
    sans acheteurs.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

log = logging.getLogger("solana-agent")

RPC_URL = "https://mainnet.helius-rpc.com/"
ENHANCED_TX_URL = "https://api.helius.xyz/v0/transactions"

# Free tier : 10 req/s. On vise 3 req/s.
MIN_REQUEST_INTERVAL_S = 0.34
MAX_RETRIES = 3

# Taille maximale d'un lot de signatures envoye a la voie C.
ENRICH_BATCH_SIZE = 100

_last_call_at: float = 0.0
_request_count: int = 0
_loss_count: int = 0


def _throttle() -> None:
    global _last_call_at
    elapsed = time.monotonic() - _last_call_at
    if elapsed < MIN_REQUEST_INTERVAL_S:
        time.sleep(MIN_REQUEST_INTERVAL_S - elapsed)
    _last_call_at = time.monotonic()


def api_key() -> str:
    """Cle Helius. Absente : on leve, on ne continue pas en silence."""
    key = os.environ.get("HELIUS_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "HELIUS_API_KEY absente : impossible d'interroger Helius, arret."
        )
    return key


def request_stats() -> tuple[int, int]:
    """(appels effectues, pertes definitives) depuis le demarrage."""
    return _request_count, _loss_count


def _post(url: str, body: dict, label: str) -> Any | None:
    """POST avec retry. None = PERTE definitive, deja loguee."""
    global _request_count, _loss_count
    backoff = 1.0

    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
        _request_count += 1
        try:
            response = requests.post(url, json=body, timeout=60)
        except requests.RequestException as exc:
            log.warning(
                "%s : erreur reseau (%s), tentative %d/%d",
                label, type(exc).__name__, attempt, MAX_RETRIES,
            )
        else:
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError:
                    log.warning(
                        "%s : JSON illisible, tentative %d/%d",
                        label, attempt, MAX_RETRIES,
                    )
            elif response.status_code == 429 or response.status_code >= 500:
                log.warning(
                    "%s : HTTP %d, tentative %d/%d",
                    label, response.status_code, attempt, MAX_RETRIES,
                )
            else:
                # 4xx autre que 429 : inutile de reessayer.
                log.error(
                    "PERTE : %s abandonne (HTTP %d, non recuperable) - %s",
                    label, response.status_code, response.text[:300],
                )
                _loss_count += 1
                return None

        if attempt < MAX_RETRIES:
            time.sleep(backoff)
            backoff *= 2

    log.error("PERTE : %s abandonne apres %d tentatives", label, MAX_RETRIES)
    _loss_count += 1
    return None


def transactions_for_address(
    address: str, limit: int, sort_order: str = "asc"
) -> list[dict] | None:
    """Voie A. Transactions d'une adresse. None = PERTE."""
    detailed = transactions_for_address_detailed(address, limit, sort_order)
    return None if detailed is None else detailed[0]


def rpc(method: str, params: Any) -> Any | None:
    """Appel JSON-RPC generique. None = PERTE. Rend le payload tel quel,
    erreur JSON-RPC comprise : c'est ce que les sondes veulent voir."""
    return _post(
        f"{RPC_URL}?api-key={api_key()}",
        {"jsonrpc": "2.0", "id": "sonde", "method": method, "params": params},
        method,
    )


def pagination_token(result: dict) -> str | None:
    """Jeton de page suivante, s'il y en a un."""
    token = result.get("paginationToken")
    return token if isinstance(token, str) and token else None


def transactions_for_address_detailed(
    address: str,
    limit: int,
    sort_order: str = "asc",
    page_token: str | None = None,
) -> tuple[list[dict], dict] | None:
    """(transactions, objet result brut). None = PERTE.

    Helius renvoie result = {"data": [...], "paginationToken": ...}, pas une
    liste : c'est la forme confirmee par la sonde du 18/09. Le result brut
    est rendu tel quel pour que l'appelant y lise le jeton de pagination.
    """
    global _loss_count
    label = f"getTransactionsForAddress({address[:8]}...)"
    config: dict[str, Any] = {"limit": limit, "sortOrder": sort_order}
    if page_token:
        config["paginationToken"] = page_token
    payload = _post(
        f"{RPC_URL}?api-key={api_key()}",
        {
            "jsonrpc": "2.0",
            "id": "discovery",
            "method": "getTransactionsForAddress",
            "params": [address, config],
        },
        label,
    )
    if payload is None:
        return None

    if not isinstance(payload, dict) or "error" in payload:
        log.error(
            "PERTE : %s - erreur JSON-RPC : %s",
            label, str(payload)[:300] if payload else payload,
        )
        _loss_count += 1
        return None

    result = payload.get("result")
    items = result if isinstance(result, list) else None
    if items is None and isinstance(result, dict):
        data = result.get("data")
        items = data if isinstance(data, list) else None
    if items is None:
        log.error("PERTE : %s - 'result' sans liste exploitable", label)
        _loss_count += 1
        return None
    return items, result if isinstance(result, dict) else {}


def transfers_by_address(
    address: str,
    limit: int,
    sort_order: str = "asc",
    page_token: str | None = None,
) -> tuple[list[dict], dict] | None:
    """(transferts, result brut). None = PERTE.

    Sonde du 19/09 : meme forme que getTransactionsForAddress
    (result = {data, paginationToken}), sortOrder asc et desc acceptes,
    limit plafonne a 100. Une ligne est une JAMBE de transfert d'un mint,
    pas une transaction : plusieurs lignes partagent une signature.

    Toute cle inconnue fait rejeter l'objet de config entier : on n'envoie
    que limit, sortOrder et paginationToken.
    """
    global _loss_count
    label = f"getTransfersByAddress({address[:8]}...)"
    config: dict[str, Any] = {"limit": limit, "sortOrder": sort_order}
    if page_token:
        config["paginationToken"] = page_token
    payload = _post(
        f"{RPC_URL}?api-key={api_key()}",
        {
            "jsonrpc": "2.0",
            "id": "v2",
            "method": "getTransfersByAddress",
            "params": [address, config],
        },
        label,
    )
    if payload is None:
        return None
    if not isinstance(payload, dict) or "error" in payload:
        log.error("PERTE : %s - erreur JSON-RPC : %s", label, str(payload)[:300])
        _loss_count += 1
        return None

    result = payload.get("result")
    items = result if isinstance(result, list) else None
    if items is None and isinstance(result, dict):
        data = result.get("data")
        items = data if isinstance(data, list) else None
    if items is None:
        log.error("PERTE : %s - 'result' sans liste exploitable", label)
        _loss_count += 1
        return None
    return items, result if isinstance(result, dict) else {}


def enrich_signatures(signatures: list[str]) -> list[dict] | None:
    """Voie C. Transactions enrichies. None = PERTE sur au moins un lot.

    Les signatures sont envoyees par lots de ENRICH_BATCH_SIZE au maximum.
    """
    global _loss_count
    enriched: list[dict] = []

    for start in range(0, len(signatures), ENRICH_BATCH_SIZE):
        batch = signatures[start:start + ENRICH_BATCH_SIZE]
        label = f"enrich[{start}:{start + len(batch)}]"
        payload = _post(
            f"{ENHANCED_TX_URL}?api-key={api_key()}",
            {"transactions": batch},
            label,
        )
        if payload is None:
            return None
        if not isinstance(payload, list):
            log.error(
                "PERTE : %s - reponse inattendue : %s", label, str(payload)[:300]
            )
            _loss_count += 1
            return None
        enriched.extend(item for item in payload if isinstance(item, dict))

    return enriched
