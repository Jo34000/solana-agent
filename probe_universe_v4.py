"""Sonde jetable v4 : assembler la liste datee des graduations.

Script d'observation, lance via RUN_MODE=probe_universe_v4. Aucune ecriture
en base HORS sol_run_log, aucune conclusion de trading.

Acquis du run 19:11, qui ne sont plus remesures : frais de migration a
0,0015 SOL sur 86 % des lignes, 324 graduations le 17/09, 8/10 tokens
retrouves a +/- 120 s dont 7 a la signature pres, 683 transactions de la
journee en UN appel getTransactionsForAddress full / limit 1000.

Reste a etablir :
  - jusqu'ou remonte le compte de frais, et qui couvre l'avant ;
  - la liste datee [mint, horodatage, signature] d'une journee : c'est le
    BUG de la v3, dont la voie (0) cherchait un mint sur des lignes qui
    n'en portent pas ;
  - le prix sur des tokens morts, mesure toujours pas faite.

Ecarts releves AVANT ecriture, et ce qui a ete decide :

  1. Les 324 signatures du 17/09 "de la section A de la v3" n'ont PAS ete
     persistees : la v3 n'a ecrit que leur nombre. La journee est donc
     re-scannee (7 appels environ, 70 credits), et cette fois la liste
     [signature, horodatage, frais] part dans sol_run_log pour que la
     prochaine sonde ne la repaie pas.
  2. En revanche les ADRESSES completes, elles, ont ete persistees par la
     v3 : la v4 les relit dans sol_run_log au lieu de re-deriver les
     comptes recurrents. C'est exactement ce pour quoi la table existe.
     Repli : MIGRATION_ACCOUNTS, puis arret explicite.
  3. "341 mints pour 324 signatures" venait d'un ENSEMBLE de mints, pas
     d'une jointure : la v3 fusionnait tous les tokenTransfers de tous les
     lots. La v4 joint PAR SIGNATURE et compte les signatures a 0, 1 ou
     plusieurs mints candidats, ce qui est la seule facon de savoir si 341
     est un bon ordre de grandeur ou une coincidence.
  4. Le critere "signature portant un mint distinct" est retire de la
     conclusion de la section A, comme demande : une ligne de frais ne
     porte que du SOL, ce critere ne pouvait jamais etre vrai. La
     conclusion ne repose plus que sur le taux d'appariement.
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

RUN_MODE = "probe_universe_v4"
SOURCE_RUN_MODE = "probe_universe_v3"

PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# Comptes du run 19:11, connus par leurs 8 premiers caracteres. Les adresses
# completes sont relues dans sol_run_log.
FEE_PREFIX = "9C4nRvhh"          # compte de frais de migration
BACKUP_PREFIX = "39azUYFW"       # second candidat, 65 tx/h

# Un mint candidat n'est ni du SOL ni un stablecoin : sinon la jambe de
# cotation passerait pour le token gradue.
STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
    "USDSwr9ApdHk5bvJKMjzff41FfuX8bSxdKcR81vTwcA",    # USDS
    "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",   # PYUSD
}
IGNORED_MINTS = SOL_MINTS | STABLE_MINTS

FEE_AMOUNT = 0.0015              # frais de migration, run de 19:11
SECOND_AMOUNT = 0.0150           # second montant, a elucider

# --- Plafonds (identiques a la v3) ----------------------------------------
CAPS_GLOBAL = {
    "getTransfersByAddress": 800,
    "getTransactionsForAddress": 20,
    "enhanced": 10,
    "coingecko": 10,
}
CAPS_SECTION = {
    # "prix" couvre aussi la preparation : les 10 graduations datees.
    "prix": {"coingecko": 8, "getTransfersByAddress": 15},
    "A": {"getTransfersByAddress": 40, "getTransactionsForAddress": 4},
    "B": {"getTransfersByAddress": 40, "enhanced": 8,
          "getTransactionsForAddress": 4},
    "C": {"getTransfersByAddress": 600},
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
MATCH_WINDOW_S = 120
SAMPLE_SIZE = 30
CURVE_TOKENS = 10
CURVE_MAX_PAGES = 30
DAY_MAX_PAGES = 40
ENRICH_BATCH = 100
VALIDATION_TOKENS = 3
VALIDATION_TOLERANCE_S = 120
RANDOM_SEED = 20260923

TARGET_DAY = "2026-09-17"

TRAJECTORY_POINTS = (
    ("5 min", 300), ("15 min", 900), ("30 min", 1800), ("1 h", 3600),
    ("3 h", 10800), ("6 h", 21600), ("24 h", 86400), ("7 j", 604800),
)
DEAD_RATIO = 0.30

SAMPLING_RATES = (("toutes", 1.0), ("une sur trois", 1 / 3),
                  ("une sur dix", 0.1))

# Deux regimes distincts : ce que la sonde paie UNE FOIS pour comprendre, et
# ce qu'un run de production repaierait chaque jour.
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
# SECTION A - Couverture temporelle du compte de frais
# ---------------------------------------------------------------------------


def load_accounts() -> tuple[dict[str, str], str]:
    """Adresses completes, relues dans sol_run_log plutot que re-derivees."""
    raw = os.environ.get("MIGRATION_ACCOUNTS", "").strip()
    if raw:
        addresses = [a.strip() for a in raw.split(",") if a.strip()]
        return _by_prefix(addresses), "MIGRATION_ACCOUNTS"

    try:
        rows = db.fetch_run_log(SOURCE_RUN_MODE, "A", 5)
    except Exception as error:               # noqa: BLE001
        log.error("PERTE : relecture de %s impossible : %s",
                  RUN_LOG_TABLE, error)
        return {}, "echec"

    for row in rows:
        payload = row.get("payload") or {}
        addresses = [a for a in (payload.get("accounts") or [])
                     if isinstance(a, str)]
        retained = payload.get("retenu")
        if isinstance(retained, str) and retained not in addresses:
            addresses.append(retained)
        mapped = _by_prefix(addresses)
        if mapped:
            print(f"  adresses relues dans {RUN_LOG_TABLE}, run "
                  f"{str(row.get('run_at'))[:19]}")
            return mapped, f"{RUN_LOG_TABLE}/{SOURCE_RUN_MODE}"
    print(f"  aucune adresse exploitable dans {RUN_LOG_TABLE} : la v3 "
          f"a-t-elle bien tourne ?")
    return {}, "echec"


def _by_prefix(addresses: list[str]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for address in addresses:
        if address.startswith(FEE_PREFIX):
            mapped["frais"] = address
        elif address.startswith(BACKUP_PREFIX):
            mapped["secours"] = address
    if not mapped and addresses:
        mapped["frais"] = addresses[0]
        print(f"  aucun prefixe connu : on prend la premiere adresse, "
              f"{addresses[0][:12]}..")
    return mapped


def first_transfer(address: str) -> tuple[float | None, str | None]:
    payload = transfers(address, {"limit": 1, "sortOrder": "asc"})
    rows = rows_of(payload)
    if not rows:
        return None, None
    when = _line_time(rows[0])
    signature = rows[0].get("signature")
    return (when or None), (signature if isinstance(signature, str) else None)


def last_transfer(address: str) -> tuple[float | None, str | None]:
    payload = transfers(address, {"limit": 1, "sortOrder": "desc"})
    rows = rows_of(payload)
    if not rows:
        return None, None
    when = _line_time(rows[0])
    signature = rows[0].get("signature")
    return (when or None), (signature if isinstance(signature, str) else None)


def find_in_window(account: str, token: dict) -> dict | None:
    """Une ligne du compte tombe-t-elle a +/- 2 min de la graduation ?"""
    when = token.get("graduated_at")
    if not when:
        return None
    payload = transfers(account, {
        "limit": 100, "sortOrder": "asc",
        "filters": {"blockTime": {"gte": int(when - MATCH_WINDOW_S),
                                  "lte": int(when + MATCH_WINDOW_S)}},
    })
    if payload == "CAPPED":
        return None
    rows = rows_of(payload)
    if rows is None:
        return {"erreur": error_of(payload) or "PERTE"}
    signatures = {line.get("signature") for line in rows}
    return {"lignes": len(rows),
            "signature_identique": token.get("pool_signature") in signatures}


def section_a(tokens: list[dict]) -> dict:
    start_section("A", "Couverture temporelle du compte de frais")
    accounts, provenance = load_accounts()
    if not accounts.get("frais"):
        print("  aucun compte de frais : section non mesurable")
        return {"provenance": provenance, "accounts": accounts}
    fee = accounts["frais"]
    backup = accounts.get("secours")
    print(f"  compte de frais   : {fee}")
    print(f"  compte de secours : {backup or 'inconnu'}")

    started, _ = first_transfer(fee)
    print(f"\n  premiere transaction de {fee[:8]}.. : "
          f"{_iso(started)[:19] if started else 'INDETERMINEE'}")

    known = [t for t in tokens if t.get("graduated_at")]
    print(f"\n--- appariement a +/- {MATCH_WINDOW_S} s, {len(known)} "
          f"graduations datees ---")
    found: list[dict] = []
    missing: list[dict] = []
    for token in known:
        outcome = find_in_window(fee, token)
        label = f"{str(token.get('symbol')):>10} " \
                f"({_iso(token['graduated_at'])[:10]})"
        if not outcome or outcome.get("erreur") or not outcome.get("lignes"):
            missing.append(token)
            print(f"    {label} : ABSENT")
            continue
        found.append(token)
        print(f"    {label} : {outcome['lignes']:3d} ligne(s), signature "
              f"identique {'OUI' if outcome['signature_identique'] else 'non'}")

    rate = len(found) / len(known) if known else 0
    print(f"\n  retrouves : {len(found)}/{len(known)} ({rate:.0%})")
    print("  le critere 'signature portant un mint distinct' est RETIRE : "
          "une ligne de frais ne porte que du SOL, il ne pouvait jamais "
          "etre vrai.")

    backup_hits: list[dict] = []
    if missing and backup:
        print(f"\n--- les {len(missing)} absents, cherches sur "
              f"{backup[:8]}.. ---")
        for token in missing:
            outcome = find_in_window(backup, token)
            hit = bool(outcome and outcome.get("lignes"))
            if hit:
                backup_hits.append(token)
            print(f"    {str(token.get('symbol')):>10} "
                  f"({_iso(token['graduated_at'])[:10]}) : "
                  f"{'TROUVE' if hit else 'absent'} sur le compte de secours")
    elif missing:
        print("\n  compte de secours inconnu : les absents ne sont pas "
              "recherches ailleurs")

    oldest_found = min((t["graduated_at"] for t in found), default=None)
    older_than_account = [
        t.get("symbol") for t in missing
        if started and t.get("graduated_at") and t["graduated_at"] < started
    ]
    print("\n  CONCLUSION :")
    if started:
        print(f"    {fee[:8]}.. couvre les graduations a partir du "
              f"{_iso(started)[:10]}")
    if oldest_found:
        print(f"    plus ancienne graduation retrouvee : "
              f"{_iso(oldest_found)[:10]}")
    if older_than_account:
        print(f"    les absents anterieurs a cette date : "
              f"{', '.join(str(s) for s in older_than_account)} "
              f"-> le compte n'existait pas encore, ce n'est pas un defaut "
              f"d'appariement")
    elif missing:
        print(f"    {len(missing)} absent(s) POSTERIEUR(S) au debut de "
              f"l'historique : l'appariement est en cause, pas la couverture")
    if backup:
        print(f"    le compte de secours couvre la periode anterieure : "
              f"{'OUI' if backup_hits else 'NON'} "
              f"({len(backup_hits)}/{len(missing)} absents retrouves)")

    return {"provenance": provenance, "frais": fee, "secours": backup,
            "debut_historique": _iso(started) if started else None,
            "retrouves": len(found), "testes": len(known),
            "taux": round(rate, 3),
            "absents": [t.get("symbol") for t in missing],
            "absents_anterieurs": older_than_account,
            "secours_retrouve": len(backup_hits),
            "plus_ancienne_retrouvee": _iso(oldest_found) if oldest_found
            else None}


# ---------------------------------------------------------------------------
# SECTION B - Assembler la liste datee (le bug de la v3)
# ---------------------------------------------------------------------------


def scan_fee_day(account: str, day: str) -> dict:
    """Signatures de frais d'une journee, avec leur horodatage et leur montant.

    Regime de CROISIERE : un run quotidien referait exactement ces appels.
    """
    start, end = _day_bounds(day)
    groups: dict[str, dict] = {}
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
        for signature, lines in group_by_signature(rows).items():
            fee = max((_amount(line) for line in lines
                       if line.get("mint") in SOL_MINTS), default=0.0)
            stamps = [_line_time(line) for line in lines if _line_time(line) > 0]
            entry = groups.setdefault(signature, {"time": 0.0, "fee": 0.0,
                                                  "lignes": 0})
            entry["time"] = max(entry["time"], max(stamps) if stamps else start)
            entry["fee"] = max(entry["fee"], fee)
            entry["lignes"] += len(lines)
        page_token = next_page_token(payload)
        if not page_token:
            break
    else:
        stopped = f"plafond de {DAY_MAX_PAGES} pages"
        log.warning("Journee %s : plafond de %d pages, chiffres minores",
                    day, DAY_MAX_PAGES)

    return {"groups": groups, "lignes": lines_total, "calls": calls,
            "stopped": stopped}


def pick_mint(transaction: dict) -> dict:
    """{mint retenu, candidats, coffre}. Le retenu est celui du plus gros
    transfert.

    Les jambes SOL, WSOL et stablecoins sont ecartees : sinon la jambe de
    cotation passerait pour le token gradue. Le coffre est le compte qui
    recoit ce plus gros transfert : c'est l'adresse la plus proche du pool
    que cette transaction expose, et la section C s'en sert pour lire un
    prix avant de se rabattre sur le mint.
    """
    amounts: dict[str, float] = {}
    vaults: dict[str, tuple[float, str | None]] = {}
    for leg in transaction.get("tokenTransfers") or []:
        if not isinstance(leg, dict):
            continue
        mint = leg.get("mint")
        if not (isinstance(mint, str) and mint and mint not in IGNORED_MINTS):
            continue
        amount = _amount(leg)
        amounts[mint] = amounts.get(mint, 0.0) + amount
        best = vaults.get(mint)
        if best is None or amount > best[0]:
            destination = leg.get("toUserAccount")
            vaults[mint] = (amount, destination
                            if isinstance(destination, str) else None)
    if not amounts:
        return {"mint": None, "candidats": [], "coffre": None}
    chosen = max(amounts, key=lambda m: amounts[m])
    return {"mint": chosen, "candidats": sorted(amounts),
            "coffre": vaults.get(chosen, (0.0, None))[1]}


def join_by_signature(signatures: list[str]) -> dict:
    """Voie 2 : Enhanced par lots, JOINTE par signature (bug de la v3)."""
    per_signature: dict[str, dict] = {}
    calls = 0
    example = None
    for start in range(0, len(signatures), ENRICH_BATCH):
        batch = signatures[start:start + ENRICH_BATCH]
        enriched = enhanced(batch)
        if enriched is None:
            log.warning("Lot %d indisponible : jointure amputee",
                        start // ENRICH_BATCH + 1)
            break
        calls += 1
        for transaction in enriched:
            signature = transaction.get("signature")
            if not isinstance(signature, str):
                continue
            picked = pick_mint(transaction)
            picked["timestamp"] = _to_float(transaction.get("timestamp"))
            per_signature[signature] = picked
            if example is None and len(picked["candidats"]) > 1:
                example = transaction
    return {"par_signature": per_signature, "calls": calls,
            "exemple_multiple": example}


def validate_listing(listing: list[dict], rng: random.Random) -> dict:
    """La derniere transaction de la courbe tombe-t-elle a l'heure annoncee ?"""
    print(f"\n--- validation sur {VALIDATION_TOKENS} mints : PDA de la "
          f"bonding curve ---")
    print("  La courbe est videe a la migration : sa DERNIERE transaction "
          "doit tomber a l'heure de graduation annoncee.")
    sample = rng.sample(listing, min(VALIDATION_TOKENS, len(listing)))
    results = []
    agree = 0
    for entry in sample:
        try:
            curve, _ = solana_addr.bonding_curve_address(entry["mint"],
                                                         PUMPFUN_PROGRAM)
        except ValueError as error:
            print(f"    {entry['mint'][:8]}.. : PDA inderivable ({error})")
            results.append({"mint": entry["mint"], "erreur": str(error)})
            continue
        when, _ = last_transfer(curve)
        if when is None:
            print(f"    {entry['mint'][:8]}.. : courbe sans transaction "
                  f"connue")
            results.append({"mint": entry["mint"], "ecart_s": None})
            continue
        gap = abs(when - entry["time"])
        ok = gap <= VALIDATION_TOLERANCE_S
        agree += 1 if ok else 0
        print(f"    {entry['mint'][:8]}.. : annonce {_iso(entry['time'])[11:19]}"
              f" | derniere tx de la courbe {_iso(when)[11:19]} | ecart "
              f"{gap:.0f} s -> {'CONCORDE' if ok else 'DISCORDE'}")
        results.append({"mint": entry["mint"], "ecart_s": round(gap),
                        "concorde": ok})
    verdict = agree == len(sample) and bool(sample)
    print(f"  {agree}/{len(sample)} concordent -> liste "
          f"{'FIABLE' if verdict else 'A REVOIR'}")
    return {"concordances": agree, "testes": len(sample),
            "fiable": verdict, "details": results}


def section_b(account: str, day: str, rng: random.Random) -> dict:
    start_section("B", f"Liste datee des graduations du {day}")
    print("  La v3 cherchait un mint sur les lignes de frais, qui ne "
          "portent que du SOL : sa liste etait vide. On joint donc les "
          "mints par SIGNATURE, via Enhanced.")
    print(f"  Les 324 signatures du 19:11 n'ayant PAS ete persistees, la "
          f"journee est re-scannee ; cette fois la liste part dans "
          f"{RUN_LOG_TABLE}.")

    set_regime(REGIME_CRUISE)
    day_scan = scan_fee_day(account, day)
    groups = day_scan["groups"]
    ordered = sorted(groups.items(), key=lambda item: item[1]["time"])
    signatures = [signature for signature, _ in ordered]
    print(f"\n  {day_scan['lignes']} lignes -> {len(signatures)} signatures "
          f"de frais, {day_scan['calls']} appels ({day_scan['stopped']})")
    if not signatures:
        set_regime(REGIME_PROBE)
        print("  aucune signature : section non mesurable")
        return {}

    joined = join_by_signature(signatures)
    set_regime(REGIME_PROBE)
    per_signature = joined["par_signature"]
    print(f"  jointure Enhanced : {len(per_signature)}/{len(signatures)} "
          f"signatures enrichies en {joined['calls']} appels "
          f"({joined['calls'] * CREDIT_COST['enhanced']:,} credits)")

    zero = one = several = 0
    for data in per_signature.values():
        count = len(data["candidats"])
        if count == 0:
            zero += 1
        elif count == 1:
            one += 1
        else:
            several += 1
    absent = len(signatures) - len(per_signature)
    print("\n  mints candidats par signature :")
    print(f"    0 mint        : {zero:4d}")
    print(f"    1 mint        : {one:4d}")
    print(f"    plusieurs     : {several:4d}")
    if absent:
        print(f"    non enrichies : {absent:4d}")

    if joined["exemple_multiple"] is not None:
        print("\n  exemple complet d'une signature a plusieurs candidats :")
        print("    " + _as_json(joined["exemple_multiple"])[:1500]
              .replace("\n", "\n    "))

    # --- croisement avec le montant du frais ------------------------------
    print("\n  rendement en mints selon le montant du frais :")
    buckets: dict[str, list[bool]] = defaultdict(list)
    for signature, data in ordered:
        fee = round(data["fee"], 4)
        label = (f"{FEE_AMOUNT:.4f}" if abs(fee - FEE_AMOUNT) < 1e-6 else
                 f"{SECOND_AMOUNT:.4f}" if abs(fee - SECOND_AMOUNT) < 1e-6
                 else "autre")
        entry = per_signature.get(signature) or {}
        buckets[label].append(bool(entry.get("mint")))
    bucket_stats = {}
    for label, hits in sorted(buckets.items()):
        rate = 100 * sum(hits) / len(hits)
        bucket_stats[label] = {"signatures": len(hits),
                               "avec_mint": sum(hits), "taux": round(rate, 1)}
        print(f"    {label:>8} SOL : {sum(hits):4d}/{len(hits):4d} "
              f"signatures rendent un mint ({rate:5.1f} %)")
    if len(bucket_stats) > 1:
        rates = {k: v["taux"] for k, v in bucket_stats.items()}
        spread = max(rates.values()) - min(rates.values())
        print(f"    ecart entre montants : {spread:.1f} points -> "
              + ("deux evenements DIFFERENTS se cachent derriere ces "
                 "montants" if spread >= 20 else
                 "meme rendement, un seul type d'evenement"))

    listing = []
    for signature, data in ordered:
        entry = per_signature.get(signature) or {}
        mint = entry.get("mint")
        if not mint:
            continue
        listing.append({"mint": mint, "time": data["time"],
                        "heure": _iso(data["time"])[11:19],
                        "signature": signature,
                        "coffre": entry.get("coffre"),
                        "frais": round(data["fee"], 6),
                        "candidats": len(entry.get("candidats") or [])})
    print(f"\n  LISTE DATEE : {len(listing)} graduations "
          f"[mint, horodatage, signature]")
    for item in listing[:10]:
        print(f"    {item['heure']}  {item['mint']}  {item['signature'][:16]}..")

    validation = validate_listing(listing, rng) if listing else {}

    return {"jour": day, "signatures": len(signatures),
            "lignes": day_scan["lignes"], "appels_scan": day_scan["calls"],
            "appels_enhanced": joined["calls"],
            "repartition": {"zero": zero, "un": one, "plusieurs": several,
                            "non_enrichies": absent},
            "montants": bucket_stats, "listing": listing,
            "validation": validation, "arret": day_scan["stopped"]}


# ---------------------------------------------------------------------------
# SECTION C - Echantillon aleatoire (inchangee depuis la v3)
# ---------------------------------------------------------------------------


def measure_token(entry: dict) -> dict:
    """Trajectoire en SOL. Le prix a la graduation sert de reference."""
    now = datetime.now(timezone.utc).timestamp()
    graduated = _to_float(entry.get("time"))
    mint = entry["mint"]
    addresses = [a for a in (entry.get("coffre"), mint) if a]
    calls = 0

    address = addresses[0] if addresses else mint
    reference = None
    for candidate in addresses:
        price, _, spent = page_price(candidate, graduated, tolerance_for(3600))
        calls += spent
        if price is not None:
            address, reference = candidate, price
            break

    prices: dict[str, float] = {}
    due = obtained = 0
    best_mcap = 0.0
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
            best_mcap = max(best_mcap, price * usd * supply)

    verdict = "indetermine"
    if reference and "24 h" in prices:
        verdict = "mort" if prices["24 h"] < DEAD_RATIO * reference else "vivant"
    elif reference and due and not obtained:
        verdict = "muet"
    return {"mint": mint, "reference": reference, "due": due,
            "obtained": obtained, "calls": calls, "supply": supply,
            "mcap": best_mcap, "verdict": verdict,
            "source": "coffre" if address != mint else "mint"}


def read_curve(mint: str, graduated: float) -> dict:
    """Lecture ascendante de la courbe derivee, jusqu'a la graduation."""
    try:
        curve, _ = solana_addr.bonding_curve_address(mint, PUMPFUN_PROGRAM)
    except ValueError as error:
        return {"erreur": str(error), "calls": 0, "buyers": 0}

    buyers: set[str] = set()
    signatures: set[str] = set()
    calls = 0
    page_token = None
    stopped = "historique epuise"
    for _ in range(CURVE_MAX_PAGES):
        config: dict[str, Any] = {"limit": 100, "sortOrder": "asc"}
        if page_token:
            config["paginationToken"] = page_token
        payload = transfers(curve, config)
        if payload == "CAPPED":
            stopped = "plafond de budget"
            break
        calls += 1
        rows = rows_of(payload)
        if not rows:
            break
        past_end = False
        for line in rows:
            when = _line_time(line)
            if when and graduated and when > graduated:
                past_end = True
                continue
            signature = line.get("signature")
            if isinstance(signature, str):
                signatures.add(signature)
            if line.get("mint") != mint:
                continue
            buyer = line.get("toUserAccount")
            if isinstance(buyer, str) and buyer and buyer != curve:
                buyers.add(buyer)
        if past_end:
            stopped = "graduation atteinte"
            break
        page_token = next_page_token(payload)
        if not page_token:
            break
    else:
        stopped = f"plafond de {CURVE_MAX_PAGES} pages"
    return {"curve": curve, "calls": calls, "buyers": len(buyers),
            "signatures": len(signatures), "stopped": stopped}


def section_c(listing: list[dict], rng: random.Random) -> dict:
    start_section("C", "Echantillon aleatoire")
    if not listing:
        print("  la section B n'a produit aucune liste.")
        print("  SECTION ARRETEE : jamais de repli sur un echantillon non "
              "aleatoire, le taux de succes par classe ne voudrait rien dire.")
        return {"arretee": True, "raison": "section B sans liste"}

    sample = rng.sample(listing, min(SAMPLE_SIZE, len(listing)))
    print(f"  seed {RANDOM_SEED} | population {len(listing)} | "
          f"tires {len(sample)} (morts compris)")
    print(f"  prix = mediane des swaps de la page, en SOL. "
          f"Mort = prix a 24 h < {DEAD_RATIO:.0%} du prix de graduation.\n")

    set_regime(REGIME_CRUISE)
    results = []
    for entry in sample:
        outcome = measure_token(entry)
        results.append(outcome)
        log.info("  %s.. %s : %s | %d/%d points | source %s | mcap max %.0f $",
                 entry["mint"][:8], entry["heure"], outcome["verdict"],
                 outcome["obtained"], outcome["due"], outcome["source"],
                 outcome["mcap"])

    by_verdict: Counter = Counter(r["verdict"] for r in results)
    sources: Counter = Counter(r["source"] for r in results)
    print(f"\n  classement : {dict(by_verdict)}")
    print(f"  adresse de cotation utilisee : {dict(sources)}")
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

    trajectory_calls = [r["calls"] for r in results]
    print(f"  appels par token (trajectoire) : mediane "
          f"{statistics.median(trajectory_calls):.1f} | "
          f"max {max(trajectory_calls)}")

    mcaps = sorted(r["mcap"] for r in results if r["mcap"] > 0)
    if mcaps:
        print("  capitalisations max observees, brut :")
        for label, value in (("min", mcaps[0]),
                             ("mediane", statistics.median(mcaps)),
                             ("max", mcaps[-1])):
            print(f"    {label:>8} : {value:>14,.0f} $")

    print(f"\n--- courbe de {CURVE_TOKENS} d'entre eux ---")
    curve_calls: list[int] = []
    curve_buyers: list[int] = []
    for entry in sample[:CURVE_TOKENS]:
        outcome = read_curve(entry["mint"], _to_float(entry.get("time")))
        if outcome.get("erreur"):
            print(f"    {entry['mint'][:8]}.. : {outcome['erreur']}")
            continue
        curve_calls.append(outcome["calls"])
        curve_buyers.append(outcome["buyers"])
        print(f"    {entry['mint'][:8]}.. : {outcome['signatures']} tx, "
              f"{outcome['buyers']} acheteurs distincts, "
              f"{outcome['calls']} appels ({outcome['stopped']})")
    set_regime(REGIME_PROBE)
    if curve_calls:
        print(f"  appels par courbe : mediane "
              f"{statistics.median(curve_calls):.0f} | max {max(curve_calls)}")
    if curve_buyers:
        print(f"  acheteurs distincts : mediane "
              f"{statistics.median(curve_buyers):.0f} | max {max(curve_buyers)}")

    return {"arretee": False, "seed": RANDOM_SEED, "population": len(listing),
            "echantillon": len(sample), "verdicts": dict(by_verdict),
            "sources": dict(sources), "par_classe": per_class,
            "appels_trajectoire": trajectory_calls,
            "appels_courbe": curve_calls, "acheteurs": curve_buyers,
            "mcaps": mcaps}


# ---------------------------------------------------------------------------
# SECTION D - Recapitulatif et dimensionnement
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
    """Ce que la sonde paie une fois, contre ce qu'un run quotidien repaie."""
    print("\nRepartition par regime :")
    summary = {}
    for regime in (REGIME_PROBE, REGIME_CRUISE):
        counter = _regime_calls[regime]
        total = credits_of(counter)
        calls = sum(counter.values())
        summary[regime] = {"appels": calls, "credits": total,
                           "detail": dict(counter)}
        label = ("exploration, NON recurrente" if regime == REGIME_PROBE
                 else "refait par chaque run quotidien")
        print(f"  {regime:10} : {calls:4d} appels, {total:7,} credits "
              f"({label})")
        for method, count in sorted(counter.items()):
            print(f"      {method:26} {count:4d}")
    return summary


def final_recap(results: dict) -> dict:
    print("\n" + "=" * 74)
    print("SECTION D - RECAPITULATIF ET DIMENSIONNEMENT")
    print("=" * 74)

    unit = CREDIT_COST["getTransfersByAddress"]
    a = results.get("a") or {}
    b = results.get("b") or {}
    c = results.get("c") or {}

    graduations = len(b.get("listing") or [])
    scan_credits = (b.get("appels_scan") or 0) * unit
    enhanced_credits = ((b.get("appels_enhanced") or 0)
                        * CREDIT_COST["enhanced"])
    listing_credits = scan_credits + enhanced_credits

    traj = c.get("appels_trajectoire") or []
    curve = c.get("appels_courbe") or []
    per_traj = statistics.median(traj) if traj else 0
    per_curve = statistics.median(curve) if curve else 0

    print(f"\nCouverture (A) : {str(a.get('frais') or '?')[:8]}.. demarre le "
          f"{str(a.get('debut_historique'))[:10]}, "
          f"{a.get('retrouves')}/{a.get('testes')} graduations retrouvees")
    if a.get("absents_anterieurs"):
        print(f"  absents anterieurs au compte : "
              f"{', '.join(str(s) for s in a['absents_anterieurs'])}")

    validation = b.get("validation") or {}
    print(f"\nListe datee (B) : {graduations} graduations sur "
          f"{b.get('signatures', 0)} signatures de frais")
    print(f"  repartition des candidats : {b.get('repartition')}")
    print(f"  validation PDA : {validation.get('concordances')}/"
          f"{validation.get('testes')} -> "
          f"{'FIABLE' if validation.get('fiable') else 'A REVOIR'}")
    print(f"\nCout par journee listee : {listing_credits:,} credits "
          f"({scan_credits:,} de transferts + {enhanced_credits:,} "
          f"d'Enhanced)")
    print(f"Cout par token (trajectoire) : {per_traj * unit:,.0f} "
          f"({per_traj:.1f} appels)")
    print(f"Cout par token (courbe)      : {per_curve * unit:,.0f} "
          f"({per_curve:.0f} appels)")

    if c.get("par_classe"):
        print("\nTaux de succes du prix par classe :")
        for verdict, stats in c["par_classe"].items():
            print(f"  {verdict:>12} : {stats['taux']:5.1f} % "
                  f"({stats['tokens']} tokens)")

    show_budget("Consommation reelle de cette sonde :")
    regimes = show_regimes()
    if _sol_misses:
        log.warning("%d conversion(s) USD hors couverture", _sol_misses)

    print(f"\nDIMENSIONNEMENT sur {MONTHLY_CREDITS:,} credits/mois")
    projection: dict[str, dict] = {}
    if not graduations or not per_traj:
        print("  NON DIMENSIONNABLE : il manque la liste datee (section B) "
              "ou le cout par token (section C).")
    else:
        per_token = (per_traj + per_curve) * unit
        print(f"  {graduations} graduations/jour, suivi complet = "
              f"{per_token:,.0f} credits/token")
        for label, rate in SAMPLING_RATES:
            followed = graduations * rate
            daily = listing_credits + followed * per_token
            monthly = daily * 30
            verdict = "TIENT" if monthly <= MONTHLY_CREDITS else "NE TIENT PAS"
            print(f"  {label:<15} : {followed:6.0f} tokens/jour -> "
                  f"{monthly:>12,.0f} credits/mois  {verdict}")
            projection[label] = {"tokens_par_jour": round(followed, 1),
                                 "credits_par_mois": round(monthly),
                                 "tient": monthly <= MONTHLY_CREDITS}
        print(f"  Rappel : les {regimes[REGIME_PROBE]['credits']:,} credits "
              f"d'exploration ci-dessus ne sont PAS dans cette projection, "
              f"ils ne se repaient pas.")

    if _capped:
        print(f"\n  {len(_capped)} plafond(s) atteint(s) : chiffres MINORANTS")
        for section, method in sorted(_capped):
            print(f"    section {section} : {method}")

    return {"gradues_par_jour": graduations,
            "credits_listing_jour": listing_credits,
            "appels_trajectoire": per_traj, "appels_courbe": per_curve,
            "regimes": regimes, "projection": projection,
            "sol_misses": _sol_misses, "credits_sonde": credits_spent(),
            "plafonds_atteints": sorted(f"{s}:{m}" for s, m in _capped)}


# ---------------------------------------------------------------------------
# Entree
# ---------------------------------------------------------------------------


def pick_tokens(count: int) -> tuple[list[dict], str]:
    """Memes tokens PumpSwap que les sondes precedentes."""
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
    if len(pump) >= count:
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


def load_graduations(tokens: list[dict]) -> int:
    known = 0
    for token in tokens:
        when, signature = first_transfer(token["pool_address"])
        token["graduated_at"] = when
        token["pool_signature"] = signature
        if when:
            known += 1
    return known


def main() -> None:
    global _run_at
    setup_logging()
    diagnose_environment()
    present = bool(os.environ.get("HELIUS_API_KEY", "").strip())
    log.info("  %-18s : %s", "HELIUS_API_KEY",
             "presente" if present else "ABSENTE")
    helius.api_key()  # leve si absente

    _run_at = datetime.now(timezone.utc).isoformat()
    print("\nSonde univers v4 : assembler la liste datee des graduations.")
    print(f"Aucune ecriture en base hors {RUN_LOG_TABLE}.")
    print(f"Plafonds globaux : {CAPS_GLOBAL}")
    show_budget("Cout unitaire retenu :")

    if not check_run_log():
        return

    rng = random.Random(RANDOM_SEED)
    results: dict[str, Any] = {}
    results["prix"] = load_sol_prices()
    log_run("prix", "prix du SOL", results["prix"])

    tokens, provenance = pick_tokens(TOKENS_SECTION_A)
    if not tokens:
        log.error("Aucun token de reference, sonde interrompue.")
        log_run("run", "arret", {"raison": "aucun token de reference"})
        return
    known = load_graduations(tokens)
    print(f"  {known}/{len(tokens)} graduations datees ({provenance})")

    results["a"] = section_a(tokens)
    log_run("A", "couverture temporelle", results["a"])

    account = results["a"].get("frais")
    if account:
        results["b"] = section_b(account, TARGET_DAY, rng)
        log_run("B", f"liste datee du {TARGET_DAY}", results["b"])
    else:
        print("\nSECTION B sautee : aucun compte de frais identifie.")
        results["b"] = {}

    results["c"] = section_c((results["b"] or {}).get("listing") or [], rng)
    log_run("C", "echantillon aleatoire", results["c"])

    recap = final_recap(results)
    log_run("D", "recapitulatif", recap)
    print(f"\n{RUN_LOG_TABLE} : run {_run_at} ecrit.")


if __name__ == "__main__":
    main()
