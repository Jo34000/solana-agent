"""Sonde jetable : peut-on reconstituer et suivre l'univers des gradues ?

Script d'observation, jamais appele par le pipeline. Lance via
RUN_MODE=probe_universe ou directement.

Question posee : un humain recevant une alerte avec 5 a 60 min de latence
dispose-t-il d'une fenetre exploitable sur les tokens gradues ? Avant de
chercher des wallets, il faut savoir si l'univers est listable
retroactivement et a quel cout.

Cette sonde MESURE la faisabilite et le cout. Elle ne conclut rien sur le
trading, n'ecrit rien en base, et respecte des plafonds par section : un
plafond atteint coupe la section avec un warning, jamais en silence.

Contrainte permanente : offres gratuites. Helius free tier (1M
credits/mois), CoinGecko Demo (cle PARTAGEE avec l'agent ETH, donc appels
minimises), Supabase gratuit.

Ecarts releves avant ecriture, et ce qui a ete decide :
  - PumpSwap n'est plus collecte depuis le 18/09 (PREFERRED_DEXES ne
    contient que meteora, raydium-clmm et raydium). sol_analyzed_tokens
    peut donc en contenir peu. Repli : aller chercher les pools PumpSwap
    directement via dex_pools("pumpswap"), ce qui preserve la semantique
    pump.fun, plutot que de basculer sur "le DEX le plus represente" ou
    il n'y a pas de bonding curve.
  - Le prix du SOL reutilise de wallet_validation_v2 est en bougies
    JOURNALIERES : les mcap intra-journalieres en heritent d'une
    imprecision, signalee dans le resume.
  - GeckoTerminal n'expose pas de new_pools par DEX : le filtrage est
    fait cote client sur les new_pools reseau-large, et la pagination est
    plafonnee a 10 pages sur le plan Demo.
"""

from __future__ import annotations

import json
import logging
import os
import random
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

import geckoterminal as gt
import helius
import supabase_client as db
import wallet_validation_v2 as v2
from config import (
    ANALYZED_TABLE,
    SOL_MINTS,
    diagnose_environment,
    setup_logging,
)

log = logging.getLogger("solana-agent")

# --- Plafonds -------------------------------------------------------------
# Globaux, puis par section. Une section qui atteint son plafond s'arrete
# avec un warning et la sonde passe a la suivante.
CAPS_GLOBAL = {
    "getTransfersByAddress": 1500,
    "getTransactionsForAddress": 60,
    "enhanced": 10,
    "coingecko": 40,
}
CAPS_SECTION = {
    1: {"getTransfersByAddress": 15, "getTransactionsForAddress": 6},
    2: {"getTransfersByAddress": 120, "coingecko": 6},
    3: {"getTransfersByAddress": 90},
    4: {"getTransfersByAddress": 20, "getTransactionsForAddress": 40,
        "enhanced": 8, "coingecko": 14},
    5: {"getTransfersByAddress": 500, "coingecko": 6},
    6: {"getTransfersByAddress": 450},
}

# Cout annonce, a recouper avec le dashboard Helius.
CREDIT_COST = {
    "getTransfersByAddress": 10,
    "getTransactionsForAddress": 100,
    "enhanced": 0,       # inclus dans le forfait REST, a confirmer
    "getTokenSupply": 1,
    "coingecko": 0,      # quota separe, pas des credits Helius
}

MONTHLY_CREDITS = 1_000_000

TOKENS_SECTION_2 = 10
INSTANTS_PER_TOKEN = 8
SAMPLE_SIZE = 30
EARLY_BUYER_TOKENS = 8
EARLY_BUYER_MAX_PAGES = 50
RANDOM_SEED = 20260920

# Points de trajectoire apres graduation, en secondes.
TRAJECTORY_POINTS = (
    ("5 min", 300), ("15 min", 900), ("30 min", 1800), ("1 h", 3600),
    ("3 h", 10800), ("6 h", 21600), ("24 h", 86400), ("7 j", 604800),
)

_calls: Counter = Counter()
_section_calls: Counter = Counter()
_current_section = 0
_capped: set[tuple[int, str]] = set()


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def start_section(number: int, title: str) -> None:
    global _current_section, _section_calls
    _current_section = number
    _section_calls = Counter()
    print("\n" + "=" * 74)
    print(f"SECTION {number} - {title}")
    print("=" * 74)


def can_spend(method: str) -> bool:
    """Reste-t-il du budget pour un appel ? Plafond atteint -> warning."""
    if _calls[method] >= CAPS_GLOBAL.get(method, 10**9):
        key = (0, method)
        if key not in _capped:
            _capped.add(key)
            log.warning("PLAFOND GLOBAL atteint : %s (%d appels), sections "
                        "suivantes amputees", method, CAPS_GLOBAL[method])
        return False
    limit = CAPS_SECTION.get(_current_section, {}).get(method)
    if limit is not None and _section_calls[method] >= limit:
        key = (_current_section, method)
        if key not in _capped:
            _capped.add(key)
            log.warning("PLAFOND de section %d atteint : %s (%d appels), "
                        "section interrompue", _current_section, method, limit)
        return False
    return True


def _spend(method: str) -> None:
    _calls[method] += 1
    _section_calls[method] += 1


def _masked(text: str) -> str:
    key = os.environ.get("HELIUS_API_KEY", "").strip()
    return text.replace(key, "***") if key else text


def _as_json(value: Any) -> str:
    try:
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(value)


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _amount(line: dict) -> float:
    for key in ("uiAmount", "tokenAmount", "amount"):
        if key in line:
            value = _to_float(line.get(key))
            if value:
                return value
    return 0.0


def _line_time(line: dict) -> float:
    return _to_float(line.get("timestamp") or line.get("blockTime"))


def _iso(moment: float) -> str:
    try:
        return datetime.fromtimestamp(moment, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return repr(moment)


# ---------------------------------------------------------------------------
# Appels comptabilises
# ---------------------------------------------------------------------------


def transfers(address: str, config: dict) -> Any | None:
    """getTransfersByAddress brut, payload tel quel (erreur comprise)."""
    if not can_spend("getTransfersByAddress"):
        return "CAPPED"
    _spend("getTransfersByAddress")
    return helius.rpc("getTransfersByAddress", [address, config])


def transactions(address: str, config: dict) -> Any | None:
    if not can_spend("getTransactionsForAddress"):
        return "CAPPED"
    _spend("getTransactionsForAddress")
    return helius.rpc("getTransactionsForAddress", [address, config])


def enhanced(signatures: list[str]) -> list[dict] | None:
    if not can_spend("enhanced"):
        return None
    _spend("enhanced")
    return helius.enrich_signatures(signatures)


def token_supply(mint: str) -> dict | None:
    _spend("getTokenSupply")
    return helius.get_token_supply(mint)


def gecko(call, *args, **kwargs):
    if not can_spend("coingecko"):
        return None
    _spend("coingecko")
    return call(*args, **kwargs)


def rows_of(payload: Any) -> list[dict] | None:
    """Liste de result, quelle que soit sa forme. None si erreur/absente."""
    if payload in (None, "CAPPED") or not isinstance(payload, dict):
        return None
    if "error" in payload:
        return None
    result = payload.get("result")
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, list):
            return data
    return None


def group_by_signature(lines: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for line in lines:
        if isinstance(line, dict) and isinstance(line.get("signature"), str):
            groups[line["signature"]].append(line)
    return groups


def swap_price(lines: list[dict]) -> tuple[float, str] | None:
    """Prix d'un swap : jambe SOL / jambe token. (prix en SOL, mint)."""
    sol = 0.0
    token_amount = 0.0
    mint = None
    for line in lines:
        line_mint = line.get("mint")
        if not isinstance(line_mint, str):
            continue
        if line_mint in SOL_MINTS:
            sol += _amount(line)
        elif mint is None or line_mint == mint:
            mint = line_mint
            token_amount += _amount(line)
    if sol > 0 and token_amount > 0 and mint:
        return sol / token_amount, mint
    return None


# ---------------------------------------------------------------------------
# SECTION 1 - Syntaxe des filtres
# ---------------------------------------------------------------------------

# UNE cle a la fois : acquis du 19/09, une cle inconnue fait rejeter tout
# l'objet de config.
TRANSFER_FILTERS = (
    ("filters.blockTime gte+lte", lambda now: {
        "filters": {"blockTime": {"gte": int(now - 30 * 86400),
                                  "lte": int(now - 20 * 86400)}}}),
    ("filters.amount gte", lambda now: {"filters": {"amount": {"gte": 1}}}),
    ("filters.slot", lambda now: {"filters": {"slot": {"gte": 1}}}),
    ("direction", lambda now: {"direction": "in"}),
    ("mint", lambda now: {"mint": sorted(SOL_MINTS)[0]}),
    ("solMode", lambda now: {"solMode": True}),
)

TRANSACTION_FILTERS = (
    ("filters.blockTime", lambda now: {
        "filters": {"blockTime": {"gte": int(now - 30 * 86400)}}}),
    ("filters.status", lambda now: {"filters": {"status": "success"}}),
    ("transactionDetails signatures", lambda now: {
        "transactionDetails": "signatures"}),
)


def _check_blocktime(rows: list[dict], config: dict) -> str:
    window = (config.get("filters") or {}).get("blockTime")
    if not isinstance(window, dict):
        return ""
    stamps = [_line_time(r) for r in rows if _line_time(r) > 0]
    if not stamps:
        return "    preuve : aucune date exploitable"
    low, high = window.get("gte"), window.get("lte")
    inside = sum(
        1 for s in stamps
        if (low is None or s >= low) and (high is None or s <= high)
    )
    verdict = "APPLIQUE" if inside == len(stamps) else "NON applique"
    return (f"    preuve : {inside}/{len(stamps)} dates dans la plage "
            f"-> filtre {verdict}\n"
            f"    plage reelle : {_iso(min(stamps))} -> {_iso(max(stamps))}")


def _check_amount(rows: list[dict], config: dict) -> str:
    threshold = ((config.get("filters") or {}).get("amount") or {}).get("gte")
    if threshold is None:
        return ""
    amounts = [_amount(r) for r in rows]
    above = sum(1 for a in amounts if a >= threshold)
    verdict = "APPLIQUE" if above == len(amounts) else "NON applique"
    return (f"    preuve : {above}/{len(amounts)} montants >= {threshold} "
            f"-> filtre {verdict}")


def section_1(wallet: str) -> dict:
    start_section(1, "Syntaxe des filtres")
    print("Une cle a la fois : acquis du 19/09, une cle inconnue fait "
          "rejeter tout l'objet de config.\n")
    now = datetime.now(timezone.utc).timestamp()
    accepted: dict[str, list[str]] = {"transfers": [], "transactions": []}

    for label, method, variants, family in (
        ("getTransfersByAddress", transfers, TRANSFER_FILTERS, "transfers"),
        ("getTransactionsForAddress", transactions, TRANSACTION_FILTERS,
         "transactions"),
    ):
        print(f"--- {label} ---")
        for name, build in variants:
            extra = build(now)
            config = {"limit": 20, "sortOrder": "desc", **extra}
            payload = method(wallet, config)
            if payload == "CAPPED":
                break
            print(f"\n> {name}")
            print(f"  config : {json.dumps(config)}")
            if payload is None:
                print("  PERTE, non concluant")
                continue
            if isinstance(payload, dict) and "error" in payload:
                print("  REJET, message brut :")
                print("    " + _as_json(payload).replace("\n", "\n    "))
                continue
            rows = rows_of(payload)
            if rows is None:
                print(f"  accepte, mais pas de liste : {_as_json(payload)[:300]}")
                continue
            print(f"  ACCEPTE : {len(rows)} elements")
            accepted[family].append(name)
            for proof in (_check_blocktime(rows, config),
                          _check_amount(rows, config)):
                if proof:
                    print(proof)
        print()

    _section_1_solmode(wallet)
    return accepted


def _section_1_solmode(wallet: str) -> None:
    """SOL natif et WSOL : comment solMode les rend-il ?"""
    print("--- solMode : meme signature, avec et sans ---")
    base = transfers(wallet, {"limit": 100, "sortOrder": "desc"})
    with_mode = transfers(wallet, {"limit": 100, "sortOrder": "desc",
                                   "solMode": True})
    rows_base = rows_of(base)
    if rows_base is None:
        print("  appel de reference indisponible")
        return
    rows_mode = rows_of(with_mode)
    if rows_mode is None:
        print("  solMode rejete ou indisponible : comparaison impossible")
        if isinstance(with_mode, dict) and "error" in with_mode:
            print("    " + _as_json(with_mode)[:400].replace("\n", "\n    "))
        return

    groups_base = group_by_signature(rows_base)
    groups_mode = group_by_signature(rows_mode)
    shared = [
        s for s in groups_base
        if s in groups_mode and any(
            l.get("mint") in SOL_MINTS for l in groups_base[s]
        )
    ]
    if not shared:
        print("  aucune signature commune portant une jambe SOL")
        return
    signature = shared[0]
    print(f"  signature : {signature}")
    for title, group in (("sans solMode", groups_base[signature]),
                         ("avec solMode", groups_mode[signature])):
        print(f"    {title} ({len(group)} lignes) :")
        for line in group:
            mint = line.get("mint")
            tag = " <- SOL/WSOL" if mint in SOL_MINTS else ""
            print(f"      mint={mint} montant={_amount(line)}{tag}")


# ---------------------------------------------------------------------------
# Echantillon de tokens
# ---------------------------------------------------------------------------


def pick_tokens(count: int) -> tuple[list[dict], str]:
    """(tokens, provenance). Priorite a PumpSwap, quelle qu'en soit la source.

    PumpSwap n'etant plus collecte depuis le 18/09, sol_analyzed_tokens peut
    n'en contenir aucun. Le repli va alors chercher les pools PumpSwap
    directement chez GeckoTerminal : basculer sur "le DEX le plus
    represente" donnerait des pools Raydium ou Meteora, ou la notion de
    bonding curve n'existe pas, et les sections 3 a 6 mesureraient autre
    chose.
    """
    response = (
        db.get_client()
        .table(ANALYZED_TABLE)
        .select("mint, symbol, dex, pool_address, pool_created_at")
        .not_.is_("pool_address", "null")
        .limit(2000)
        .execute()
    )
    rows = [r for r in (response.data or []) if r.get("pool_address")]
    distribution = Counter(r.get("dex") or "?" for r in rows)
    print(f"sol_analyzed_tokens : {len(rows)} tokens avec un pool")
    print(f"  repartition par DEX : {dict(distribution.most_common(8))}")

    pump = [r for r in rows if (r.get("dex") or "").startswith("pumpswap")]
    if len(pump) >= count:
        print(f"  -> {count} tokens PumpSwap pris en base")
        return pump[:count], "sol_analyzed_tokens/pumpswap"

    print(f"  seulement {len(pump)} tokens PumpSwap en base "
          f"(PumpSwap retire de la collecte le 18/09)")
    result = gecko(gt.dex_pools, "pumpswap", 1, "h24_volume_usd_desc")
    if result:
        pools, _ = result
        extra = []
        for pool in pools:
            attributes = pool.get("attributes") or {}
            base = (pool.get("relationships", {}).get("base_token", {})
                    .get("data", {}).get("id", ""))
            mint = base.split("_", 1)[-1] if "_" in base else None
            address = attributes.get("address")
            if mint and address:
                extra.append({
                    "mint": mint,
                    "symbol": (attributes.get("name") or "?").split("/")[0].strip(),
                    "dex": "pumpswap",
                    "pool_address": address,
                    "pool_created_at": attributes.get("pool_created_at"),
                })
        merged = pump + [e for e in extra if e["mint"] not in
                         {p["mint"] for p in pump}]
        if merged:
            print(f"  -> repli GeckoTerminal : {len(extra)} pools PumpSwap "
                  f"recuperes, echantillon de {min(count, len(merged))}")
            return merged[:count], "geckoterminal/dex_pools(pumpswap)"

    fallback_dex = distribution.most_common(1)[0][0] if distribution else "?"
    chosen = [r for r in rows if (r.get("dex") or "?") == fallback_dex][:count]
    print(f"  -> AUCUN pool PumpSwap accessible. Repli sur le DEX le plus "
          f"represente : {fallback_dex}. ATTENTION : pas de bonding curve "
          f"sur ce DEX, les sections 3 et 6 mesureront autre chose.")
    return chosen, f"sol_analyzed_tokens/{fallback_dex}"


# ---------------------------------------------------------------------------
# SECTION 2 - Prix a un instant donne
# ---------------------------------------------------------------------------


def price_at(pool: str, moment: float) -> tuple[float | None, int]:
    """(prix en SOL du premier swap a partir de t, appels consommes)."""
    payload = transfers(pool, {
        "limit": 20, "sortOrder": "asc",
        "filters": {"blockTime": {"gte": int(moment)}},
    })
    if payload == "CAPPED":
        return None, 0
    rows = rows_of(payload)
    if rows is None:
        return None, 1
    for lines in group_by_signature(rows).values():
        priced = swap_price(lines)
        if priced:
            return priced[0], 1
    return None, 1


def section_2(tokens: list[dict]) -> dict:
    start_section(2, "Prix a un instant donne, pool gradue")
    now = datetime.now(timezone.utc).timestamp()
    obtained = attempts = calls = 0
    per_token: list[tuple[str, int, int]] = []

    for token in tokens:
        pool = token["pool_address"]
        created = v2._to_float(0)
        raw = token.get("pool_created_at")
        if isinstance(raw, str):
            try:
                created = datetime.fromisoformat(
                    raw.replace("Z", "+00:00")).timestamp()
            except ValueError:
                created = 0.0
        if not created:
            created = now - 30 * 86400
        span = max(now - created, 3600)

        got = 0
        used = 0
        for index in range(INSTANTS_PER_TOKEN):
            moment = created + span * (index + 1) / (INSTANTS_PER_TOKEN + 1)
            price, spent = price_at(pool, moment)
            used += spent
            attempts += 1
            if price is not None:
                got += 1
                obtained += 1
            if spent == 0:
                break
        calls += used
        per_token.append((token.get("symbol") or "?", got, used))
        log.info("  %-10s : %d/%d points obtenus, %d appels",
                 token.get("symbol") or "?", got, INSTANTS_PER_TOKEN, used)

    rate = 100 * obtained / attempts if attempts else 0
    per_point = calls / obtained if obtained else 0
    print(f"\n{obtained}/{attempts} points de prix obtenus ({rate:.1f} %)")
    print(f"appels par point de prix obtenu : {per_point:.2f}")
    return {"obtained": obtained, "attempts": attempts, "calls": calls,
            "per_point": per_point, "per_token": per_token}


def section_2_control(tokens: list[dict]) -> None:
    """Controle : ecart entre le prix reconstruit et l'OHLCV horaire."""
    print("\n--- controle sur 3 tokens : OHLCV horaire CoinGecko ---")
    print("Le prix reconstruit est en SOL, l'OHLCV en USD : la comparaison "
          "passe par le prix du SOL a la date.")
    now = datetime.now(timezone.utc).timestamp()
    for token in tokens[:3]:
        pool = token["pool_address"]
        moment = now - 3 * 86400
        price_sol, _ = price_at(pool, moment)
        candles = gecko(gt.ohlcv, pool, "hour", 24, int(moment + 3600))
        if price_sol is None or not candles:
            print(f"  {token.get('symbol')} : comparaison impossible "
                  f"(prix {price_sol is not None}, ohlcv {bool(candles)})")
            continue
        ordered = sorted(candles, key=lambda c: _to_float(c[0]))
        closest = min(ordered, key=lambda c: abs(_to_float(c[0]) - moment))
        usd_gecko = _to_float(closest[4])
        sol_usd = v2.sol_price_at(moment)
        if not sol_usd or usd_gecko <= 0:
            print(f"  {token.get('symbol')} : prix du SOL indisponible")
            continue
        usd_rebuilt = price_sol * sol_usd
        gap = 100 * (usd_rebuilt - usd_gecko) / usd_gecko
        print(f"  {token.get('symbol'):>10} : reconstruit {usd_rebuilt:.8f} $ "
              f"| OHLCV {usd_gecko:.8f} $ | ecart {gap:+.1f} %")


# ---------------------------------------------------------------------------
# SECTION 3 - Bonding curve et graduation
# ---------------------------------------------------------------------------


def find_bonding_curve(mint: str) -> tuple[str | None, float | None]:
    """(adresse de la courbe, date du premier transfert du mint).

    La courbe est la contrepartie des toutes premieres jambes du mint :
    c'est d'elle que sortent les tokens a l'achat.
    """
    payload = transfers(mint, {"limit": 50, "sortOrder": "asc"})
    rows = rows_of(payload)
    if not rows:
        return None, None
    first_time = min((_line_time(r) for r in rows if _line_time(r) > 0),
                     default=None)
    counterparts: Counter = Counter()
    for line in rows:
        if line.get("mint") != mint:
            continue
        for field in ("fromUserAccount", "toUserAccount"):
            value = line.get(field)
            if isinstance(value, str) and value:
                counterparts[value] += 1
    if not counterparts:
        return None, first_time
    return counterparts.most_common(1)[0][0], first_time


def graduation_time(pool: str) -> float | None:
    """Date de la premiere transaction du pool gradue."""
    payload = transfers(pool, {"limit": 1, "sortOrder": "asc"})
    rows = rows_of(payload)
    if not rows:
        return None
    stamps = [_line_time(r) for r in rows if _line_time(r) > 0]
    return min(stamps) if stamps else None


def section_3(tokens: list[dict]) -> dict:
    start_section(3, "Bonding curve et graduation")
    found = 0
    durations: list[float] = []
    prices_ok = prices_try = 0
    graduations: dict[str, float] = {}

    for token in tokens:
        mint = token["mint"]
        curve, created = find_bonding_curve(mint)
        graduated = graduation_time(token["pool_address"])
        if graduated:
            graduations[mint] = graduated

        line = f"  {token.get('symbol') or '?':>10} : courbe "
        if curve:
            found += 1
            line += f"{curve[:8]}..."
        else:
            line += "NON trouvee"

        if created and graduated and graduated > created:
            hours = (graduated - created) / 3600
            durations.append(hours)
            line += f" | creation -> graduation {hours:.1f} h"
        else:
            line += " | duree indeterminable"

        got = 0
        if curve and created and graduated and graduated > created:
            span = graduated - created
            for index in range(3):
                moment = created + span * (index + 1) / 4
                price, spent = price_at(curve, moment)
                prices_try += 1
                if spent == 0:
                    break
                if price is not None:
                    got += 1
                    prices_ok += 1
        line += f" | prix avant graduation {got}/3"
        log.info(line)

    print(f"\nbonding curve trouvee : {found}/{len(tokens)}")
    if durations:
        print(f"creation -> graduation : mediane {statistics.median(durations):.1f} h "
              f"| min {min(durations):.1f} h | max {max(durations):.1f} h")
    if prices_try:
        print(f"prix avant graduation : {prices_ok}/{prices_try} obtenus "
              f"({100 * prices_ok / prices_try:.0f} %)")
    return {"found": found, "durations": durations,
            "prices_ok": prices_ok, "prices_try": prices_try,
            "graduations": graduations}


# ---------------------------------------------------------------------------
# SECTION 4 - Lister l'univers retroactivement
# ---------------------------------------------------------------------------


def creation_accounts(pool: str) -> set[str]:
    """Comptes impliques dans la premiere transaction du pool."""
    payload = transfers(pool, {"limit": 1, "sortOrder": "asc"})
    rows = rows_of(payload)
    if not rows:
        return set()
    signature = rows[0].get("signature")
    if not isinstance(signature, str):
        return set()
    enriched = enhanced([signature])
    if not enriched:
        return set()
    transaction = enriched[0]
    accounts: set[str] = set()
    fee_payer = transaction.get("feePayer")
    if isinstance(fee_payer, str):
        accounts.add(fee_payer)
    for entry in transaction.get("accountData") or []:
        if isinstance(entry, dict) and isinstance(entry.get("account"), str):
            accounts.add(entry["account"])
    for instruction in transaction.get("instructions") or []:
        if isinstance(instruction, dict):
            program = instruction.get("programId")
            if isinstance(program, str):
                accounts.add(program)
    return accounts


def count_created_pools(account: str, day_start: float) -> tuple[int, int]:
    """(mints distincts vus, appels) sur 24 h passees pour un compte."""
    payload = transactions(account, {
        "limit": 100, "sortOrder": "asc",
        "filters": {"blockTime": {"gte": int(day_start),
                                  "lte": int(day_start + 86400)}},
    })
    if payload == "CAPPED":
        return 0, 0
    rows = rows_of(payload)
    if rows is None:
        return 0, 1
    return len(rows), 1


def section_4(tokens: list[dict]) -> dict:
    start_section(4, "Lister l'univers retroactivement")
    print("Question : peut-on reconstituer la liste des gradues d'une "
          "journee PASSEE, morts compris, sans l'avoir enregistree ?\n")

    print("--- comptes presents a la creation de 3 pools ---")
    seen: Counter = Counter()
    for token in tokens[:3]:
        accounts = creation_accounts(token["pool_address"])
        print(f"  {token.get('symbol') or '?':>10} : {len(accounts)} comptes")
        for account in accounts:
            seen[account] += 1

    shared = [a for a, n in seen.items() if n >= 2]
    print(f"\ncomptes presents sur au moins 2 des 3 tokens : {len(shared)}")
    for account in shared[:10]:
        print(f"    {account} ({seen[account]}/3)")

    now = datetime.now(timezone.utc).timestamp()
    per_day: dict[str, dict[str, int]] = {}
    for offset in (3, 10):
        day_start = now - offset * 86400
        label = _iso(day_start)[:10]
        per_day[label] = {}
        for account in shared[:4]:
            count, spent = count_created_pools(account, day_start)
            if spent == 0:
                break
            per_day[label][account[:8]] = count
        print(f"  journee {label} : {per_day[label]}")

    alternative = _section_4_new_pools()
    return {"shared_accounts": shared, "per_day": per_day,
            "new_pools": alternative}


def _section_4_new_pools() -> dict:
    """Alternative : new_pools CoinGecko, filtre PumpSwap cote client.

    GeckoTerminal n'expose pas de new_pools par DEX et la pagination est
    plafonnee a 10 pages sur le plan Demo : cette alternative est mesuree,
    pas supposee.
    """
    print("\n--- alternative : new_pools CoinGecko ---")
    now = datetime.now(timezone.utc).timestamp()
    total = pump = 0
    ages: list[float] = []
    pages = 0
    for page in range(1, 4):
        result = gecko(gt.new_pools, page)
        if result is None:
            break
        pools, _ = result
        if not pools:
            break
        pages += 1
        for pool in pools:
            total += 1
            attributes = pool.get("attributes") or {}
            dex = (pool.get("relationships", {}).get("dex", {})
                   .get("data", {}).get("id", ""))
            if "pump" in dex:
                pump += 1
            raw = attributes.get("pool_created_at")
            if isinstance(raw, str):
                try:
                    created = datetime.fromisoformat(
                        raw.replace("Z", "+00:00")).timestamp()
                    ages.append((now - created) / 3600)
                except ValueError:
                    pass
    if not total:
        print("  aucun pool renvoye")
        return {}
    span = (max(ages) - min(ages)) if len(ages) > 1 else 0.0
    rate = total / span if span > 0 else 0
    print(f"  {pages} pages, {total} pools, dont {pump} sur un DEX 'pump'")
    if ages:
        print(f"  amplitude couverte : {span:.2f} h "
              f"(du plus ancien {max(ages):.2f} h au plus recent {min(ages):.2f} h)")
        print(f"  rythme observe : {rate:.0f} pools/h")
        pages_for_24h = (24 / span * pages) if span > 0 else float("inf")
        print(f"  pages necessaires pour couvrir 24 h : "
              f"{pages_for_24h:.0f} (plafond du plan Demo : {gt.MAX_PAGE})")
        if pages_for_24h > gt.MAX_PAGE:
            print("  -> new_pools NE PEUT PAS couvrir 24 h retroactivement "
                  "sur le plan Demo")
    return {"pages": pages, "total": total, "pump": pump,
            "span_hours": span, "rate_per_hour": rate}


# ---------------------------------------------------------------------------
# SECTION 5 - Trajectoire d'un echantillon aleatoire
# ---------------------------------------------------------------------------


def section_5(tokens: list[dict], graduations: dict[str, float]) -> dict:
    start_section(5, "Trajectoire d'un echantillon aleatoire")
    print(f"seed aleatoire : {RANDOM_SEED}")

    rng = random.Random(RANDOM_SEED)
    sample = list(tokens)
    rng.shuffle(sample)
    sample = sample[:SAMPLE_SIZE]
    print(f"{len(sample)} tokens tires (morts compris)\n")

    supplies: dict[str, float] = {}
    billion = 0
    mcaps: list[float] = []
    alive = dead = 0
    alive_ok = 0
    calls_before = _section_calls["getTransfersByAddress"]

    for token in sample:
        mint = token["mint"]
        pool = token["pool_address"]
        graduated = graduations.get(mint) or graduation_time(pool)
        if not graduated:
            continue

        supply = supplies.get(mint)
        if supply is None:
            value = token_supply(mint)
            supply = _to_float((value or {}).get("uiAmount"))
            supplies[mint] = supply
            if 0.9e9 <= supply <= 1.1e9:
                billion += 1

        obtained = 0
        best_mcap = 0.0
        for _, delta in TRAJECTORY_POINTS:
            price, spent = price_at(pool, graduated + delta)
            if spent == 0:
                break
            if price is None:
                continue
            obtained += 1
            sol_usd = v2.sol_price_at(graduated + delta)
            if sol_usd and supply > 0:
                best_mcap = max(best_mcap, price * sol_usd * supply)

        # Vivant ou mort : un pool sans aucun prix sur 8 instants est
        # considere mort. C'est exactement la population que GeckoTerminal
        # perdait en 404.
        if obtained:
            alive += 1
            alive_ok += obtained
            if best_mcap > 0:
                mcaps.append(best_mcap)
        else:
            dead += 1

        log.info("  %-10s : %d/%d points | supply %.3g | mcap max %.0f $",
                 token.get("symbol") or "?", obtained, len(TRAJECTORY_POINTS),
                 supply, best_mcap)

    used = _section_calls["getTransfersByAddress"] - calls_before
    measured = alive + dead
    print(f"\ntokens traites {measured} | avec au moins un prix {alive} | "
          f"sans aucun prix {dead}")
    if measured:
        print(f"taux de succes : {100 * alive / measured:.1f} % "
              f"(GeckoTerminal renvoyait 404 sur les morts)")
    print(f"appels par token : {used / measured:.1f}" if measured else "")
    print(f"supply a 1 milliard : {billion}/{len(supplies)} tokens")
    if mcaps:
        ordered = sorted(mcaps)
        print("capitalisations max observees (brut, aucune conclusion) :")
        for label, value in (
            ("min", ordered[0]),
            ("p25", ordered[len(ordered) // 4]),
            ("mediane", statistics.median(ordered)),
            ("p75", ordered[3 * len(ordered) // 4]),
            ("max", ordered[-1]),
        ):
            print(f"    {label:>8} : {value:>14,.0f} $")
    return {"sample": len(sample), "alive": alive, "dead": dead,
            "calls": used, "mcaps": mcaps, "billion": billion,
            "supplies": len(supplies)}


# ---------------------------------------------------------------------------
# SECTION 6 - Cout des premiers acheteurs
# ---------------------------------------------------------------------------


def section_6(tokens: list[dict], graduations: dict[str, float]) -> dict:
    start_section(6, "Cout des premiers acheteurs")
    per_token_calls: list[int] = []
    buyers_counts: list[int] = []
    windows = Counter()
    total_buyers = 0
    capped_tokens = 0

    for token in tokens[:EARLY_BUYER_TOKENS]:
        mint = token["mint"]
        curve, created = find_bonding_curve(mint)
        graduated = graduations.get(mint) or graduation_time(token["pool_address"])
        if not curve or not created:
            log.info("  %-10s : courbe introuvable, ignore",
                     token.get("symbol") or "?")
            continue
        end = graduated or (created + 86400)

        buyers: set[str] = set()
        transactions_seen: set[str] = set()
        token_calls = 0
        page_token = None
        stopped = "fin"
        for page in range(EARLY_BUYER_MAX_PAGES):
            config = {"limit": 100, "sortOrder": "asc"}
            if page_token:
                config["paginationToken"] = page_token
            payload = transfers(curve, config)
            if payload == "CAPPED":
                stopped = "plafond global"
                break
            token_calls += 1
            rows = rows_of(payload)
            if not rows:
                break
            past_end = False
            for line in rows:
                moment = _line_time(line)
                if moment > end:
                    past_end = True
                    continue
                signature = line.get("signature")
                if isinstance(signature, str):
                    transactions_seen.add(signature)
                if line.get("mint") == mint:
                    buyer = line.get("toUserAccount")
                    if isinstance(buyer, str) and buyer != curve:
                        if buyer not in buyers:
                            buyers.add(buyer)
                            delta = moment - created
                            if delta <= 60:
                                windows["60 s"] += 1
                            if delta <= 300:
                                windows["5 min"] += 1
                            if delta <= 1800:
                                windows["30 min"] += 1
            if past_end:
                stopped = "graduation atteinte"
                break
            result = payload.get("result") if isinstance(payload, dict) else None
            page_token = helius.pagination_token(result or {})
            if not page_token:
                break
        else:
            stopped = "plafond de pages"
            capped_tokens += 1

        per_token_calls.append(token_calls)
        buyers_counts.append(len(buyers))
        total_buyers += len(buyers)
        log.info("  %-10s : %d tx, %d acheteurs distincts, %d appels (%s)",
                 token.get("symbol") or "?", len(transactions_seen),
                 len(buyers), token_calls, stopped)

    if per_token_calls:
        print(f"\nappels par token : mediane {statistics.median(per_token_calls):.0f} "
              f"| max {max(per_token_calls)}")
    if buyers_counts:
        print(f"acheteurs distincts avant graduation : mediane "
              f"{statistics.median(buyers_counts):.0f} | max {max(buyers_counts)}")
    if total_buyers:
        print("part des acheteurs dans les premieres :")
        for label in ("60 s", "5 min", "30 min"):
            share = 100 * windows[label] / total_buyers
            print(f"    {label:>7} : {windows[label]:4d} ({share:5.1f} %)")
    if capped_tokens:
        log.warning("%d token(s) ont atteint le plafond de %d pages",
                    capped_tokens, EARLY_BUYER_MAX_PAGES)
    return {"calls": per_token_calls, "buyers": buyers_counts,
            "windows": dict(windows), "capped": capped_tokens}


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


def credits_spent() -> int:
    return sum(_calls[m] * CREDIT_COST.get(m, 0) for m in _calls)


def show_budget(title: str) -> None:
    print(f"\n{title}")
    for method, count in sorted(_calls.items()):
        cost = CREDIT_COST.get(method, 0)
        cap = CAPS_GLOBAL.get(method)
        cap_text = f" / plafond {cap}" if cap else ""
        print(f"  {method:28} : {count:5d} appels{cap_text}"
              f" -> {count * cost:>7,} credits ({cost}/appel)")
    print(f"  {'TOTAL':28} : {credits_spent():>7,} credits")


def final_recap(results: dict) -> None:
    print("\n" + "=" * 74)
    print("RECAPITULATIF (a copier-coller)")
    print("=" * 74)

    provenance = results.get("provenance", "?")
    print(f"\nEchantillon : {provenance}")
    if "pumpswap" not in provenance:
        print("  ATTENTION : echantillon hors PumpSwap. Les sections 3 et 6 "
              "mesurent un DEX sans bonding curve.")

    s2 = results.get("s2") or {}
    if s2:
        print(f"\nPrix a un instant (section 2) : "
              f"{s2['obtained']}/{s2['attempts']} points obtenus, "
              f"{s2['per_point']:.2f} appel(s) par point")

    s3 = results.get("s3") or {}
    if s3:
        durations = s3.get("durations") or []
        print(f"Bonding curve (section 3) : trouvee {s3['found']} fois"
              + (f", graduation mediane {statistics.median(durations):.1f} h"
                 if durations else ""))

    s4 = results.get("s4") or {}
    new_pools = (s4.get("new_pools") or {})
    if new_pools:
        print(f"Listage retroactif (section 4) : new_pools couvre "
              f"{new_pools.get('span_hours', 0):.2f} h sur "
              f"{new_pools.get('pages', 0)} pages "
              f"({new_pools.get('rate_per_hour', 0):.0f} pools/h)")
    print(f"  comptes recurrents a la creation : "
          f"{len(s4.get('shared_accounts') or [])}")

    s5 = results.get("s5") or {}
    per_token_5 = (s5.get("calls", 0) / s5["sample"]) if s5.get("sample") else 0
    if s5:
        measured = s5["alive"] + s5["dead"]
        print(f"Trajectoire (section 5) : {s5['alive']}/{measured} tokens avec "
              f"au moins un prix, {per_token_5:.1f} appels par token")

    s6 = results.get("s6") or {}
    median_6 = statistics.median(s6["calls"]) if s6.get("calls") else 0
    if s6:
        print(f"Premiers acheteurs (section 6) : mediane {median_6:.0f} appels "
              f"par token")

    show_budget("Consommation reelle de cette sonde :")

    # --- Projection --------------------------------------------------------
    print(f"\nPROJECTION sur {MONTHLY_CREDITS:,} credits/mois")
    print("  Hypotheses de cout a recouper avec le dashboard Helius :")
    print(f"    getTransfersByAddress {CREDIT_COST['getTransfersByAddress']} "
          f"credits/appel, getTransactionsForAddress "
          f"{CREDIT_COST['getTransactionsForAddress']}")

    unit = CREDIT_COST["getTransfersByAddress"]
    lines = []
    if new_pools.get("rate_per_hour"):
        per_day_pools = new_pools["rate_per_hour"] * 24
        lines.append(("(a) lister une journee",
                      "non chiffrable : new_pools ne remonte pas 24 h "
                      f"(~{per_day_pools:.0f} pools/j observes)"))
    else:
        lines.append(("(a) lister une journee", "source non etablie"))

    if per_token_5 > 0:
        capacity = MONTHLY_CREDITS / (per_token_5 * unit)
        lines.append(("(b) suivre en trajectoire",
                      f"{capacity:,.0f} tokens/mois "
                      f"({per_token_5:.1f} appels x {unit} credits)"))
    if median_6 > 0:
        capacity = MONTHLY_CREDITS / (median_6 * unit)
        lines.append(("(c) + premiers acheteurs",
                      f"{capacity:,.0f} tokens/mois "
                      f"({median_6:.0f} appels x {unit} credits)"))
    if per_token_5 > 0 and median_6 > 0:
        both = per_token_5 + median_6
        capacity = MONTHLY_CREDITS / (both * unit)
        lines.append(("(b)+(c) les deux",
                      f"{capacity:,.0f} tokens/mois"))
    for label, text in lines:
        print(f"  {label:28} : {text}")

    print("\n  Le prix du SOL reutilise de v2 est en bougies JOURNALIERES : "
          "les mcap\n  intra-journalieres de la section 5 en heritent d'une "
          "imprecision.")
    if _capped:
        print(f"\n  {len(_capped)} plafond(s) atteint(s) : les chiffres "
              "correspondants sont des minorants.")


def main() -> None:
    setup_logging()
    diagnose_environment()
    present = bool(os.environ.get("HELIUS_API_KEY", "").strip())
    log.info("  %-18s : %s", "HELIUS_API_KEY",
             "presente" if present else "ABSENTE")
    helius.api_key()  # leve si absente

    print("\nSonde univers : mesure de faisabilite et de cout.")
    print("Aucune ecriture en base, aucune conclusion de trading.")
    print(f"Plafonds globaux : {CAPS_GLOBAL}")
    show_budget("Cout unitaire annonce (a recouper avec le dashboard) :")

    if not v2.load_sol_prices():
        log.warning("Prix du SOL indisponible : les conversions USD des "
                    "sections 2 et 5 seront sautees.")

    tokens, provenance = pick_tokens(TOKENS_SECTION_2)
    if not tokens:
        log.error("Aucun token exploitable, sonde interrompue.")
        return
    reference = tokens[0]["pool_address"]

    results: dict[str, Any] = {"provenance": provenance}
    results["s1"] = section_1(reference)
    results["s2"] = section_2(tokens)
    section_2_control(tokens)
    results["s3"] = section_3(tokens)
    graduations = results["s3"].get("graduations") or {}
    results["s4"] = section_4(tokens)
    results["s5"] = section_5(tokens, graduations)
    results["s6"] = section_6(tokens, graduations)

    final_recap(results)


if __name__ == "__main__":
    main()
