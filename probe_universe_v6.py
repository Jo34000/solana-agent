"""Sonde jetable v6 : le pool depuis la transaction brute, et valide.

Script d'observation, lance via RUN_MODE=probe_universe_v6. Aucune ecriture
en base HORS sol_run_log, aucune conclusion de trading.

Acquis du run 05:51 : un pool retrouve (J4KLqQLH -> CipSp7GS rend des
swaps), 1 181 signatures sur 39azUYFW le 17/09 dont 857 absentes de
9C4nRvhh, et 683 transactions brutes de 9C4nRvhh lues en UN appel full.

Trois regles de conduite, appliquees partout :

  1. Une regle deduite est VALIDEE sur les cas dont on connait la reponse
     avant d'etre appliquee ailleurs. Le taux de validation est affiche.
  2. Toute conclusion imprimee est verifiee contre les nombres qu'elle
     resume. Une conclusion qui les contredit s'affiche INCOHERENTE et
     n'est pas reutilisee plus loin.
  3. Une section qui depend d'une regle non validee s'arrete.

Ecarts releves AVANT ecriture, et ce qui a ete decide :

  1. Les 857 signatures propres a 39azUYFW ne sont PAS persistees : la v5
     n'a ecrit que leurs nombres. La journee de 39azUYFW est donc
     re-scannee (environ 250 credits), et cette fois la liste part dans
     sol_run_log. Les 324 signatures de 9C4nRvhh, elles, sont relues.
  2. Le ticket suppose que getTransactionsForAddress full rend
     meta.preTokenBalances et meta.postTokenBalances. C'est precisement ce
     que la section A verifie : si ces cles manquent, la sonde affiche le
     premier element BRUT et s'arrete la, au lieu de conclure sur du vide.
  3. Une seule page de bougies horaires suffit (41 jours de couverture
     pour un echantillon du 17/09, soit 4 jours d'anciennete) : cela tient
     dans le plafond CoinGecko de 5 et laisse deux appels de marge.
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
import supabase_client as db
from config import (
    ANALYZED_TABLE,
    RUN_LOG_TABLE,
    SOL_MINTS,
    diagnose_environment,
    setup_logging,
)

log = logging.getLogger("solana-agent")

RUN_MODE = "probe_universe_v6"
SOURCE_RUN_MODE = "probe_universe_v4"

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
    "enhanced": 6,
    "coingecko": 5,
}
CAPS_SECTION = {
    "prix": {"coingecko": 5},
    "A": {"getTransfersByAddress": 15, "getTransactionsForAddress": 4,
          "enhanced": 2},
    "B": {"getTransfersByAddress": 60, "enhanced": 2},
    "C": {"getTransfersByAddress": 310},
}

CREDIT_COST = {
    "getTransfersByAddress": 10,
    "getTransactionsForAddress": 100,
    "enhanced": 100,     # doc Helius : "Credit cost: 100 credits per call"
    "getTokenSupply": 1,
    "coingecko": 0,
}

MONTHLY_CREDITS = 1_000_000

TOKENS_VALIDATION = 10
VALIDATION_MIN = 9          # sous ce seuil, C et D s'arretent
SAMPLE_SIZE = 30
B_SAMPLE = 100
DAY_MAX_PAGES = 60
ENRICH_BATCH = 100
RANDOM_SEED = 20260925

TARGET_DAY = "2026-09-17"

TRAJECTORY_POINTS = (
    ("5 min", 300), ("15 min", 900), ("30 min", 1800), ("1 h", 3600),
    ("3 h", 10800), ("6 h", 21600), ("24 h", 86400), ("7 j", 604800),
)
# Les trois variantes de criblage. Chacune est un SOUS-ENSEMBLE des 8
# points : sa capitalisation vue ne peut donc pas depasser celle vue en
# trajectoire complete, et c'est une invariante verifiee.
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
# SECTION A - Mint et pool depuis la transaction brute
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


def validate_rule(tokens: list[dict]) -> dict:
    """La regle retrouve-t-elle le pool_address des tokens connus ?"""
    print(f"\n--- VALIDATION sur {len(tokens)} tokens dont le pool est "
          f"connu ---")
    signatures: list[str] = []
    keep: list[dict] = []
    for token in tokens:
        payload = transfers(token["pool_address"], {"limit": 1,
                                                    "sortOrder": "asc"})
        rows = rows_of(payload)
        if not rows:
            continue
        signature = rows[0].get("signature")
        if isinstance(signature, str):
            signatures.append(signature)
            keep.append(token)
    if not signatures:
        print("    aucune transaction de migration recuperee")
        return {"testes": 0, "exacts": 0, "taux": 0.0, "valide": False}

    enriched = enhanced(signatures[:ENRICH_BATCH]) or []
    by_signature = {t.get("signature"): t for t in enriched
                    if isinstance(t, dict)}
    exact = 0
    details = []
    for token, signature in zip(keep, signatures):
        transaction = by_signature.get(signature)
        if transaction is None:
            print(f"    {str(token.get('symbol')):>10} : non enrichie")
            details.append({"symbol": token.get("symbol"), "pool": None})
            continue
        outcome = mint_and_pool(deltas_from_enhanced(transaction))
        ok = outcome["pool"] == token["pool_address"]
        exact += 1 if ok else 0
        print(f"    {str(token.get('symbol')):>10} : pool trouve "
              f"{str(outcome['pool'])[:12]}.. | attendu "
              f"{token['pool_address'][:12]}.. | "
              f"{'EXACT' if ok else 'FAUX'} ({outcome['pools']} candidat(s))")
        details.append({"symbol": token.get("symbol"), "exact": ok,
                        "candidats": outcome["pools"]})
    rate = exact / len(keep) if keep else 0.0
    print(f"  validation : {exact}/{len(keep)} ({rate:.0%})")
    return {"testes": len(keep), "exacts": exact, "taux": round(rate, 3),
            "valide": exact >= VALIDATION_MIN, "details": details}


def section_a(account: str, tokens: list[dict],
              reference: list[dict]) -> dict:
    start_section("A", "Mint et pool depuis la transaction brute")
    print("  Regle : le mint gradue est celui dont un compte AUGMENTE, hors "
          "SOL et stablecoins ; le pool est le owner qui voit augmenter a "
          "la fois un compte de ce mint et un compte WSOL.")

    day = fetch_day_raw(account, TARGET_DAY)
    rows = day["rows"]
    if rows is None:
        print(f"  REJET ou PERTE : {day['erreur'] or 'payload inattendu'}")
        return {"exploitable": False, "appels": day["calls"]}
    print(f"\n  {len(rows)} transactions brutes en {day['calls']} appel(s)")

    with_meta = sum(1 for r in rows
                    if isinstance(r, dict) and isinstance(r.get("meta"), dict))
    print(f"  dont {with_meta} portent un champ meta")
    if not with_meta:
        print("  meta.pre/postTokenBalances ABSENT : la regle du ticket n'est "
              "pas applicable a ce payload. Premier element brut :")
        print("    " + _as_json(rows[0])[:900].replace("\n", "\n    "))
        return {"exploitable": False, "appels": day["calls"],
                "transactions": len(rows), "avec_meta": 0}

    listing: list[dict] = []
    counts = Counter()
    for row in rows:
        if not isinstance(row, dict):
            continue
        outcome = mint_and_pool(deltas_from_raw(row))
        if outcome["pools"] == 0:
            counts["zero"] += 1
        elif outcome["pools"] == 1:
            counts["un"] += 1
        else:
            counts["plusieurs"] += 1
        signature = row.get("signature")
        when = _to_float(row.get("blockTime"))
        if outcome["mint"] and outcome["pool"] and isinstance(signature, str):
            listing.append({"mint": outcome["mint"], "pool": outcome["pool"],
                            "signature": signature, "time": when,
                            "heure": _iso(when)[11:19]})

    examined = sum(counts.values())
    print("\n  pools candidats par transaction :")
    for label in ("zero", "un", "plusieurs"):
        print(f"    {label:>10} : {counts[label]:4d}")
    coherent("somme des transactions", examined == len(rows),
             f"{examined} classees pour {len(rows)} transactions")
    print(f"  liste [mint, pool, horodatage] : {len(listing)} entrees")

    validation = validate_rule(tokens[:TOKENS_VALIDATION])

    # Concordance des mints avec la liste Enhanced de la v4.
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
        "regle validee mais aucun pool trouve",
        not validated or bool(listing),
        f"validation {validation['exacts']}/{validation['testes']} mais "
        f"{len(listing)} pool(s) sur la journee",
    ) and validated
    if usable:
        print(f"    regle VALIDEE ({validation['exacts']}/"
              f"{validation['testes']}) : les sections C et D peuvent "
              f"l'utiliser")
    else:
        print(f"    regle NON validee ({validation['exacts']}/"
              f"{validation['testes']}, seuil {VALIDATION_MIN}/"
              f"{validation['testes'] or TOKENS_VALIDATION}) : les sections "
              f"C et D s'arretent")
    return {"exploitable": True, "appels": day["calls"],
            "transactions": len(rows), "avec_meta": with_meta,
            "pools": dict(counts), "listing": listing,
            "validation": validation, "concordance": round(rate, 1),
            "communes": len(shared), "identiques": agree,
            "utilisable": usable}


# ---------------------------------------------------------------------------
# SECTION B - Que sont les 857 signatures propres a 39azUYFW ?
# ---------------------------------------------------------------------------


def day_signatures(account: str, day: str) -> dict:
    """Signatures d'un compte sur une journee complete."""
    start, end = _day_bounds(day)
    signatures: set[str] = set()
    calls = 0
    page_token = None
    stopped = "journee couverte"

    for _ in range(DAY_MAX_PAGES):
        config: dict[str, Any] = {
            "limit": 100, "sortOrder": "asc",
            "filters": {"blockTime": {"gte": int(start), "lte": int(end)}},
        }
        if page_token:
            config["paginationToken"] = page_token
        payload = transfers(account, config)
        if payload == "CAPPED":
            stopped = "plafond de budget"
            break
        calls += 1
        rows = rows_of(payload)
        if rows is None:
            stopped = "PERTE : " + (error_of(payload) or "payload inattendu")
            break
        if not rows:
            break
        for line in rows:
            signature = line.get("signature")
            if isinstance(signature, str):
                signatures.add(signature)
        page_token = next_page_token(payload)
        if not page_token:
            break
    else:
        stopped = f"plafond de {DAY_MAX_PAGES} pages"
        log.warning("Journee %s : plafond de %d pages, chiffres minores",
                    day, DAY_MAX_PAGES)
    return {"signatures": signatures, "calls": calls, "stopped": stopped}


def section_b(accounts: dict[str, str], reference: list[dict],
              rng: random.Random) -> dict:
    start_section("B", f"Les signatures propres a {BACKUP_PREFIX}")
    backup = accounts.get("secours")
    known = {e["signature"] for e in reference if e.get("signature")}
    print(f"  {len(known)} signatures de {FEE_PREFIX} relues (liste v4)")
    if not backup:
        print("  compte de secours inconnu : section non mesurable")
        return {}
    print(f"  la v5 n'a persiste que des NOMBRES : la journee de "
          f"{BACKUP_PREFIX} est re-scannee.")

    scan = day_signatures(backup, TARGET_DAY)
    other = scan["signatures"]
    own = sorted(other - known)
    print(f"  {backup[:8]}.. : {len(other)} signatures en {scan['calls']} "
          f"appels ({scan['stopped']})")
    print(f"  propres a {BACKUP_PREFIX} : {len(own)}")
    coherent("propres <= total", len(own) <= len(other),
             f"{len(own)} propres pour {len(other)} signatures")
    if not own:
        print("  aucune signature propre : rien a echantillonner")
        return {"secours": len(other), "propres": 0}

    sample = rng.sample(own, min(B_SAMPLE, len(own)))
    print(f"\n  echantillon de {len(sample)} signatures (seed {RANDOM_SEED}), "
          f"un appel Enhanced")
    enriched = enhanced(sample) or []
    if not enriched:
        print("  enrichissement indisponible : section non concluante")
        return {"secours": len(other), "propres": len(own),
                "echantillon": len(sample)}

    kinds: Counter = Counter()
    payers: Counter = Counter()
    graduations = 0
    for transaction in enriched:
        kind = str(transaction.get("type") or "?")
        source = str(transaction.get("source") or "?")
        kinds[f"{kind} / {source}"] += 1
        if kind == "CREATE_POOL" and source == "PUMP_AMM":
            graduations += 1
            payer = transaction.get("feePayer")
            if isinstance(payer, str):
                payers[payer] += 1

    print(f"\n  types rencontres ({len(enriched)} transactions enrichies) :")
    for label, count in kinds.most_common(8):
        print(f"    {label:<34} {count:4d} "
              f"({100 * count / len(enriched):5.1f} %)")
    coherent("somme des types", sum(kinds.values()) == len(enriched),
             f"{sum(kinds.values())} types pour {len(enriched)} transactions")

    print(f"\n  CREATE_POOL / PUMP_AMM : {graduations}/{len(enriched)}")
    if payers:
        print("  feePayer de ces graduations :")
        for payer, count in payers.most_common(5):
            tag = ("compte de frais" if payer.startswith(FEE_PREFIX) else
                   "compte de secours" if payer.startswith(BACKUP_PREFIX)
                   else "INCONNU")
            print(f"    {payer[:12]}.. x{count} ({tag})")

    low, high = wilson(graduations, len(enriched))
    extra_low, extra_high = len(own) * low, len(own) * high
    total_low = len(known) + extra_low
    total_high = len(known) + extra_high
    estimate = len(known) + len(own) * (graduations / len(enriched))
    print(f"\n  GRADUATIONS REELLES DU {TARGET_DAY} :")
    print(f"    connues (liste v4, validees par PDA) : {len(known)}")
    print(f"    estimees parmi les {len(own)} propres : "
          f"{len(own) * graduations / len(enriched):.0f} "
          f"[{extra_low:.0f} ; {extra_high:.0f}] a 95 %")
    print(f"    TOTAL : {estimate:.0f} [{total_low:.0f} ; {total_high:.0f}]")
    coherent("total >= connues", total_low >= len(known) - 1,
             f"borne basse {total_low:.0f} sous les {len(known)} connues")

    return {"secours": len(other), "propres": len(own),
            "echantillon": len(enriched), "graduations_echantillon": graduations,
            "types": dict(kinds.most_common(10)),
            "fee_payers": dict(payers.most_common(5)),
            "estimation": round(estimate), "borne_basse": round(total_low),
            "borne_haute": round(total_high), "connues": len(known),
            "appels": scan["calls"], "arret": scan["stopped"],
            "signatures_propres": own[:1000]}


# ---------------------------------------------------------------------------
# SECTION C - Echantillon aleatoire, avec le pool valide
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
    start_section("C", "Echantillon aleatoire, avec le pool valide")
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


def section_d(results: list[dict], graduations: int,
              per_full_calls: float) -> dict:
    start_section("D", "Criblage et dimensionnement")
    if not results:
        print("  section C non executee : rien a cribler")
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
# Recapitulatif
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
    print("RECAPITULATIF")
    print("=" * 74)

    a = results.get("a") or {}
    b = results.get("b") or {}
    c = results.get("c") or {}
    d = results.get("d") or {}
    validation = a.get("validation") or {}

    print(f"\nRegle mint + pool (A) : validee "
          f"{validation.get('exacts', 0)}/{validation.get('testes', 0)}, "
          f"{'UTILISABLE' if a.get('utilisable') else 'NON utilisable'}")
    print(f"  {len(a.get('listing') or [])} couples [mint, pool] sur la "
          f"journee, concordance des mints {a.get('concordance', 0)} %")
    if b:
        print(f"\nGraduations du {TARGET_DAY} (B) : {b.get('estimation')} "
              f"[{b.get('borne_basse')} ; {b.get('borne_haute')}] a 95 %")
        print(f"  {b.get('connues')} connues + {b.get('propres')} signatures "
              f"propres dont {b.get('graduations_echantillon')}/"
              f"{b.get('echantillon')} sont des CREATE_POOL / PUMP_AMM")
    if c.get("par_classe"):
        print("\nTaux de succes du prix par classe (C) :")
        for verdict, stats in c["par_classe"].items():
            print(f"  {verdict:>12} : {stats['taux']:5.1f} % "
                  f"({stats['tokens']} tokens)")
    if d.get("cout_complet"):
        print(f"\nCout d'une trajectoire complete : "
              f"{d['cout_complet']:,.0f} credits/token")

    show_budget("Consommation reelle de cette sonde :")
    regimes = show_regimes()

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

    return {"regle_validee": bool(a.get("utilisable")),
            "validation": validation,
            "graduations": b.get("estimation"),
            "intervalle": [b.get("borne_basse"), b.get("borne_haute")],
            "par_classe": c.get("par_classe"),
            "cout_complet": d.get("cout_complet"),
            "regimes": regimes, "credits_sonde": credits_spent(),
            "incoherences": list(_incoherences),
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
    if pump:
        return pump[:count], "sol_analyzed_tokens/pumpswap"

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
    print("\nSonde univers v6 : le pool depuis la transaction brute, valide "
          "avant usage.")
    print(f"Aucune ecriture en base hors {RUN_LOG_TABLE}.")
    print(f"Plafonds globaux : {CAPS_GLOBAL}")
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
        log.error("Aucun token PumpSwap de reference : la regle ne peut pas "
                  "etre validee.")
        log_run("run", "arret", {"raison": "aucun token de reference"})
        return
    print(f"  {len(tokens)} tokens de validation ({source})")

    results["a"] = section_a(accounts["frais"], tokens, reference)
    listing = results["a"].get("listing") or []
    log_run("A", "mint et pool depuis la transaction brute", results["a"])

    results["b"] = section_b(accounts, reference, rng)
    log_run("B", f"signatures propres a {BACKUP_PREFIX}", results["b"])

    results["c"] = section_c(listing, bool(results["a"].get("utilisable")),
                             rng)
    measured = results["c"].pop("_results", [])
    log_run("C", "echantillon aleatoire", results["c"])

    graduations = (results["b"] or {}).get("estimation") or len(reference)
    calls = results["c"].get("appels") or []
    per_full = statistics.median(calls) if calls else 0
    if measured and per_full:
        results["d"] = section_d(measured, graduations, per_full)
    else:
        print("\nSECTION D sautee : la section C n'a produit aucune mesure.")
        results["d"] = {"arretee": True}
    log_run("D", "criblage et dimensionnement", results["d"])

    recap = final_recap(results)
    log_run("recap", "recapitulatif", recap)
    print(f"\n{RUN_LOG_TABLE} : run {_run_at} ecrit.")


if __name__ == "__main__":
    main()
