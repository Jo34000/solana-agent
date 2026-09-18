"""Phase 2 : extraction des early buyers des winners Solana.

Pour chaque winner de sol_analyzed_tokens pas encore traite :
  A. getTransactionsForAddress(mint, ordre ascendant) -> signatures
  C. POST /v0/transactions -> transactions enrichies
puis identification des achats, attribution d'un rang, et accumulation
dans sol_early_buys.

sol_smart_wallets est ensuite recalcule depuis la TOTALITE de
sol_early_buys : c'est l'accumulation inter-runs qui fait monter
winners_count au fil des semaines, pas un run isole.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import helius
import supabase_client as db
from config import (
    ACTIVATION_MIN_WINNERS,
    ACTIVATION_TOP_RANK,
    EARLY_BUYER_MAX_RANK,
    EARLY_TX_LIMIT,
    diagnose_environment,
    setup_logging,
)

log = logging.getLogger("solana-agent")

LAMPORTS_PER_SOL = 1_000_000_000


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _readable_time(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def sol_spent_by(transaction: dict, wallet: str) -> float | None:
    """SOL sortant du wallet, en lamports / 1e9. None si non determinable."""
    transfers = transaction.get("nativeTransfers")
    if not isinstance(transfers, list):
        return None
    lamports = sum(
        _to_float(item.get("amount"))
        for item in transfers
        if isinstance(item, dict) and item.get("fromUserAccount") == wallet
    )
    if lamports <= 0:
        return None
    return round(lamports / LAMPORTS_PER_SOL, 9)


def is_buy(transaction: dict, mint: str, wallet: str) -> bool:
    """Le wallet RECOIT-il le mint cible dans cette transaction ?

    Une entree ou le wallet est l'emetteur du mint est une vente : elle
    n'est pas comptee, et ne suffit pas a disqualifier la transaction si
    une autre entree constitue bien une reception.
    """
    transfers = transaction.get("tokenTransfers")
    if not isinstance(transfers, list):
        return False
    for item in transfers:
        if not isinstance(item, dict) or item.get("mint") != mint:
            continue
        if item.get("toUserAccount") == wallet:
            return True
    return False


def extract_buys(
    mint: str, transactions: list[dict], launch_slot: int | None
) -> list[dict]:
    """Achats du mint, dans l'ordre, avec un rang par wallet distinct.

    Les transactions doivent etre fournies dans l'ordre chronologique : le
    rang en depend entierement.
    """
    extracted_at = datetime.now(timezone.utc).isoformat()
    seen: set[str] = set()
    buys: list[dict] = []

    for transaction in transactions:
        wallet = transaction.get("feePayer")
        if not isinstance(wallet, str) or not wallet:
            continue
        if wallet in seen:  # un wallet ne compte qu'une fois
            continue
        if not is_buy(transaction, mint, wallet):
            continue

        seen.add(wallet)
        slot = transaction.get("slot")
        buys.append({
            "mint": mint,
            "wallet": wallet,
            "buy_rank": len(seen),
            "is_bundle": launch_slot is not None and slot == launch_slot,
            "sol_amount": sol_spent_by(transaction, wallet),
            "signature": transaction.get("signature"),
            "slot": slot,
            "block_time": _readable_time(transaction.get("timestamp")),
            "extracted_at": extracted_at,
        })

    return buys


def process_mint(mint: str, symbol: str) -> list[dict] | None:
    """Achats d'un mint. None = PERTE : le token ne doit pas etre marque."""
    signatures_data = helius.transactions_for_address(mint, EARLY_TX_LIMIT)
    if signatures_data is None:
        return None
    if not signatures_data:
        log.info("  %-10s : aucune transaction remontee", symbol)
        return []

    # Slot de lancement : celui de la toute premiere transaction du lot
    # brut, avant le filtre err. Une transaction echouee marque quand meme
    # le slot du bundle de lancement.
    first = signatures_data[0]
    launch_slot = first.get("slot") if isinstance(first, dict) else None

    ordered = [
        item for item in signatures_data
        if isinstance(item, dict)
        and item.get("err") is None
        and isinstance(item.get("signature"), str)
    ]
    if not ordered:
        log.info("  %-10s : aucune transaction sans erreur", symbol)
        return []

    signatures = [item["signature"] for item in ordered]
    enriched = helius.enrich_signatures(signatures)
    if enriched is None:
        return None

    # La voie C ne garantit pas de renvoyer les transactions dans l'ordre
    # des signatures envoyees : on les reordonne sur l'ordre ascendant de
    # la voie A, dont depend entierement le rang.
    by_signature = {
        item.get("signature"): item for item in enriched
        if isinstance(item.get("signature"), str)
    }
    in_order = [by_signature[s] for s in signatures if s in by_signature]

    buys = extract_buys(mint, in_order, launch_slot)
    bundled = sum(1 for buy in buys if buy["is_bundle"])
    log.info(
        "  %-10s : %d tx | %d enrichies | %d achats (dont %d bundle) "
        "| slot lancement %s",
        symbol, len(signatures_data), len(in_order), len(buys), bundled,
        launch_slot,
    )
    return buys


def build_smart_wallets(early_buys: list[dict]) -> list[dict]:
    """Agrege sol_early_buys en etat par wallet.

    TOUS les wallets sont enregistres, actifs ou non : c'est l'accumulation
    inter-runs qui fera monter winners_count.
    """
    tokens: dict[str, set[str]] = defaultdict(set)
    best: dict[str, int] = {}

    for row in early_buys:
        wallet = row.get("wallet")
        mint = row.get("mint")
        rank = row.get("buy_rank")
        if not isinstance(wallet, str) or not isinstance(mint, str):
            continue
        tokens[wallet].add(mint)
        if isinstance(rank, int) and (wallet not in best or rank < best[wallet]):
            best[wallet] = rank

    updated_at = datetime.now(timezone.utc).isoformat()
    wallets: list[dict] = []
    for wallet, mints in tokens.items():
        winners_count = len(mints)
        best_rank = best.get(wallet)
        by_overlap = winners_count >= ACTIVATION_MIN_WINNERS
        by_rank = best_rank is not None and best_rank <= ACTIVATION_TOP_RANK
        if by_overlap:
            reason = "recoupement"
        elif by_rank:
            reason = "rang_bas"
        else:
            reason = None
        wallets.append({
            "wallet": wallet,
            "winners_count": winners_count,
            "winner_tokens": sorted(mints),
            "best_rank": best_rank,
            "active": bool(by_overlap or by_rank),
            "activation_reason": reason,
            "updated_at": updated_at,
        })

    return wallets


def run() -> int:
    setup_logging()
    diagnose_environment()
    helius.api_key()  # leve si absente, avant tout appel

    targets = db.fetch_winners_to_process()
    if not targets:
        log.info("Aucun winner a traiter.")
        return 0

    raw_buys = 0
    processed = 0
    lost: list[str] = []

    for target in targets:
        mint = target.get("mint")
        symbol = target.get("symbol") or "?"
        if not isinstance(mint, str) or not mint:
            continue

        buys = process_mint(mint, symbol)
        if buys is None:  # PERTE deja loguee cote client
            lost.append(symbol)
            continue

        db.insert_early_buys(buys)
        # Marque meme a zero acheteur : sinon le token serait rejoue a
        # chaque run. Jamais en cas de PERTE, en revanche.
        db.mark_buyers_extracted(mint)
        processed += 1
        raw_buys += len(buys)

    if lost:
        log.warning(
            "PERTE : %d token(s) non traites, non marques, rejoues au "
            "prochain run : %s", len(lost), ", ".join(lost),
        )

    # Recalcul global : toute la table, pas seulement ce run.
    early = db.fetch_early_buys(EARLY_BUYER_MAX_RANK)
    wallets = build_smart_wallets(early)
    db.upsert_smart_wallets(wallets)

    active = [w for w in wallets if w["active"]]
    overlap = sum(1 for w in active if w["activation_reason"] == "recoupement")
    low_rank = sum(1 for w in active if w["activation_reason"] == "rang_bas")
    calls, losses = helius.request_stats()

    log.info("=== Resume ===")
    log.info(
        "tokens traites %d | acheteurs bruts %d | hors bundle %d | "
        "wallets distincts %d | actifs %d (recoupement %d / rang bas %d)",
        processed, raw_buys, len(early), len(wallets),
        len(active), overlap, low_rank,
    )
    log.info("appels Helius %d | pertes %d", calls, losses)

    top = sorted(
        active,
        key=lambda w: (-w["winners_count"], w["best_rank"] or 10**9),
    )[:10]
    for wallet in top:
        log.info(
            "  WALLET %s  winners %d  meilleur rang %s  (%s)",
            wallet["wallet"], wallet["winners_count"],
            wallet["best_rank"], wallet["activation_reason"],
        )
    return len(active)


if __name__ == "__main__":
    run()
