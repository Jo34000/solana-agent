"""Experience 1 : la fenetre exploitable apres une graduation.

Ce n'est plus une sonde. Les resultats sont ecrits dans sol_grad_paths,
token par token, au fil de l'eau : un arret au plafond de credits ne perd
rien de ce qui precede, et une relance saute les mints deja presents.

Question posee : un humain alerte avec 5, 15 ou 60 minutes de latence
dispose-t-il d'une fenetre exploitable ? La reponse est une MATRICE
(latence x horizon), pas une opinion.

Acquis des sondes, qui ne sont plus remesures : regle du pool validee
9/10 en brut comme en Enhanced, test de la courbe 476/476, environ 1 100
graduations par jour, prix obtenu a 92 % y compris sur les tokens morts.

Ecarts releves AVANT ecriture, et ce qui a ete decide :

  1. "Sans appel si possible" pour la section 0 : la premiere verification
     l'est, la seconde ne l'est pas. Les 1 181 signatures de 39azUYFW ne
     sont pas persistees telles quelles, mais elles se reconstituent
     exactement : 857 propres (sonde v6) + 324 connues (sonde v4), et le
     total est VERIFIE contre le nombre que la v6 a ecrit. En revanche les
     48 transactions "pool_absent" n'existent qu'en COMPTE dans la v7 :
     les croiser avec les 48 frais a 0,0150 SOL demande de relire la
     journee du 17/09, soit UN appel a 100 credits.
  2. Un token INACTIF a un instant et une PERTE d'appel sont deux choses
     differentes. Un inactif est une mesure (prix null, actif = false) ;
     une perte n'en est pas une et n'est jamais enregistree comme telle,
     sans quoi la matrice compterait des silences d'API comme des tokens
     sans acheteur.
  3. Le schema de sol_grad_paths n'est pas specifie : il est defini dans
     supabase_client.py, rappele dans le README, et l'ecriture est TESTEE
     au demarrage avant la moindre depense.
  4. Le rendement du benchmark naif depend de ce qu'on fait des tokens
     devenus inactifs a l'horizon. La sonde en donne DEUX versions :
     exclus (optimiste, biais du survivant) et comptes a zero (pessimiste).
     La verite est entre les deux, et aucune des deux n'est presentee
     seule.
"""

from __future__ import annotations

import json
import logging
import os
import random
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

import geckoterminal as gt
import graduations as rules
import helius
import supabase_client as db
from config import (
    GRAD_PATHS_TABLE,
    RUN_LOG_TABLE,
    diagnose_environment,
    setup_logging,
)

log = logging.getLogger("solana-agent")

RUN_MODE = "exp1_window"

# Sondes dont les acquis sont relus.
V4_RUN_MODE = "probe_universe_v4"
V6_RUN_MODE = "probe_universe_v6"
V7_RUN_MODE = "probe_universe_v7"

FEE_PREFIX = "9C4nRvhh"
BACKUP_PREFIX = "39azUYFW"
TARGET_DAY = "2026-09-17"      # journee des verifications de la section 0
VARIANT_FEE = 0.0150           # frais des migrations a elucider


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, "").strip() or default))
    except ValueError:
        log.warning("%s illisible, valeur par defaut %s", name, default)
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        log.warning("%s illisible, valeur par defaut %s", name, default)
        return default


WINDOW_DAYS = _env_int("WINDOW_DAYS", 3)
MAX_CREDITS = _env_int("MAX_CREDITS", 130_000)
ENTRY_MCAP_USD = _env_float("ENTRY_MCAP_USD", 60_000.0)
STAGE1_SAMPLE = _env_float("STAGE1_SAMPLE", 1.0)
RANDOM_SEED = _env_int("RANDOM_SEED", 20260927)

# Une journee n'est mesurable que si son horizon le plus lointain est
# echu : 7 jours, plus une marge d'un jour.
MATURITY_DAYS = 8

STAGE1_POINTS = (("5 min", 300), ("15 min", 900), ("60 min", 3600))
STAGE2_POINTS = (("2 h", 7200), ("3 h", 10800), ("6 h", 21600),
                 ("12 h", 43200), ("24 h", 86400), ("3 j", 259200),
                 ("7 j", 604800))
ALL_POINTS = STAGE1_POINTS + STAGE2_POINTS
POINT_SECONDS = dict(ALL_POINTS)
LATENCIES = [label for label, _ in STAGE1_POINTS]
BASE_RATE_THRESHOLDS = (100_000.0, 1_000_000.0, 5_000_000.0)
RETURN_BUCKETS = ((2.0, ">= x2"), (5.0, ">= x5"), (10.0, ">= x10"))
LOSS_BUCKET = 0.3

DAY_MAX_PAGES = 40

CREDIT_COST = {
    "getTransfersByAddress": 10,
    "getTransactionsForAddress": 100,
    "enhanced": 100,
    "getTokenSupply": 1,
    "coingecko": 0,
}

_credits = 0
_credits_by_section: Counter = Counter()
_calls: Counter = Counter()
_current_section = "?"
_budget_reached = False
_incoherences: list[str] = []
_run_at = ""


# ---------------------------------------------------------------------------
# Budget : un plafond de CREDITS, pas de nombres d'appels
# ---------------------------------------------------------------------------


def start_section(letter: str, title: str) -> None:
    global _current_section
    _current_section = letter
    print("\n" + "=" * 74)
    print(f"SECTION {letter} - {title}")
    print("=" * 74)


def credits_left() -> int:
    return MAX_CREDITS - _credits


def can_spend(method: str) -> bool:
    """Le plafond est un ARRET PROPRE, jamais un silence."""
    global _budget_reached
    cost = CREDIT_COST.get(method, 0)
    if _credits + cost > MAX_CREDITS:
        if not _budget_reached:
            _budget_reached = True
            log.warning("PLAFOND DE CREDITS atteint : %d/%d consommes, arret "
                        "propre. Ce qui est ecrit en base est conserve.",
                        _credits, MAX_CREDITS)
        return False
    return True


def _spend(method: str) -> None:
    global _credits
    cost = CREDIT_COST.get(method, 0)
    _credits += cost
    _calls[method] += 1
    _credits_by_section[_current_section] += cost


def _masked(text: str) -> str:
    key = os.environ.get("HELIUS_API_KEY", "").strip()
    return text.replace(key, "***") if key else text


def _as_json(value: Any) -> str:
    try:
        return _masked(json.dumps(value, indent=2, ensure_ascii=False,
                                  default=str))
    except (TypeError, ValueError):
        return _masked(repr(value))


def _iso(moment: float) -> str:
    try:
        return datetime.fromtimestamp(moment, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return repr(moment)


def _day_bounds(day: str) -> tuple[float, float]:
    start = datetime.fromisoformat(day + "T00:00:00+00:00").timestamp()
    return start, start + 86400


def coherent(label: str, condition: bool, detail: str) -> bool:
    if condition:
        return True
    _incoherences.append(f"{label} : {detail}")
    print(f"    INCOHERENT - {label} : {detail}")
    log.warning("INCOHERENCE : %s (%s)", label, detail)
    return False


def window_days() -> list[str]:
    """Journees completes se terminant il y a au moins MATURITY_DAYS jours."""
    today = datetime.now(timezone.utc).date()
    end = today - timedelta(days=MATURITY_DAYS)
    return [(end - timedelta(days=offset)).isoformat()
            for offset in range(WINDOW_DAYS - 1, -1, -1)]


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


def token_supply(mint: str) -> dict | None:
    if not can_spend("getTokenSupply"):
        return None
    _spend("getTokenSupply")
    return helius.get_token_supply(mint)


def gecko(call, *args, **kwargs):
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


# ---------------------------------------------------------------------------
# Relecture des acquis, et journal
# ---------------------------------------------------------------------------


def read_payload(run_mode: str, section: str) -> dict:
    try:
        rows = db.fetch_run_log(run_mode, section, 5)
    except Exception as error:               # noqa: BLE001
        log.error("PERTE : relecture de %s/%s impossible : %s",
                  run_mode, section, error)
        return {}
    for row in rows:
        payload = row.get("payload") or {}
        if payload:
            return payload
    return {}


def log_run(section: str, label: str, payload: dict) -> None:
    try:
        db.insert_run_log(RUN_MODE, _run_at, section, label, payload)
    except Exception as error:               # noqa: BLE001
        log.error("PERTE : %s non ecrit dans %s : %s", label, RUN_LOG_TABLE,
                  error)
        raise


CREATE_SQL = """create table if not exists sol_grad_paths (
  mint           text primary key,
  pool           text,
  signature      text,
  signer         text,
  grad_at        timestamptz,
  jour           date,
  status         text,
  stage          int,
  supply         numeric,
  points         jsonb,
  mcap_max_usd   numeric,
  points_actifs  int,
  points_mesures int,
  updated_at     timestamptz default now()
);"""


def check_tables() -> bool:
    """Les deux tables sont testees AVANT la moindre depense de credit."""
    try:
        log_run("run", "debut", {"fenetre": window_days(),
                                 "max_credits": MAX_CREDITS,
                                 "entry_mcap_usd": ENTRY_MCAP_USD,
                                 "stage1_sample": STAGE1_SAMPLE})
    except Exception as error:               # noqa: BLE001
        print(f"\n{RUN_LOG_TABLE} inutilisable : {error}")
        return False
    try:
        db.upsert_grad_path({
            "mint": "__probe__", "status": "test_ecriture", "stage": 0,
            "points": {}, "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as error:               # noqa: BLE001
        print(f"\n{GRAD_PATHS_TABLE} inutilisable : {error}")
        print("L'experience s'arrete AVANT de depenser le moindre credit.")
        print("SQL de creation attendu :\n" + CREATE_SQL)
        return False
    print(f"{GRAD_PATHS_TABLE} : ecriture confirmee")
    return True


# ---------------------------------------------------------------------------
# Prix du SOL, bougies horaires
# ---------------------------------------------------------------------------

_sol_hourly: list[list] = []
_sol_daily: list[list] = []
_sol_misses = 0


def load_sol_prices() -> dict:
    """Une page horaire (41 jours) couvre largement une fenetre de 3 jours."""
    global _sol_hourly, _sol_daily
    result = gecko(gt.token_pools, rules.WSOL_MINT)
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
        if not base_id.endswith(rules.WSOL_MINT):
            continue
        liquidity = rules.to_float(attributes.get("reserve_in_usd"))
        if liquidity > best_liquidity:
            best, best_liquidity = pool, liquidity
    if best is None:
        log.error("PERTE : aucun pool avec le SOL en base token")
        return {}
    attributes = best.get("attributes") or {}
    address = attributes.get("address") or best.get("id", "").split("_", 1)[-1]
    _sol_hourly = sorted(gecko(gt.ohlcv, address, "hour", 1000) or [],
                         key=lambda c: rules.to_float(c[0]))
    _sol_daily = sorted(gecko(gt.ohlcv, address, "day", 365) or [],
                        key=lambda c: rules.to_float(c[0]))
    days = 0.0
    if _sol_hourly:
        days = (rules.to_float(_sol_hourly[-1][0])
                - rules.to_float(_sol_hourly[0][0])) / 86400
    print(f"  prix du SOL : {len(_sol_hourly)} bougies horaires "
          f"({days:.0f} jours), {len(_sol_daily)} journalieres en repli")
    return {"horaire": len(_sol_hourly), "jours": round(days, 1)}


def sol_price_at(moment: float) -> float | None:
    global _sol_misses
    for candles, window in ((_sol_hourly, 3600), (_sol_daily, 86400)):
        if not candles:
            continue
        closest = min(candles,
                      key=lambda c: abs(rules.to_float(c[0]) - moment))
        if abs(rules.to_float(closest[0]) - moment) <= window:
            price = rules.to_float(closest[4])
            if price:
                return price
    _sol_misses += 1
    return None


def tolerance_for(delta: float) -> float:
    return max(900.0, delta * 0.25)


def price_at(pool: str, moment: float,
             delta: float) -> tuple[float | None, str]:
    """(prix en SOL, etat). etat : actif, inactif, perte ou plafond.

    INACTIF et PERTE ne sont pas la meme chose : le premier est une
    mesure, le second un silence de l'API, et il n'est jamais enregistre
    comme une absence d'acheteur.
    """
    payload = transfers(pool, {
        "limit": 100, "sortOrder": "asc",
        "filters": {"blockTime": {"gte": int(moment)}},
    })
    if payload == "CAPPED":
        return None, "plafond"
    rows = rows_of(payload)
    if rows is None:
        return None, "perte"
    price, _ = rules.median_swap_price(rows, moment, tolerance_for(delta))
    return (price, "actif") if price is not None else (None, "inactif")


# ---------------------------------------------------------------------------
# SECTION 0 - Deux verifications
# ---------------------------------------------------------------------------


def fetch_day_raw(account: str, day: str) -> dict:
    """La journee en getTransactionsForAddress full / limit 1000, paginee."""
    start, end = _day_bounds(day)
    config = {"limit": 1000, "sortOrder": "asc", "transactionDetails": "full",
              "filters": {"blockTime": {"gte": int(start), "lte": int(end)}}}
    payload = transactions(account, config)
    calls = 1
    rows = rows_of(payload)
    if rows is None:
        return {"rows": None, "calls": calls,
                "erreur": error_of(payload) or "payload inattendu"}
    page_token = next_page_token(payload)
    while page_token and calls < DAY_MAX_PAGES:
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
    return {"rows": rows, "calls": calls, "erreur": ""}


def section_0(accounts: dict[str, str]) -> dict:
    start_section("0", "Deux verifications sur les donnees du 17/09")

    # --- (1) 39azUYFW est-il l'index unique de l'univers ? ----------------
    liste_a = [e for e in (read_payload(V7_RUN_MODE, "A").get("listing") or [])
               if isinstance(e, dict) and e.get("signature")]
    v6_payload = read_payload(V6_RUN_MODE, "B")
    propres = [s for s in (v6_payload.get("signatures_propres") or [])
               if isinstance(s, str)]
    connues = [e.get("signature") for e in
               (read_payload(V4_RUN_MODE, "B").get("listing") or [])
               if isinstance(e, dict) and e.get("signature")]
    index = set(propres) | set(connues)
    annonce = v6_payload.get("secours")
    print(f"  liste A (sonde v7)      : {len(liste_a)} signatures")
    print(f"  index 39azUYFW reconstitue : {len(propres)} propres + "
          f"{len(connues)} connues = {len(index)}")
    if annonce:
        coherent("index reconstitue", len(index) == annonce,
                 f"{len(index)} reconstituees pour {annonce} annoncees par "
                 f"la sonde v6")
        print(f"    (la sonde v6 en annoncait {annonce})")

    signatures_a = {e["signature"] for e in liste_a}
    inside = signatures_a & index
    share = len(inside) / len(signatures_a) if signatures_a else 0.0
    print(f"  intersection : {len(inside)}/{len(signatures_a)} "
          f"({share:.1%})")
    complete = bool(signatures_a) and len(inside) == len(signatures_a)
    autres = read_payload(V7_RUN_MODE, "B").get("autres_signataires") or {}
    if complete:
        print(f"  -> {BACKUP_PREFIX} est l'INDEX UNIQUE de l'univers : une "
              f"seule adresse a lire par journee")
    else:
        print(f"  -> index INCOMPLET : {len(signatures_a) - len(inside)} "
              f"signature(s) de la liste A en sont absentes")
        print("     comptes supplementaires a lire :")
        for account in [accounts.get("frais")] + list(autres):
            if account:
                print(f"       {account}")

    # --- (2) les 48 pool_absent sont-elles les 48 a 0,0150 SOL ? ----------
    print(f"\n  les migrations a {VARIANT_FEE} SOL sont-elles celles sans "
          f"pool ? (un appel, 100 credits : la v7 n'a persiste que des "
          f"comptes)")
    variant_signatures = {e.get("signature") for e in
                          (read_payload(V4_RUN_MODE, "B").get("listing") or [])
                          if isinstance(e, dict)
                          and abs(rules.to_float(e.get("frais")) - VARIANT_FEE)
                          < 1e-6}
    fee_account = accounts.get("frais")
    without_pool: dict[str, str] = {}
    if fee_account:
        day = fetch_day_raw(fee_account, TARGET_DAY)
        if day["rows"] is None:
            print(f"    relecture impossible : {day['erreur']}")
        else:
            for row in day["rows"]:
                if not isinstance(row, dict):
                    continue
                outcome = rules.graduation_of(row)
                if outcome and outcome.get("motif") == "pool_absent":
                    signature, _ = rules.extract_signature(row)
                    if signature:
                        without_pool[signature] = outcome.get("mint") or ""
    shared = set(without_pool) & variant_signatures
    print(f"    sans pool : {len(without_pool)} | a {VARIANT_FEE} SOL : "
          f"{len(variant_signatures)} | communes : {len(shared)}")
    coincide = bool(without_pool) and len(shared) == len(without_pool)
    if coincide:
        print("    -> elles COINCIDENT : ces migrations sont une variante "
              "non couverte, comptees et non mesurees")
    elif shared:
        print("    -> recouvrement PARTIEL : les deux ensembles ne se "
              "confondent pas")
    else:
        print("    -> aucun recouvrement : deux phenomenes distincts")

    written = 0
    for signature, mint in without_pool.items():
        if not mint:
            continue
        try:
            db.upsert_grad_path({
                "mint": mint, "signature": signature, "jour": TARGET_DAY,
                "status": "variante_non_couverte", "stage": 0, "points": {},
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            written += 1
        except Exception as error:           # noqa: BLE001
            log.error("PERTE : variante %s non ecrite : %s", mint[:8], error)
            raise
    if written:
        print(f"    {written} migration(s) enregistree(s) en "
              f"status = variante_non_couverte")

    return {"liste_a": len(signatures_a), "index": len(index),
            "intersection": len(inside), "part": round(share, 4),
            "index_unique": complete,
            "comptes_supplementaires": [a for a in
                                        [accounts.get("frais")] + list(autres)
                                        if a] if not complete else [],
            "sans_pool": len(without_pool),
            "variante_frais": len(variant_signatures),
            "communes": len(shared), "coincident": coincide,
            "variantes_ecrites": written}


# ---------------------------------------------------------------------------
# SECTION 1 - Univers de la fenetre
# ---------------------------------------------------------------------------


def section_1(account: str, days: list[str]) -> dict:
    start_section("1", f"Univers de la fenetre ({', '.join(days)})")
    universe: list[dict] = []
    per_day: dict[str, int] = {}
    motifs: Counter = Counter()
    calls = 0
    seen: set[str] = set()

    for day in days:
        outcome = fetch_day_raw(account, day)
        calls += outcome["calls"]
        rows = outcome["rows"]
        if rows is None:
            log.error("PERTE : journee %s illisible (%s)", day,
                      outcome["erreur"])
            per_day[day] = 0
            continue
        kept = 0
        for row in rows:
            if not isinstance(row, dict):
                motifs["ligne_inattendue"] += 1
                continue
            result = rules.graduation_of(row)
            motifs[result.get("motif", "?")] += 1
            if result.get("motif") != "graduation":
                continue
            mint = result["mint"]
            if mint in seen:
                motifs["doublon"] += 1
                continue
            seen.add(mint)
            kept += 1
            universe.append({"mint": mint, "pool": result["pool"],
                             "grad_at": result["grad_at"],
                             "signature": result["signature"],
                             "signer": result.get("signer"), "jour": day})
        per_day[day] = kept
        print(f"  {day} : {len(rows)} transactions -> {kept} graduations "
              f"({outcome['calls']} appel(s))")

    print(f"\n  motifs : {dict(motifs.most_common())}")
    total = sum(per_day.values())
    coherent("univers = somme des journees", total == len(universe),
             f"{total} par journee pour {len(universe)} au total")
    print(f"  univers : {len(universe)} graduations sur {len(days)} journees "
          f"({total / max(len(days), 1):.0f} par jour)")
    return {"universe": universe, "par_jour": per_day,
            "motifs": dict(motifs), "appels": calls}


# ---------------------------------------------------------------------------
# SECTIONS 2 et 3 - Les deux etapes de mesure
# ---------------------------------------------------------------------------


def measure_points(record: dict, points: tuple[tuple[str, int], ...],
                   supply: float) -> tuple[dict, bool]:
    """Mesure une serie d'instants. (points, plafond atteint)."""
    measured: dict[str, dict] = {}
    pool = record["pool"]
    graduated = rules.to_float(record["grad_at"])
    for label, delta in points:
        price, state = price_at(pool, graduated + delta, float(delta))
        if state == "plafond":
            return measured, True
        entry: dict[str, Any] = {"actif": state == "actif",
                                 "prix_sol": price, "etat": state}
        if price is not None and supply > 0:
            usd = sol_price_at(graduated + delta)
            if usd:
                entry["mcap_usd"] = price * usd * supply
        measured[label] = entry
    return measured, False


def row_from(record: dict, points: dict, supply: float, stage: int) -> dict:
    mcaps = [p["mcap_usd"] for p in points.values() if p.get("mcap_usd")]
    return {
        "mint": record["mint"], "pool": record["pool"],
        "signature": record.get("signature"), "signer": record.get("signer"),
        "grad_at": _iso(rules.to_float(record["grad_at"])),
        "jour": record["jour"], "status": "mesure", "stage": stage,
        "supply": supply or None, "points": points,
        "mcap_max_usd": max(mcaps) if mcaps else None,
        "points_actifs": sum(1 for p in points.values() if p.get("actif")),
        "points_mesures": sum(1 for p in points.values()
                              if p.get("etat") in ("actif", "inactif")),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def section_2(universe: list[dict], existing: dict[str, dict],
              rng: random.Random) -> dict:
    start_section("2", "Etape 1 : trois premiers instants, toutes les "
                       "graduations")
    todo = [g for g in universe if g["mint"] not in existing]
    print(f"  {len(existing)} deja en base, {len(todo)} a mesurer")
    if STAGE1_SAMPLE < 1.0:
        keep = max(1, int(round(len(todo) * STAGE1_SAMPLE)))
        todo = rng.sample(todo, min(keep, len(todo)))
        print(f"  STAGE1_SAMPLE={STAGE1_SAMPLE} (seed {RANDOM_SEED}) : "
              f"{len(todo)} tirees au hasard")

    unit = (len(STAGE1_POINTS) * CREDIT_COST["getTransfersByAddress"]
            + CREDIT_COST["getTokenSupply"])
    print(f"  cout unitaire : {unit} credits | budget restant : "
          f"{credits_left():,} -> {credits_left() // unit if unit else 0} "
          f"token(s) mesurables")

    written = 0
    states: Counter = Counter()
    stopped = "termine"
    for record in todo:
        if not can_spend("getTransfersByAddress"):
            stopped = "plafond de credits"
            break
        supply = rules.to_float((token_supply(record["mint"]) or {})
                                .get("uiAmount"))
        points, capped = measure_points(record, STAGE1_POINTS, supply)
        for entry in points.values():
            states[entry["etat"]] += 1
        if points:
            db.upsert_grad_path(row_from(record, points, supply, 1))
            written += 1
            existing[record["mint"]] = {**record, "points": points,
                                        "supply": supply, "stage": 1}
        if capped:
            stopped = "plafond de credits"
            break
        if written % 50 == 0 and written:
            log.info("  etape 1 : %d ecrites, %d credits consommes",
                     written, _credits)

    print(f"\n  {written} trajectoires ecrites ({stopped})")
    print(f"  etats des points : {dict(states)}")
    if states["perte"]:
        log.warning("%d point(s) en PERTE : ce ne sont PAS des tokens "
                    "inactifs, ils sont enregistres comme perte",
                    states["perte"])
    return {"a_mesurer": len(todo), "ecrites": written,
            "etats": dict(states), "arret": stopped}


def section_3(existing: dict[str, dict]) -> dict:
    start_section("3", f"Etape 2 : au-dela de {ENTRY_MCAP_USD:,.0f} $ de "
                       f"capitalisation")
    candidates = []
    for mint, record in existing.items():
        if record.get("stage", 0) >= 2 or record.get("status") not in (
                None, "mesure"):
            continue
        points = record.get("points") or {}
        mcaps = [p.get("mcap_usd") for label, p in points.items()
                 if label in dict(STAGE1_POINTS) and p.get("mcap_usd")]
        if mcaps and max(mcaps) >= ENTRY_MCAP_USD:
            candidates.append((mint, record))
    unit = len(STAGE2_POINTS) * CREDIT_COST["getTransfersByAddress"]
    print(f"  {len(candidates)} token(s) franchissent le seuil a l'un des "
          f"trois instants")
    print(f"  cout unitaire : {unit} credits | budget restant : "
          f"{credits_left():,} -> {credits_left() // unit if unit else 0} "
          f"token(s) suivables")

    updated = 0
    states: Counter = Counter()
    stopped = "termine"
    for mint, record in candidates:
        if not can_spend("getTransfersByAddress"):
            stopped = "plafond de credits"
            break
        supply = rules.to_float(record.get("supply"))
        points, capped = measure_points(record, STAGE2_POINTS, supply)
        for entry in points.values():
            states[entry["etat"]] += 1
        merged = {**(record.get("points") or {}), **points}
        if points:
            db.upsert_grad_path(row_from(record, merged, supply, 2))
            record["points"] = merged
            record["stage"] = 2
            updated += 1
        if capped:
            stopped = "plafond de credits"
            break
    print(f"\n  {updated} trajectoires completees ({stopped})")
    print(f"  etats des points : {dict(states)}")
    return {"candidats": len(candidates), "completees": updated,
            "etats": dict(states), "arret": stopped}


# ---------------------------------------------------------------------------
# SECTION 4 - La matrice, calculee en local
# ---------------------------------------------------------------------------


def percentile(values: list[float], share: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(share * (len(ordered) - 1))))
    return ordered[index]


def point_of(row: dict, label: str) -> dict:
    points = row.get("points") or {}
    entry = points.get(label)
    return entry if isinstance(entry, dict) else {}


def section_4(rows: list[dict]) -> dict:
    start_section("4", "La matrice latence x horizon (aucun appel API)")
    measured = [r for r in rows if (r.get("status") or "mesure") == "mesure"]
    print(f"  {len(measured)} trajectoire(s) exploitables sur {len(rows)} "
          f"ligne(s) en base")
    if not measured:
        return {"tokens": 0}

    matrix: dict[str, Any] = {}
    for latency in LATENCIES:
        seconds = POINT_SECONDS[latency]
        active = [r for r in measured if point_of(r, latency).get("actif")]
        above = [r for r in active
                 if rules.to_float(point_of(r, latency).get("mcap_usd"))
                 >= ENTRY_MCAP_USD]
        print(f"\n  --- latence {latency} ---")
        print(f"    actives a T+{latency} : {len(active)}/{len(measured)} "
              f"({100 * len(active) / len(measured):.1f} %)")
        print(f"    au-dessus de {ENTRY_MCAP_USD:,.0f} $ : {len(above)} "
              f"({100 * len(above) / len(measured):.1f} % de l'univers)")
        coherent(f"au-dessus <= actives ({latency})",
                 len(above) <= len(active),
                 f"{len(above)} au-dessus pour {len(active)} actives")
        if not above:
            matrix[latency] = {"actives": len(active), "au_dessus": 0,
                               "horizons": {}}
            continue

        print(f"    {'horizon':>8}{'n':>5}{'mediane':>9}{'p75':>8}{'p90':>8}"
              f"{'>=x2':>7}{'>=x5':>7}{'>=x10':>7}{'<=x0.3':>8}"
              f"{'creux':>8}{'moy.ex':>8}{'moy.0':>7}")
        horizons: dict[str, Any] = {}
        for label, delta in ALL_POINTS:
            if delta <= seconds:
                continue
            returns: list[float] = []
            drawdowns: list[float] = []
            zeros = 0
            for row in above:
                entry = rules.to_float(point_of(row, latency).get("prix_sol"))
                if entry <= 0:
                    continue
                target = point_of(row, label)
                price = rules.to_float(target.get("prix_sol"))
                if price > 0:
                    returns.append(price / entry)
                elif target.get("etat") == "inactif":
                    zeros += 1
                lows = [rules.to_float(point_of(row, mid).get("prix_sol"))
                        / entry
                        for mid, mid_delta in ALL_POINTS
                        if seconds < mid_delta < delta
                        and rules.to_float(point_of(row, mid).get("prix_sol"))
                        > 0]
                if lows:
                    drawdowns.append(min(lows))
            if not returns and not zeros:
                continue
            median = statistics.median(returns) if returns else 0.0
            wins = {name: sum(1 for r in returns if r >= edge) / len(returns)
                    if returns else 0.0 for edge, name in RETURN_BUCKETS}
            losses = (sum(1 for r in returns if r <= LOSS_BUCKET)
                      / len(returns)) if returns else 0.0
            worst = statistics.median(drawdowns) if drawdowns else 0.0
            mean_excluded = statistics.fmean(returns) if returns else 0.0
            mean_zero = (sum(returns) / (len(returns) + zeros)
                         if (returns or zeros) else 0.0)
            horizons[label] = {
                "n": len(returns), "inactifs": zeros,
                "mediane": round(median, 3),
                "p75": round(percentile(returns, 0.75), 3),
                "p90": round(percentile(returns, 0.90), 3),
                **{name: round(value, 4) for name, value in wins.items()},
                "<= x0.3": round(losses, 4),
                "creux_median": round(worst, 3),
                "moyenne_exclus": round(mean_excluded, 3),
                "moyenne_zero": round(mean_zero, 3),
            }
            print(f"    {label:>8}{len(returns):>5}{median:>9.2f}"
                  f"{percentile(returns, 0.75):>8.2f}"
                  f"{percentile(returns, 0.90):>8.2f}"
                  f"{wins['>= x2']:>7.1%}{wins['>= x5']:>7.1%}"
                  f"{wins['>= x10']:>7.1%}{losses:>8.1%}{worst:>8.2f}"
                  f"{mean_excluded:>8.2f}{mean_zero:>7.2f}")
        matrix[latency] = {"actives": len(active), "au_dessus": len(above),
                           "horizons": horizons}

    print("\n  moy.ex = rendement moyen equipondere en EXCLUANT les tokens "
          "devenus inactifs a l'horizon (biais du survivant).")
    print("  moy.0  = les memes, comptes a ZERO. La verite est entre les "
          "deux, aucune des deux ne vaut seule.")

    print("\n  --- taux de base : capitalisation atteinte a un instant "
          "mesure ---")
    base: dict[str, Any] = {}
    for threshold in BASE_RATE_THRESHOLDS:
        hits = sum(1 for r in measured
                   if rules.to_float(r.get("mcap_max_usd")) >= threshold)
        share = hits / len(measured)
        base[f"{threshold:,.0f}"] = {"tokens": hits, "part": round(share, 5)}
        print(f"    >= {threshold:>12,.0f} $ : {hits:5d} tokens "
              f"({share:7.3%})")
    return {"tokens": len(measured), "matrice": matrix, "taux_de_base": base}


# ---------------------------------------------------------------------------
# Recapitulatif
# ---------------------------------------------------------------------------


def show_budget() -> None:
    print("\nCredits consommes :")
    for method, count in sorted(_calls.items()):
        cost = CREDIT_COST.get(method, 0)
        print(f"  {method:28} : {count:5d} appels -> {count * cost:>8,} "
              f"credits")
    print(f"  {'TOTAL':28} : {_credits:>8,} / {MAX_CREDITS:,} credits")
    print("\nPar section :")
    for section, cost in sorted(_credits_by_section.items()):
        print(f"  section {section:2} : {cost:>8,} credits")


def final_recap(results: dict) -> dict:
    print("\n" + "=" * 74)
    print("RECAPITULATIF")
    print("=" * 74)
    section_1_result = results.get("1") or {}
    print(f"\nFenetre : {', '.join(results.get('jours') or [])}")
    print(f"Graduations par journee : {section_1_result.get('par_jour')}")
    print(f"Etape 1 : {(results.get('2') or {}).get('ecrites', 0)} ecrites "
          f"({(results.get('2') or {}).get('arret')})")
    print(f"Etape 2 : {(results.get('3') or {}).get('completees', 0)} "
          f"completees ({(results.get('3') or {}).get('arret')})")
    show_budget()
    if _sol_misses:
        log.warning("%d conversion(s) USD hors couverture", _sol_misses)
    if _incoherences:
        print(f"\n{len(_incoherences)} INCOHERENCE(S) :")
        for item in _incoherences:
            print(f"  - {item}")
    else:
        print("\nAucune incoherence.")
    if _budget_reached:
        print("\nPLAFOND DE CREDITS atteint : la mesure est INCOMPLETE. "
              "Relancer le mode reprend ou il s'est arrete.")
    return {"jours": results.get("jours"),
            "par_jour": section_1_result.get("par_jour"),
            "etape1": results.get("2"), "etape2": results.get("3"),
            "matrice": (results.get("4") or {}).get("matrice"),
            "taux_de_base": (results.get("4") or {}).get("taux_de_base"),
            "credits": _credits, "par_section": dict(_credits_by_section),
            "plafond_atteint": _budget_reached,
            "incoherences": list(_incoherences)}


# ---------------------------------------------------------------------------
# Entree
# ---------------------------------------------------------------------------


def load_accounts() -> dict[str, str]:
    raw = os.environ.get("MIGRATION_ACCOUNTS", "").strip()
    addresses = [a.strip() for a in raw.split(",") if a.strip()] if raw else []
    if not addresses:
        payload = read_payload(V4_RUN_MODE, "A")
        addresses = [a for a in (payload.get("frais"),
                                 payload.get("secours")) if a]
    mapped: dict[str, str] = {}
    for address in addresses:
        if address.startswith(FEE_PREFIX):
            mapped["frais"] = address
        elif address.startswith(BACKUP_PREFIX):
            mapped["secours"] = address
    return mapped


def load_existing(days: list[str]) -> dict[str, dict]:
    """Garde-fou anti-relance : ce qui est deja en base n'est pas repaye."""
    records: dict[str, dict] = {}
    for row in db.fetch_grad_paths(days):
        mint = row.get("mint")
        if not isinstance(mint, str):
            continue
        moment = 0.0
        raw = row.get("grad_at")
        if isinstance(raw, str):
            try:
                moment = datetime.fromisoformat(
                    raw.replace("Z", "+00:00")).timestamp()
            except ValueError:
                moment = 0.0
        records[mint] = {**row, "grad_at": moment}
    return records


def main() -> None:
    global _run_at
    setup_logging()
    diagnose_environment()
    present = bool(os.environ.get("HELIUS_API_KEY", "").strip())
    log.info("  %-18s : %s", "HELIUS_API_KEY",
             "presente" if present else "ABSENTE")
    helius.api_key()  # leve si absente

    _run_at = datetime.now(timezone.utc).isoformat()
    days = window_days()
    print("\nExperience 1 : la fenetre exploitable apres une graduation.")
    print(f"  fenetre           : {', '.join(days)} "
          f"(journees completes, echues depuis {MATURITY_DAYS} jours)")
    print(f"  plafond           : {MAX_CREDITS:,} credits")
    print(f"  seuil d'entree    : {ENTRY_MCAP_USD:,.0f} $")
    print(f"  echantillon etape 1 : {STAGE1_SAMPLE} (seed {RANDOM_SEED})")
    print(f"  ecriture          : {GRAD_PATHS_TABLE}, token par token")

    unit1 = (len(STAGE1_POINTS) * CREDIT_COST["getTransfersByAddress"]
             + CREDIT_COST["getTokenSupply"])
    unit2 = len(STAGE2_POINTS) * CREDIT_COST["getTransfersByAddress"]
    expected = 1100 * WINDOW_DAYS * STAGE1_SAMPLE
    print(f"\n  budget previsionnel, a {expected:.0f} graduations attendues :")
    print(f"    etape 1 : {expected * unit1:,.0f} credits "
          f"({unit1} par token)")
    print(f"    reste pour l'etape 2 : "
          f"{max(0, MAX_CREDITS - expected * unit1):,.0f} credits, soit "
          f"{max(0, MAX_CREDITS - expected * unit1) // unit2:,.0f} token(s) "
          f"suivis a {unit2} credits")

    if not check_tables():
        return

    accounts = load_accounts()
    print(f"\n  compte de frais   : {accounts.get('frais') or 'INCONNU'}")
    print(f"  compte de secours : {accounts.get('secours') or 'inconnu'}")

    rng = random.Random(RANDOM_SEED)
    results: dict[str, Any] = {"jours": days}
    results["0"] = section_0(accounts)
    log_run("0", "verifications", results["0"])

    index = accounts.get("secours") if results["0"].get("index_unique") \
        else None
    if not index:
        print("\nARRET : l'index n'est pas unique, ou le compte de secours "
              "est inconnu. Les comptes a lire sont listes ci-dessus, et "
              "l'experience ne peut pas prelever un univers complet.")
        log_run("run", "arret", {"raison": "index non unique",
                                 "comptes": results["0"].get(
                                     "comptes_supplementaires")})
        return

    results["prix"] = load_sol_prices()
    existing = load_existing(days)
    results["1"] = section_1(index, days)
    universe = results["1"].pop("universe", [])
    log_run("1", "univers de la fenetre", results["1"])

    results["2"] = section_2(universe, existing, rng)
    log_run("2", "etape 1", results["2"])
    results["3"] = section_3(existing)
    log_run("3", "etape 2", results["3"])

    results["4"] = section_4(list(load_existing(days).values()))
    log_run("4", "matrice", results["4"])

    recap = final_recap(results)
    log_run("recap", "recapitulatif", recap)
    print(f"\n{RUN_LOG_TABLE} : run {_run_at} ecrit.")


if __name__ == "__main__":
    main()
