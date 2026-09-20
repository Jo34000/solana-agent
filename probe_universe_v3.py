"""Sonde jetable v3 : le mint de chaque graduation, et le prix des morts.

Script d'observation, lance via RUN_MODE=probe_universe_v3. Aucune ecriture
en base HORS sol_run_log, aucune conclusion de trading.

Acquis du run 15:35, qui ne sont plus remesures : PDA de courbe correct
(10/10), filters.status = "succeeded" accepte, solMode merged/separate,
un compte de migration candidat.

Reste a etablir, et c'est tout l'objet de cette sonde :
  - une ligne du compte de migration = une graduation, oui ou non ;
  - le mint de chaque graduation, donc la liste datee d'une journee ;
  - le comportement du prix sur des tokens MORTS, qui est le biais que
    GeckoTerminal masquait en 404.

Contrainte permanente : offres gratuites. Helius free tier (1M
credits/mois), CoinGecko Demo (cle PARTAGEE avec l'agent ETH), Supabase
gratuit.

Ecarts releves AVANT ecriture, et ce qui a ete decide :

  1. "9C4nRvhh" et "39azUYFW" sont des PREFIXES de 8 caracteres, pas des
     adresses : la sonde du 20/09 logue account[:8].., et les adresses
     completes ne sont nulle part (rien n'a ete ecrit en base). On ne peut
     pas interroger Helius avec un prefixe. La sonde RE-DERIVE donc les
     comptes candidats par la procedure du 20/09 (comptes recurrents a la
     creation, puis criblage horaire) et les APPARIE par prefixe. La
     variable d'environnement MIGRATION_ACCOUNTS (adresses completes,
     separees par des virgules) court-circuite cette re-derivation quand
     on les aura.
  2. sol_run_log n'existe probablement pas encore. L'ecriture est testee
     AU DEMARRAGE, avant toute depense de credits : table absente -> la
     sonde s'arrete en affichant le SQL de creation. C'est aussi la
     reponse au trou du 20/09 : les acquis d'une sonde doivent survivre au
     ticket suivant.
  3. filters.tokenAccounts et transactionDetails "full" n'ont jamais ete
     valides. Le 17/09 avait etabli que getTransactionsForAddress ne rend
     que signature / slot / err / blockTime. La voie (1) de la section B
     teste donc les cles UNE PAR UNE avant de les combiner, et logue le
     message de rejet brut : une cle inconnue fait rejeter tout l'objet.
  4. "324 lignes du 17/09" : ce sont des LIGNES de transfert, pas des
     signatures. Sans les deux comptes, "1 ligne = 1 graduation" est
     intestable. La section A donne lignes, signatures et mints distincts.
  5. Enhanced coute 100 credits par appel (doc Helius) : la voie (2) de la
     section B coute 100 credits par lot de 100 signatures, et c'est
     exactement ce que la comparaison doit chiffrer.
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

RUN_MODE = "probe_universe_v3"

PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# Prefixes des deux comptes candidats du run 15:35. Les adresses completes
# n'ont pas ete conservees : la sonde reconstruit et apparie.
CANDIDATE_PREFIXES = ("9C4nRvhh", "39azUYFW")

EXCLUDED_PREFIXES = (
    "6EF8rrec", "pAMMBay6", "Tokenz", "Tokenkeg", "ATokenGP",
    "ComputeB", "SysvarRe", "1111", "So1111",
)

# --- Plafonds -------------------------------------------------------------
CAPS_GLOBAL = {
    "getTransfersByAddress": 800,
    "getTransactionsForAddress": 20,
    "enhanced": 10,
    "coingecko": 10,
}
CAPS_SECTION = {
    "D": {"coingecko": 8},
    "A": {"getTransfersByAddress": 120, "getTransactionsForAddress": 10,
          "enhanced": 2},
    "B": {"getTransfersByAddress": 40, "getTransactionsForAddress": 8,
          "enhanced": 6},
    "C": {"getTransfersByAddress": 560},
}

CREDIT_COST = {
    "getTransfersByAddress": 10,
    "getTransactionsForAddress": 100,
    "enhanced": 100,     # doc Helius : "Credit cost: 100 credits per call"
    "getTokenSupply": 1,
    "coingecko": 0,      # quota separe, pas des credits Helius
}

MONTHLY_CREDITS = 1_000_000

TOKENS_SECTION_A = 10
MATCH_WINDOW_S = 120        # +/- 2 min autour de la graduation connue
SAMPLE_SIZE = 30
CURVE_TOKENS = 10
CURVE_MAX_PAGES = 30
DAY_MAX_PAGES = 40
SCREEN_MAX_ACCOUNTS = 8
ENRICH_BATCH = 100
RANDOM_SEED = 20260922

TARGET_DAY = "2026-09-17"

TRAJECTORY_POINTS = (
    ("5 min", 300), ("15 min", 900), ("30 min", 1800), ("1 h", 3600),
    ("3 h", 10800), ("6 h", 21600), ("24 h", 86400), ("7 j", 604800),
)
DEAD_RATIO = 0.30

# Taux d'echantillonnage projetes en section E.
SAMPLING_RATES = (("toutes", 1.0), ("une sur trois", 1 / 3),
                  ("une sur dix", 0.1))

_calls: Counter = Counter()
_section_calls: Counter = Counter()
_current_section = "?"
_capped: set[tuple[str, str]] = set()
_run_at = ""


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def start_section(letter: str, title: str) -> None:
    global _current_section, _section_calls
    _current_section = letter
    _section_calls = Counter()
    print("\n" + "=" * 74)
    print(f"SECTION {letter} - {title}")
    print("=" * 74)


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
# Journal sol_run_log : la seule ecriture autorisee
# ---------------------------------------------------------------------------


def log_run(section: str, label: str, payload: dict) -> None:
    """Ecrit une mesure. Un echec est LOGUE et compte, jamais avale."""
    try:
        db.insert_run_log(RUN_MODE, _run_at, section, label, payload)
    except Exception as error:               # noqa: BLE001 - on veut tout voir
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
    """Teste l'ecriture AVANT toute depense de credits."""
    try:
        log_run("run", "debut", {"caps": CAPS_GLOBAL,
                                 "couts": CREDIT_COST,
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
    """Prix d'un swap : jambe SOL / jambe token, achats et ventes."""
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


def token_mint_of(lines: list[dict]) -> str | None:
    counts: Counter = Counter()
    for line in lines:
        mint = line.get("mint")
        if isinstance(mint, str) and mint and mint not in SOL_MINTS:
            counts[mint] += 1
    return counts.most_common(1)[0][0] if counts else None


def collect_mints(node: Any, found: set[str] | None = None) -> set[str]:
    """Tous les mints non-SOL d'un payload, quelle qu'en soit la forme."""
    if found is None:
        found = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "mint" and isinstance(value, str) and value:
                if value not in SOL_MINTS:
                    found.add(value)
            else:
                collect_mints(value, found)
    elif isinstance(node, list):
        for item in node:
            collect_mints(item, found)
    return found


# ---------------------------------------------------------------------------
# SECTION D - Prix du SOL, 120 jours au moins
# ---------------------------------------------------------------------------

WSOL_MINT = "So11111111111111111111111111111111111111112"
MIN_COVERAGE_DAYS = 120
HOURLY_PAGES = 3            # 1000 bougies horaires ~ 41,6 jours par page

_sol_hourly: list[list] = []
_sol_daily: list[list] = []
_sol_misses = 0
_sol_daily_used = 0


def _reference_pool() -> str | None:
    """Pool le plus liquide ayant le SOL en BASE token."""
    result = gecko(gt.token_pools, WSOL_MINT)
    if result is None:
        log.error("PERTE : pools du SOL indisponibles")
        return None
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
        return None
    attributes = best.get("attributes") or {}
    return attributes.get("address") or best.get("id", "").split("_", 1)[-1]


def section_d() -> dict:
    """Bougies horaires sur plusieurs pages, repli journalier au-dela."""
    start_section("D", "Prix du SOL (chargee en amont : C la consomme)")
    global _sol_hourly, _sol_daily

    pool = _reference_pool()
    if not pool:
        return {"couverture_h": 0, "couverture_j": 0}

    collected: list[list] = []
    before = None
    for page in range(HOURLY_PAGES):
        candles = gecko(gt.ohlcv, pool, "hour", 1000, before)
        if not candles:
            log.warning("bougies horaires : page %d indisponible, arret",
                        page + 1)
            break
        collected += candles
        oldest = min(_to_float(c[0]) for c in candles)
        before = int(oldest) - 1
        print(f"  page {page + 1} : {len(candles)} bougies horaires, "
              f"jusqu'a {_iso(oldest)[:16]}")
    _sol_hourly = sorted(collected, key=lambda c: _to_float(c[0]))

    hours = 0.0
    if _sol_hourly:
        hours = (_to_float(_sol_hourly[-1][0]) - _to_float(_sol_hourly[0][0]))
    days = hours / 86400
    print(f"  horaire : {len(_sol_hourly)} bougies, {days:.1f} jours couverts")

    daily = gecko(gt.ohlcv, pool, "day", 365)
    _sol_daily = sorted(daily or [], key=lambda c: _to_float(c[0]))
    if _sol_daily:
        span = (_to_float(_sol_daily[-1][0]) - _to_float(_sol_daily[0][0])) / 86400
        print(f"  repli journalier : {len(_sol_daily)} bougies, "
              f"{span:.0f} jours - utilise SEULEMENT hors couverture horaire, "
              f"et logue a chaque fois")
    else:
        log.warning("Pas de repli journalier : une conversion hors couverture "
                    "horaire sera comptee comme manquee")

    if days < MIN_COVERAGE_DAYS:
        log.warning("Couverture horaire de %.1f jours < %d demandes : le "
                    "repli journalier prendra le relais", days,
                    MIN_COVERAGE_DAYS)
    return {"couverture_h": len(_sol_hourly), "couverture_j": round(days, 1),
            "journalier": len(_sol_daily), "pool": pool}


def sol_price_at(moment: float) -> float | None:
    """USD par SOL. Horaire d'abord, journalier ensuite, sinon manque."""
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


# ---------------------------------------------------------------------------
# Prix median d'une page de swaps
# ---------------------------------------------------------------------------


def tolerance_for(delta: float) -> float:
    return max(900.0, delta * 0.25)


def page_price(address: str, moment: float,
               tolerance: float) -> tuple[float | None, int, int]:
    """(prix median en SOL, nb de swaps retenus, appels consommes)."""
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
# Preparation : tokens de reference et compte de migration
# ---------------------------------------------------------------------------


def pick_tokens(count: int) -> tuple[list[dict], str]:
    """Memes tokens PumpSwap que les sondes precedentes."""
    response = (
        db.get_client()
        .table(ANALYZED_TABLE)
        .select("mint, symbol, dex, pool_address, pool_created_at")
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
                extra.append({
                    "mint": mint,
                    "symbol": (attributes.get("name") or "?").split("/")[0].strip(),
                    "dex": "pumpswap",
                    "pool_address": address,
                })
    merged = pump + [e for e in extra if e["mint"] not in
                     {p["mint"] for p in pump}]
    return merged[:count], ("geckoterminal/dex_pools(pumpswap)" if merged
                            else "aucune")


def first_transfer(address: str) -> tuple[float | None, str | None]:
    """(date, signature) de la premiere transaction connue d'une adresse."""
    payload = transfers(address, {"limit": 1, "sortOrder": "asc"})
    rows = rows_of(payload)
    if not rows:
        return None, None
    line = rows[0]
    when = _line_time(line)
    signature = line.get("signature")
    return (when or None), (signature if isinstance(signature, str) else None)


def program_ids(transaction: dict) -> set[str]:
    found: set[str] = set()
    for instruction in transaction.get("instructions") or []:
        if not isinstance(instruction, dict):
            continue
        program = instruction.get("programId")
        if isinstance(program, str):
            found.add(program)
        for inner in instruction.get("innerInstructions") or []:
            if isinstance(inner, dict) and isinstance(inner.get("programId"), str):
                found.add(inner["programId"])
    return found


def is_excluded(account: str, mints: set[str]) -> bool:
    if account in mints:
        return True
    return any(account.startswith(prefix) for prefix in EXCLUDED_PREFIXES)


def resolve_migration_accounts(tokens: list[dict]) -> tuple[list[str], str]:
    """(adresses completes, provenance). Les prefixes du 20/09 ne suffisent pas.

    Une adresse Solana ne s'interroge pas par ses 8 premiers caracteres :
    la sonde reconstruit les comptes recurrents puis les APPARIE aux
    prefixes connus. MIGRATION_ACCOUNTS court-circuite tout ca.
    """
    raw = os.environ.get("MIGRATION_ACCOUNTS", "").strip()
    if raw:
        addresses = [a.strip() for a in raw.split(",") if a.strip()]
        print(f"  MIGRATION_ACCOUNTS fournie : {len(addresses)} adresse(s)")
        return addresses, "MIGRATION_ACCOUNTS"

    print(f"  prefixes du run 15:35 a reapparier : "
          f"{', '.join(CANDIDATE_PREFIXES)}")
    signatures: list[str] = []
    mints = {t["mint"] for t in tokens} | {t["pool_address"] for t in tokens}
    for token in tokens[:3]:
        _, signature = first_transfer(token["pool_address"])
        if signature:
            signatures.append(signature)
    if not signatures:
        print("  aucune signature de creation : re-derivation impossible")
        return [], "echec"

    enriched = enhanced(signatures)
    if not enriched:
        print("  enrichissement indisponible : re-derivation impossible")
        return [], "echec"

    seen: Counter = Counter()
    for transaction in enriched:
        accounts: set[str] = set()
        fee_payer = transaction.get("feePayer")
        if isinstance(fee_payer, str):
            accounts.add(fee_payer)
        for entry in transaction.get("accountData") or []:
            if isinstance(entry, dict) and isinstance(entry.get("account"), str):
                accounts.add(entry["account"])
        accounts |= program_ids(transaction)
        for account in accounts:
            seen[account] += 1

    kept = [a for a, n in seen.items()
            if n >= 2 and not is_excluded(a, mints)]
    matched = [a for prefix in CANDIDATE_PREFIXES
               for a in kept if a.startswith(prefix)]
    if matched:
        print(f"  {len(matched)} compte(s) reapparie(s) aux prefixes : "
              + ", ".join(a[:12] + ".." for a in matched))
        return matched, "reapparie par prefixe"

    print(f"  AUCUN des {len(kept)} comptes recurrents ne commence par "
          f"{CANDIDATE_PREFIXES} : criblage horaire de repli")
    return _screen(kept), "criblage horaire"


def _screen(accounts: list[str]) -> list[str]:
    """Repli : garder les comptes au volume horaire de quelques dizaines."""
    now = datetime.now(timezone.utc).timestamp()
    retained: list[str] = []
    for account in accounts[:SCREEN_MAX_ACCOUNTS]:
        payload = transactions(account, {
            "limit": 1000, "sortOrder": "desc",
            "filters": {"blockTime": {"gte": int(now - 3600), "lte": int(now)}},
        })
        if payload == "CAPPED":
            break
        rows = rows_of(payload)
        if rows is None:
            print(f"    {account[:8]}.. : non mesurable "
                  f"({error_of(payload) or 'PERTE'})")
            continue
        verdict = "SATURE" if len(rows) >= 900 else (
            "candidat" if 5 <= len(rows) <= 300 else "hors cible")
        print(f"    {account[:8]}.. : {len(rows):4d} tx/h -> {verdict}")
        if verdict == "candidat":
            retained.append(account)
    return retained


# ---------------------------------------------------------------------------
# SECTION A - Le compte marque-t-il les graduations ?
# ---------------------------------------------------------------------------


def scan_day(account: str, day: str) -> dict:
    """Journee complete d'un compte : lignes, signatures, mints, montants."""
    start, end = _day_bounds(day)
    groups: dict[str, dict] = {}
    lines_total = 0
    sol_amounts: list[float] = []
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
            for line in lines:
                if line.get("mint") in SOL_MINTS:
                    amount = _amount(line)
                    if amount:
                        sol_amounts.append(amount)
            if signature in groups:
                continue
            stamps = [_line_time(line) for line in lines if _line_time(line) > 0]
            pool = None
            for line in lines:
                if line.get("mint") in SOL_MINTS:
                    candidate = line.get("toUserAccount")
                    if isinstance(candidate, str) and candidate != account:
                        pool = candidate
                        break
            groups[signature] = {
                "time": max(stamps) if stamps else start,
                "mint": token_mint_of(lines),
                "pool": pool,
                "lignes": len(lines),
            }
        page_token = next_page_token(payload)
        if not page_token:
            break
    else:
        stopped = f"plafond de {DAY_MAX_PAGES} pages"
        log.warning("Journee %s : plafond de %d pages, chiffres minores",
                    day, DAY_MAX_PAGES)

    return {"day": day, "groups": groups, "lignes": lines_total,
            "sol_amounts": sol_amounts, "calls": calls, "stopped": stopped}


def match_graduations(account: str, tokens: list[dict]) -> dict:
    """Chaque graduation connue a-t-elle une ligne sur ce compte ?"""
    found = same_signature = tested = 0
    details: list[dict] = []
    for token in tokens:
        when = token.get("graduated_at")
        if not when:
            continue
        tested += 1
        payload = transfers(account, {
            "limit": 100, "sortOrder": "asc",
            "filters": {"blockTime": {"gte": int(when - MATCH_WINDOW_S),
                                      "lte": int(when + MATCH_WINDOW_S)}},
        })
        if payload == "CAPPED":
            tested -= 1
            break
        rows = rows_of(payload)
        if rows is None:
            print(f"    {token.get('symbol'):>10} : PERTE "
                  f"({error_of(payload) or 'payload inattendu'})")
            continue
        signatures = {line.get("signature") for line in rows}
        hit = bool(rows)
        exact = token.get("pool_signature") in signatures
        found += 1 if hit else 0
        same_signature += 1 if exact else 0
        mints = collect_mints(rows)
        details.append({"symbol": token.get("symbol"), "hit": hit,
                        "signature_identique": exact,
                        "mint_attendu_present": token["mint"] in mints})
        print(f"    {str(token.get('symbol')):>10} : "
              f"{len(rows):3d} ligne(s) dans +/-{MATCH_WINDOW_S} s | "
              f"signature identique : {'OUI' if exact else 'non'} | "
              f"mint du token present : "
              f"{'OUI' if token['mint'] in mints else 'non'}")
    return {"teste": tested, "trouve": found,
            "signature_identique": same_signature, "details": details}


def amount_distribution(amounts: list[float]) -> dict:
    """Un montant fixe dominant signerait un frais de migration."""
    if not amounts:
        return {}
    rounded = Counter(round(a, 4) for a in amounts)
    top = rounded.most_common(5)
    dominant, count = top[0]
    share = 100 * count / len(amounts)
    print(f"  montants SOL : {len(amounts)} lignes, "
          f"{len(rounded)} valeurs distinctes")
    for value, number in top:
        print(f"    {value:>12.4f} SOL : {number:4d} fois "
              f"({100 * number / len(amounts):5.1f} %)")
    print(f"  mediane {statistics.median(amounts):.4f} SOL | "
          f"min {min(amounts):.4f} | max {max(amounts):.4f}")
    if share >= 50:
        print(f"  -> montant fixe dominant {dominant} SOL a {share:.0f} % : "
              f"signature d'un frais de migration")
    else:
        print(f"  -> pas de montant fixe dominant (le plus frequent fait "
              f"{share:.0f} %) : ce ne sont pas des frais fixes")
    return {"dominant": dominant, "part": round(share, 1),
            "valeurs_distinctes": len(rounded),
            "mediane": round(statistics.median(amounts), 6)}


def section_a(tokens: list[dict]) -> dict:
    start_section("A", "Le compte de migration marque-t-il les graduations ?")

    accounts, provenance = resolve_migration_accounts(tokens)
    if not accounts:
        print("\n  aucun compte exploitable : section A non concluante")
        return {"provenance": provenance, "accounts": [], "conclusion": None}

    known = [t for t in tokens if t.get("graduated_at")]
    print(f"\n  {len(known)}/{len(tokens)} tokens ont une heure de "
          f"graduation connue")

    outcome: dict[str, Any] = {"provenance": provenance, "accounts": accounts}
    retained = None
    for index, account in enumerate(accounts):
        print(f"\n--- compte {index + 1} : {account} ---")
        match = match_graduations(account, known)
        print(f"  retrouves : {match['trouve']}/{match['teste']} | "
              f"meme signature que la creation du pool : "
              f"{match['signature_identique']}/{match['teste']}")
        outcome[f"match_{account[:8]}"] = match
        if match["teste"] and match["trouve"] >= 0.8 * match["teste"]:
            retained = account
            break
        print("  moins de 80 % retrouves : on essaie le compte suivant")

    if retained is None and accounts:
        retained = accounts[0]
        print(f"\n  aucun compte ne passe les 80 % : on poursuit sur "
              f"{retained[:8]}.. pour mesurer quand meme")

    print(f"\n--- journee {TARGET_DAY} sur {retained[:8]}.. ---")
    day = scan_day(retained, TARGET_DAY)
    groups = day["groups"]
    mints = {g["mint"] for g in groups.values() if g["mint"]}
    ratio = day["lignes"] / len(groups) if groups else 0
    print(f"  {day['lignes']} lignes | {len(groups)} signatures | "
          f"{len(mints)} mints distincts | {day['calls']} appels "
          f"({day['stopped']})")
    print(f"  lignes par signature : {ratio:.2f}")
    amounts = amount_distribution(day["sol_amounts"])

    match = outcome.get(f"match_{retained[:8]}") or {}
    tested = match.get("teste") or 0
    hit_rate = (match.get("trouve", 0) / tested) if tested else 0
    mint_rate = len(mints) / len(groups) if groups else 0
    verdict = hit_rate >= 0.8 and mint_rate >= 0.9
    print("\n  CONCLUSION :")
    print(f"    une SIGNATURE = une graduation : "
          f"{'OUI' if verdict else 'NON'} "
          f"({hit_rate:.0%} des graduations connues retrouvees, "
          f"{mint_rate:.0%} de signatures portant un mint distinct)")
    print(f"    une LIGNE = une graduation : NON, il y a {ratio:.2f} lignes "
          f"par signature (jambe SOL + jambe token)")

    outcome.update({"retenu": retained, "jour": day["day"],
                    "lignes": day["lignes"], "signatures": len(groups),
                    "mints": len(mints), "appels": day["calls"],
                    "lignes_par_signature": round(ratio, 2),
                    "montants": amounts, "conclusion": verdict,
                    "arret": day["stopped"]})
    outcome["_groups"] = groups
    return outcome


# ---------------------------------------------------------------------------
# SECTION B - Extraire les mints d'une journee
# ---------------------------------------------------------------------------


def try_config(account: str, label: str, config: dict) -> dict:
    """Un essai de getTransactionsForAddress, message de rejet brut inclus."""
    payload = transactions(account, config)
    if payload == "CAPPED":
        return {"label": label, "statut": "plafond"}
    print(f"\n  > {label}")
    print(f"    config : {json.dumps(config)[:200]}")
    if payload is None:
        print("    PERTE, non concluant")
        return {"label": label, "statut": "perte"}
    error = error_of(payload)
    if error:
        print(f"    REJET : {error}")
        return {"label": label, "statut": "rejet", "message": error}
    rows = rows_of(payload)
    if rows is None:
        print(f"    accepte, mais pas de liste : {_as_json(payload)[:200]}")
        return {"label": label, "statut": "accepte", "lignes": 0, "mints": 0}
    mints = collect_mints(rows)
    print(f"    ACCEPTE : {len(rows)} elements, {len(mints)} mint(s) "
          f"exploitable(s)")
    if rows and not mints:
        print(f"    premier element : {_as_json(rows[0])[:300]}")
    return {"label": label, "statut": "accepte", "lignes": len(rows),
            "mints": len(mints), "_rows": rows, "_mints": mints,
            "_payload": payload}


def voie_transactions(account: str, day: str) -> dict:
    """Voie 1 : getTransactionsForAddress, transactionDetails full."""
    print("\n--- voie (1) getTransactionsForAddress ---")
    print("  Le 17/09 avait etabli que cette methode ne rend que signature, "
          "slot, err et blockTime. Les cles sont testees UNE PAR UNE : une "
          "cle inconnue fait rejeter tout l'objet.")
    start, end = _day_bounds(day)
    window = {"blockTime": {"gte": int(start), "lte": int(end)}}

    attempts = [
        ("transactionDetails full seul",
         {"limit": 20, "sortOrder": "asc", "transactionDetails": "full"}),
        ("filters.tokenAccounts seul",
         {"limit": 20, "sortOrder": "asc",
          "filters": {"tokenAccounts": True}}),
        ("full + fenetre de la journee",
         {"limit": 1000, "sortOrder": "asc", "transactionDetails": "full",
          "filters": window}),
    ]
    calls_before = _section_calls["getTransactionsForAddress"]
    tried = [try_config(account, label, config) for label, config in attempts]

    best = None
    for outcome in reversed(tried):
        if outcome.get("statut") == "accepte" and outcome.get("mints"):
            best = outcome
            break

    mints: set[str] = set(best.get("_mints") or []) if best else set()
    pages = 1 if best else 0
    if best:
        page_token = next_page_token(best.get("_payload"))
        while page_token:
            config = {"limit": 1000, "sortOrder": "asc",
                      "transactionDetails": "full", "filters": window,
                      "paginationToken": page_token}
            payload = transactions(account, config)
            if payload == "CAPPED":
                break
            rows = rows_of(payload)
            if not rows:
                break
            pages += 1
            mints |= collect_mints(rows)
            page_token = next_page_token(payload)

    calls = _section_calls["getTransactionsForAddress"] - calls_before
    credits = calls * CREDIT_COST["getTransactionsForAddress"]
    if not best:
        print("\n  voie (1) INEXPLOITABLE : aucun essai ne rend de mint.")
    else:
        print(f"\n  voie (1) : {len(mints)} mints sur {pages} page(s)")
    return {"mints": mints, "calls": calls, "credits": credits,
            "essais": [{k: v for k, v in t.items() if not k.startswith("_")}
                       for t in tried], "exploitable": bool(best)}


def voie_enhanced(signatures: list[str]) -> dict:
    """Voie 2 : Enhanced /v0/transactions, lots de 100 signatures."""
    print("\n--- voie (2) Enhanced /v0/transactions ---")
    print(f"  {len(signatures)} signatures, lots de {ENRICH_BATCH} a "
          f"{CREDIT_COST['enhanced']} credits le lot")
    mints: set[str] = set()
    dated: dict[str, float] = {}
    calls = 0
    for start in range(0, len(signatures), ENRICH_BATCH):
        batch = signatures[start:start + ENRICH_BATCH]
        enriched = enhanced(batch)
        if enriched is None:
            log.warning("Lot %d indisponible (plafond ou PERTE), voie (2) "
                        "amputee", start // ENRICH_BATCH + 1)
            break
        calls += 1
        for transaction in enriched:
            when = _to_float(transaction.get("timestamp"))
            for mint in collect_mints(transaction.get("tokenTransfers") or []):
                mints.add(mint)
                if when and (mint not in dated or when < dated[mint]):
                    dated[mint] = when
    credits = calls * CREDIT_COST["enhanced"]
    print(f"  voie (2) : {len(mints)} mints, {calls} appels, "
          f"{credits:,} credits")
    return {"mints": mints, "dated": dated, "calls": calls,
            "credits": credits}


def section_b(account: str, groups: dict[str, dict], day: str) -> dict:
    start_section("B", f"Extraire les mints de la journee {day}")
    if not groups:
        print("  aucune signature pour cette journee : section non mesurable")
        return {}

    ordered = sorted(groups.items(), key=lambda item: item[1]["time"])
    signatures = [signature for signature, _ in ordered]
    reference = {data["mint"] for _, data in ordered if data["mint"]}
    print(f"  {len(signatures)} signatures a couvrir")
    print(f"  voie (0) transferts, DEJA PAYEE en section A : "
          f"{len(reference)} mints pour 0 credit de plus")

    voie1 = voie_transactions(account, day)
    voie2 = voie_enhanced(signatures)

    best_set = reference or voie2["mints"] or voie1["mints"]
    print("\n  comparaison :")
    print(f"    {'voie':<34}{'mints':>7}{'appels':>8}{'credits':>10}"
          f"{'par graduation':>16}")
    rows_out = []
    for label, mints, calls, credits in (
        ("(0) transferts (section A)", reference, 0, 0),
        ("(1) getTransactionsForAddress", voie1["mints"], voie1["calls"],
         voie1["credits"]),
        ("(2) Enhanced", voie2["mints"], voie2["calls"], voie2["credits"]),
    ):
        per = credits / len(mints) if mints else 0
        print(f"    {label:<34}{len(mints):>7}{calls:>8}{credits:>10,}"
              f"{per:>16.1f}")
        rows_out.append({"voie": label, "mints": len(mints), "appels": calls,
                         "credits": credits, "par_graduation": round(per, 2)})

    missing = best_set - (voie1["mints"] | voie2["mints"])
    if missing:
        print(f"    {len(missing)} mint(s) que seule la voie (0) voit")

    listing = []
    for signature, data in ordered:
        mint = data["mint"]
        if not mint:
            continue
        listing.append({"mint": mint, "heure": _iso(data["time"])[11:19],
                        "time": data["time"], "pool": data.get("pool"),
                        "signature": signature})
    print(f"\n  liste datee : {len(listing)} graduations "
          f"(10 premieres ci-dessous, la liste complete part dans "
          f"{RUN_LOG_TABLE})")
    for entry in listing[:10]:
        print(f"    {entry['heure']}  {entry['mint']}")

    return {"comparaison": rows_out, "listing": listing,
            "voie1_exploitable": voie1["exploitable"],
            "essais_voie1": voie1["essais"],
            "mints_voie2": len(voie2["mints"]),
            "credits_voie1": voie1["credits"],
            "credits_voie2": voie2["credits"]}


# ---------------------------------------------------------------------------
# SECTION C - Echantillon enfin aleatoire
# ---------------------------------------------------------------------------


def measure_token(entry: dict) -> dict:
    """Trajectoire en SOL. Le prix a la graduation sert de reference."""
    now = datetime.now(timezone.utc).timestamp()
    graduated = _to_float(entry.get("time"))
    mint = entry["mint"]
    addresses = [a for a in (entry.get("pool"), mint) if a]
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
    return {"mint": mint, "reference": reference, "prices": prices,
            "due": due, "obtained": obtained, "calls": calls,
            "supply": supply, "mcap": best_mcap, "verdict": verdict}


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
    for page in range(CURVE_MAX_PAGES):
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


def section_c(listing: list[dict]) -> dict:
    start_section("C", "Echantillon enfin aleatoire")
    if not listing:
        print("  la section B n'a produit aucune liste de mints.")
        print("  SECTION ARRETEE : pas de repli sur les tokens de la "
              "section B des sondes precedentes, l'echantillon ne serait "
              "plus aleatoire et le taux de succes par classe ne voudrait "
              "rien dire.")
        return {"arretee": True, "raison": "section B sans liste"}

    rng = random.Random(RANDOM_SEED)
    sample = rng.sample(listing, min(SAMPLE_SIZE, len(listing)))
    print(f"  seed {RANDOM_SEED} | population {len(listing)} | "
          f"tires {len(sample)} (morts compris)")
    print(f"  prix = mediane des swaps de la page, en SOL. "
          f"Mort = prix a 24 h < {DEAD_RATIO:.0%} du prix de graduation.\n")

    results: list[dict] = []
    for entry in sample:
        outcome = measure_token(entry)
        results.append(outcome)
        log.info("  %s.. %s : %s | %d/%d points | mcap max %.0f $",
                 entry["mint"][:8], entry["heure"], outcome["verdict"],
                 outcome["obtained"], outcome["due"], outcome["mcap"])

    by_verdict: Counter = Counter(r["verdict"] for r in results)
    print(f"\n  classement : {dict(by_verdict)}")
    print("  taux de succes du prix PAR CLASSE :")
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
    if curve_calls:
        print(f"  appels par courbe : mediane "
              f"{statistics.median(curve_calls):.0f} | max {max(curve_calls)}")
    if curve_buyers:
        print(f"  acheteurs distincts : mediane "
              f"{statistics.median(curve_buyers):.0f} | max {max(curve_buyers)}")

    return {"arretee": False, "seed": RANDOM_SEED, "population": len(listing),
            "echantillon": len(sample), "verdicts": dict(by_verdict),
            "par_classe": per_class,
            "appels_trajectoire": trajectory_calls,
            "appels_courbe": curve_calls, "acheteurs": curve_buyers,
            "mcaps": mcaps}


# ---------------------------------------------------------------------------
# SECTION E - Recapitulatif et dimensionnement
# ---------------------------------------------------------------------------


def credits_spent() -> int:
    return sum(_calls[method] * CREDIT_COST.get(method, 0) for method in _calls)


def show_budget(title: str) -> None:
    print(f"\n{title}")
    for method, count in sorted(_calls.items()):
        cost = CREDIT_COST.get(method, 0)
        cap = CAPS_GLOBAL.get(method)
        cap_text = f" / plafond {cap}" if cap else ""
        print(f"  {method:28} : {count:5d} appels{cap_text}"
              f" -> {count * cost:>7,} credits ({cost}/appel)")
    print(f"  {'TOTAL':28} : {credits_spent():>7,} credits")


def final_recap(results: dict) -> dict:
    print("\n" + "=" * 74)
    print("SECTION E - RECAPITULATIF ET DIMENSIONNEMENT")
    print("=" * 74)

    unit = CREDIT_COST["getTransfersByAddress"]
    a = results.get("a") or {}
    b = results.get("b") or {}
    c = results.get("c") or {}
    d = results.get("d") or {}

    graduations = a.get("signatures") or 0
    listing_calls = a.get("appels") or 0
    listing_credits = listing_calls * unit

    traj = c.get("appels_trajectoire") or []
    curve = c.get("appels_courbe") or []
    per_traj = statistics.median(traj) if traj else 0
    per_curve = statistics.median(curve) if curve else 0

    print(f"\nCompte de migration : {a.get('retenu') or 'aucun'} "
          f"({a.get('provenance')})")
    print(f"  une signature = une graduation : "
          f"{'OUI' if a.get('conclusion') else 'NON ou non etabli'}")
    print(f"  {a.get('lignes_par_signature', 0):.2f} lignes par signature")
    print(f"\nGradues par jour ({a.get('jour')})     : {graduations}")
    print(f"Credits pour lister une journee   : {listing_credits:,} "
          f"({listing_calls} appels de transferts)")
    if b:
        print(f"  voie (1) getTransactionsForAddress : "
              f"{b.get('credits_voie1', 0):,} credits "
              f"({'exploitable' if b.get('voie1_exploitable') else 'INEXPLOITABLE'})")
        print(f"  voie (2) Enhanced                  : "
              f"{b.get('credits_voie2', 0):,} credits")
    print(f"Credits par token (trajectoire)   : {per_traj * unit:,.0f} "
          f"({per_traj:.1f} appels)")
    print(f"Credits par token (courbe)        : {per_curve * unit:,.0f} "
          f"({per_curve:.0f} appels)")

    if c.get("par_classe"):
        print("\nTaux de succes du prix par classe :")
        for verdict, stats in c["par_classe"].items():
            print(f"  {verdict:>12} : {stats['taux']:5.1f} % "
                  f"({stats['tokens']} tokens)")

    print(f"\nPrix du SOL : {d.get('couverture_j', 0)} jours en horaire "
          f"({MIN_COVERAGE_DAYS} demandes), {_sol_daily_used} conversion(s) "
          f"par repli journalier")
    print(f"  conversions hors couverture : {_sol_misses} "
          f"({'OK' if _sol_misses == 0 else 'A CORRIGER, doit finir a 0'})")

    show_budget("Consommation reelle de cette sonde :")

    print(f"\nDIMENSIONNEMENT sur {MONTHLY_CREDITS:,} credits/mois")
    print(f"  {graduations} graduations/jour, suivi complet = trajectoire + "
          f"courbe = {(per_traj + per_curve) * unit:,.0f} credits/token")
    projection = {}
    if not graduations or not per_traj:
        print("  NON DIMENSIONNABLE : il manque le nombre de graduations "
              "(section A) ou le cout par token (section C).")
        return {"gradues_par_jour": graduations,
                "credits_listing_jour": listing_credits,
                "appels_trajectoire": per_traj, "appels_courbe": per_curve,
                "sol_misses": _sol_misses,
                "sol_repli_journalier": _sol_daily_used,
                "projection": {}, "credits_sonde": credits_spent(),
                "plafonds_atteints": sorted(f"{s}:{m}" for s, m in _capped)}
    for label, rate in SAMPLING_RATES:
        followed = graduations * rate
        daily = listing_credits + followed * (per_traj + per_curve) * unit
        monthly = daily * 30
        verdict = "TIENT" if monthly <= MONTHLY_CREDITS else "NE TIENT PAS"
        print(f"  {label:<15} : {followed:6.0f} tokens/jour -> "
              f"{monthly:>12,.0f} credits/mois  {verdict}")
        projection[label] = {"tokens_par_jour": round(followed, 1),
                             "credits_par_mois": round(monthly),
                             "tient": monthly <= MONTHLY_CREDITS}

    if _capped:
        print(f"\n  {len(_capped)} plafond(s) atteint(s) : chiffres MINORANTS")
        for section, method in sorted(_capped):
            print(f"    section {section} : {method}")

    return {"gradues_par_jour": graduations,
            "credits_listing_jour": listing_credits,
            "appels_trajectoire": per_traj, "appels_courbe": per_curve,
            "sol_misses": _sol_misses, "sol_repli_journalier": _sol_daily_used,
            "projection": projection,
            "credits_sonde": credits_spent(),
            "plafonds_atteints": sorted(f"{s}:{m}" for s, m in _capped)}


# ---------------------------------------------------------------------------
# Entree
# ---------------------------------------------------------------------------


def load_graduations(tokens: list[dict]) -> int:
    """Heure et signature de graduation des tokens de reference."""
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
    print("\nSonde univers v3 : le mint de chaque graduation, et le prix "
          "des morts.")
    print(f"Aucune ecriture en base hors {RUN_LOG_TABLE}.")
    print(f"Plafonds globaux : {CAPS_GLOBAL}")
    show_budget("Cout unitaire retenu :")

    # Le journal est teste AVANT toute depense : une table absente ne doit
    # pas se decouvrir apres 10 000 credits.
    if not check_run_log():
        return

    results: dict[str, Any] = {}
    results["d"] = section_d()
    log_run("D", "prix du SOL", results["d"])

    tokens, provenance = pick_tokens(TOKENS_SECTION_A)
    if not tokens:
        log.error("Aucun token de reference, sonde interrompue.")
        log_run("run", "arret", {"raison": "aucun token de reference"})
        return
    known = load_graduations(tokens)
    print(f"  {known}/{len(tokens)} graduations datees ({provenance})")

    results["a"] = section_a(tokens)
    groups = results["a"].pop("_groups", {})
    log_run("A", "compte de migration", results["a"])

    retained = results["a"].get("retenu")
    if retained and groups:
        results["b"] = section_b(retained, groups, TARGET_DAY)
        log_run("B", f"mints du {TARGET_DAY}", results["b"])
    else:
        print("\nSECTION B sautee : pas de journee exploitable en section A.")
        results["b"] = {}

    results["c"] = section_c((results["b"] or {}).get("listing") or [])
    log_run("C", "echantillon aleatoire", results["c"])

    recap = final_recap(results)
    log_run("E", "recapitulatif", recap)
    print(f"\n{RUN_LOG_TABLE} : run {_run_at} ecrit, 6 lignes.")


if __name__ == "__main__":
    main()
