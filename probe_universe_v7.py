"""Sonde jetable v7 : reparer la liste, et definir la graduation.

Script d'observation, lance via RUN_MODE=probe_universe_v7. Aucune ecriture
en base HORS sol_run_log, aucune conclusion de trading.

Memes regles de conduite que la v6 : une regle est VALIDEE avant d'etre
appliquee, toute conclusion est verifiee contre ses propres nombres, et
une section qui depend d'une regle non validee s'arrete.

Acquis du run 10:08 : regle du pool validee 9/10 en format Enhanced ;
476 des 683 transactions brutes de 9C4nRvhh ont exactement un pool
candidat ; 20 % des signatures propres a 39azUYFW ont 9C4nRvhh pour
feePayer, les autres des feePayers inconnus.

Ecarts releves AVANT ecriture, et ce qui a ete decide :

  1. LE SEUIL N'EST PAS EN CAUSE. La v6 teste `exact >= VALIDATION_MIN`
     avec VALIDATION_MIN = 9 : 9/10 validait deja, et la reproduction le
     confirme. Ce qui a ferme la section C, c'est la garde de coherence
     "regle validee mais aucun pool trouve" : la liste etait VIDE malgre
     476 transactions a un pool unique, parce que l'entree exigeait
     `isinstance(row["signature"], str)` et que la signature n'est pas a
     la racine de la ligne brute. C'est exactement ce que repare la
     section A. Le seuil reste `>= 9/10`, ecrit et logue comme tel.
  2. Les 857 signatures propres a 39azUYFW SONT persistees par la v6
     (section B, champ signatures_propres) : la v7 les relit au lieu de
     re-scanner la journee. Repli explicite et logue sinon.
  3. getTransaction n'a pas de cout publie a cote des methodes Helius.
     La documentation de facturation indique 1 credit pour un appel RPC
     standard et 10 pour une lecture ARCHIVALE ; la sonde compte 10 par
     prudence, et 30 appels au plafond font 300 credits dans les deux cas.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

import geckoterminal as gt
import helius
import solana_addr
import supabase_client as db
from config import (
    ANALYZED_TABLE,
    RUN_LOG_TABLE,
    SOL_MINTS,
    diagnose_environment,
    setup_logging,
)

log = logging.getLogger("solana-agent")

RUN_MODE = "probe_universe_v7"
SOURCE_RUN_MODE = "probe_universe_v4"    # comptes et liste datee
V6_RUN_MODE = "probe_universe_v6"        # signatures propres a 39azUYFW

PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
FEE_PREFIX = "9C4nRvhh"
BACKUP_PREFIX = "39azUYFW"

WSOL_MINT = "So11111111111111111111111111111111111111112"
STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
    "USDSwr9ApdHk5bvJKMjzff41FfuX8bSxdKcR81vTwcA",    # USDS
    "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",   # PYUSD
}
IGNORED_MINTS = SOL_MINTS | STABLE_MINTS

CAPS_GLOBAL = {
    "getTransfersByAddress": 400,
    "getTransactionsForAddress": 5,
    "enhanced": 4,
    "getTransaction": 30,
    "coingecko": 5,
}
CAPS_SECTION = {
    "prix": {"coingecko": 5},
    "A": {"getTransfersByAddress": 12, "getTransactionsForAddress": 4,
          "getTransaction": 12},
    "B": {"getTransfersByAddress": 60, "enhanced": 2, "getTransaction": 6},
    "C": {"getTransfersByAddress": 320},
}

CREDIT_COST = {
    "getTransfersByAddress": 10,
    "getTransactionsForAddress": 100,
    "enhanced": 100,     # doc Helius : "Credit cost: 100 credits per call"
    "getTransaction": 10,  # 1 en RPC standard, 10 en lecture archivale
    "getTokenSupply": 1,
    "coingecko": 0,
}

MONTHLY_CREDITS = 1_000_000

TOKENS_VALIDATION = 10
VALIDATION_MIN = 9          # une regle est validee a >= 9/10
TRACE_SAMPLES = 3           # transactions dont chaque champ est affiche
SAMPLE_SIZE = 30
B_SAMPLE = 100
DAY_MAX_PAGES = 60
ENRICH_BATCH = 100
RANDOM_SEED = 20260926

TARGET_DAY = "2026-09-17"
WATCHED_SYMBOLS = ("CATE", "Martians")

TRAJECTORY_POINTS = (
    ("5 min", 300), ("15 min", 900), ("30 min", 1800), ("1 h", 3600),
    ("3 h", 10800), ("6 h", 21600), ("24 h", 86400), ("7 j", 604800),
)
SCREENS = (
    ("1 point", ("6 h",)),
    ("2 points", ("1 h", "24 h")),
    ("3 points", ("30 min", "6 h", "24 h")),
)
DEAD_RATIO = 0.30
MCAP_THRESHOLDS = (25_000, 50_000, 100_000, 250_000)

REGIME_PROBE = "sonde"
REGIME_CRUISE = "croisiere"

_calls: Counter = Counter()
_section_calls: Counter = Counter()
_regime_calls: dict[str, Counter] = {REGIME_PROBE: Counter(),
                                     REGIME_CRUISE: Counter()}
_regime = REGIME_PROBE
_current_section = "?"
_capped: set[tuple[str, str]] = set()
_incoherences: list[str] = []
_run_at = ""


# ---------------------------------------------------------------------------
# Budget, en distinguant sonde et regime de croisiere
# ---------------------------------------------------------------------------


def start_section(letter: str, title: str) -> None:
    global _current_section, _section_calls
    _current_section = letter
    _section_calls = Counter()
    print("\n" + "=" * 74)
    print(f"SECTION {letter} - {title}")
    print("=" * 74)


def set_regime(regime: str) -> None:
    """Un appel qu'un run quotidien referait est en regime de croisiere."""
    global _regime
    _regime = regime


def can_spend(method: str) -> bool:
    if _calls[method] >= CAPS_GLOBAL.get(method, 10**9):
        key = ("*", method)
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
            log.warning("PLAFOND de section %s atteint : %s (%d appels), "
                        "section interrompue", _current_section, method, limit)
        return False
    return True


def _spend(method: str) -> None:
    _calls[method] += 1
    _section_calls[method] += 1
    _regime_calls[_regime][method] += 1


def _masked(text: str) -> str:
    key = os.environ.get("HELIUS_API_KEY", "").strip()
    return text.replace(key, "***") if key else text


def _as_json(value: Any) -> str:
    try:
        return _masked(json.dumps(value, indent=2, ensure_ascii=False,
                                  default=str))
    except (TypeError, ValueError):
        return _masked(repr(value))


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


def _day_bounds(day: str) -> tuple[float, float]:
    start = datetime.fromisoformat(day + "T00:00:00+00:00").timestamp()
    return start, start + 86400


# ---------------------------------------------------------------------------
# Journal sol_run_log
# ---------------------------------------------------------------------------


def log_run(section: str, label: str, payload: dict) -> None:
    """Ecrit une mesure. Un echec est logue puis releve, jamais avale."""
    try:
        db.insert_run_log(RUN_MODE, _run_at, section, label, payload)
    except Exception as error:               # noqa: BLE001
        log.error("PERTE : %s non ecrit dans %s (section %s) : %s",
                  label, RUN_LOG_TABLE, section, error)
        raise


CREATE_SQL = """create table if not exists sol_run_log (
  id       bigserial primary key,
  run_mode text not null,
  run_at   timestamptz not null default now(),
  section  text,
  label    text,
  payload  jsonb
);"""


def check_run_log() -> bool:
    try:
        log_run("run", "debut", {"caps": CAPS_GLOBAL, "couts": CREDIT_COST,
                                 "jour_cible": TARGET_DAY})
    except Exception as error:               # noqa: BLE001
        print(f"\nsol_run_log inutilisable : {error}")
        print("La sonde s'arrete AVANT de depenser le moindre credit.")
        print("SQL de creation attendu :\n" + CREATE_SQL)
        return False
    print(f"sol_run_log : ecriture confirmee (run_at={_run_at})")
    return True


# ---------------------------------------------------------------------------
# Appels comptabilises
# ---------------------------------------------------------------------------


def transfers(address: str, config: dict) -> Any | None:
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
    if payload in (None, "CAPPED") or not isinstance(payload, dict):
        return None
    if "error" in payload:
        return None
    result = payload.get("result")
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ("data", "items", "transactions"):
            value = result.get(key)
            if isinstance(value, list):
                return value
    return None


def error_of(payload: Any) -> str:
    if isinstance(payload, dict) and "error" in payload:
        return _as_json(payload["error"])[:300].replace("\n", " ")
    return ""


def next_page_token(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    return helius.pagination_token(result)


def group_by_signature(lines: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for line in lines:
        if isinstance(line, dict) and isinstance(line.get("signature"), str):
            groups[line["signature"]].append(line)
    return groups


def swap_price(lines: list[dict]) -> tuple[float, str] | None:
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


def candidate_mints(node: Any, found: set[str] | None = None) -> set[str]:
    """Mints d'un payload, hors SOL, WSOL et stablecoins."""
    if found is None:
        found = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "mint" and isinstance(value, str) and value:
                if value not in IGNORED_MINTS:
                    found.add(value)
            else:
                candidate_mints(value, found)
    elif isinstance(node, list):
        for item in node:
            candidate_mints(item, found)
    return found


# ---------------------------------------------------------------------------
# Prix du SOL. UNE page horaire suffit : 41 jours de couverture pour un
# echantillon du 17/09, et deux appels CoinGecko de marge sous le plafond.
# ---------------------------------------------------------------------------

HOURLY_PAGES = 1

_sol_hourly: list[list] = []
_sol_daily: list[list] = []
_sol_misses = 0
_sol_daily_used = 0


def load_sol_prices() -> dict:
    """Bougies horaires (~41 j), repli journalier au-dela."""
    global _sol_hourly, _sol_daily
    start_section("prix", "Prix du SOL (1 page horaire, repli journalier)")

    result = gecko(gt.token_pools, WSOL_MINT)
    if result is None:
        log.error("PERTE : pools du SOL indisponibles")
        return {}
    pools, _ = result
    best = None
    best_liquidity = -1.0
    for pool in pools:
        attributes = pool.get("attributes") or {}
        base_id = (pool.get("relationships", {}).get("base_token", {})
                   .get("data", {}).get("id", ""))
        if not base_id.endswith(WSOL_MINT):
            continue
        liquidity = _to_float(attributes.get("reserve_in_usd"))
        if liquidity > best_liquidity:
            best, best_liquidity = pool, liquidity
    if best is None:
        log.error("PERTE : aucun pool avec le SOL en base token")
        return {}
    attributes = best.get("attributes") or {}
    address = attributes.get("address") or best.get("id", "").split("_", 1)[-1]

    collected: list[list] = []
    before = None
    for page in range(HOURLY_PAGES):
        candles = gecko(gt.ohlcv, address, "hour", 1000, before)
        if not candles:
            break
        collected += candles
        before = int(min(_to_float(c[0]) for c in candles)) - 1
    _sol_hourly = sorted(collected, key=lambda c: _to_float(c[0]))
    days = 0.0
    if _sol_hourly:
        days = (_to_float(_sol_hourly[-1][0])
                - _to_float(_sol_hourly[0][0])) / 86400
    _sol_daily = sorted(gecko(gt.ohlcv, address, "day", 365) or [],
                        key=lambda c: _to_float(c[0]))
    print(f"  {len(_sol_hourly)} bougies horaires ({days:.0f} jours), "
          f"{len(_sol_daily)} bougies journalieres en repli")
    return {"horaire": len(_sol_hourly), "jours": round(days, 1),
            "journalier": len(_sol_daily)}


def sol_price_at(moment: float) -> float | None:
    global _sol_misses, _sol_daily_used
    if _sol_hourly:
        closest = min(_sol_hourly, key=lambda c: abs(_to_float(c[0]) - moment))
        if abs(_to_float(closest[0]) - moment) <= 3600:
            price = _to_float(closest[4])
            if price:
                return price
    if _sol_daily:
        closest = min(_sol_daily, key=lambda c: abs(_to_float(c[0]) - moment))
        if abs(_to_float(closest[0]) - moment) <= 86400:
            price = _to_float(closest[4])
            if price:
                _sol_daily_used += 1
                return price
    _sol_misses += 1
    log.warning("Conversion hors couverture : %s", _iso(moment))
    return None


def tolerance_for(delta: float) -> float:
    return max(900.0, delta * 0.25)


def page_price(address: str, moment: float,
               tolerance: float) -> tuple[float | None, int, int]:
    """(prix median en SOL des swaps de la page, swaps retenus, appels)."""
    payload = transfers(address, {
        "limit": 100, "sortOrder": "asc",
        "filters": {"blockTime": {"gte": int(moment)}},
    })
    if payload == "CAPPED":
        return None, 0, 0
    rows = rows_of(payload)
    if rows is None:
        return None, 0, 1
    prices: list[float] = []
    for lines in group_by_signature(rows).values():
        stamps = [_line_time(line) for line in lines if _line_time(line) > 0]
        when = max(stamps) if stamps else 0.0
        if when and when - moment > tolerance:
            continue
        priced = swap_price(lines)
        if priced:
            prices.append(priced[0])
    if not prices:
        return None, 0, 1
    return statistics.median(prices), len(prices), 1






# ---------------------------------------------------------------------------
# Coherence : une conclusion qui contredit ses nombres ne sert plus a rien
# ---------------------------------------------------------------------------


def coherent(label: str, condition: bool, detail: str) -> bool:
    """Verifie une conclusion contre ses propres nombres."""
    if condition:
        return True
    _incoherences.append(f"{label} : {detail}")
    print(f"    INCOHERENT - {label} : {detail}")
    log.warning("INCOHERENCE : %s (%s)", label, detail)
    return False


def wilson(hits: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Intervalle de confiance a 95 % d'une proportion, sans dependance."""
    if total <= 0:
        return 0.0, 0.0
    ratio = hits / total
    divisor = 1 + z * z / total
    centre = (ratio + z * z / (2 * total)) / divisor
    half = z * math.sqrt(ratio * (1 - ratio) / total
                         + z * z / (4 * total * total)) / divisor
    return max(0.0, centre - half), min(1.0, centre + half)


# ---------------------------------------------------------------------------
# Relecture de ce que les sondes precedentes ont etabli
# ---------------------------------------------------------------------------


def load_accounts() -> dict[str, str]:
    raw = os.environ.get("MIGRATION_ACCOUNTS", "").strip()
    addresses = [a.strip() for a in raw.split(",") if a.strip()] if raw else []
    if not addresses:
        try:
            rows = db.fetch_run_log(SOURCE_RUN_MODE, "A", 5)
        except Exception as error:           # noqa: BLE001
            log.error("PERTE : relecture des comptes impossible : %s", error)
            rows = []
        for row in rows:
            payload = row.get("payload") or {}
            addresses = [a for a in (payload.get("frais"),
                                     payload.get("secours")) if a]
            if addresses:
                break
    mapped: dict[str, str] = {}
    for address in addresses:
        if address.startswith(FEE_PREFIX):
            mapped["frais"] = address
        elif address.startswith(BACKUP_PREFIX):
            mapped["secours"] = address
    return mapped


def load_v4_listing() -> list[dict]:
    """Liste datee de la v4 : sert de reference de mints et de signatures."""
    try:
        rows = db.fetch_run_log(SOURCE_RUN_MODE, "B", 5)
    except Exception as error:               # noqa: BLE001
        log.error("PERTE : relecture de la liste v4 impossible : %s", error)
        return []
    for row in rows:
        payload = row.get("payload") or {}
        listing = [e for e in (payload.get("listing") or [])
                   if isinstance(e, dict) and e.get("mint")]
        if listing:
            print(f"  liste v4 relue : {len(listing)} graduations "
                  f"(run {str(row.get('run_at'))[:19]})")
            return listing
    print("  aucune liste v4 relue")
    return []


# ---------------------------------------------------------------------------
# La regle mint + pool, validee 9/10 par la v6 en format Enhanced
# ---------------------------------------------------------------------------


def deltas_from_raw(transaction: dict) -> list[tuple[str, str, float]]:
    """(mint, owner, variation) depuis meta.pre/postTokenBalances."""
    meta = transaction.get("meta")
    if not isinstance(meta, dict):
        return []
    before: dict[Any, tuple[str, str, float]] = {}
    for entry in meta.get("preTokenBalances") or []:
        if not isinstance(entry, dict):
            continue
        key = entry.get("accountIndex")
        amount = _to_float((entry.get("uiTokenAmount") or {}).get("uiAmount"))
        before[key] = (entry.get("mint") or "", entry.get("owner") or "",
                       amount)
    deltas: list[tuple[str, str, float]] = []
    for entry in meta.get("postTokenBalances") or []:
        if not isinstance(entry, dict):
            continue
        key = entry.get("accountIndex")
        mint = entry.get("mint") or ""
        owner = entry.get("owner") or ""
        amount = _to_float((entry.get("uiTokenAmount") or {}).get("uiAmount"))
        previous = before.get(key)
        deltas.append((mint, owner, amount - (previous[2] if previous else 0.0)))
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
            amount = _to_float(raw.get("tokenAmount"))
            decimals = int(_to_float(raw.get("decimals")))
            if decimals:
                amount /= 10 ** decimals
            deltas.append((change.get("mint") or "",
                           change.get("userAccount") or "", amount))
    return deltas


def mint_and_pool(deltas: list[tuple[str, str, float]]) -> dict:
    """Mint gradue et pool, par la regle du ticket.

    mint  = le mint hors SOL / WSOL / stablecoin dont un compte AUGMENTE ;
    pool  = le owner qui voit augmenter a la fois un compte de ce mint ET
            un compte WSOL. Un seul owner doit remplir les deux conditions.
    """
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
            "pools": len(pools), "mints": len(gains),
            "candidats": pools[:3]}




# ---------------------------------------------------------------------------
# SECTION A - Reparer la liste [mint, pool, horodatage, signature]
# ---------------------------------------------------------------------------


def get_transaction(signature: str) -> Any | None:
    """getTransaction standard, format brut (meta.pre/postTokenBalances)."""
    if not can_spend("getTransaction"):
        return "CAPPED"
    _spend("getTransaction")
    return helius.rpc("getTransaction", [
        signature,
        {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
    ])


def object_of(payload: Any) -> dict | None:
    """result d'un appel qui rend un OBJET et non une liste."""
    if payload in (None, "CAPPED") or not isinstance(payload, dict):
        return None
    if "error" in payload:
        return None
    result = payload.get("result")
    return result if isinstance(result, dict) else None


def extract_signature(row: dict) -> tuple[str | None, str]:
    """(signature, chemin). La v6 n'essayait QUE la racine, d'ou sa liste vide."""
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
        value = _to_float(row.get(key))
        if value:
            return value, key
    meta = row.get("meta")
    if isinstance(meta, dict):
        value = _to_float(meta.get("blockTime"))
        if value:
            return value, "meta.blockTime"
    return 0.0, "introuvable"


def fee_payer_of(row: dict) -> str | None:
    """Premier compte signataire d'une transaction brute."""
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


def trace_extraction(row: dict, rank: int) -> str:
    """Affiche chaque champ extrait et dit ou l'entree disparait."""
    print(f"\n    --- transaction {rank} ---")
    print(f"    cles de premier niveau : "
          f"{sorted(row.keys()) if isinstance(row, dict) else type(row)}")
    signature, path_signature = extract_signature(row)
    when, path_time = extract_time(row)
    outcome = mint_and_pool(deltas_from_raw(row))
    print(f"    signature : {str(signature)[:24]}.. (lue en {path_signature})")
    print(f"    blockTime : {when:.0f} -> {_iso(when)[:19] if when else 'aucun'}"
          f" (lu en {path_time})")
    print(f"    mint      : {str(outcome['mint'])[:24]}")
    print(f"    pool      : {str(outcome['pool'])[:24]} "
          f"({outcome['pools']} candidat(s))")
    if signature is None:
        step = "la signature n'est pas a la racine : c'est la que la v6 perdait l'entree"
    elif not when:
        step = "l'horodatage manque"
    elif not outcome["mint"]:
        step = "aucun mint en hausse"
    elif not outcome["pool"]:
        step = f"{outcome['pools']} pool(s) candidat(s), il en faut exactement un"
    else:
        step = "entree COMPLETE"
    print(f"    -> {step}")
    return step


def fetch_day_raw(account: str, day: str) -> dict:
    """La journee en getTransactionsForAddress full / limit 1000."""
    start, end = _day_bounds(day)
    config = {"limit": 1000, "sortOrder": "asc", "transactionDetails": "full",
              "filters": {"blockTime": {"gte": int(start), "lte": int(end)}}}
    set_regime(REGIME_CRUISE)
    payload = transactions(account, config)
    calls = 1
    rows = rows_of(payload)
    if rows is not None:
        page_token = next_page_token(payload)
        while page_token:
            follow = dict(config)
            follow["paginationToken"] = page_token
            more = transactions(account, follow)
            if more == "CAPPED":
                break
            calls += 1
            extra = rows_of(more)
            if not extra:
                break
            rows += extra
            page_token = next_page_token(more)
    set_regime(REGIME_PROBE)
    return {"rows": rows, "calls": calls,
            "erreur": error_of(payload) if rows is None else ""}


def validate_raw_rule(tokens: list[dict]) -> dict:
    """Format BRUT : getTransaction sur 10 migrations dont on sait le pool."""
    print(f"\n--- VALIDATION FORMAT BRUT sur {len(tokens)} tokens "
          f"(getTransaction) ---")
    exact = 0
    tested = 0
    details = []
    fetched: dict[str, dict] = {}
    for token in tokens:
        payload = transfers(token["pool_address"], {"limit": 1,
                                                    "sortOrder": "asc"})
        rows = rows_of(payload)
        if not rows:
            continue
        signature = rows[0].get("signature")
        if not isinstance(signature, str):
            continue
        raw = object_of(get_transaction(signature))
        if raw is None:
            print(f"    {str(token.get('symbol')):>10} : getTransaction "
                  f"indisponible")
            continue
        tested += 1
        fetched[token["mint"]] = raw
        outcome = mint_and_pool(deltas_from_raw(raw))
        ok = outcome["pool"] == token["pool_address"]
        exact += 1 if ok else 0
        print(f"    {str(token.get('symbol')):>10} : pool "
              f"{str(outcome['pool'])[:12]}.. | attendu "
              f"{token['pool_address'][:12]}.. | "
              f"{'EXACT' if ok else 'FAUX'} ({outcome['pools']} candidat(s))")
        details.append({"symbol": token.get("symbol"), "exact": ok,
                        "mint": token["mint"]})
    rate = exact / tested if tested else 0.0
    valide = exact >= VALIDATION_MIN
    print(f"  validation format brut : {exact}/{tested} ({rate:.0%}) "
          f"-> {'VALIDEE' if valide else 'NON validee'} "
          f"(seuil >= {VALIDATION_MIN}/10)")
    return {"testes": tested, "exacts": exact, "taux": round(rate, 3),
            "valide": valide, "details": details, "brutes": fetched}


def section_a(account: str, tokens: list[dict],
              reference: list[dict]) -> dict:
    start_section("A", "Reparer la liste [mint, pool, horodatage, signature]")
    print("  La v6 exigeait la signature A LA RACINE de la ligne brute. Le "
          "seuil de validation, lui, acceptait bien 9/10 : c'est la liste "
          "vide qui a ferme la suite.")

    day = fetch_day_raw(account, TARGET_DAY)
    rows = day["rows"]
    if rows is None:
        print(f"  REJET ou PERTE : {day['erreur'] or 'payload inattendu'}")
        return {"exploitable": False, "appels": day["calls"]}
    print(f"\n  {len(rows)} transactions brutes en {day['calls']} appel(s)")

    # Les transactions a UN pool : celles que la v6 comptait sans les garder.
    single = [r for r in rows if isinstance(r, dict)
              and mint_and_pool(deltas_from_raw(r))["pools"] == 1]
    print(f"  dont {len(single)} a exactement un pool candidat")
    print(f"\n  --- chaque champ extrait, sur {TRACE_SAMPLES} d'entre elles "
          f"---")
    steps = []
    for rank, row in enumerate(single[:TRACE_SAMPLES], start=1):
        steps.append(trace_extraction(row, rank))

    # Entonnoir : ou les entrees se perdent, motif par motif.
    funnel: Counter = Counter()
    listing: list[dict] = []
    paths: Counter = Counter()
    for row in rows:
        if not isinstance(row, dict):
            funnel["ligne_inattendue"] += 1
            continue
        outcome = mint_and_pool(deltas_from_raw(row))
        if not outcome["mint"]:
            funnel["aucun_mint"] += 1
            continue
        if outcome["pools"] != 1:
            funnel["pool_absent" if outcome["pools"] == 0
                   else "pools_multiples"] += 1
            continue
        signature, path = extract_signature(row)
        paths[path] += 1
        if signature is None:
            funnel["signature_introuvable"] += 1
            continue
        when, _ = extract_time(row)
        if not when:
            funnel["horodatage_introuvable"] += 1
            continue
        funnel["retenue"] += 1
        listing.append({"mint": outcome["mint"], "pool": outcome["pool"],
                        "signature": signature, "time": when,
                        "heure": _iso(when)[11:19],
                        "payeur": fee_payer_of(row)})

    print("\n  entonnoir d'extraction :")
    for motif, count in funnel.most_common():
        print(f"    {motif:24} : {count:4d}")
    coherent("somme de l'entonnoir", sum(funnel.values()) == len(rows),
             f"{sum(funnel.values())} classees pour {len(rows)} transactions")
    coherent("retenues <= un pool", funnel["retenue"] <= len(single),
             f"{funnel['retenue']} retenues pour {len(single)} a un pool")
    print(f"  signature lue en : {dict(paths)}")
    print(f"  LISTE REPAREE : {len(listing)} entrees "
          f"(la v6 en produisait 0)")

    validation = validate_raw_rule(tokens[:TOKENS_VALIDATION])

    by_signature = {e["signature"]: e["mint"] for e in reference
                    if e.get("signature")}
    shared = [e for e in listing if e["signature"] in by_signature]
    agree = sum(1 for e in shared if by_signature[e["signature"]] == e["mint"])
    rate = 100 * agree / len(shared) if shared else 0
    print(f"\n  concordance des mints avec la liste v4 : {agree}/{len(shared)} "
          f"({rate:.1f} %)")

    print("\n  CONCLUSION :")
    validated = validation["valide"]
    usable = coherent(
        "regle validee mais liste vide",
        not validated or bool(listing),
        f"validation {validation['exacts']}/{validation['testes']} mais "
        f"{len(listing)} entree(s)",
    ) and validated
    print(f"    regle {'VALIDEE' if validated else 'NON validee'} "
          f"({validation['exacts']}/{validation['testes']}, seuil "
          f">= {VALIDATION_MIN}) | liste {len(listing)} entrees "
          f"-> sections B et C {'ouvertes' if usable else 'fermees'}")
    return {"exploitable": True, "appels": day["calls"],
            "transactions": len(rows), "un_pool": len(single),
            "entonnoir": dict(funnel), "chemins_signature": dict(paths),
            "etapes_tracees": steps, "listing": listing,
            "validation": {k: v for k, v in validation.items()
                           if k != "brutes"},
            "concordance": round(rate, 1), "communes": len(shared),
            "identiques": agree, "utilisable": usable,
            "_brutes": validation["brutes"], "_rows": rows}


# ---------------------------------------------------------------------------
# SECTION B - Definition de la graduation, independante du signataire
# ---------------------------------------------------------------------------


def curve_of(mint: str) -> str | None:
    """PDA de la bonding curve : seeds ["bonding-curve", mint]."""
    try:
        curve, _ = solana_addr.bonding_curve_address(mint, PUMPFUN_PROGRAM)
    except ValueError:
        return None
    return curve


def is_graduation(deltas: list[tuple[str, str, float]],
                  mint: str) -> tuple[bool, str | None]:
    """La courbe du mint CEDE-t-elle ses tokens dans cette transaction ?

    C'est la definition independante du signataire : peu importe qui paie
    les frais, une graduation vide la bonding curve de son mint.
    """
    curve = curve_of(mint)
    if not curve:
        return False, None
    for line_mint, owner, delta in deltas:
        if line_mint == mint and owner == curve and delta < 0:
            return True, curve
    return False, curve


def load_own_signatures() -> tuple[list[str], str]:
    """Les 857 signatures propres, relues dans sol_run_log (v6)."""
    try:
        rows = db.fetch_run_log(V6_RUN_MODE, "B", 5)
    except Exception as error:               # noqa: BLE001
        log.error("PERTE : relecture des signatures propres impossible : %s",
                  error)
        return [], "echec"
    for row in rows:
        payload = row.get("payload") or {}
        own = [s for s in (payload.get("signatures_propres") or [])
               if isinstance(s, str)]
        if own:
            print(f"  {len(own)} signatures propres relues dans "
                  f"{RUN_LOG_TABLE} (run {str(row.get('run_at'))[:19]})")
            return own, f"{RUN_LOG_TABLE}/{V6_RUN_MODE}"
    print(f"  aucune signature propre dans {RUN_LOG_TABLE}")
    return [], "absente"


def rescan_own(backup: str, known: set[str]) -> list[str]:
    """Repli COUTEUX : re-scan de la journee de 39azUYFW."""
    log.warning("Repli : re-scan de la journee de %s, environ %d credits",
                BACKUP_PREFIX, 25 * CREDIT_COST["getTransfersByAddress"])
    start, end = _day_bounds(TARGET_DAY)
    signatures: set[str] = set()
    page_token = None
    for _ in range(DAY_MAX_PAGES):
        config: dict[str, Any] = {
            "limit": 100, "sortOrder": "asc",
            "filters": {"blockTime": {"gte": int(start), "lte": int(end)}},
        }
        if page_token:
            config["paginationToken"] = page_token
        payload = transfers(backup, config)
        if payload == "CAPPED":
            break
        rows = rows_of(payload)
        if not rows:
            break
        for line in rows:
            signature = line.get("signature")
            if isinstance(signature, str):
                signatures.add(signature)
        page_token = next_page_token(payload)
        if not page_token:
            break
    return sorted(signatures - known)


CLASS_IN_A = "(a) migration deja dans la liste A"
CLASS_FEE_MISSING = "(a') signee par le compte de frais mais absente de A"
CLASS_OTHER = "(b) migration signee par un AUTRE compte"
CLASS_DIRECT = "(c) pool sans courbe, creation directe"


def section_b(section_a_result: dict, accounts: dict[str, str],
              tokens: list[dict], rng: random.Random) -> dict:
    start_section("B", "Definition de la graduation, sans le signataire")
    print("  Graduation = CREATE_POOL dans lequel le compte de la bonding "
          "curve du mint voit son solde de ce mint DIMINUER. Le signataire "
          "n'entre pas dans la definition.")

    listing = section_a_result.get("listing") or []
    rows = section_a_result.get("_rows") or []
    by_signature = {}
    for row in rows:
        if isinstance(row, dict):
            signature, _ = extract_signature(row)
            if signature:
                by_signature[signature] = row

    passed = 0
    no_curve = 0
    for entry in listing:
        row = by_signature.get(entry["signature"])
        if row is None:
            continue
        ok, curve = is_graduation(deltas_from_raw(row), entry["mint"])
        entry["graduation"] = ok
        entry["courbe"] = curve
        if ok:
            passed += 1
        elif curve is None:
            no_curve += 1
    print(f"\n  liste de la section A : {passed}/{len(listing)} passent le "
          f"test de la courbe")
    if no_curve:
        print(f"    dont {no_curve} sans PDA derivable")
    coherent("passants <= liste", passed <= len(listing),
             f"{passed} passants pour {len(listing)} entrees")

    own, provenance = load_own_signatures()
    backup = accounts.get("secours")
    if not own and backup:
        known = {e["signature"] for e in listing}
        own = rescan_own(backup, known)
        provenance = "repli : re-scan"
    if not own:
        print("  aucune signature propre exploitable : estimation impossible")
        return {"liste_passants": passed, "liste_totale": len(listing),
                "propres": 0}

    sample = rng.sample(own, min(B_SAMPLE, len(own)))
    print(f"\n--- {len(sample)} signatures propres tirees au hasard "
          f"({provenance}) ---")
    enriched = enhanced(sample) or []
    if not enriched:
        print("  enrichissement indisponible : section non concluante")
        return {"liste_passants": passed, "liste_totale": len(listing),
                "propres": len(own), "echantillon": 0}

    in_a = {e["signature"] for e in listing}
    fee_account = accounts.get("frais") or ""
    kinds: Counter = Counter()
    buckets: Counter = Counter()
    other_signers: Counter = Counter()
    for transaction in enriched:
        kind = str(transaction.get("type") or "?")
        source = str(transaction.get("source") or "?")
        kinds[f"{kind} / {source}"] += 1
        if kind != "CREATE_POOL":
            continue
        deltas = deltas_from_enhanced(transaction)
        outcome = mint_and_pool(deltas)
        mint = outcome["mint"]
        ok, _ = is_graduation(deltas, mint) if mint else (False, None)
        payer = transaction.get("feePayer")
        signature = transaction.get("signature")
        if not ok:
            buckets[CLASS_DIRECT] += 1
            continue
        if signature in in_a:
            buckets[CLASS_IN_A] += 1
        elif isinstance(payer, str) and payer == fee_account:
            buckets[CLASS_FEE_MISSING] += 1
        else:
            buckets[CLASS_OTHER] += 1
            if isinstance(payer, str):
                other_signers[payer] += 1

    print(f"\n  types rencontres ({len(enriched)} transactions) :")
    for label, count in kinds.most_common(6):
        print(f"    {label:<36} {count:4d}")
    print("\n  parmi les CREATE_POOL :")
    for label, count in buckets.most_common():
        print(f"    {label:<48} {count:4d}")
    coherent("somme des classes CREATE_POOL",
             sum(buckets.values()) <= len(enriched),
             f"{sum(buckets.values())} classees pour {len(enriched)} "
             f"transactions")
    if other_signers:
        print("\n  signataires a lire en plus du compte de frais :")
        for signer, count in other_signers.most_common(8):
            print(f"    {signer} x{count}")

    # Ce que l'echantillon ajoute : les graduations que la liste A n'a pas.
    graduations_sample = buckets[CLASS_OTHER] + buckets[CLASS_FEE_MISSING]
    low, high = wilson(graduations_sample, len(enriched))
    extra = len(own) * graduations_sample / len(enriched)
    total = passed + extra
    total_low = passed + len(own) * low
    total_high = passed + len(own) * high
    print(f"\n  GRADUATIONS REELLES DU {TARGET_DAY} :")
    print(f"    depuis la liste A (courbe cedee)     : {passed}")
    print(f"    estimees parmi les {len(own)} propres : {extra:.0f} "
          f"[{len(own) * low:.0f} ; {len(own) * high:.0f}]")
    print(f"    TOTAL : {total:.0f} [{total_low:.0f} ; {total_high:.0f}] "
          f"a 95 %")
    coherent("total >= liste A", total_low >= passed - 1,
             f"borne basse {total_low:.0f} sous les {passed} de la liste A")

    watched = _watched_tokens(section_a_result, tokens)

    to_read = [accounts.get("frais")] + [s for s, _ in
                                         other_signers.most_common(5)]
    print("\n  COMPTES A LIRE POUR ETRE COMPLET :")
    for account in [a for a in to_read if a]:
        print(f"    {account}")
    if not other_signers:
        print("    (le compte de frais suffit sur cet echantillon)")

    return {"liste_passants": passed, "liste_totale": len(listing),
            "graduations_validees": [e for e in listing
                                     if e.get("graduation")][:1000],
            "propres": len(own), "provenance": provenance,
            "echantillon": len(enriched), "types": dict(kinds.most_common(8)),
            "classes": dict(buckets), "autres_signataires":
            dict(other_signers.most_common(8)),
            "estimation": round(total), "borne_basse": round(total_low),
            "borne_haute": round(total_high), "surveilles": watched,
            "comptes_a_lire": [a for a in to_read if a]}


def _watched_tokens(section_a_result: dict, tokens: list[dict]) -> dict:
    """CATE et Martians : qui a signe, et la courbe cede-t-elle ?"""
    print(f"\n--- {' et '.join(WATCHED_SYMBOLS)} : signataire et courbe ---")
    fetched = section_a_result.get("_brutes") or {}
    outcome: dict[str, dict] = {}
    for token in tokens:
        symbol = str(token.get("symbol") or "")
        if symbol not in WATCHED_SYMBOLS:
            continue
        raw = fetched.get(token["mint"])
        if raw is None:
            payload = transfers(token["pool_address"], {"limit": 1,
                                                        "sortOrder": "asc"})
            rows = rows_of(payload)
            signature = rows[0].get("signature") if rows else None
            raw = (object_of(get_transaction(signature))
                   if isinstance(signature, str) else None)
        if raw is None:
            print(f"    {symbol:>10} : transaction de migration "
                  f"indisponible")
            outcome[symbol] = {}
            continue
        deltas = deltas_from_raw(raw)
        ok, curve = is_graduation(deltas, token["mint"])
        payer = fee_payer_of(raw)
        tag = ("compte de frais" if payer and payer.startswith(FEE_PREFIX)
               else "compte de secours" if payer
               and payer.startswith(BACKUP_PREFIX) else "AUTRE compte")
        print(f"    {symbol:>10} : signee par {str(payer)[:12]}.. ({tag}) | "
              f"courbe {str(curve)[:8]}.. cede ses tokens : "
              f"{'OUI' if ok else 'NON'}")
        outcome[symbol] = {"payeur": payer, "courbe_cede": ok}
    if not outcome:
        print(f"    aucun des symboles {WATCHED_SYMBOLS} n'est dans les "
              f"tokens de reference : cas non diagnostique")
    return outcome


# ---------------------------------------------------------------------------
# SECTION C - Echantillon aleatoire et criblages (sections C et D de la v6)
# ---------------------------------------------------------------------------


def measure_token(entry: dict) -> dict:
    """Trajectoire en SOL sur le pool valide. Mcap point par point."""
    now = datetime.now(timezone.utc).timestamp()
    graduated = _to_float(entry.get("time"))
    mint = entry["mint"]
    address = entry.get("pool") or mint
    calls = 0

    price, _, spent = page_price(address, graduated, tolerance_for(3600))
    calls += spent
    reference = price

    prices: dict[str, float] = {}
    mcaps: dict[str, float] = {}
    due = obtained = 0
    supply = _to_float((token_supply(mint) or {}).get("uiAmount"))

    for label, delta in TRAJECTORY_POINTS:
        moment = graduated + delta
        if moment > now:
            continue
        due += 1
        price, _, spent = page_price(address, moment, tolerance_for(delta))
        calls += spent
        if price is None:
            continue
        obtained += 1
        prices[label] = price
        usd = sol_price_at(moment)
        if usd and supply > 0:
            mcaps[label] = price * usd * supply

    verdict = "indetermine"
    if reference and "24 h" in prices:
        verdict = "mort" if prices["24 h"] < DEAD_RATIO * reference else "vivant"
    elif reference and due and not obtained:
        verdict = "muet"
    return {"mint": mint, "pool": address, "reference": reference,
            "prices": prices, "mcaps": mcaps, "due": due,
            "obtained": obtained, "calls": calls, "supply": supply,
            "mcap": max(mcaps.values(), default=0.0), "verdict": verdict}


def section_c(listing: list[dict], usable: bool,
              rng: random.Random) -> dict:
    start_section("C", "Echantillon aleatoire sur la liste validee")
    if not usable:
        print("  la regle de la section A n'est pas validee : SECTION "
              "ARRETEE. Mesurer des prix sur un pool non valide ne mesure "
              "rien.")
        return {"arretee": True, "raison": "regle non validee"}
    if not listing:
        print("  aucune liste [mint, pool] : SECTION ARRETEE.")
        return {"arretee": True, "raison": "liste vide"}

    sample = rng.sample(listing, min(SAMPLE_SIZE, len(listing)))
    print(f"  seed {RANDOM_SEED} | population {len(listing)} | "
          f"tires {len(sample)} (morts compris)\n")

    set_regime(REGIME_CRUISE)
    results = []
    for entry in sample:
        outcome = measure_token(entry)
        results.append(outcome)
        log.info("  %s.. %s : %s | %d/%d points | mcap max %.0f $",
                 entry["mint"][:8], entry.get("heure", "?"),
                 outcome["verdict"], outcome["obtained"], outcome["due"],
                 outcome["mcap"])
    set_regime(REGIME_PROBE)

    by_verdict: Counter = Counter(r["verdict"] for r in results)
    print(f"\n  classement : {dict(by_verdict)}")
    coherent("somme des classes", sum(by_verdict.values()) == len(results),
             f"{sum(by_verdict.values())} classes pour {len(results)} tokens")

    print("  TAUX DE SUCCES DU PRIX PAR CLASSE :")
    per_class: dict[str, dict] = {}
    for verdict in ("vivant", "mort", "muet", "indetermine"):
        group = [r for r in results if r["verdict"] == verdict]
        if not group:
            continue
        due = sum(r["due"] for r in group)
        obtained = sum(r["obtained"] for r in group)
        rate = 100 * obtained / due if due else 0
        coherent(f"points obtenus <= dus ({verdict})", obtained <= due,
                 f"{obtained} obtenus pour {due} dus")
        per_class[verdict] = {"tokens": len(group), "points": due,
                              "obtenus": obtained, "taux": round(rate, 1)}
        print(f"    {verdict:>12} : {obtained:4d}/{due:4d} points "
              f"({rate:5.1f} %) sur {len(group)} tokens")

    calls = [r["calls"] for r in results]
    print(f"  appels par token : mediane {statistics.median(calls):.1f} | "
          f"max {max(calls)}")

    mcaps = sorted(r["mcap"] for r in results if r["mcap"] > 0)
    if mcaps:
        print("  distribution des capitalisations max :")
        for label, value in (("min", mcaps[0]),
                             ("p25", mcaps[len(mcaps) // 4]),
                             ("mediane", statistics.median(mcaps)),
                             ("p75", mcaps[3 * len(mcaps) // 4]),
                             ("max", mcaps[-1])):
            print(f"    {label:>8} : {value:>14,.0f} $")
        buckets: Counter = Counter()
        for value in mcaps:
            if value < 10_000:
                buckets["< 10 k$"] += 1
            elif value < 100_000:
                buckets["10-100 k$"] += 1
            elif value < 1_000_000:
                buckets["100 k-1 M$"] += 1
            else:
                buckets["> 1 M$"] += 1
        print(f"    repartition : {dict(buckets)}")
        print(f"    ({len(results) - len(mcaps)} sans capitalisation : pas "
              f"de prix ou supply inconnue)")

    return {"arretee": False, "seed": RANDOM_SEED, "echantillon": len(sample),
            "verdicts": dict(by_verdict), "par_classe": per_class,
            "appels": calls, "mcaps": mcaps, "_results": results}


# ---------------------------------------------------------------------------
# SECTION D - Criblage et dimensionnement
# ---------------------------------------------------------------------------


def screen_variants(results: list[dict]) -> dict:
    """Ce que verrait chaque criblage, compare a la trajectoire complete."""
    print("  Chaque variante est un SOUS-ENSEMBLE des 8 points : sa "
          "capitalisation vue ne peut pas depasser celle de la trajectoire "
          "complete. C'est verifie.")
    table: dict[str, dict] = {}
    for name, labels in SCREENS:
        seen = []
        for outcome in results:
            mcaps = outcome.get("mcaps") or {}
            value = max((mcaps[label] for label in labels if label in mcaps),
                        default=0.0)
            coherent(f"criblage {name} <= complet",
                     value <= outcome["mcap"] + 1e-6,
                     f"{value:.0f} vu a {len(labels)} point(s) pour "
                     f"{outcome['mcap']:.0f} en complet")
            seen.append({"mint": outcome["mint"], "vue": value,
                         "complet": outcome["mcap"]})
        table[name] = {"points": list(labels), "vues": seen}
    return table


def criblage(results: list[dict], graduations: int,
             per_full_calls: float) -> dict:
    print("\n--- criblages compares a la trajectoire complete ---")
    if not results:
        print("  aucune mesure : rien a cribler")
        return {"arretee": True}
    print("  Les points de criblage sont un sous-ensemble des 8 points deja "
          "lus : cette section ne coute RIEN. Les couts annonces sont ceux "
          "d'un run de production.\n")

    table = screen_variants(results)
    unit = CREDIT_COST["getTransfersByAddress"]
    total = len(results)

    print("\n  tokens manques et retenus a tort, par seuil :")
    print(f"    {'variante':<10}{'seuil':>10}{'complet':>9}{'crible':>8}"
          f"{'manques':>9}{'a tort':>8}")
    seuils: dict[str, dict] = {}
    for name, data in table.items():
        for threshold in MCAP_THRESHOLDS:
            full = {row["mint"] for row in data["vues"]
                    if row["complet"] >= threshold}
            screen = {row["mint"] for row in data["vues"]
                      if row["vue"] >= threshold}
            missed = full - screen
            wrong = screen - full
            coherent(f"faux positifs impossibles ({name}, {threshold})",
                     not wrong,
                     f"{len(wrong)} token(s) retenus a tort alors que la "
                     f"vue criblee ne peut pas depasser la complete")
            seuils[f"{name}|{threshold}"] = {
                "retenus_complet": len(full), "retenus_criblage": len(screen),
                "manques": len(missed), "a_tort": len(wrong),
                "ratio": (len(screen) / total) if total else 0.0,
                "part_retenue": round(100 * len(screen) / total, 1)
                if total else 0}
            print(f"    {name:<10}{threshold:>10,}{len(full):>9}"
                  f"{len(screen):>8}{len(missed):>9}{len(wrong):>8}")

    print(f"\n  BUDGET MENSUEL, {graduations} graduations/jour")
    complete_cost = per_full_calls * unit
    projection: dict[str, dict] = {}
    print(f"    {'variante':<10}{'seuil':>10}{'crible/j':>10}"
          f"{'suivis/j':>10}{'credits/mois':>14}")
    for name, labels in SCREENS:
        screen_cost = len(labels) * unit
        for threshold in MCAP_THRESHOLDS:
            stats = seuils[f"{name}|{threshold}"]
            share = stats["ratio"]
            for population, tag in ((1.0, "toute"), (0.5, "moitie")):
                screened = graduations * population
                followed = screened * share
                monthly = 30 * (screened * screen_cost
                                + followed * complete_cost)
                key = f"{name}|{threshold}|{tag}"
                projection[key] = {"credits_par_mois": round(monthly),
                                   "tient": monthly <= MONTHLY_CREDITS,
                                   "suivis_par_jour": round(followed, 1)}
                if tag == "toute":
                    print(f"    {name:<10}{threshold:>10,}{screened:>10.0f}"
                          f"{followed:>10.1f}{monthly:>14,.0f}"
                          + ("  TIENT" if monthly <= MONTHLY_CREDITS
                             else "  NE TIENT PAS"))
    print("\n  criblage de la MOITIE de la population, tiree au hasard :")
    for name, _ in SCREENS:
        for threshold in MCAP_THRESHOLDS:
            entry = projection[f"{name}|{threshold}|moitie"]
            print(f"    {name:<10}{threshold:>10,} : "
                  f"{entry['credits_par_mois']:>12,} credits/mois"
                  + ("  TIENT" if entry["tient"] else "  NE TIENT PAS"))

    return {"arretee": False, "seuils": seuils, "projection": projection,
            "cout_complet": complete_cost, "tokens": total}




# ---------------------------------------------------------------------------
# SECTION D - Recapitulatif
# ---------------------------------------------------------------------------


def credits_of(counter: Counter) -> int:
    return sum(counter[method] * CREDIT_COST.get(method, 0)
               for method in counter)


def credits_spent() -> int:
    return credits_of(_calls)


def show_budget(title: str) -> None:
    print(f"\n{title}")
    for method, count in sorted(_calls.items()):
        cost = CREDIT_COST.get(method, 0)
        cap = CAPS_GLOBAL.get(method)
        cap_text = f" / plafond {cap}" if cap else ""
        print(f"  {method:28} : {count:5d} appels{cap_text}"
              f" -> {count * cost:>7,} credits ({cost}/appel)")
    print(f"  {'TOTAL':28} : {credits_spent():>7,} credits")


def show_regimes() -> dict:
    print("\nRepartition par regime :")
    summary = {}
    for regime in (REGIME_PROBE, REGIME_CRUISE):
        counter = _regime_calls[regime]
        total = credits_of(counter)
        summary[regime] = {"appels": sum(counter.values()), "credits": total,
                           "detail": dict(counter)}
        label = ("exploration, NON recurrente" if regime == REGIME_PROBE
                 else "refait par chaque run quotidien")
        print(f"  {regime:10} : {sum(counter.values()):4d} appels, "
              f"{total:7,} credits ({label})")
    return summary


def final_recap(results: dict) -> dict:
    print("\n" + "=" * 74)
    print("SECTION D - RECAPITULATIF")
    print("=" * 74)

    unit = CREDIT_COST["getTransfersByAddress"]
    a = results.get("a") or {}
    b = results.get("b") or {}
    c = results.get("c") or {}
    d = results.get("criblage") or {}
    validation = a.get("validation") or {}

    graduations = b.get("estimation") or b.get("liste_passants") or 0
    source = ("section B, liste A + echantillon des propres"
              if b.get("estimation") else "section A seule")
    listing_credits = ((a.get("appels") or 0)
                       * CREDIT_COST["getTransactionsForAddress"])
    calls = c.get("appels") or []
    per_full = statistics.median(calls) if calls else 0

    print(f"\nRegle du pool, format BRUT : {validation.get('exacts', 0)}/"
          f"{validation.get('testes', 0)} (seuil >= {VALIDATION_MIN}) -> "
          f"{'UTILISABLE' if a.get('utilisable') else 'NON utilisable'}")
    print(f"Liste reparee : {len(a.get('listing') or [])} entrees sur "
          f"{a.get('transactions', 0)} transactions "
          f"({a.get('un_pool', 0)} a un pool unique)")
    print(f"\nGraduations par jour : {graduations} "
          f"[{b.get('borne_basse', '?')} ; {b.get('borne_haute', '?')}] "
          f"a 95 %")
    print(f"  source : {source}")
    if b.get("comptes_a_lire"):
        print(f"  comptes a lire pour etre complet : "
              f"{len(b['comptes_a_lire'])}")

    print(f"\nCout d'une journee listee : {listing_credits:,} credits "
          f"({a.get('appels', 0)} appel(s) full)")
    for name, labels in SCREENS:
        print(f"Cout du criblage {name:<9} : {len(labels) * unit:>6,} "
              f"credits/token")
    print(f"Cout de la trajectoire complete : {per_full * unit:,.0f} "
          f"credits/token ({per_full:.1f} appels)")

    if c.get("par_classe"):
        print("\nTaux de succes du prix par classe :")
        for verdict, stats in c["par_classe"].items():
            print(f"  {verdict:>12} : {stats['taux']:5.1f} % "
                  f"({stats['tokens']} tokens)")

    show_budget("Consommation reelle de cette sonde :")
    regimes = show_regimes()

    print(f"\nPLAN EN DEUX TEMPS sur {MONTHLY_CREDITS:,} credits/mois")
    projection = d.get("projection") or {}
    if not projection:
        print("  non dimensionnable : la section C n'a pas mesure de "
              "trajectoire.")
    else:
        retained = [(key, value) for key, value in projection.items()
                    if key.endswith("|toute") and value["tient"]]
        print(f"  {len(retained)} combinaison(s) tiennent dans le budget "
              f"sur la population entiere")
        for key, value in sorted(retained,
                                 key=lambda item: -item[1]["suivis_par_jour"]
                                 )[:5]:
            name, threshold, _ = key.split("|")
            print(f"    {name:<9} seuil {int(threshold):>8,} $ : "
                  f"{value['suivis_par_jour']:6.1f} tokens/jour suivis, "
                  f"{value['credits_par_mois']:>10,} credits/mois")
        if not retained:
            print("    aucune : le plan en deux temps ne tient pas sur la "
                  "population entiere aux seuils testes")

    if _incoherences:
        print(f"\n{len(_incoherences)} INCOHERENCE(S) relevee(s) :")
        for item in _incoherences:
            print(f"  - {item}")
        print("  Les conclusions correspondantes ne sont pas reutilisees.")
    else:
        print("\nAucune incoherence : chaque conclusion est coherente avec "
              "les nombres qu'elle resume.")
    if _capped:
        print(f"\n{len(_capped)} plafond(s) atteint(s) : chiffres MINORANTS")
        for section, method in sorted(_capped):
            print(f"  section {section} : {method}")

    return {"validation_brute": validation, "utilisable": a.get("utilisable"),
            "liste": len(a.get("listing") or []),
            "graduations": graduations, "source_graduations": source,
            "intervalle": [b.get("borne_basse"), b.get("borne_haute")],
            "credits_journee": listing_credits,
            "credits_trajectoire": per_full * unit,
            "credits_criblage": {name: len(labels) * unit
                                 for name, labels in SCREENS},
            "par_classe": c.get("par_classe"), "regimes": regimes,
            "projection": projection, "incoherences": list(_incoherences),
            "credits_sonde": credits_spent(),
            "plafonds_atteints": sorted(f"{s}:{m}" for s, m in _capped)}


# ---------------------------------------------------------------------------
# Entree
# ---------------------------------------------------------------------------


def pick_tokens(count: int) -> tuple[list[dict], str]:
    """Tokens PumpSwap dont pool_address est connu : la reponse a valider."""
    response = (
        db.get_client()
        .table(ANALYZED_TABLE)
        .select("mint, symbol, dex, pool_address")
        .not_.is_("pool_address", "null")
        .limit(2000)
        .execute()
    )
    rows = [r for r in (response.data or []) if r.get("pool_address")]
    pump = [r for r in rows if (r.get("dex") or "").startswith("pumpswap")]
    print(f"sol_analyzed_tokens : {len(rows)} tokens avec un pool, "
          f"dont {len(pump)} PumpSwap")
    # Les symboles surveilles passent devant, sans rompre l'ordre du reste.
    watched = [r for r in pump if str(r.get("symbol")) in WATCHED_SYMBOLS]
    others = [r for r in pump if str(r.get("symbol")) not in WATCHED_SYMBOLS]
    ordered = watched + others
    if ordered:
        return ordered[:count], "sol_analyzed_tokens/pumpswap"

    result = gecko(gt.dex_pools, "pumpswap", 1, "h24_volume_usd_desc")
    extra: list[dict] = []
    if result:
        pools, _ = result
        for pool in pools:
            attributes = pool.get("attributes") or {}
            base = (pool.get("relationships", {}).get("base_token", {})
                    .get("data", {}).get("id", ""))
            mint = base.split("_", 1)[-1] if "_" in base else None
            address = attributes.get("address")
            if mint and address:
                extra.append({"mint": mint, "dex": "pumpswap",
                              "pool_address": address,
                              "symbol": (attributes.get("name") or "?")
                              .split("/")[0].strip()})
    return extra[:count], ("geckoterminal/dex_pools(pumpswap)" if extra
                           else "aucune")


def main() -> None:
    global _run_at
    setup_logging()
    diagnose_environment()
    present = bool(os.environ.get("HELIUS_API_KEY", "").strip())
    log.info("  %-18s : %s", "HELIUS_API_KEY",
             "presente" if present else "ABSENTE")
    helius.api_key()  # leve si absente

    _run_at = datetime.now(timezone.utc).isoformat()
    print("\nSonde univers v7 : reparer la liste, et definir la graduation.")
    print(f"Aucune ecriture en base hors {RUN_LOG_TABLE}.")
    print(f"Plafonds globaux : {CAPS_GLOBAL}")
    print(f"Seuil de validation : une regle est validee a >= "
          f"{VALIDATION_MIN}/10.")
    show_budget("Cout unitaire retenu :")

    if not check_run_log():
        return

    rng = random.Random(RANDOM_SEED)
    results: dict[str, Any] = {}
    results["prix"] = load_sol_prices()
    log_run("prix", "prix du SOL", results["prix"])

    print("\n--- ce que les sondes precedentes ont etabli ---")
    accounts = load_accounts()
    print(f"  compte de frais   : {accounts.get('frais') or 'INCONNU'}")
    print(f"  compte de secours : {accounts.get('secours') or 'inconnu'}")
    reference = load_v4_listing()
    if not accounts.get("frais"):
        log.error("Compte de frais inconnu : la sonde ne peut rien lire.")
        log_run("run", "arret", {"raison": "compte de frais inconnu"})
        return

    tokens, source = pick_tokens(TOKENS_VALIDATION)
    if not tokens:
        log.error("Aucun token PumpSwap de reference.")
        log_run("run", "arret", {"raison": "aucun token de reference"})
        return
    print(f"  {len(tokens)} tokens de validation ({source})")

    results["a"] = section_a(accounts["frais"], tokens, reference)
    raw_rows = results["a"].pop("_rows", [])
    fetched = results["a"].pop("_brutes", {})
    log_run("A", "liste reparee", results["a"])

    results["b"] = section_b({**results["a"], "_rows": raw_rows,
                              "_brutes": fetched}, accounts, tokens, rng)
    log_run("B", "definition de la graduation", results["b"])

    graduated = [e for e in (results["a"].get("listing") or [])
                 if e.get("graduation")]
    if not graduated:
        graduated = results["a"].get("listing") or []
        if graduated:
            log.warning("Aucune entree ne passe le test de la courbe : "
                        "l'echantillon retombe sur la liste complete, et "
                        "ce n'est PAS la meme population.")
    results["c"] = section_c(graduated, bool(results["a"].get("utilisable")),
                             rng)
    measured = results["c"].pop("_results", [])
    log_run("C", "echantillon aleatoire", results["c"])

    graduations = (results["b"] or {}).get("estimation") or len(graduated)
    calls = results["c"].get("appels") or []
    per_full = statistics.median(calls) if calls else 0
    if measured and per_full:
        results["criblage"] = criblage(measured, graduations, per_full)
    else:
        results["criblage"] = {"arretee": True}
    log_run("C", "criblages", results["criblage"])

    recap = final_recap(results)
    log_run("D", "recapitulatif", recap)
    print(f"\n{RUN_LOG_TABLE} : run {_run_at} ecrit.")


if __name__ == "__main__":
    main()
