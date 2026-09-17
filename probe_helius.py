"""Sonde jetable : quelle forme ont les reponses Helius ?

Script d'observation, jamais appele par le pipeline. Lance via
RUN_MODE=probe_helius (voir main.py) ou directement.

Phase 2 : retrouver les premiers acheteurs des winners de la phase 1. Avant
d'ecrire le moindre parsing, on regarde ce que l'API renvoie reellement.

Acquis du run du 17/09 20:22 :
  - getTransactionsForAddress repond en HTTP 200 et renvoie
    result = {"data": [...]}, pas une liste. Le recapitulatif precedent
    annoncait un echec sur des donnees presentes : c'etait un bug de la
    sonde, corrige ici.
  - l'ordre ascendant est confirme (blockTime, slot et transactionIndex
    croissants), ce que cette sonde re-verifie et logue explicitement.
  - les objets renvoyes ne portent que signature, slot, transactionIndex,
    err, memo, blockTime, confirmationStatus : pas de tokenTransfers. Une
    etape d'enrichissement est donc necessaire, et c'est l'objet des
    voies A-bis et C ci-dessous.
  - la voie REST par adresse (/v0/addresses/{addr}/transactions) renvoyait
    l'ordre DESCENDANT, inutilisable pour des early buyers. Elle n'est plus
    testee.

La sonde DECRIT, elle n'interprete pas : aucun parsing metier, aucun filtre,
aucune notion d'acheteur ni de rang. Les messages d'erreur bruts de Helius
sont affiches tels quels — ce sont eux qui donneront le nom correct d'un
parametre rejete.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import requests

from config import diagnose_environment, setup_logging

log = logging.getLogger("solana-agent")

HELIUS_RPC_URL = "https://mainnet.helius-rpc.com/"
HELIUS_TX_URL = "https://api.helius.xyz/v0/transactions"

# Free tier : 10 requetes/seconde. On reste tres en dessous — la sonde fait
# une dizaine d'appels. Throttle DEDIE, independant de celui de CoinGecko.
MIN_REQUEST_INTERVAL_S = 0.5

TX_LIMIT = 20
ENRICH_SIGNATURES = 5
JSON_TRUNCATE = 4000

RPC_METHOD = "getTransactionsForAddress"

# (famille, symbole, mint) — winners reels de la phase 1.
WINNER_MINTS: tuple[tuple[str, str, str], ...] = (
    ("pump.fun", "CATE", "Ai66LHZG9MCzg1WKdawwqduVAXpNDUuV8M3uyq5ppump"),
    ("hors pump", "STONK", "6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx"),
)

BASE_CONFIG: dict[str, Any] = {"limit": TX_LIMIT, "sortOrder": "asc"}

# Options tentees pour obtenir les transactions COMPLETES plutot que les
# seules metadonnees. Aucun de ces noms n'est certain : un rejet est une
# information, le message brut de Helius donnera le nom correct.
FULL_TX_VARIANTS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("encoding=jsonParsed", {"encoding": "jsonParsed",
                             "maxSupportedTransactionVersion": 0}),
    ("transactionDetails=full", {"transactionDetails": "full",
                                 "maxSupportedTransactionVersion": 0}),
    ("showTransactionDetails=true", {"showTransactionDetails": True}),
)

# Cles candidates, cherchees n'importe ou dans la reponse. On ne suppose pas
# laquelle existe : on affiche celles qu'on trouve.
SIGNER_KEYS = ("feePayer", "fee_payer", "signer", "signers", "payer")
TRANSFER_KEYS = ("tokenTransfers", "token_transfers", "tokenBalanceChanges",
                 "nativeTransfers")
PROGRAM_KEYS = ("programId", "program_id", "program", "platform", "source")
TYPE_KEYS = ("type", "transactionType", "transaction_type", "description")
MINT_KEYS = ("mint", "tokenMint", "token_mint", "mintAddress")
LIST_KEYS = ("data", "items", "transactions", "result", "value")

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


def _masked(text: str) -> str:
    """La cle ne doit jamais apparaitre dans les logs."""
    key = os.environ.get("HELIUS_API_KEY", "").strip()
    return text.replace(key, "***") if key else text


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


def _short(value: Any, limit: int = 200) -> str:
    text = value if isinstance(value, str) else _as_json(value)
    return text if len(text) <= limit else text[:limit] + "..."


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


def _extract_list(result: Any) -> tuple[list | None, str]:
    """La liste de transactions, ou qu'elle soit.

    Helius renvoie result = {"data": [...]}. Tester le type de `result`
    seul faisait conclure a un echec sur des donnees presentes.
    """
    if isinstance(result, list):
        return result, "result"
    if isinstance(result, dict):
        for key in LIST_KEYS:
            value = result.get(key)
            if isinstance(value, list):
                return value, f"result.{key}"
    return None, ""


def _order_verdict(items: list, field: str) -> str:
    values = [
        item.get(field) for item in items
        if isinstance(item, dict) and isinstance(item.get(field), (int, float))
    ]
    if len(values) < 2:
        return f"{field:18} : moins de 2 valeurs, ordre indeterminable"
    ascending = all(a <= b for a, b in zip(values, values[1:]))
    descending = all(a >= b for a, b in zip(values, values[1:]))
    if ascending and not descending:
        verdict = "ASCENDANT"
    elif descending and not ascending:
        verdict = "DESCENDANT"
    elif ascending and descending:
        verdict = "constant"
    else:
        verdict = "NON MONOTONE"
    return f"{field:18} : {verdict} ({values[0]} -> {values[-1]})"


# ---------------------------------------------------------------------------
# Appels
# ---------------------------------------------------------------------------


def _post_rpc(method: str, params: Any) -> tuple[int | None, Any, str]:
    """(code HTTP, payload ou texte brut, corps de requete envoye)."""
    _throttle()
    url = f"{HELIUS_RPC_URL}?api-key={_api_key()}"
    body = {"jsonrpc": "2.0", "id": "sonde", "method": method, "params": params}
    sent = _masked(_as_json(body))
    try:
        response = requests.post(url, json=body, timeout=30)
    except requests.RequestException as exc:
        return None, f"erreur reseau : {type(exc).__name__} {exc}", sent
    try:
        return response.status_code, response.json(), sent
    except ValueError:
        return response.status_code, response.text, sent


def _post_enrich(signatures: list[str]) -> tuple[int | None, Any, str]:
    """API Enhanced Transactions, forme POST acceptant une liste de signatures."""
    _throttle()
    url = f"{HELIUS_TX_URL}?api-key={_api_key()}"
    body = {"transactions": signatures}
    sent = f"POST {_masked(url)}\n{_as_json(body)}"
    try:
        response = requests.post(url, json=body, timeout=30)
    except requests.RequestException as exc:
        return None, f"erreur reseau : {type(exc).__name__} {exc}", sent
    try:
        return response.status_code, response.json(), sent
    except ValueError:
        return response.status_code, response.text, sent


def _describe_error(status: int | None, payload: Any, sent: str) -> None:
    print(f"  code HTTP : {status if status is not None else 'pas de reponse'}")
    print("  message brut renvoye par Helius :")
    print(_truncate(payload if isinstance(payload, str) else _as_json(payload), 1500))
    print(f"  requete envoyee :\n{_truncate(sent, 800)}")


# ---------------------------------------------------------------------------
# Description (sans interpretation)
# ---------------------------------------------------------------------------


def _describe_first_transaction(transaction: Any) -> None:
    print("\n  --- structure COMPLETE de la premiere transaction ---")
    print(_truncate(_as_json(transaction)))

    print("\n  --- champs reperes dans cette transaction ---")
    for label, names in (
        ("signataire / payeur de frais", SIGNER_KEYS),
        ("transferts", TRANSFER_KEYS),
        ("programme / plateforme", PROGRAM_KEYS),
        ("type de transaction", TYPE_KEYS),
        ("mint", MINT_KEYS),
    ):
        matches = _walk(transaction, names)
        if not matches:
            print(f"  {label:30} : aucun champ de nom {list(names)}")
            continue
        print(f"  {label:30} :")
        for path, value in matches[:6]:
            print(f"      {path} = {_short(value)}")

    # Les noms de champs du premier transfert disent eux-memes lesquels
    # portent source, destination, montant et mint.
    for path, value in _walk(transaction, TRANSFER_KEYS):
        if isinstance(value, list) and value and isinstance(value[0], dict):
            print(f"\n  --- champs d'un element de {path} ({len(value)} elements) ---")
            for key, sub in value[0].items():
                print(f"      {key:22} = {_short(sub)}")
            break


# ---------------------------------------------------------------------------
# Voie A : getTransactionsForAddress
# ---------------------------------------------------------------------------


def probe_rpc(symbol: str, mint: str) -> list | None:
    """Voie A. Retourne la liste de transactions, ou None si inexploitable."""
    print(f"\n[A] {symbol} — {RPC_METHOD} (JSON-RPC)")
    status, payload, sent = _post_rpc(RPC_METHOD, [mint, dict(BASE_CONFIG)])

    print(f"  corps de requete envoye :\n{_truncate(sent, 800)}")

    if status != 200 or not isinstance(payload, dict) or "error" in payload:
        _describe_error(status, payload, sent)
        return None

    print(f"  code HTTP : {status}")
    items, path = _extract_list(payload.get("result"))
    if items is None:
        print("  aucune liste trouvee dans 'result'. Payload brut :")
        print(_truncate(_as_json(payload)))
        return None

    print(f"  liste trouvee en : {path}")
    print(f"  elements retournes : {len(items)}")
    if not items:
        return items

    first, last = items[0], items[-1]
    print(f"  PREMIER blockTime : {first.get('blockTime')} "
          f"({_readable_time(first.get('blockTime'))})")
    print(f"  DERNIER blockTime : {last.get('blockTime')} "
          f"({_readable_time(last.get('blockTime'))})")
    print("  ordre observe :")
    for field in ("blockTime", "slot", "transactionIndex"):
        print(f"      {_order_verdict(items, field)}")
    print(f"  cles d'un element : {sorted(first.keys())}")
    return items


def probe_full_tx_variants(symbol: str, mint: str) -> None:
    """Voie A-bis : la methode peut-elle rendre les transactions completes ?"""
    print(f"\n[A-bis] {symbol} — options pour obtenir les transactions completes")
    print("  Aucun de ces noms de parametre n'est certain. Un rejet est une")
    print("  information : le message brut donnera le nom correct.\n")

    for label, extra in FULL_TX_VARIANTS:
        config = {**BASE_CONFIG, **extra}
        status, payload, sent = _post_rpc(RPC_METHOD, [mint, config])
        print(f"  > {label}")
        print(f"    config envoyee : {_as_json(config)}")

        if status != 200 or not isinstance(payload, dict) or "error" in payload:
            print(f"    code HTTP : {status if status is not None else 'aucune'}")
            print("    REJET, message brut :")
            raw = payload if isinstance(payload, str) else _as_json(payload)
            print("      " + _truncate(raw, 800).replace("\n", "\n      "))
            continue

        items, path = _extract_list(payload.get("result"))
        if not items:
            print(f"    code HTTP {status}, accepte mais aucun element ({path})")
            continue
        keys = sorted(items[0].keys()) if isinstance(items[0], dict) else []
        print(f"    code HTTP {status}, {len(items)} elements en {path}")
        print(f"    cles d'un element : {keys}")
        richer = [k for k in keys if k not in
                  ("signature", "slot", "transactionIndex", "err", "memo",
                   "blockTime", "confirmationStatus")]
        print(f"    cles au-dela des metadonnees : {richer or 'aucune'}")


# ---------------------------------------------------------------------------
# Voie C : enrichissement par signature
# ---------------------------------------------------------------------------


def probe_enrichment(symbol: str, items: list) -> bool:
    """Voie C : POST /v0/transactions avec une liste de signatures."""
    print(f"\n[C] {symbol} — enrichissement par signature "
          f"(POST {HELIUS_TX_URL})")

    signatures = [
        item.get("signature") for item in items
        if isinstance(item, dict) and item.get("err") is None
        and isinstance(item.get("signature"), str)
    ][:ENRICH_SIGNATURES]

    print(f"  signatures retenues (err == null) : {len(signatures)}")
    for signature in signatures:
        print(f"      {signature}")
    if not signatures:
        print("  aucune signature sans erreur dans le lot, rien a enrichir.")
        return False

    status, payload, sent = _post_enrich(signatures)
    if status != 200 or not isinstance(payload, list) or not payload:
        _describe_error(status, payload, sent)
        return False

    print(f"  code HTTP : {status}")
    print(f"  transactions enrichies : {len(payload)}")
    _describe_first_transaction(payload[0])
    return True


def probe_slot_collision(symbol: str, items: list) -> None:
    """Combien de transactions du lot partagent le slot de la premiere ?"""
    print(f"\n[D] {symbol} — transactions partageant le slot de la premiere")
    slots = [item.get("slot") for item in items if isinstance(item, dict)]
    if not slots or slots[0] is None:
        print("  slot de la premiere transaction indisponible.")
        return
    first_slot = slots[0]
    counts = Counter(slots)
    print(f"  slot de la premiere transaction : {first_slot}")
    print(f"  transactions a ce slot, parmi les {len(items)} du lot : "
          f"{counts[first_slot]}")
    print(f"  slots distincts dans le lot : {len(counts)}")


# ---------------------------------------------------------------------------


def main() -> None:
    setup_logging()
    diagnose_environment()

    # HELIUS_API_KEY n'est pas dans le diagnostic de config.py (hors perimetre
    # de ce ticket) : on la logue ici, au meme format.
    present = bool(os.environ.get("HELIUS_API_KEY", "").strip())
    log.info("  %-18s : %s", "HELIUS_API_KEY", "presente" if present else "ABSENTE")
    _api_key()  # leve si absente

    print(f"\nSonde Helius : {len(WINNER_MINTS)} mints")
    print("La voie REST par adresse n'est plus testee (ordre descendant).")
    print("Aucune conclusion tiree ici : la sonde decrit ce qu'elle recoit.\n")

    outcomes: list[tuple[str, str, bool]] = []
    for family, symbol, mint in WINNER_MINTS:
        print("=" * 72)
        print(f"{symbol} ({family})")
        print(f"mint : {mint}")
        print("=" * 72)

        items = probe_rpc(symbol, mint)
        # "Exploitable" depend de la presence de donnees, pas du type Python
        # renvoye par l'API.
        outcomes.append((symbol, "A rpc", bool(items)))

        if symbol != "CATE" or not items:
            continue
        probe_full_tx_variants(symbol, mint)
        outcomes.append((symbol, "C enrich", probe_enrichment(symbol, items)))
        probe_slot_collision(symbol, items)

    print("\n" + "=" * 72)
    print("Recapitulatif")
    for symbol, route, ok in outcomes:
        print(f"  {symbol:8} {route:10} : "
              f"{'donnees exploitables' if ok else 'aucune donnee'}")

    if not any(ok for _, _, ok in outcomes):
        print(
            "\nAucune voie n'a rendu de donnees. Les messages bruts ci-dessus "
            "indiquent ce que Helius attend."
        )


if __name__ == "__main__":
    main()
