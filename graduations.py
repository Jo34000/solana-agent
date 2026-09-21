"""Les regles etablies par les sondes, sorties des fichiers jetables.

Une experience ne doit pas dependre d'une sonde : ces regles sont
desormais validees, elles servent au pipeline, elles vivent ici.

Ce qui a ete valide, et par quoi :

  - le POOL d'une migration : le owner qui voit AUGMENTER a la fois un
    compte du mint gradue et un compte WSOL. Valide 9/10 en format
    Enhanced (sonde v6) puis en format brut (sonde v7).
  - la GRADUATION : un CREATE_POOL dans lequel le compte de la bonding
    curve du mint (PDA seeds ["bonding-curve", mint]) voit son solde de ce
    mint DIMINUER. Le signataire n'entre pas dans la definition : 476/476
    des migrations de la liste du 17/09 passent ce test.
  - la SIGNATURE d'une ligne brute n'est PAS a la racine : elle est sous
    transaction.signatures[0]. La sonde v6 la cherchait a la racine et
    produisait une liste vide sur 476 transactions valides.

Aucun appel reseau ici : ce module ne fait que lire des payloads.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import solana_addr

PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

WSOL_MINT = "So11111111111111111111111111111111111111112"
SOL_MINT = "So11111111111111111111111111111111111111111"
SOL_MINTS = {SOL_MINT, WSOL_MINT}
STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
    "USDSwr9ApdHk5bvJKMjzff41FfuX8bSxdKcR81vTwcA",    # USDS
    "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",   # PYUSD
}
IGNORED_MINTS = SOL_MINTS | STABLE_MINTS

_curve_cache: dict[str, str | None] = {}


def to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def amount_of(line: dict) -> float:
    for key in ("uiAmount", "tokenAmount", "amount"):
        if key in line:
            value = to_float(line.get(key))
            if value:
                return value
    return 0.0


def line_time(line: dict) -> float:
    return to_float(line.get("timestamp") or line.get("blockTime"))


# ---------------------------------------------------------------------------
# Lecture d'une transaction, format brut ou Enhanced
# ---------------------------------------------------------------------------


def deltas_from_raw(transaction: dict) -> list[tuple[str, str, float]]:
    """(mint, owner, variation) depuis meta.pre/postTokenBalances."""
    meta = transaction.get("meta")
    if not isinstance(meta, dict):
        return []
    before: dict[Any, float] = {}
    for entry in meta.get("preTokenBalances") or []:
        if isinstance(entry, dict):
            before[entry.get("accountIndex")] = to_float(
                (entry.get("uiTokenAmount") or {}).get("uiAmount"))
    deltas: list[tuple[str, str, float]] = []
    for entry in meta.get("postTokenBalances") or []:
        if not isinstance(entry, dict):
            continue
        key = entry.get("accountIndex")
        deltas.append((
            entry.get("mint") or "",
            entry.get("owner") or "",
            to_float((entry.get("uiTokenAmount") or {}).get("uiAmount"))
            - before.get(key, 0.0),
        ))
    return deltas


def deltas_from_enhanced(transaction: dict) -> list[tuple[str, str, float]]:
    """Meme logique mint / owner / signe, format Enhanced."""
    deltas: list[tuple[str, str, float]] = []
    for entry in transaction.get("accountData") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("tokenBalanceChanges") or []:
            if not isinstance(change, dict):
                continue
            raw = change.get("rawTokenAmount") or {}
            amount = to_float(raw.get("tokenAmount"))
            decimals = int(to_float(raw.get("decimals")))
            if decimals:
                amount /= 10 ** decimals
            deltas.append((change.get("mint") or "",
                           change.get("userAccount") or "", amount))
    return deltas


def extract_signature(row: dict) -> tuple[str | None, str]:
    """(signature, chemin). Elle n'est PAS a la racine d'une ligne brute."""
    value = row.get("signature")
    if isinstance(value, str) and value:
        return value, "signature"
    transaction = row.get("transaction")
    if isinstance(transaction, dict):
        signatures = transaction.get("signatures")
        if isinstance(signatures, list) and signatures and isinstance(
                signatures[0], str):
            return signatures[0], "transaction.signatures[0]"
        value = transaction.get("signature")
        if isinstance(value, str) and value:
            return value, "transaction.signature"
    signatures = row.get("signatures")
    if isinstance(signatures, list) and signatures and isinstance(
            signatures[0], str):
        return signatures[0], "signatures[0]"
    return None, "introuvable"


def extract_time(row: dict) -> tuple[float, str]:
    for key in ("blockTime", "timestamp", "block_time"):
        value = to_float(row.get(key))
        if value:
            return value, key
    meta = row.get("meta")
    if isinstance(meta, dict):
        value = to_float(meta.get("blockTime"))
        if value:
            return value, "meta.blockTime"
    return 0.0, "introuvable"


def fee_payer_of(row: dict) -> str | None:
    """Premier compte signataire, format brut ou Enhanced."""
    payer = row.get("feePayer")
    if isinstance(payer, str) and payer:
        return payer
    keys = (row.get("transaction") or {}).get("message", {}).get("accountKeys")
    if isinstance(keys, list) and keys:
        first = keys[0]
        if isinstance(first, dict):
            return first.get("pubkey")
        if isinstance(first, str):
            return first
    return None


# ---------------------------------------------------------------------------
# Mint, pool, graduation
# ---------------------------------------------------------------------------


def mint_and_pool(deltas: list[tuple[str, str, float]]) -> dict:
    """Mint gradue et pool. Un seul owner doit remplir les deux conditions."""
    gains: dict[str, float] = {}
    mint_owners: dict[str, set[str]] = defaultdict(set)
    wsol_owners: set[str] = set()
    for mint, owner, delta in deltas:
        if delta <= 0 or not mint:
            continue
        if mint in IGNORED_MINTS:
            if mint == WSOL_MINT and owner:
                wsol_owners.add(owner)
            continue
        gains[mint] = gains.get(mint, 0.0) + delta
        if owner:
            mint_owners[mint].add(owner)
    if not gains:
        return {"mint": None, "pool": None, "pools": 0, "mints": 0}
    chosen = max(gains, key=lambda m: gains[m])
    pools = sorted(mint_owners[chosen] & wsol_owners)
    return {"mint": chosen, "pool": pools[0] if len(pools) == 1 else None,
            "pools": len(pools), "mints": len(gains)}


def curve_of(mint: str) -> str | None:
    """PDA de la bonding curve pump.fun, memoisee."""
    if mint not in _curve_cache:
        try:
            _curve_cache[mint] = solana_addr.bonding_curve_address(
                mint, PUMPFUN_PROGRAM)[0]
        except ValueError:
            _curve_cache[mint] = None
    return _curve_cache[mint]


def is_graduation(deltas: list[tuple[str, str, float]],
                  mint: str) -> tuple[bool, str | None]:
    """La courbe du mint cede-t-elle ses tokens ? Le signataire n'entre pas."""
    curve = curve_of(mint)
    if not curve:
        return False, None
    for line_mint, owner, delta in deltas:
        if line_mint == mint and owner == curve and delta < 0:
            return True, curve
    return False, curve


def graduation_of(row: dict) -> dict | None:
    """[mint, pool, grad_at, signer, signature] ou None si ce n'en est pas une.

    Les trois regles validees, appliquees dans l'ordre. Un motif de rejet
    est toujours rendu : une entree perdue en silence est un bug.
    """
    outcome = mint_and_pool(deltas_from_raw(row))
    if not outcome["mint"]:
        return {"motif": "aucun_mint"}
    if outcome["pools"] != 1:
        return {"motif": "pool_absent" if outcome["pools"] == 0
                else "pools_multiples", "mint": outcome["mint"]}
    signature, _ = extract_signature(row)
    if signature is None:
        return {"motif": "signature_introuvable", "mint": outcome["mint"]}
    when, _ = extract_time(row)
    if not when:
        return {"motif": "horodatage_introuvable", "mint": outcome["mint"]}
    graduated, curve = is_graduation(deltas_from_raw(row), outcome["mint"])
    if not graduated:
        return {"motif": "courbe_ne_cede_pas", "mint": outcome["mint"],
                "signature": signature}
    return {"motif": "graduation", "mint": outcome["mint"],
            "pool": outcome["pool"], "grad_at": when, "signature": signature,
            "signer": fee_payer_of(row), "courbe": curve}


# ---------------------------------------------------------------------------
# Prix : mediane des swaps d'une page
# ---------------------------------------------------------------------------


def group_by_signature(lines: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for line in lines:
        if isinstance(line, dict) and isinstance(line.get("signature"), str):
            groups[line["signature"]].append(line)
    return groups


def swap_price(lines: list[dict]) -> float | None:
    """Prix d'un swap en SOL : jambe SOL / jambe token, achats et ventes."""
    sol = 0.0
    token_amount = 0.0
    mint = None
    for line in lines:
        line_mint = line.get("mint")
        if not isinstance(line_mint, str):
            continue
        if line_mint in SOL_MINTS:
            sol += amount_of(line)
        elif mint is None or line_mint == mint:
            mint = line_mint
            token_amount += amount_of(line)
    if sol > 0 and token_amount > 0 and mint:
        return sol / token_amount
    return None


def median_swap_price(rows: list[dict], moment: float,
                      tolerance: float) -> tuple[float | None, int]:
    """(prix median en SOL des swaps de la page, nombre retenu).

    Seuls les swaps de la fenetre [moment, moment + tolerance] comptent.
    Aucun swap dans cette fenetre n'est pas une perte : c'est un token
    INACTIF a cet instant, et l'appelant l'enregistre comme tel.
    """
    prices: list[float] = []
    for lines in group_by_signature(rows).values():
        stamps = [line_time(line) for line in lines if line_time(line) > 0]
        when = max(stamps) if stamps else 0.0
        # Fenetre [moment, moment + tolerance] : un swap ANTERIEUR n'est pas
        # un prix a cet instant, il est ecarte comme un swap trop tardif.
        if when and not 0 <= when - moment <= tolerance:
            continue
        price = swap_price(lines)
        if price:
            prices.append(price)
    if not prices:
        return None, 0
    prices.sort()
    middle = len(prices) // 2
    median = (prices[middle] if len(prices) % 2
              else (prices[middle - 1] + prices[middle]) / 2)
    return median, len(prices)
