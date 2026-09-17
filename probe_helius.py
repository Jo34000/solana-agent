"""Sonde jetable : quelle forme ont les reponses Helius ?

Script d'observation, jamais appele par le pipeline. Lance via
RUN_MODE=probe_helius (voir main.py) ou directement.

Phase 2 : retrouver les premiers acheteurs des winners de la phase 1. Avant
d'ecrire le moindre parsing, on regarde ce que l'API renvoie reellement.

Deux voies d'acces sont testees sur deux mints winners reels, un de chaque
famille :
  A. getTransactionsForAddress sur l'endpoint JSON-RPC
  B. l'API Enhanced Transactions REST, en repli

La sonde DECRIT, elle n'interprete pas : aucun parsing, aucun filtre, aucune
notion d'acheteur ni de rang. Les messages d'erreur bruts de Helius sont
affiches tels quels — ce sont eux qui donneront le bon nom de methode ou de
parametre.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import requests

from config import diagnose_environment, setup_logging

log = logging.getLogger("solana-agent")

HELIUS_RPC_URL = "https://mainnet.helius-rpc.com/"
HELIUS_REST_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"

# Free tier : 10 requetes/seconde. On reste tres en dessous — la sonde ne
# fait que 4 appels. Throttle DEDIE, independant de celui de CoinGecko.
MIN_REQUEST_INTERVAL_S = 0.5

TX_LIMIT = 20
JSON_TRUNCATE = 4000

# (famille, symbole, mint) — winners reels de la phase 1.
WINNER_MINTS: tuple[tuple[str, str, str], ...] = (
    ("pump.fun", "CATE", "Ai66LHZG9MCzg1WKdawwqduVAXpNDUuV8M3uyq5ppump"),
    ("hors pump", "STONK", "6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx"),
)

# Noms de champs candidats, cherches n'importe ou dans la reponse. On ne
# suppose pas lequel existe : on affiche ceux qu'on trouve.
# 'source' n'est pas liste ici : chez Helius il porte la plateforme, pas le
# signataire. Il reste reporte sous PROGRAM_KEYS.
SIGNER_KEYS = ("feePayer", "fee_payer", "signer", "signers", "payer")
TRANSFER_KEYS = ("tokenTransfers", "token_transfers", "tokenBalanceChanges",
                 "nativeTransfers")
PROGRAM_KEYS = ("programId", "program_id", "program", "platform", "source")
TYPE_KEYS = ("type", "transactionType", "transaction_type", "description")
TIME_KEYS = ("timestamp", "blockTime", "block_time", "slot")

_last_call_at: float = 0.0


def _throttle() -> None:
    global _last_call_at
    elapsed = time.monotonic() - _last_call_at
    if elapsed < MIN_REQUEST_INTERVAL_S:
        time.sleep(MIN_REQUEST_INTERVAL_S - elapsed)
    _last_call_at = time.monotonic()


def _api_key() -> str:
    """Cle Helius. Absente en mode sonde : on leve, on ne continue pas."""
    key = os.environ.get("HELIUS_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "HELIUS_API_KEY absente : la sonde Helius ne peut rien faire. "
            "La renseigner cote Railway avant de relancer."
        )
    return key


def _masked(url: str) -> str:
    """URL sans la cle : elle ne doit jamais apparaitre dans les logs."""
    key = os.environ.get("HELIUS_API_KEY", "").strip()
    return url.replace(key, "***") if key else url


def _truncate(text: str, limit: int = JSON_TRUNCATE) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [tronque a {limit} caracteres]"


def _as_json(value: Any) -> str:
    try:
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(value)


def _readable_time(value: Any) -> str:
    """Epoch secondes -> ISO. Toute autre valeur est rendue telle quelle."""
    if isinstance(value, (int, float)) and 1_000_000_000 < value < 20_000_000_000:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    return repr(value)


def _walk(value: Any, names: tuple[str, ...], path: str = "",
          found: list[tuple[str, Any]] | None = None,
          depth: int = 0) -> list[tuple[str, Any]]:
    """Chemins vers toutes les cles dont le nom figure dans `names`."""
    if found is None:
        found = []
    if depth > 6 or len(found) > 40:
        return found
    if isinstance(value, dict):
        for key, sub in value.items():
            here = f"{path}.{key}" if path else key
            if key in names:
                found.append((here, sub))
            _walk(sub, names, here, found, depth + 1)
    elif isinstance(value, list):
        for index, sub in enumerate(value[:3]):
            _walk(sub, names, f"{path}[{index}]", found, depth + 1)
    return found


def _short(value: Any, limit: int = 200) -> str:
    text = value if isinstance(value, str) else _as_json(value)
    return text if len(text) <= limit else text[:limit] + "..."


# ---------------------------------------------------------------------------
# Appels
# ---------------------------------------------------------------------------


def _post_rpc(method: str, params: Any) -> tuple[int | None, Any, str]:
    """(code HTTP, payload ou texte brut, corps de requete envoye)."""
    _throttle()
    url = f"{HELIUS_RPC_URL}?api-key={_api_key()}"
    body = {"jsonrpc": "2.0", "id": "sonde", "method": method, "params": params}
    try:
        response = requests.post(url, json=body, timeout=30)
    except requests.RequestException as exc:
        return None, f"erreur reseau : {type(exc).__name__} {exc}", _as_json(body)
    try:
        return response.status_code, response.json(), _as_json(body)
    except ValueError:
        return response.status_code, response.text, _as_json(body)


def _get_rest(address: str, params: dict[str, Any]) -> tuple[int | None, Any, str]:
    """(code HTTP, payload ou texte brut, URL masquee)."""
    _throttle()
    url = HELIUS_REST_URL.format(address=address)
    query = {"api-key": _api_key(), **params}
    try:
        response = requests.get(url, params=query, timeout=30)
    except requests.RequestException as exc:
        return None, f"erreur reseau : {type(exc).__name__} {exc}", _masked(url)
    try:
        return response.status_code, response.json(), _masked(response.url)
    except ValueError:
        return response.status_code, response.text, _masked(response.url)


# ---------------------------------------------------------------------------
# Description (sans interpretation)
# ---------------------------------------------------------------------------


def _describe_batch(transactions: list) -> None:
    print(f"  transactions retournees : {len(transactions)}")
    if not transactions:
        return

    # Horodatages du premier et du dernier element du lot, pour verifier
    # l'ordre chronologique. On ne corrige rien, on constate.
    for position, index in (("PREMIERE", 0), ("DERNIERE", len(transactions) - 1)):
        stamps = _walk(transactions[index], TIME_KEYS)
        rendered = ", ".join(
            f"{path}={_readable_time(value)}" for path, value in stamps[:4]
        ) or "aucun champ temporel reconnu"
        print(f"  {position:9} tx : {rendered}")


def _describe_first_transaction(transaction: Any) -> None:
    print("\n  --- structure COMPLETE de la premiere transaction ---")
    print(_truncate(_as_json(transaction)))

    print("\n  --- champs reperes dans cette transaction ---")
    for label, names in (
        ("signataire / payeur de frais", SIGNER_KEYS),
        ("transferts", TRANSFER_KEYS),
        ("programme / plateforme", PROGRAM_KEYS),
        ("type de transaction", TYPE_KEYS),
    ):
        matches = _walk(transaction, names)
        if not matches:
            print(f"  {label:30} : aucun champ de nom {list(names)}")
            continue
        print(f"  {label:30} :")
        for path, value in matches[:6]:
            print(f"      {path} = {_short(value)}")

    # Pour les transferts, les noms de champs du premier element disent
    # eux-memes lesquels portent source, destination et montant.
    for path, value in _walk(transaction, TRANSFER_KEYS):
        if isinstance(value, list) and value and isinstance(value[0], dict):
            print(f"\n  --- champs d'un element de {path} ({len(value)} elements) ---")
            for key, sub in value[0].items():
                print(f"      {key} = {_short(sub)}")
            break


def _describe_error(status: int | None, payload: Any, sent: str) -> None:
    print(f"  code HTTP : {status if status is not None else 'pas de reponse'}")
    print("  message brut renvoye par Helius :")
    print(_truncate(payload if isinstance(payload, str) else _as_json(payload), 1500))
    print(f"  requete envoyee : {_truncate(sent, 600)}")


# ---------------------------------------------------------------------------
# Les deux voies
# ---------------------------------------------------------------------------


def probe_rpc(symbol: str, mint: str) -> bool:
    """Voie A : getTransactionsForAddress sur l'endpoint JSON-RPC."""
    print(f"\n[A] {symbol} — getTransactionsForAddress (JSON-RPC)")
    status, payload, sent = _post_rpc(
        "getTransactionsForAddress",
        [mint, {"limit": TX_LIMIT, "sortOrder": "asc"}],
    )

    if status != 200 or not isinstance(payload, dict) or "error" in payload:
        _describe_error(status, payload, sent)
        return False

    print(f"  code HTTP : {status}")
    result = payload.get("result")
    transactions = result if isinstance(result, list) else None
    if transactions is None:
        print("  'result' n'est pas une liste, payload brut :")
        print(_truncate(_as_json(payload)))
        return False

    _describe_batch(transactions)
    if transactions:
        _describe_first_transaction(transactions[0])
    return True


def probe_rest(symbol: str, mint: str) -> bool:
    """Voie B : API Enhanced Transactions REST."""
    print(f"\n[B] {symbol} — Enhanced Transactions (REST)")
    status, payload, url = _get_rest(mint, {"limit": TX_LIMIT})

    if status != 200 or not isinstance(payload, list):
        _describe_error(status, payload, url)
        return False

    print(f"  code HTTP : {status}")
    print(f"  URL : {url}")
    _describe_batch(payload)
    if payload:
        _describe_first_transaction(payload[0])
    return True


def main() -> None:
    setup_logging()
    diagnose_environment()

    # HELIUS_API_KEY n'est pas dans le diagnostic de config.py (hors perimetre
    # de ce ticket) : on la logue ici, au meme format.
    present = bool(os.environ.get("HELIUS_API_KEY", "").strip())
    log.info("  %-18s : %s", "HELIUS_API_KEY", "presente" if present else "ABSENTE")
    _api_key()  # leve si absente

    print(f"\nSonde Helius : {len(WINNER_MINTS)} mints x 2 voies d'acces")
    print("Aucune conclusion tiree ici : la sonde decrit ce qu'elle recoit.\n")

    outcomes: list[tuple[str, str, bool]] = []
    for family, symbol, mint in WINNER_MINTS:
        print("=" * 72)
        print(f"{symbol} ({family})")
        print(f"mint : {mint}")
        print("=" * 72)
        outcomes.append((symbol, "A rpc", probe_rpc(symbol, mint)))
        outcomes.append((symbol, "B rest", probe_rest(symbol, mint)))

    print("\n" + "=" * 72)
    print("Recapitulatif")
    for symbol, route, ok in outcomes:
        print(f"  {symbol:8} {route:8} : {'reponse exploitable' if ok else 'echec'}")

    if not any(ok for _, _, ok in outcomes):
        print(
            "\nLes deux voies ont echoue sur les deux mints. Les messages "
            "bruts ci-dessus indiquent le nom de methode ou de parametre "
            "attendu par Helius."
        )


if __name__ == "__main__":
    main()
