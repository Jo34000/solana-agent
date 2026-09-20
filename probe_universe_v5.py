"""Sonde jetable v5 : ou lire le prix, et combien de graduations en tout.

Script d'observation, lance via RUN_MODE=probe_universe_v5. Aucune ecriture
en base HORS sol_run_log, aucune conclusion de trading.

Acquis du run 20:08, qui ne sont plus remesures : liste datee de 324
graduations du 17/09, validee par PDA (3/3 a 0 s d'ecart), transactions de
type CREATE_POOL / PUMP_AMM, 9C4nRvhh en feePayer.

Reste a etablir :
  - l'ADRESSE ou lire le prix. C'est bloquant : sans elle, la trajectoire
    ne mesure rien, et c'est deja la troisieme sonde qui bute dessus ;
  - le total REEL des graduations d'une journee, si plusieurs comptes les
    marquent ;
  - le prix des tokens morts, toujours pas mesure.

Ecarts releves AVANT ecriture, et ce qui a ete decide :

  1. La liste datee du 17/09 et le cout de la courbe sont RELUS dans
     sol_run_log (run probe_universe_v4). C'est enfin la table qui sert :
     ni re-scan, ni re-enrichissement. Repli explicite et logue si elle
     est vide : re-scan de la journee + jointure Enhanced, 4 lots.
  2. La "part des CREATE_POOL PUMP_AMM non couverte" n'est pas mesurable
     dans les plafonds : il faudrait scanner le programme PumpSwap entier,
     dont le volume depasse de loin les 1000 transactions par appel.
     A la place, la section B mesure ce qui est mesurable : intersection
     et union des deux comptes connus, et les feePayer des transactions de
     migration deja enrichies en section A. Un feePayer qui n'est aucun
     des deux comptes EST la preuve d'un troisieme chemin.
  3. La section E ne coute RIEN : les 3 points du criblage (+30 min, +6 h,
     +24 h) sont un sous-ensemble des 8 points deja lus en section D. La
     sonde les rejoue sur les mesures existantes au lieu de les repayer ;
     le cout annonce pour le criblage est donc le cout THEORIQUE d'un run
     de production, pas une depense de cette sonde.
  4. Le ticket ne fixe pas le seuil de capitalisation de la section E : la
     sonde en teste quatre et donne la distribution, plutot que d'en
     inventer un.
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
from config import (
    ANALYZED_TABLE,
    RUN_LOG_TABLE,
    SOL_MINTS,
    diagnose_environment,
    setup_logging,
)

log = logging.getLogger("solana-agent")

RUN_MODE = "probe_universe_v5"
SOURCE_RUN_MODE = "probe_universe_v4"

FEE_PREFIX = "9C4nRvhh"
BACKUP_PREFIX = "39azUYFW"

STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
    "USDSwr9ApdHk5bvJKMjzff41FfuX8bSxdKcR81vTwcA",    # USDS
    "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",   # PYUSD
}
IGNORED_MINTS = SOL_MINTS | STABLE_MINTS

CAPS_GLOBAL = {
    "getTransfersByAddress": 600,
    "getTransactionsForAddress": 20,
    "enhanced": 6,
    "coingecko": 10,
}
CAPS_SECTION = {
    "prix": {"coingecko": 8, "getTransfersByAddress": 5},
    "repli": {"getTransfersByAddress": 40, "enhanced": 3},
    "A": {"getTransfersByAddress": 40, "enhanced": 2},
    "B": {"getTransfersByAddress": 40},
    "C": {"getTransactionsForAddress": 6},
    "D": {"getTransfersByAddress": 470, "enhanced": 1},
}

CREDIT_COST = {
    "getTransfersByAddress": 10,
    "getTransactionsForAddress": 100,
    "enhanced": 100,     # doc Helius : "Credit cost: 100 credits per call"
    "getTokenSupply": 1,
    "coingecko": 0,
}

MONTHLY_CREDITS = 1_000_000

TOKENS_SECTION_A = 10
RULE_TOKENS = 3             # tokens PumpSwap servant a deduire la regle
RULE_TESTS = 3              # mints de la liste servant a la verifier
BRUTEFORCE_MAX = 25         # comptes testes un par un si la regle echoue
SAMPLE_SIZE = 30
DAY_MAX_PAGES = 40
ENRICH_BATCH = 100
RANDOM_SEED = 20260924

TARGET_DAY = "2026-09-17"

TRAJECTORY_POINTS = (
    ("5 min", 300), ("15 min", 900), ("30 min", 1800), ("1 h", 3600),
    ("3 h", 10800), ("6 h", 21600), ("24 h", 86400), ("7 j", 604800),
)
SCREEN_LABELS = ("30 min", "6 h", "24 h")
DEAD_RATIO = 0.30
MCAP_THRESHOLDS = (25_000, 50_000, 100_000, 250_000)

SAMPLING_RATES = (("toutes", 1.0), ("une sur trois", 1 / 3),
                  ("une sur dix", 0.1))

REGIME_PROBE = "sonde"
REGIME_CRUISE = "croisiere"

_calls: Counter = Counter()
_section_calls: Counter = Counter()
_regime_calls: dict[str, Counter] = {REGIME_PROBE: Counter(),
                                     REGIME_CRUISE: Counter()}
_regime = REGIME_PROBE
_current_section = "?"
_capped: set[tuple[str, str]] = set()
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
# Prix du SOL (acquis de la v3 : horaire sur plusieurs pages, repli journalier)
# ---------------------------------------------------------------------------

WSOL_MINT = "So11111111111111111111111111111111111111112"
HOURLY_PAGES = 3

_sol_hourly: list[list] = []
_sol_daily: list[list] = []
_sol_misses = 0
_sol_daily_used = 0


def load_sol_prices() -> dict:
    """Bougies horaires sur 3 pages (~125 j), repli journalier au-dela."""
    global _sol_hourly, _sol_daily
    start_section("prix", "Prix du SOL (acquis v3, rejoue a l'identique)")

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
# Relecture de ce que la v4 a etabli
# ---------------------------------------------------------------------------


def load_listing() -> tuple[list[dict], str]:
    """Liste datee du 17/09, relue dans sol_run_log plutot que repayee."""
    try:
        rows = db.fetch_run_log(SOURCE_RUN_MODE, "B", 5)
    except Exception as error:               # noqa: BLE001
        log.error("PERTE : relecture de %s impossible : %s",
                  RUN_LOG_TABLE, error)
        return [], "echec"
    for row in rows:
        payload = row.get("payload") or {}
        listing = [e for e in (payload.get("listing") or [])
                   if isinstance(e, dict) and e.get("mint")]
        if listing:
            print(f"  liste relue dans {RUN_LOG_TABLE} : {len(listing)} "
                  f"graduations, run {str(row.get('run_at'))[:19]}")
            return listing, f"{RUN_LOG_TABLE}/{SOURCE_RUN_MODE}"
    print(f"  aucune liste exploitable dans {RUN_LOG_TABLE}")
    return [], "absente"


def load_v4_recap() -> dict:
    """Couts deja mesures par la v4 : la courbe, notamment."""
    try:
        rows = db.fetch_run_log(SOURCE_RUN_MODE, "D", 3)
    except Exception as error:               # noqa: BLE001
        log.error("PERTE : relecture du recap v4 impossible : %s", error)
        return {}
    for row in rows:
        payload = row.get("payload") or {}
        if payload.get("appels_courbe") is not None:
            return payload
    return {}


def load_accounts() -> dict[str, str]:
    """Adresses completes des deux comptes de migration."""
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


# ---------------------------------------------------------------------------
# SECTION A - La regle d'adresse de cotation
# ---------------------------------------------------------------------------


def collect_paths(node: Any, prefix: str = "") -> list[tuple[str, str]]:
    """(chemin normalise, valeur) de toutes les feuilles texte du payload.

    Les indices de liste deviennent [*] : deux transactions differentes
    donnent alors le MEME chemin, ce qui permet d'en deduire une regle.
    """
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{prefix}.{key}" if prefix else key
            found += collect_paths(value, child)
    elif isinstance(node, list):
        for item in node:
            found += collect_paths(item, f"{prefix}[*]")
    elif isinstance(node, str) and node:
        found.append((prefix, node))
    return found


def paths_of(transaction: dict, target: str) -> list[str]:
    return sorted({path for path, value in collect_paths(transaction)
                   if value == target})


def values_at(transaction: dict, pattern: str) -> list[str]:
    seen: list[str] = []
    for path, value in collect_paths(transaction):
        if path == pattern and value not in seen:
            seen.append(value)
    return seen


def first_transfer(address: str) -> tuple[float | None, str | None]:
    payload = transfers(address, {"limit": 1, "sortOrder": "asc"})
    rows = rows_of(payload)
    if not rows:
        return None, None
    when = _line_time(rows[0])
    signature = rows[0].get("signature")
    return (when or None), (signature if isinstance(signature, str) else None)


def derive_rule(tokens: list[dict]) -> dict:
    """Ou apparait pool_address dans la transaction de migration ?"""
    print(f"\n--- ou apparait pool_address ? {RULE_TOKENS} tokens PumpSwap "
          f"dont le pool est connu ---")
    signatures: list[str] = []
    keep: list[dict] = []
    for token in tokens[:RULE_TOKENS]:
        _, signature = first_transfer(token["pool_address"])
        if signature:
            signatures.append(signature)
            keep.append(token)
            print(f"    {str(token.get('symbol')):>10} : migration "
                  f"{signature[:16]}..")
    if not signatures:
        print("    aucune transaction de migration recuperee")
        return {}

    enriched = enhanced(signatures) or []
    payers = [t.get("feePayer") for t in enriched
              if isinstance(t, dict) and isinstance(t.get("feePayer"), str)]
    per_token: list[set[str]] = []
    for token in keep:
        # Appariement par le contenu : la transaction ou le pool apparait.
        transaction = next(
            (t for t in enriched
             if isinstance(t, dict) and paths_of(t, token["pool_address"])),
            None,
        )
        symbol = str(token.get("symbol"))
        if transaction is None:
            print(f"    {symbol:>10} : pool_address ABSENT de la transaction "
                  f"enrichie")
            per_token.append(set())
            continue
        found = paths_of(transaction, token["pool_address"])
        per_token.append(set(found))
        print(f"    {symbol:>10} : {len(found)} emplacement(s)")
        for path in found[:6]:
            print(f"        {path}")

    common = set.intersection(*per_token) if per_token and all(per_token) else set()
    print("\n  REGLE DEDUITE :")
    if common:
        for pattern in sorted(common):
            print(f"    pool_address se lit en  {pattern}")
    else:
        print("    aucun emplacement commun aux trois : pas de regle simple")
    return {"patterns": sorted(common),
            "par_token": [sorted(p) for p in per_token],
            "feePayers": payers, "enrichies": len(enriched)}


def try_address(address: str, moment: float) -> tuple[bool, int]:
    """L'adresse rend-elle des swaps a T+5 min ?"""
    price, count, calls = page_price(address, moment + 300, tolerance_for(300))
    return price is not None, calls


def apply_rule(patterns: list[str], listing: list[dict],
               rng: random.Random) -> dict:
    """La regle rend-elle une adresse qui repond, sur 3 mints de la liste ?"""
    print(f"\n--- application a {RULE_TESTS} mints du {TARGET_DAY} ---")
    sample = rng.sample(listing, min(RULE_TESTS, len(listing)))
    enriched = enhanced([e["signature"] for e in sample]) or []
    by_signature = {t.get("signature"): t for t in enriched
                    if isinstance(t, dict)}

    working: list[str] = []
    details = []
    for entry in sample:
        transaction = by_signature.get(entry["signature"])
        if transaction is None:
            print(f"    {entry['mint'][:8]}.. : transaction non enrichie")
            continue
        tried = []
        for pattern in patterns:
            for address in values_at(transaction, pattern)[:3]:
                ok, _ = try_address(address, _to_float(entry["time"]))
                tried.append({"pattern": pattern, "adresse": address,
                              "repond": ok})
                print(f"    {entry['mint'][:8]}.. : {pattern} -> "
                      f"{address[:12]}.. : "
                      f"{'DES SWAPS' if ok else 'rien a T+5 min'}")
                if ok and pattern not in working:
                    working.append(pattern)
                if ok:
                    break
            if working:
                break
        details.append({"mint": entry["mint"], "essais": tried})
    payers = [t.get("feePayer") for t in enriched
              if isinstance(t, dict) and isinstance(t.get("feePayer"), str)]
    return {"patterns_ok": working, "details": details, "feePayers": payers,
            "echantillon": [e["mint"] for e in sample]}


def bruteforce(entry: dict) -> dict:
    """Repli : tester un par un les comptes de la transaction de migration."""
    print(f"\n--- repli : chaque compte de la migration de "
          f"{entry['mint'][:8]}.., un par un ---")
    enriched = enhanced([entry["signature"]]) or []
    if not enriched:
        print("    transaction non enrichie, repli impossible")
        return {}
    transaction = enriched[0]
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path, value in collect_paths(transaction):
        if 32 <= len(value) <= 44 and value not in seen and value not in IGNORED_MINTS:
            seen.add(value)
            candidates.append((path, value))
    print(f"    {len(candidates)} compte(s) candidat(s), "
          f"{min(len(candidates), BRUTEFORCE_MAX)} testes")
    winners = []
    for path, address in candidates[:BRUTEFORCE_MAX]:
        ok, calls = try_address(address, _to_float(entry["time"]))
        if calls == 0:
            print("    plafond atteint, repli interrompu")
            break
        if ok:
            winners.append({"pattern": path, "adresse": address})
            print(f"    REPOND : {address[:12]}.. lu en {path}")
    if not winners:
        print("    aucun compte de la transaction ne rend de swap a T+5 min")
    return {"testes": min(len(candidates), BRUTEFORCE_MAX),
            "gagnants": winners}


def section_a(tokens: list[dict], listing: list[dict],
              rng: random.Random) -> dict:
    start_section("A", "La regle d'adresse de cotation (bloquant)")
    print("  Sans adresse ou lire un prix, la trajectoire ne mesure rien. "
          "La regle est DEDUITE des tokens dont le pool est connu, puis "
          "VERIFIEE sur la liste du 17/09.")
    rule = derive_rule(tokens)
    patterns = rule.get("patterns") or []

    applied = {}
    if patterns and listing:
        applied = apply_rule(patterns, listing, rng)
    elif not listing:
        print("\n  liste du 17/09 absente : la regle ne peut pas etre "
              "verifiee")

    working = applied.get("patterns_ok") or []
    fallback = {}
    if not working and listing:
        fallback = bruteforce(listing[0])
        working = [w["pattern"] for w in (fallback.get("gagnants") or [])]

    print("\n  CONCLUSION :")
    if working:
        print(f"    adresse de cotation = {working[0]}")
        print("    la section D peut mesurer des prix.")
    else:
        print("    AUCUNE adresse ne rend de swap : la section D ne sera "
              "PAS executee, comme demande.")
    return {"regle": rule, "application": applied, "repli": fallback,
            "patterns_retenus": working}


# ---------------------------------------------------------------------------
# SECTION B - Y a-t-il plusieurs comptes de migration ?
# ---------------------------------------------------------------------------


def day_signatures(account: str, day: str) -> dict:
    """Signatures d'un compte sur une journee complete."""
    start, end = _day_bounds(day)
    signatures: set[str] = set()
    times: dict[str, float] = {}
    lines_total = 0
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
        lines_total += len(rows)
        for line in rows:
            signature = line.get("signature")
            if isinstance(signature, str):
                signatures.add(signature)
                when = _line_time(line)
                if when:
                    times[signature] = max(times.get(signature, 0.0), when)
        page_token = next_page_token(payload)
        if not page_token:
            break
    else:
        stopped = f"plafond de {DAY_MAX_PAGES} pages"
        log.warning("Journee %s : plafond de %d pages, chiffres minores",
                    day, DAY_MAX_PAGES)
    return {"signatures": signatures, "times": times,
            "lignes": lines_total, "calls": calls, "stopped": stopped}


def fee_payers(section_a: dict, accounts: dict[str, str]) -> dict:
    """Les feePayer deja enrichis en section A sont-ils les comptes connus ?

    La part des CREATE_POOL / PUMP_AMM non couverte exigerait de scanner
    le programme entier, tres au-dela de 1000 transactions par appel : ce
    n'est pas mesurable dans les plafonds. Un feePayer qui n'est aucun des
    deux comptes connus est en revanche une preuve directe d'un troisieme
    chemin, et il est deja paye.
    """
    known = {a for a in accounts.values() if a}
    seen: Counter = Counter()
    for block in (section_a.get("regle") or {}, section_a.get("application") or {}):
        for entry in (block.get("feePayers") or []):
            seen[entry] += 1
    if not seen:
        return {}
    unknown = {payer: count for payer, count in seen.items()
               if payer not in known}
    print("\n--- feePayer des migrations enrichies en section A ---")
    for payer, count in seen.most_common(6):
        tag = "connu" if payer in known else "INCONNU"
        print(f"    {payer[:12]}.. x{count} ({tag})")
    if unknown:
        print(f"  -> {len(unknown)} feePayer inconnu(s) : un troisieme "
              f"chemin de migration existe")
    else:
        print("  -> tous les feePayer sont des comptes connus")
    return {"connus": len(seen) - len(unknown), "inconnus": len(unknown),
            "detail": dict(seen.most_common(10))}


def section_b(accounts: dict[str, str], listing: list[dict],
              section_a_result: dict) -> dict:
    start_section("B", "Y a-t-il plusieurs comptes de migration ?")
    backup = accounts.get("secours")
    reference = {e["signature"] for e in listing if e.get("signature")}
    print(f"  compte de frais : {len(reference)} signatures relues "
          f"(liste du {TARGET_DAY})")
    if not backup:
        print("  compte de secours inconnu : intersection impossible")
        return {"reference": len(reference)}

    scan = day_signatures(backup, TARGET_DAY)
    other = scan["signatures"]
    print(f"  {backup[:8]}.. : {len(other)} signatures, {scan['calls']} "
          f"appels ({scan['stopped']})")

    both = reference & other
    union = reference | other
    print(f"\n  intersection : {len(both)}")
    print(f"  union        : {len(union)}  <- graduations reelles du "
          f"{TARGET_DAY}, pour ces deux comptes")
    print(f"  propres a {FEE_PREFIX} : {len(reference - other)}")
    print(f"  propres a {BACKUP_PREFIX} : {len(other - reference)}")
    if both and len(both) >= 0.9 * len(reference):
        print("  -> les deux comptes marquent LES MEMES graduations, le "
              "second n'ajoute presque rien")
    elif not both:
        print("  -> aucun recouvrement : les deux comptes marquent des "
              "evenements DIFFERENTS")

    payers = fee_payers(section_a_result, accounts)
    return {"reference": len(reference), "secours": len(other),
            "intersection": len(both), "union": len(union),
            "propres_frais": len(reference - other),
            "propres_secours": len(other - reference),
            "appels": scan["calls"], "arret": scan["stopped"],
            "fee_payers": payers}


# ---------------------------------------------------------------------------
# SECTION C - Les mints a 100 credits
# ---------------------------------------------------------------------------


def mints_by_signature(rows: list[dict]) -> dict[str, str]:
    """Un mint par signature, meme regle d'exclusion que la v4."""
    found: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        signature = row.get("signature")
        if not isinstance(signature, str):
            continue
        mints = candidate_mints(row)
        if len(mints) == 1:
            found[signature] = next(iter(mints))
        elif mints:
            # Plusieurs candidats : on garde le premier par ordre stable,
            # et la comparaison avec Enhanced dira si c'est le bon.
            found[signature] = sorted(mints)[0]
    return found


def section_c(account: str, listing: list[dict], day: str) -> dict:
    start_section("C", "Les mints a 100 credits")
    print("  Un appel getTransactionsForAddress full / limit 1000 couvre la "
          "journee pour 100 credits. Rend-il les mints ?")
    start, end = _day_bounds(day)
    config = {"limit": 1000, "sortOrder": "asc", "transactionDetails": "full",
              "filters": {"blockTime": {"gte": int(start), "lte": int(end)}}}
    set_regime(REGIME_CRUISE)
    payload = transactions(account, config)
    calls = 1
    rows = rows_of(payload)
    pages = 1
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
            pages += 1
            rows += extra
            page_token = next_page_token(more)
    set_regime(REGIME_PROBE)

    if rows is None:
        print(f"  REJET ou PERTE : {error_of(payload) or 'payload inattendu'}")
        return {"exploitable": False, "appels": calls}
    print(f"  {len(rows)} transactions en {pages} page(s), {calls} appel(s), "
          f"{calls * CREDIT_COST['getTransactionsForAddress']:,} credits")

    found = mints_by_signature(rows)
    print(f"  {len(found)} signatures rendent un mint "
          f"({100 * len(found) / len(rows) if rows else 0:.0f} % des "
          f"transactions)")
    if rows and not found:
        print(f"  premier element brut : {_as_json(rows[0])[:400]}")

    reference = {e["signature"]: e["mint"] for e in listing
                 if e.get("signature")}
    shared = set(found) & set(reference)
    agree = sum(1 for s in shared if found[s] == reference[s])
    rate = 100 * agree / len(shared) if shared else 0
    print("\n  comparaison a la liste Enhanced de la v4 :")
    print(f"    signatures communes : {len(shared)}")
    print(f"    mints identiques    : {agree} ({rate:.1f} %)")
    missed = len(set(reference) - set(found))
    print(f"    manquees par cette voie : {missed}")

    gtfa_credits = calls * CREDIT_COST["getTransactionsForAddress"]
    enhanced_credits = (
        (len(reference) + ENRICH_BATCH - 1) // ENRICH_BATCH
    ) * CREDIT_COST["enhanced"] if reference else 0
    print("\n  VOIE A RETENIR EN CROISIERE :")
    if rate >= 95 and gtfa_credits < enhanced_credits:
        print(f"    getTransactionsForAddress full : {gtfa_credits:,} credits "
              f"contre {enhanced_credits:,} pour Enhanced, et {rate:.0f} % de "
              f"concordance")
        retained = "getTransactionsForAddress"
    elif found:
        print(f"    Enhanced reste necessaire : concordance {rate:.1f} %, "
              f"{missed} signature(s) manquee(s)")
        retained = "enhanced"
    else:
        print("    cette voie ne rend AUCUN mint : Enhanced reste la seule")
        retained = "enhanced"
    return {"exploitable": bool(found), "appels": calls,
            "transactions": len(rows), "avec_mint": len(found),
            "communes": len(shared), "identiques": agree,
            "concordance": round(rate, 1), "manquees": missed,
            "credits_gtfa": gtfa_credits, "credits_enhanced": enhanced_credits,
            "voie_retenue": retained}


# ---------------------------------------------------------------------------
# SECTION D - Echantillon aleatoire, avec l'adresse corrigee
# ---------------------------------------------------------------------------


def resolve_addresses(sample: list[dict], patterns: list[str]) -> dict:
    """Adresse de cotation de chaque mint, par la regle de la section A."""
    resolved: dict[str, str] = {}
    signatures = [e["signature"] for e in sample if e.get("signature")]
    enriched = enhanced(signatures[:ENRICH_BATCH]) or []
    by_signature = {t.get("signature"): t for t in enriched
                    if isinstance(t, dict)}
    for entry in sample:
        transaction = by_signature.get(entry.get("signature"))
        if transaction is None:
            continue
        for pattern in patterns:
            values = values_at(transaction, pattern)
            if values:
                resolved[entry["mint"]] = values[0]
                break
    return resolved


def measure_token(entry: dict, address: str | None) -> dict:
    """Trajectoire en SOL. Capitalisation retenue point par point."""
    now = datetime.now(timezone.utc).timestamp()
    graduated = _to_float(entry.get("time"))
    mint = entry["mint"]
    candidates = [a for a in (address, entry.get("coffre"), mint) if a]
    calls = 0

    used = candidates[0]
    reference = None
    for candidate in candidates:
        price, _, spent = page_price(candidate, graduated, tolerance_for(3600))
        calls += spent
        if price is not None:
            used, reference = candidate, price
            break

    prices: dict[str, float] = {}
    mcaps: dict[str, float] = {}
    due = obtained = 0
    supply = _to_float((token_supply(mint) or {}).get("uiAmount"))

    for label, delta in TRAJECTORY_POINTS:
        moment = graduated + delta
        if moment > now:
            continue
        due += 1
        price, _, spent = page_price(used, moment, tolerance_for(delta))
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
    source = ("regle" if used == address else
              "coffre" if used == entry.get("coffre") else "mint")
    return {"mint": mint, "reference": reference, "prices": prices,
            "mcaps": mcaps, "due": due, "obtained": obtained, "calls": calls,
            "supply": supply, "mcap": max(mcaps.values(), default=0.0),
            "verdict": verdict, "source": source}


def section_d(listing: list[dict], patterns: list[str],
              rng: random.Random) -> dict:
    start_section("D", "Echantillon aleatoire, avec l'adresse corrigee")
    if not listing:
        print("  aucune liste : section arretee, jamais de repli.")
        return {"arretee": True, "raison": "liste absente"}
    if not patterns:
        print("  AUCUNE adresse de cotation etablie en section A : la "
              "section est arretee, comme demande. Mesurer des prix sur une "
              "adresse qui ne repond pas ne mesurerait rien.")
        return {"arretee": True, "raison": "pas d'adresse de cotation"}

    sample = rng.sample(listing, min(SAMPLE_SIZE, len(listing)))
    print(f"  seed {RANDOM_SEED} | population {len(listing)} | "
          f"tires {len(sample)} (morts compris)")
    set_regime(REGIME_CRUISE)
    resolved = resolve_addresses(sample, patterns)
    print(f"  adresses resolues par la regle : {len(resolved)}/{len(sample)}")

    results = []
    for entry in sample:
        outcome = measure_token(entry, resolved.get(entry["mint"]))
        results.append(outcome)
        log.info("  %s.. %s : %s | %d/%d points | source %s | mcap max %.0f $",
                 entry["mint"][:8], entry.get("heure", "?"),
                 outcome["verdict"], outcome["obtained"], outcome["due"],
                 outcome["source"], outcome["mcap"])
    set_regime(REGIME_PROBE)

    by_verdict: Counter = Counter(r["verdict"] for r in results)
    sources: Counter = Counter(r["source"] for r in results)
    print(f"\n  classement : {dict(by_verdict)}")
    print(f"  adresse utilisee : {dict(sources)}")
    print("  TAUX DE SUCCES DU PRIX PAR CLASSE :")
    per_class: dict[str, dict] = {}
    for verdict in ("vivant", "mort", "muet", "indetermine"):
        group = [r for r in results if r["verdict"] == verdict]
        if not group:
            continue
        due = sum(r["due"] for r in group)
        obtained = sum(r["obtained"] for r in group)
        rate = 100 * obtained / due if due else 0
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
        print(f"    ({len(results) - len(mcaps)} token(s) sans "
              f"capitalisation : pas de prix ou supply inconnue)")

    return {"arretee": False, "seed": RANDOM_SEED, "echantillon": len(sample),
            "resolues": len(resolved), "verdicts": dict(by_verdict),
            "sources": dict(sources), "par_classe": per_class,
            "appels": calls, "mcaps": mcaps, "_results": results}


# ---------------------------------------------------------------------------
# SECTION E - Le criblage a 3 points
# ---------------------------------------------------------------------------


def section_e(results: list[dict]) -> dict:
    start_section("E", "Le criblage a 3 points")
    if not results:
        print("  section D non executee : rien a cribler")
        return {"arretee": True}
    print(f"  Points du criblage : {', '.join(SCREEN_LABELS)}.")
    print("  Cette section ne coute RIEN : ces 3 points sont un "
          "sous-ensemble des 8 deja lus en section D, elle les rejoue au "
          "lieu de les repayer. Le cout annonce est celui d'un run de "
          "production.")

    rows = []
    for outcome in results:
        mcaps = outcome.get("mcaps") or {}
        screen = max((mcaps[label] for label in SCREEN_LABELS
                      if label in mcaps), default=0.0)
        rows.append({"mint": outcome["mint"], "criblage": screen,
                     "complet": outcome["mcap"],
                     "points_criblage": sum(1 for label in SCREEN_LABELS
                                            if label in mcaps)})

    seen = [r for r in rows if r["complet"] > 0]
    if seen:
        ratios = [r["criblage"] / r["complet"] for r in seen
                  if r["complet"] > 0]
        print(f"\n  capitalisation vue a 3 points / vue a 8 points : "
              f"mediane {statistics.median(ratios):.2f} | "
              f"min {min(ratios):.2f}")
        blind = sum(1 for r in seen if r["criblage"] == 0)
        print(f"  {blind}/{len(seen)} token(s) invisibles au criblage "
              f"(aucun des 3 points ne rend de prix)")

    print("\n  tokens mal classes par le criblage, selon le seuil :")
    print(f"    {'seuil':>10}{'retenus 8pts':>14}{'retenus 3pts':>14}"
          f"{'manques':>9}{'a tort':>8}")
    thresholds = {}
    for threshold in MCAP_THRESHOLDS:
        full = {r["mint"] for r in rows if r["complet"] >= threshold}
        screen = {r["mint"] for r in rows if r["criblage"] >= threshold}
        missed = full - screen          # faux negatifs : le criblage les rate
        wrong = screen - full           # faux positifs : retenus pour rien
        thresholds[str(threshold)] = {
            "retenus_complet": len(full), "retenus_criblage": len(screen),
            "manques": len(missed), "a_tort": len(wrong),
            "part_retenue": round(100 * len(screen) / len(rows), 1)
            if rows else 0,
        }
        print(f"    {threshold:>10,}{len(full):>14}{len(screen):>14}"
              f"{len(missed):>9}{len(wrong):>8}")
    print("\n  part de la population qu'un seuil retiendrait pour la "
          "trajectoire complete :")
    for threshold in MCAP_THRESHOLDS:
        stats = thresholds[str(threshold)]
        print(f"    seuil {threshold:>9,} $ : {stats['part_retenue']:5.1f} %")

    return {"arretee": False, "points": list(SCREEN_LABELS),
            "seuils": thresholds, "tokens": len(rows)}


# ---------------------------------------------------------------------------
# Repli : reconstruire la liste si sol_run_log ne la rend pas
# ---------------------------------------------------------------------------


def pick_mint(transaction: dict) -> str | None:
    """Mint du plus gros transfert, hors SOL, WSOL et stablecoins (regle v4)."""
    amounts: dict[str, float] = {}
    for leg in transaction.get("tokenTransfers") or []:
        if not isinstance(leg, dict):
            continue
        mint = leg.get("mint")
        if isinstance(mint, str) and mint and mint not in IGNORED_MINTS:
            amounts[mint] = amounts.get(mint, 0.0) + _amount(leg)
    if not amounts:
        return None
    return max(amounts, key=lambda m: amounts[m])


def rebuild_listing(account: str, day: str) -> list[dict]:
    """Repli COUTEUX : re-scan de la journee + jointure Enhanced."""
    start_section("repli", "Reconstruction de la liste datee")
    log.warning("La liste du %s n'est pas dans %s : reconstruction, "
                "environ %d credits", day, RUN_LOG_TABLE,
                7 * CREDIT_COST["getTransfersByAddress"]
                + 4 * CREDIT_COST["enhanced"])
    scan = day_signatures(account, day)
    ordered = sorted(scan["times"].items(), key=lambda item: item[1])
    listing: list[dict] = []
    for start in range(0, len(ordered), ENRICH_BATCH):
        batch = ordered[start:start + ENRICH_BATCH]
        enriched = enhanced([signature for signature, _ in batch])
        if enriched is None:
            log.warning("Lot %d indisponible : liste amputee",
                        start // ENRICH_BATCH + 1)
            break
        times = dict(batch)
        for transaction in enriched:
            signature = transaction.get("signature")
            mint = pick_mint(transaction)
            if not (isinstance(signature, str) and mint):
                continue
            when = times.get(signature, 0.0)
            listing.append({"mint": mint, "time": when,
                            "heure": _iso(when)[11:19],
                            "signature": signature})
    print(f"  liste reconstruite : {len(listing)} graduations")
    return listing


# ---------------------------------------------------------------------------
# SECTION F - Recapitulatif et dimensionnement
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
    print("SECTION F - RECAPITULATIF ET DIMENSIONNEMENT")
    print("=" * 74)

    unit = CREDIT_COST["getTransfersByAddress"]
    a = results.get("a") or {}
    b = results.get("b") or {}
    c = results.get("c") or {}
    d = results.get("d") or {}
    e = results.get("e") or {}
    v4 = results.get("v4") or {}

    graduations = b.get("union") or b.get("reference") or 0
    if c.get("voie_retenue") == "getTransactionsForAddress":
        listing_credits = c.get("credits_gtfa", 0)
        voie = "getTransactionsForAddress full"
    else:
        listing_credits = (v4.get("credits_listing_jour")
                           or c.get("credits_enhanced", 0))
        voie = "transferts + Enhanced"

    calls = d.get("appels") or []
    per_full = statistics.median(calls) if calls else 0
    per_curve = _to_float(v4.get("appels_courbe"))
    screen_credits = len(SCREEN_LABELS) * unit

    print(f"\nAdresse de cotation (A) : "
          f"{(a.get('patterns_retenus') or ['AUCUNE'])[0]}")
    print(f"Graduations du {TARGET_DAY} (B) : {graduations} "
          f"(union des comptes connus)")
    if b.get("fee_payers", {}).get("inconnus"):
        print(f"  {b['fee_payers']['inconnus']} feePayer inconnu(s) : un "
              f"troisieme chemin existe, ce total est un MINORANT")
    print(f"Voie retenue pour lister (C) : {voie}, "
          f"{listing_credits:,} credits/jour")
    if c.get("concordance") is not None:
        print(f"  concordance des mints : {c['concordance']} %")

    if d.get("par_classe"):
        print("\nTaux de succes du prix par classe (D) :")
        for verdict, stats in d["par_classe"].items():
            print(f"  {verdict:>12} : {stats['taux']:5.1f} % "
                  f"({stats['tokens']} tokens)")
    print(f"\nCout par token, criblage 3 points : {screen_credits:,} credits")
    print(f"Cout par token, trajectoire complete : {per_full * unit:,.0f} "
          f"({per_full:.1f} appels)")
    print(f"Cout par token, courbe (mesure v4) : {per_curve * unit:,.0f} "
          f"({per_curve:.0f} appels)")

    show_budget("Consommation reelle de cette sonde :")
    regimes = show_regimes()

    print(f"\nDIMENSIONNEMENT sur {MONTHLY_CREDITS:,} credits/mois")
    projection: dict[str, dict] = {}
    if not graduations or not per_full:
        print("  NON DIMENSIONNABLE : il manque le nombre de graduations ou "
              "le cout par token.")
        return {"gradues_par_jour": graduations, "voie": voie,
                "credits_listing_jour": listing_credits,
                "credits_criblage": screen_credits,
                "credits_trajectoire": per_full * unit,
                "credits_courbe": per_curve * unit,
                "regimes": regimes, "projection": {},
                "credits_sonde": credits_spent(),
                "plafonds_atteints": sorted(f"{s}:{m}" for s, m in _capped)}

    complete = (per_full + per_curve) * unit
    print(f"  {graduations} graduations/jour, suivi complet "
          f"{complete:,.0f} credits/token\n")
    print("  PLAN EN DEUX TEMPS : cribler TOUTE la population, puis suivre "
          "les retenus")
    seuils = e.get("seuils") or {}
    for threshold in MCAP_THRESHOLDS:
        stats = seuils.get(str(threshold))
        if not stats:
            continue
        share = stats["part_retenue"] / 100
        daily = (listing_credits + graduations * screen_credits
                 + graduations * share * complete)
        monthly = daily * 30
        verdict = "TIENT" if monthly <= MONTHLY_CREDITS else "NE TIENT PAS"
        print(f"    seuil {threshold:>9,} $ ({share:5.1%} retenus) : "
              f"{monthly:>12,.0f} credits/mois  {verdict}")
        projection[f"deux_temps_{threshold}"] = {
            "part_retenue": stats["part_retenue"],
            "credits_par_mois": round(monthly),
            "tient": monthly <= MONTHLY_CREDITS}

    print("\n  ECHANTILLONNAGE : suivre une fraction tiree au hasard")
    for label, rate in SAMPLING_RATES:
        daily = listing_credits + graduations * rate * complete
        monthly = daily * 30
        verdict = "TIENT" if monthly <= MONTHLY_CREDITS else "NE TIENT PAS"
        print(f"    {label:<15} : {monthly:>12,.0f} credits/mois  {verdict}")
        projection[f"echantillon_{label}"] = {
            "credits_par_mois": round(monthly),
            "tient": monthly <= MONTHLY_CREDITS}

    print("\n  Le plan en deux temps voit TOUTE la population et n'en suit "
          "qu'une partie ;\n  l'echantillonnage n'en voit qu'une partie. A "
          "cout egal, le premier ne\n  rate que ce que le criblage classe "
          "mal (section E).")
    if _capped:
        print(f"\n  {len(_capped)} plafond(s) atteint(s) : chiffres MINORANTS")

    return {"gradues_par_jour": graduations, "voie": voie,
            "credits_listing_jour": listing_credits,
            "credits_criblage": screen_credits,
            "credits_trajectoire": per_full * unit,
            "credits_courbe": per_curve * unit,
            "regimes": regimes, "projection": projection,
            "credits_sonde": credits_spent(),
            "plafonds_atteints": sorted(f"{s}:{m}" for s, m in _capped)}


# ---------------------------------------------------------------------------
# Entree
# ---------------------------------------------------------------------------


def pick_tokens(count: int) -> tuple[list[dict], str]:
    """Tokens PumpSwap dont le pool_address est connu : base de la regle."""
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
    if len(pump) >= RULE_TOKENS:
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
    merged = pump + [e for e in extra if e["mint"] not in
                     {p["mint"] for p in pump}]
    return merged[:count], ("geckoterminal/dex_pools(pumpswap)" if merged
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
    print("\nSonde univers v5 : ou lire le prix, et combien de graduations.")
    print(f"Aucune ecriture en base hors {RUN_LOG_TABLE}.")
    print(f"Plafonds globaux : {CAPS_GLOBAL}")
    show_budget("Cout unitaire retenu :")

    if not check_run_log():
        return

    rng = random.Random(RANDOM_SEED)
    results: dict[str, Any] = {}
    results["prix"] = load_sol_prices()
    log_run("prix", "prix du SOL", results["prix"])

    print(f"\n--- ce que la v4 a etabli, relu dans {RUN_LOG_TABLE} ---")
    accounts = load_accounts()
    print(f"  compte de frais   : {accounts.get('frais') or 'INCONNU'}")
    print(f"  compte de secours : {accounts.get('secours') or 'inconnu'}")
    listing, provenance = load_listing()
    results["v4"] = load_v4_recap()
    if results["v4"]:
        print(f"  cout de la courbe (v4) : "
              f"{results['v4'].get('appels_courbe')} appels/token")

    if not listing and accounts.get("frais"):
        listing = rebuild_listing(accounts["frais"], TARGET_DAY)
        provenance = "repli : re-scan + Enhanced"
    log_run("run", "entree", {"provenance_liste": provenance,
                              "graduations": len(listing),
                              "comptes": accounts})

    tokens, source = pick_tokens(TOKENS_SECTION_A)
    if not tokens:
        log.error("Aucun token PumpSwap de reference : la regle ne peut pas "
                  "etre deduite.")
        log_run("run", "arret", {"raison": "aucun token de reference"})
        return
    print(f"  {len(tokens)} tokens de reference ({source})")

    results["a"] = section_a(tokens, listing, rng)
    log_run("A", "regle d'adresse de cotation", results["a"])

    results["b"] = section_b(accounts, listing, results["a"])
    log_run("B", "comptes de migration", results["b"])

    if accounts.get("frais"):
        results["c"] = section_c(accounts["frais"], listing, TARGET_DAY)
    else:
        print("\nSECTION C sautee : compte de frais inconnu.")
        results["c"] = {}
    log_run("C", "mints a 100 credits", results["c"])

    results["d"] = section_d(listing, results["a"].get("patterns_retenus")
                             or [], rng)
    measured = results["d"].pop("_results", [])
    log_run("D", "echantillon aleatoire", results["d"])

    results["e"] = section_e(measured)
    log_run("E", "criblage a 3 points", results["e"])

    recap = final_recap(results)
    log_run("F", "recapitulatif", recap)
    print(f"\n{RUN_LOG_TABLE} : run {_run_at} ecrit.")


if __name__ == "__main__":
    main()
