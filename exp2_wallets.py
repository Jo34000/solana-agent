"""Experience 2 : les wallets apportent-ils quelque chose ?

Un signal d'acheteur precoce vaut-il mieux que de prendre les graduations
au hasard ? La reponse se juge HORS ECHANTILLON, sur des journees que la
selection n'a pas vues, avec des regles fixees AVANT de les regarder.

Acquis de l'experience 1, qui fondent les definitions :
  - le prix a +15 min est juste en NIVEAU (facteur median 1,00 contre
    GeckoTerminal) ; le point "graduation" est faux et n'est plus utilise ;
  - aucune regle de sortie simple ne depasse 1 en moyenne nette prudente ;
  - le filtre d'elan (P15/P5) est utilisable, celui relatif a la
    graduation ne l'est pas.

Ecarts releves AVANT ecriture, et ce qui a ete decide :

  1. filters.mint N'A JAMAIS ETE VALIDE. La sonde du 19/09 a teste `mint`
     comme cle de PREMIER NIVEAU, pas dans filters, et une cle inconnue
     fait rejeter tout l'objet de config — donc aussi le blockTime dont
     depend la fenetre. La section 1 fait donc UN essai avec la cle, et
     s'en passe definitivement si elle est rejetee, en filtrant le mint
     cote client. Le verdict est logue une fois.
  2. "Tout wallet present dans plus de 10 % des tokens tires" ne se
     connait qu'une fois TOUT l'echantillon collecte. Les pools, les
     programmes et les deux comptes de migration sont donc exclus a
     l'ECRITURE ; l'exclusion de frequence est appliquee a l'ANALYSE, et
     la liste des exclus part dans le journal. Les donnees brutes restent
     en base, ce qui permet de rejouer un autre seuil sans repayer.
  3. Les journees hors echantillon n'existent pas dans sol_grad_paths.
     Leurs mesures y sont ecrites avec status = "exp2_oos", pour que les
     modes de l'experience 1, qui ne lisent que status = "mesure", ne les
     melangent jamais a leur fenetre.
  4. Le test B de la section 0 compare des NIVEAUX : nos prix sont en SOL
     et les bougies en USD. Le prix en dollars est donc reconstitue par
     mcap_usd / supply, ce qui est exactement la grandeur validee a
     +15 min par l'experience 1.
"""

from __future__ import annotations

import logging
import math
import random
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

import exp1_window as exp
import geckoterminal as gt
import graduations as rules
import helius
import supabase_client as db
from config import (
    GRAD_BUYS_TABLE,
    RUN_LOG_TABLE,
    coingecko_api_key,
    diagnose_environment,
    setup_logging,
)

log = logging.getLogger("solana-agent")

RUN_MODE = "exp2_wallets"
BACKUP_PREFIX = "39azUYFW"
FEE_PREFIX = "9C4nRvhh"

MAX_CREDITS = exp._env_int("MAX_CREDITS", 100_000)
SECTION1_CREDITS = exp._env_int("EXP2_S1_CREDITS", 45_000)
SECTION3_CREDITS = exp._env_int("EXP2_S3_CREDITS", 40_000)
GATE_CALLS = exp._env_int("EXP2_GATE_CALLS", 35)
# La porte a sa propre graine : le run du 25/09 a deja compare
# 20 tokens, et le reste de l'experience garde la sienne.
GATE_SEED = exp._env_int("EXP2_GATE_SEED", 20260930)
PAUSE_S = exp._env_float("CLOSE_PAUSE_S", 2.5)
ROUND_TRIP_COST = exp._env_float("ROUND_TRIP_COST", 0.03)
RANDOM_SEED = exp._env_int("RANDOM_SEED", 20260929)

ENTRY_MCAP_USD = 60_000.0
SIGNAL_WINDOW_S = 600            # graduation -> +10 min
ENTRIES = ("15 min", "60 min")
EXITS = ("2 h", "3 h")
TOUCH_LEVEL = 2.0
MIN_TOKENS_PER_WALLET = 5
TOP_WALLETS = 30
SCORE_PRIOR = 10.0
PLACEBO_RUNS = 200
PLACEBO_PERCENTILE = 0.95
MIN_VERDICT_N = 60
GO_TOUCH_RATIO = 1.5
BENCHMARK_SIZE = 400
SAMPLE_TOKENS = 800
BUY_MAX_PAGES = 5
WALLET_MAX_PAGES = 10
BOT_SHARE = 0.10
CHECKPOINT_EVERY = 50

IN_SAMPLE_DAYS = ("2026-09-11", "2026-09-12", "2026-09-13")
OUT_SAMPLE_DAYS = ("2026-09-14", "2026-09-15", "2026-09-16")
OOS_STATUS = "exp2_oos"

GATE_STRATA = (("calmes", 10), ("deja pompes", 5), ("morts a 3 h", 5))
GATE_LABELS = ("60 min", "2 h", "3 h")
GATE_BAND_LOW, GATE_BAND_HIGH = 0.95, 1.05
# Fenetre de bougies ALIGNEE sur la tolerance qui a servi a mesurer le
# point : les trajectoires du 11 au 13/09 ont ete prises avec une
# tolerance proportionnelle, max(15 min, 25 % de l'horizon). Comparer un
# point de 3 h a une fenetre de 10 min reprocherait a GeckoTerminal un
# ecart que notre propre mesure s'autorisait.
GATE_WINDOWS = {"60 min": 900, "2 h": 1800, "3 h": 2700}
GATE_MIN = 18
GATE_TOTAL = 20
# Deux conditions, et non une : 18 tokens sans aucun point
# hors fourchette, ET 90 % des points compares dedans.
GATE_POINT_RATIO = 0.90
CANDLE_SECONDS = 300

EXCLUDED_PREFIXES = ("6EF8rrec", "pAMMBay6", "Tokenz", "Tokenkeg", "ATokenGP",
                     "ComputeB", "SysvarRe", "1111", "So1111", FEE_PREFIX,
                     BACKUP_PREFIX)

CREDIT_COST = {
    "getTransfersByAddress": 10,
    "getTransactionsForAddress": 100,
    "getTokenSupply": 1,
}

_credits = 0
_credits_before = 0
_credits_by_section: Counter = Counter()
_calls: Counter = Counter()
_gecko_calls = 0
_last_gecko = 0.0
_current_section = "?"
_section_start = 0
_section_cap = 0
_budget_reached = False
_mint_filter: bool | None = None
_incoherences: list[str] = []
_run_at = ""


# ---------------------------------------------------------------------------
# Budget, cumule sur toutes les executions de ce mode
# ---------------------------------------------------------------------------


def start_section(number: str, title: str, cap: int = 0) -> None:
    global _current_section, _section_start, _section_cap
    _current_section = number
    _section_start = _credits
    _section_cap = cap
    print("\n" + "=" * 74)
    print(f"SECTION {number} - {title}")
    if cap:
        print(f"plafond de section : {cap:,} credits")
    print("=" * 74)


def credits_left() -> int:
    global_left = MAX_CREDITS - _credits_before - _credits
    if _section_cap:
        return min(global_left, _section_cap - (_credits - _section_start))
    return global_left


def can_spend(method: str) -> bool:
    global _budget_reached
    if CREDIT_COST.get(method, 0) > credits_left():
        if not _budget_reached:
            _budget_reached = True
            log.warning("PLAFOND atteint : %d de ce run + %d des runs "
                        "precedents = %d/%d. Arret propre.", _credits,
                        _credits_before, _credits + _credits_before,
                        MAX_CREDITS)
        return False
    return True


def _spend(method: str) -> None:
    global _credits
    _credits += CREDIT_COST.get(method, 0)
    _calls[method] += 1
    _credits_by_section[_current_section] += CREDIT_COST.get(method, 0)


def coherent(label: str, condition: bool, detail: str) -> bool:
    if condition:
        return True
    _incoherences.append(f"{label} : {detail}")
    print(f"    INCOHERENT - {label} : {detail}")
    log.warning("INCOHERENCE : %s (%s)", label, detail)
    return False


def wilson(hits: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Intervalle de confiance d'une proportion, sans dependance."""
    if total <= 0:
        return 0.0, 0.0
    ratio = hits / total
    divisor = 1 + z * z / total
    centre = (ratio + z * z / (2 * total)) / divisor
    half = z * math.sqrt(ratio * (1 - ratio) / total
                         + z * z / (4 * total * total)) / divisor
    return max(0.0, centre - half), min(1.0, centre + half)


def net(value: float) -> float:
    return value * (1.0 - ROUND_TRIP_COST)


def _iso(moment: float) -> str:
    try:
        return datetime.fromtimestamp(moment, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return repr(moment)


def _day_bounds(day: str) -> tuple[float, float]:
    start = datetime.fromisoformat(day + "T00:00:00+00:00").timestamp()
    return start, start + 86400


def _grad_at(row: dict) -> float:
    raw = row.get("grad_at")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(
                raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return rules.to_float(raw)


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


def candles_of(pool: str, graduated: float) -> list[list] | None:
    """Bougies de 5 min, un appel CoinGecko, sous plafond et avec pause."""
    global _gecko_calls, _last_gecko
    if _gecko_calls >= GATE_CALLS:
        return None
    waited = time.monotonic() - _last_gecko
    if _last_gecko and waited < PAUSE_S:
        time.sleep(PAUSE_S - waited)
    _gecko_calls += 1
    _last_gecko = time.monotonic()
    return gt.ohlcv(pool, "minute", limit=60,
                    before=int(graduated + 3 * 3600 + 900), aggregate=5)


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
        return str(payload["error"])[:200]
    return ""


def next_page_token(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    return helius.pagination_token(result) if isinstance(result, dict) else None


# ---------------------------------------------------------------------------
# Journal, tables, reprise
# ---------------------------------------------------------------------------


def log_run(section: str, label: str, payload: dict) -> None:
    try:
        db.insert_run_log(RUN_MODE, _run_at, section, label, payload)
    except Exception as error:               # noqa: BLE001
        log.error("PERTE : %s non ecrit dans %s : %s", label, RUN_LOG_TABLE,
                  error)
        raise


def checkpoint(section: str, done: int) -> None:
    """Un run tue a la main n'ecrit pas son recapitulatif : sans ces points
    de controle, ses credits seraient invisibles au plafond cumule."""
    log_run("budget", "avancement", {"section": section, "traites": done,
                                     "credits_run": _credits,
                                     "credits_cumules": _credits_before
                                     + _credits})


def consumed_before() -> int:
    total = 0
    for section, key in (("budget", "credits_run"), ("recap", "credits")):
        by_run: dict[str, int] = {}
        try:
            rows = db.fetch_run_log(RUN_MODE, section, 50)
        except Exception as error:           # noqa: BLE001
            log.error("PERTE : relecture de la consommation impossible : %s",
                      error)
            return 0
        for row in rows:
            payload = row.get("payload") or {}
            run_at = str(row.get("run_at"))
            by_run[run_at] = max(by_run.get(run_at, 0),
                                 int(rules.to_float(payload.get(key))))
        total += sum(by_run.values())
    return total


CREATE_SQL = """create table if not exists sol_grad_buys (
  mint         text not null,
  wallet       text not null,
  jour         date,
  first_buy_at timestamptz,
  sol_engage   numeric,
  rang         int,
  updated_at   timestamptz default now(),
  primary key (mint, wallet)
);"""


def check_tables() -> bool:
    """Les deux tables sont testees AVANT la moindre depense."""
    try:
        log_run("run", "debut", {"max_credits": MAX_CREDITS,
                                 "jours_echantillon": list(IN_SAMPLE_DAYS),
                                 "jours_hors_echantillon":
                                 list(OUT_SAMPLE_DAYS)})
    except Exception as error:               # noqa: BLE001
        print(f"\n{RUN_LOG_TABLE} inutilisable : {error}")
        return False
    try:
        db.upsert_grad_buys([{
            "mint": "__probe__", "wallet": "__probe__", "rang": 0,
            "sol_engage": 0,
            "updated_at": datetime.now(timezone.utc).isoformat()}])
    except Exception as error:               # noqa: BLE001
        print(f"\n{GRAD_BUYS_TABLE} inutilisable : {error}")
        print("L'experience s'arrete AVANT de depenser le moindre credit.")
        print("SQL de creation attendu :\n" + CREATE_SQL)
        return False
    print(f"{GRAD_BUYS_TABLE} : ecriture confirmee")
    return True


# ---------------------------------------------------------------------------
# Lecture des trajectoires
# ---------------------------------------------------------------------------


def price_at(row: dict, label: str) -> float:
    entry = exp.point_of(row, label)
    return (rules.to_float(entry.get("prix_sol"))
            if entry.get("actif") else 0.0)


def mcap_at(row: dict, label: str) -> float:
    return rules.to_float(exp.point_of(row, label).get("mcap_usd"))


def eligible(row: dict, entry_label: str) -> bool:
    """Eligible a l'instant d'ENTREE, sans rien savoir de la suite."""
    return mcap_at(row, entry_label) >= ENTRY_MCAP_USD


def complete(row: dict) -> bool:
    points = row.get("points") or {}
    for label, _ in exp.STAGE2_POINTS:
        entry = points.get(label)
        if not isinstance(entry, dict) or entry.get("etat") not in (
                "actif", "inactif"):
            return False
    return True


def last_active_before(row: dict, seconds: float) -> float:
    best, best_delta = 0.0, -1.0
    for label, delta in exp.ALL_POINTS:
        price = price_at(row, label)
        if delta <= seconds and price > 0 and delta > best_delta:
            best, best_delta = price, float(delta)
    return best


def outcome_of(row: dict, entry_label: str,
               exit_label: str) -> tuple[float, float] | None:
    """(rendement net prudent, rendement net optimiste)."""
    entry = price_at(row, entry_label)
    if entry <= 0:
        return None
    final = price_at(row, exit_label)
    if final > 0:
        value = net(final / entry)
        return value, value
    last = last_active_before(row, exp.POINT_SECONDS[exit_label])
    return 0.0, net(last / entry) if last > 0 else 0.0


def touches_x2(row: dict, entry_label: str) -> bool:
    """Un point mesure entre l'entree et 3 h vaut-il au moins le double ?"""
    entry = price_at(row, entry_label)
    if entry <= 0:
        return False
    start = exp.POINT_SECONDS[entry_label]
    end = exp.POINT_SECONDS["3 h"]
    for label, delta in exp.ALL_POINTS:
        if start < delta <= end and price_at(row, label) / entry >= TOUCH_LEVEL:
            return True
    return False


# ---------------------------------------------------------------------------
# SECTION 0 - Porte : les prix sont-ils justes en niveau ?
# ---------------------------------------------------------------------------


def strate_of(row: dict) -> str | None:
    """Classification ORDONNEE : mort d'abord, un token pouvant etre les deux."""
    entry = price_at(row, "15 min")
    if entry <= 0:
        return None
    late = price_at(row, "3 h")
    if late > 0 and late / entry <= 0.3:
        return "morts a 3 h"
    early = price_at(row, "5 min")
    if early <= 0:
        return None
    elan = entry / early
    if elan > 1.2:
        return "deja pompes"
    if 0.8 <= elan <= 1.2:
        return "calmes"
    return None


def already_tested() -> set[str]:
    """Mints deja compares, par exp1_close ET par les portes precedentes.

    Un token deja compare ne prouve plus rien : la porte en tire de
    NOUVEAUX, et le tirage a sa propre graine pour cela.
    """
    seen: set[str] = set()
    for run_mode, section, path in (("exp1_close", "recap", "validation"),
                                    (RUN_MODE, "0", None)):
        try:
            rows = db.fetch_run_log(run_mode, section, 10)
        except Exception as error:           # noqa: BLE001
            log.warning("Relecture de %s/%s impossible (%s) : le tirage ne "
                        "peut pas garantir des tokens nouveaux", run_mode,
                        section, error)
            continue
        found = 0
        for row in rows:
            payload = row.get("payload") or {}
            block = (payload.get(path) or {}) if path else payload
            for detail in (block.get("details") or []):
                mint = detail.get("mint") if isinstance(detail, dict) else None
                if isinstance(mint, str):
                    seen.add(mint)
                    found += 1
        print(f"  {found} token(s) deja compares par {run_mode}/{section}")
    if not seen:
        print("  aucun token deja compare retrouve : le tirage ne peut pas "
              "garantir la nouveaute")
    return seen


def band_of(candles: list[list], moment: float,
            window: float) -> tuple[float, float]:
    """[plus bas x 0,95 ; plus haut x 1,05] sur [t ; t + window]."""
    lows, highs = [], []
    for candle in candles:
        start = rules.to_float(candle[0])
        if moment - CANDLE_SECONDS < start <= moment + window:
            lows.append(rules.to_float(candle[3]))
            highs.append(rules.to_float(candle[2]))
    lows = [v for v in lows if v > 0]
    highs = [v for v in highs if v > 0]
    if not lows or not highs:
        return 0.0, 0.0
    return min(lows) * GATE_BAND_LOW, max(highs) * GATE_BAND_HIGH


def section_0(rows: list[dict]) -> dict:
    start_section("0", "Porte : validation des prix (CoinGecko seul)")
    print(f"  {GATE_CALLS} appels au plus, pause {PAUSE_S} s | porte a "
          f">= {GATE_MIN}/{GATE_TOTAL} tokens ET >= "
          f"{GATE_POINT_RATIO:.0%} des points compares")
    print("  Le test B compare des NIVEAUX : notre prix en dollars est "
          "mcap_usd / supply, la grandeur validee a +15 min.")
    print(f"  graine du tirage : {GATE_SEED}")
    if coingecko_api_key() is None:
        log.warning("Cle CoinGecko absente : la porte va echouer, et rien ne "
                    "sera valide en silence.")
    seen = already_tested()
    rng = random.Random(GATE_SEED)

    pools: dict[str, list[dict]] = {name: [] for name, _ in GATE_STRATA}
    for row in rows:
        if row["mint"] in seen or rules.to_float(row.get("supply")) <= 0:
            continue
        name = strate_of(row)
        if name:
            pools[name].append(row)
    for items in pools.values():
        rng.shuffle(items)
    print(f"  strates : { {k: len(v) for k, v in pools.items()} }")

    results: list[dict] = []
    missing = 0
    lost = 0
    sans_point = 0
    for name, wanted in GATE_STRATA:
        queue = list(pools[name])
        kept = 0
        while kept < wanted and queue and _gecko_calls < GATE_CALLS:
            row = queue.pop(0)
            candles = candles_of(row["pool"], _grad_at(row))
            if candles is None:
                lost += 1
                log.warning("PERTE : bougies de %s abandonnees, le token "
                            "n'est ni un succes ni un echec", row["mint"][:8])
                continue
            if not candles:
                missing += 1
                continue
            detail = _gate_token(row, candles, name)
            if detail["compares"] == 0:
                # Aucun point comparable n'est pas un echec : c'est une
                # absence de mesure, remplacee par un autre tirage de la
                # meme strate, comme un token absent de GeckoTerminal.
                sans_point += 1
                continue
            results.append(detail)
            kept += 1
        if kept < wanted:
            log.warning("Strate %s : %d/%d (plafond d'appels ou strate "
                        "epuisee)", name, kept, wanted)

    passed_a = sum(1 for r in results if r["test_a"])
    passed_b = sum(1 for r in results if r["test_b"])
    tested = len(results)
    points = sum(r["compares"] for r in results)
    inside = sum(r["dedans"] for r in results)
    causes: Counter = Counter()
    for detail in results:
        causes.update(detail["causes"].values())
    ratio = inside / points if points else 0.0
    print(f"\n  test A (ratios a +/-15 %) : {passed_a}/{tested}")
    print(f"  test B (tous les points compares dans la fourchette) : "
          f"{passed_b}/{tested}")
    print(f"  points compares : {inside}/{points} dans la fourchette "
          f"({ratio:.0%})")
    print(f"  points non compares : {dict(causes) if causes else 'aucun'}")
    print(f"  tokens sans aucun point comparable (retires du tirage) : "
          f"{sans_point}")
    print(f"  tokens absents de GeckoTerminal : {missing} | pertes : {lost} "
          f"| appels {_gecko_calls}/{GATE_CALLS}")
    coherent("test B <= testes", passed_b <= tested,
             f"{passed_b} pour {tested}")
    coherent("points dans la fourchette <= points compares", inside <= points,
             f"{inside} pour {points}")
    coherent("tout token retenu a au moins un point compare",
             all(r["compares"] > 0 for r in results),
             f"{sum(1 for r in results if r['compares'] == 0)} sans point")
    enough = tested >= GATE_MIN
    open_gate = (enough and passed_b >= GATE_MIN
                 and points > 0 and ratio >= GATE_POINT_RATIO)
    print(f"  PORTE : {'OUVERTE' if open_gate else 'FERMEE'}")
    if not open_gate:
        if not enough:
            print(f"    {tested} token(s) compares seulement : la porte "
                  f"demande {GATE_MIN} sur {GATE_TOTAL}.")
        print("    Aucun appel Helius ne sera fait : les prix ne sont pas "
              "surs en niveau, et l'eligibilite a 60 000 $ en depend.")
    return {"testes": tested, "test_a": passed_a, "test_b": passed_b,
            "points": points, "points_dedans": inside, "part": ratio,
            "causes": dict(causes), "sans_point": sans_point,
            "absents": missing, "pertes": lost, "graine": GATE_SEED,
            "appels": _gecko_calls, "porte": open_gate, "details": results}


def _gate_token(row: dict, candles: list[list], strate: str) -> dict:
    """Compare un token point par point, et dit pourquoi un point manque."""
    graduated = _grad_at(row)
    supply = rules.to_float(row.get("supply"))
    entry_ours = price_at(row, "15 min")
    entry_theirs = _close_at(candles, graduated + exp.POINT_SECONDS["15 min"])

    ratios_ok = []
    levels_ok = []
    detail: dict[str, Any] = {"mint": row["mint"], "strate": strate,
                              "niveaux": {}, "causes": {}}
    for label in GATE_LABELS:
        moment = graduated + exp.POINT_SECONDS[label]
        ours_usd = mcap_at(row, label) / supply if supply > 0 else 0.0
        low, high = band_of(candles, moment, GATE_WINDOWS[label])
        if ours_usd <= 0:
            detail["causes"][label] = "notre point absent (inactif)"
        elif low <= 0:
            detail["causes"][label] = "aucune bougie dans la fenetre"
        else:
            inside = low <= ours_usd <= high
            levels_ok.append(inside)
            detail["niveaux"][label] = {"notre_prix": ours_usd,
                                        "bas": low, "haut": high,
                                        "dedans": inside}
        theirs = _close_at(candles, moment)
        if entry_ours > 0 and entry_theirs > 0 and theirs > 0:
            ours = price_at(row, label) / entry_ours
            theirs_ratio = theirs / entry_theirs
            both_dead = ours <= 0.3 and theirs_ratio <= 0.3
            close = (min(ours, theirs_ratio) / max(ours, theirs_ratio) >= 0.85
                     if max(ours, theirs_ratio) > 0 else False)
            ratios_ok.append(bool(both_dead or close))
    detail["compares"] = len(levels_ok)
    detail["dedans"] = sum(1 for ok in levels_ok if ok)
    detail["test_a"] = bool(ratios_ok) and all(ratios_ok)
    detail["test_b"] = bool(levels_ok) and all(levels_ok)
    marks = {label: block["dedans"]
             for label, block in detail["niveaux"].items()}
    causes = "" if not detail["causes"] else f" | {detail['causes']}"
    print(f"    {row['mint'][:8]}.. ({strate:>12}) : A "
          f"{'ok' if detail['test_a'] else 'NON':>3} | B "
          f"{'ok' if detail['test_b'] else 'NON':>3} "
          f"{detail['dedans']}/{detail['compares']} {marks}{causes}")
    return detail


def _close_at(candles: list[list], moment: float) -> float:
    for candle in candles:
        start = rules.to_float(candle[0])
        if start <= moment < start + CANDLE_SECONDS:
            return rules.to_float(candle[4])
    return 0.0


# ---------------------------------------------------------------------------
# SECTION 1 - Acheteurs precoces, en echantillon
# ---------------------------------------------------------------------------


def excluded_wallet(wallet: str, pool: str) -> bool:
    if not wallet or wallet == pool:
        return True
    return any(wallet.startswith(prefix) for prefix in EXCLUDED_PREFIXES)


def buys_of(pool: str, mint: str, graduated: float) -> tuple[list[dict], str]:
    """Achats du token sur son pool, de la graduation a +10 min.

    filters.mint n'a jamais ete valide : un seul essai, puis filtrage du
    mint cote client si la cle est rejetee. Une cle inconnue ferait
    rejeter TOUT l'objet, donc aussi la fenetre blockTime.
    """
    global _mint_filter
    window = {"blockTime": {"gte": int(graduated),
                            "lte": int(graduated + SIGNAL_WINDOW_S)}}
    found: dict[str, dict] = {}
    page_token = None
    stopped = "fenetre couverte"

    for page in range(BUY_MAX_PAGES):
        config: dict[str, Any] = {"limit": 100, "sortOrder": "asc",
                                  "filters": dict(window)}
        if _mint_filter is not False:
            config["mint"] = mint
        if page_token:
            config["paginationToken"] = page_token
        payload = transfers(pool, config)
        if payload == "CAPPED":
            stopped = "plafond de credits"
            break
        if error_of(payload) and _mint_filter is None:
            log.warning("Cle `mint` rejetee par getTransfersByAddress : "
                        "filtrage cote client pour tout le run (%s)",
                        error_of(payload))
            _mint_filter = False
            config.pop("mint", None)
            payload = transfers(pool, config)
        elif _mint_filter is None and not error_of(payload):
            _mint_filter = True
            log.info("Cle `mint` ACCEPTEE par getTransfersByAddress")
        rows = rows_of(payload)
        if rows is None:
            stopped = "PERTE : " + (error_of(payload) or "payload inattendu")
            break
        if not rows:
            break
        for signature, lines in rules.group_by_signature(rows).items():
            sol = 0.0
            buyer = None
            when = 0.0
            for line in lines:
                line_mint = line.get("mint")
                if line_mint in rules.SOL_MINTS:
                    sol += rules.amount_of(line, sol_leg=True)
                elif line_mint == mint:
                    destination = line.get("toUserAccount")
                    source = line.get("fromUserAccount")
                    if source == pool and isinstance(destination, str):
                        buyer = destination
                        when = max(when, rules.line_time(line))
            if not buyer or sol <= 0 or excluded_wallet(buyer, pool):
                continue
            entry = found.get(buyer)
            if entry is None or when < entry["first"]:
                found[buyer] = {"first": when or graduated,
                                "sol": sol, "signature": signature}
            else:
                entry["sol"] += sol
        page_token = next_page_token(payload)
        if not page_token:
            break
    else:
        stopped = f"plafond de {BUY_MAX_PAGES} pages"
        log.warning("Token %s : %d pages atteintes, acheteurs tronques",
                    mint[:8], BUY_MAX_PAGES)

    ordered = sorted(found.items(), key=lambda item: item[1]["first"])
    return ([{"wallet": wallet, "first": data["first"], "sol": data["sol"],
              "rang": rank}
             for rank, (wallet, data) in enumerate(ordered, start=1)],
            stopped)


def section_1(rows: list[dict], rng: random.Random) -> dict:
    start_section("1", "Acheteurs precoces, en echantillon", SECTION1_CREDITS)
    population = [r for r in rows
                  if r.get("jour") in IN_SAMPLE_DAYS and complete(r)
                  and eligible(r, "15 min")]
    print(f"  {len(population)} token(s) eligibles a +15 min et complets")
    try:
        deja = {r["mint"] for r in db.fetch_grad_buys(list(IN_SAMPLE_DAYS))
                if r.get("mint")}
    except Exception as error:               # noqa: BLE001
        log.error("PERTE : relecture de %s impossible : %s", GRAD_BUYS_TABLE,
                  error)
        deja = set()
    rng.shuffle(population)
    sample = population[:SAMPLE_TOKENS]
    todo = [r for r in sample if r["mint"] not in deja]
    print(f"  {len(sample)} tires (seed {RANDOM_SEED}) | {len(deja)} deja "
          f"collectes | {len(todo)} a collecter")

    written = 0
    buyers_total = 0
    truncated = 0
    stopped = "termine"
    for index, row in enumerate(todo, start=1):
        if not can_spend("getTransfersByAddress"):
            stopped = "plafond"
            break
        buys, note = buys_of(row["pool"], row["mint"], _grad_at(row))
        if "pages" in note:
            truncated += 1
        if "PERTE" in note or "plafond de credits" in note:
            if "plafond" in note:
                stopped = "plafond"
                break
            continue
        if buys:
            db.upsert_grad_buys([{
                "mint": row["mint"], "wallet": buy["wallet"],
                "jour": row.get("jour"),
                "first_buy_at": _iso(buy["first"]),
                "sol_engage": round(buy["sol"], 9), "rang": buy["rang"],
                "updated_at": datetime.now(timezone.utc).isoformat()}
                for buy in buys])
            buyers_total += len(buys)
        written += 1
        if index % CHECKPOINT_EVERY == 0:
            checkpoint("1", written)
            log.info("  section 1 : %d/%d tokens, %d credits", written,
                     len(todo), _credits)
    checkpoint("1", written)

    print(f"\n  {written} token(s) collectes, {buyers_total} achat(s) ecrits "
          f"({stopped})")
    print(f"  tokens tronques au plafond de pages : {truncated}")
    print(f"  cle `mint` : "
          f"{'acceptee' if _mint_filter else 'rejetee, filtrage client'}")
    return {"population": len(population), "echantillon": len(sample),
            "collectes": written, "achats": buyers_total,
            "tronques": truncated, "arret": stopped,
            "mint_filter": _mint_filter,
            "credits": _credits - _section_start}


# ---------------------------------------------------------------------------
# SECTION 2 - Selection en echantillon, placebo, pre-enregistrement
# ---------------------------------------------------------------------------


def score_of(touches: int, count: int, base: float) -> float:
    """Score lisse : un wallet a 2/2 ne bat pas un wallet a 40/100."""
    return (touches + SCORE_PRIOR * base) / (count + SCORE_PRIOR)


def section_2(rows: list[dict], rng: random.Random,
              drawn: int = 0) -> dict:
    start_section("2", "Selection en echantillon (calcul local)")
    by_mint = {r["mint"]: r for r in rows}
    try:
        buys = db.fetch_grad_buys(list(IN_SAMPLE_DAYS))
    except Exception as error:               # noqa: BLE001
        log.error("PERTE : relecture de %s impossible : %s", GRAD_BUYS_TABLE,
                  error)
        return {}
    buys = [b for b in buys if b.get("mint") in by_mint
            and b.get("wallet") not in (None, "__probe__")]
    tokens = {b["mint"] for b in buys}
    print(f"  {len(buys)} achat(s) sur {len(tokens)} token(s)")
    if not tokens:
        print("  aucun achat : section interrompue")
        return {}

    per_wallet: dict[str, set[str]] = defaultdict(set)
    for buy in buys:
        per_wallet[buy["wallet"]].add(buy["mint"])

    # "10 % des tokens TIRES", pas des seuls tokens qui ont des acheteurs :
    # un token sans acheteur fait partie du tirage et doit compter.
    population = drawn or len(tokens)
    limit = BOT_SHARE * population
    print(f"  {population} token(s) tires, seuil de bot a plus de "
          f"{limit:.0f} token(s)")
    # Les deux seuils peuvent s'annuler : sous 50 tokens, exiger 5 achats
    # distincts ET moins de 10 % des tokens ne laisse personne passer.
    coherent("les deux seuils laissent une place",
             limit >= MIN_TOKENS_PER_WALLET,
             f"plus de {limit:.0f} tokens = bot, mais il en faut "
             f"{MIN_TOKENS_PER_WALLET} pour etre candidat : aucun wallet ne "
             f"peut satisfaire les deux sur {population} tokens tires")
    bots = sorted(w for w, mints in per_wallet.items() if len(mints) > limit)
    print(f"  wallets presents dans plus de {BOT_SHARE:.0%} des tokens "
          f"(> {limit:.0f}) : {len(bots)} ecartes (bots, snipers)")
    for wallet in bots[:5]:
        print(f"    {wallet[:12]}.. sur {len(per_wallet[wallet])} tokens")

    outcomes = {mint: touches_x2(by_mint[mint], "15 min") for mint in tokens}
    base = sum(outcomes.values()) / len(outcomes)
    print(f"  part 'touche x2' de la population : {base:.3%} "
          f"({sum(outcomes.values())}/{len(outcomes)})")

    candidates: dict[str, set[str]] = {
        wallet: mints for wallet, mints in per_wallet.items()
        if wallet not in bots and len(mints) >= MIN_TOKENS_PER_WALLET}
    print(f"  candidats (>= {MIN_TOKENS_PER_WALLET} tokens distincts) : "
          f"{len(candidates)}")
    if not candidates:
        print("  aucun candidat : section interrompue")
        return {"bots": len(bots), "base": base, "candidats": 0,
            "tokens_tires": population}

    ranked = []
    for wallet, mints in candidates.items():
        touches = sum(1 for mint in mints if outcomes[mint])
        prudent, optimistic = [], []
        for mint in mints:
            outcome = outcome_of(by_mint[mint], "15 min", "2 h")
            if outcome:
                prudent.append(outcome[0])
                optimistic.append(outcome[1])
        ranked.append({
            "wallet": wallet, "n": len(mints), "touches": touches,
            "part_touche": round(touches / len(mints), 4),
            "score": round(score_of(touches, len(mints), base), 5),
            "moyenne_prudente": round(statistics.fmean(prudent), 4)
            if prudent else 0.0,
            "moyenne_optimiste": round(statistics.fmean(optimistic), 4)
            if optimistic else 0.0})
    ranked.sort(key=lambda item: -item["score"])
    top = ranked[:TOP_WALLETS]
    observed = statistics.fmean([w["score"] for w in top]) if top else 0.0
    print(f"\n  {'wallet':>14}{'n':>5}{'touches':>9}{'score':>9}"
          f"{'moy.prud':>10}{'moy.opt':>9}")
    for wallet in top[:10]:
        print(f"  {wallet['wallet'][:12] + '..':>14}{wallet['n']:>5}"
              f"{wallet['touches']:>9}{wallet['score']:>9.4f}"
              f"{wallet['moyenne_prudente']:>10.3f}"
              f"{wallet['moyenne_optimiste']:>9.3f}")
    print(f"  score moyen des {len(top)} retenus : {observed:.5f}")

    # --- placebo : la structure reste, le lien token -> resultat saute ---
    print(f"\n  PLACEBO : {PLACEBO_RUNS} permutations des resultats entre "
          f"tokens")
    mints_list = list(tokens)
    values = [outcomes[m] for m in mints_list]
    placebo_scores = []
    for _ in range(PLACEBO_RUNS):
        rng.shuffle(values)
        shuffled = dict(zip(mints_list, values))
        fake = []
        for wallet, mints in candidates.items():
            touches = sum(1 for mint in mints if shuffled[mint])
            fake.append(score_of(touches, len(mints), base))
        fake.sort(reverse=True)
        placebo_scores.append(statistics.fmean(fake[:TOP_WALLETS]))
    placebo_scores.sort()
    threshold = placebo_scores[min(len(placebo_scores) - 1,
                                   int(PLACEBO_PERCENTILE
                                       * len(placebo_scores)))]
    print(f"    placebo : mediane {statistics.median(placebo_scores):.5f} | "
          f"95e percentile {threshold:.5f}")
    signal = observed > threshold
    print(f"    observe {observed:.5f} -> "
          f"{'SIGNAL' if signal else 'AUCUN SIGNAL EN ECHANTILLON'}")
    coherent("placebo calcule", len(placebo_scores) == PLACEBO_RUNS,
             f"{len(placebo_scores)} permutations")

    return {"bots": len(bots), "base": round(base, 5),
            "tokens_tires": population,
            "candidats": len(candidates), "retenus": top,
            "score_observe": round(observed, 5),
            "placebo_95": round(threshold, 5),
            "placebo_mediane": round(statistics.median(placebo_scores), 5),
            "signal": signal}


PREREGISTERED_RULES = {
    "signal": "achat d'un wallet retenu entre la graduation et +10 min",
    "entrees": list(ENTRIES),
    "sorties": list(EXITS),
    "eligibilite": f"capitalisation >= {ENTRY_MCAP_USD:.0f} $ a l'entree",
    "frais": ROUND_TRIP_COST,
    "n_minimum": MIN_VERDICT_N,
    "go": ["part touche x2 >= 1,5 x benchmark",
           "borne basse de Wilson a 95 % au-dessus du benchmark",
           "moyenne nette prudente > celle du benchmark",
           "moyenne nette optimiste > 1"],
    "no_go": "n >= 60 et borne basse de Wilson <= benchmark",
}


# ---------------------------------------------------------------------------
# SECTION 3 - Hors echantillon
# ---------------------------------------------------------------------------


def list_graduations(account: str, day: str) -> list[dict]:
    """Graduations d'une journee : index + test de la courbe."""
    start, end = _day_bounds(day)
    config = {"limit": 1000, "sortOrder": "asc", "transactionDetails": "full",
              "filters": {"blockTime": {"gte": int(start), "lte": int(end)}}}
    found: list[dict] = []
    seen: set[str] = set()
    payload = transactions(account, config)
    while True:
        rows = rows_of(payload)
        if rows is None:
            log.error("PERTE : journee %s illisible (%s)", day,
                      error_of(payload) or "payload inattendu")
            break
        for row in rows:
            if not isinstance(row, dict):
                continue
            outcome = rules.graduation_of(row)
            if outcome.get("motif") != "graduation":
                continue
            mint = outcome["mint"]
            if mint in seen:
                continue
            seen.add(mint)
            found.append({"mint": mint, "pool": outcome["pool"],
                          "grad_at": outcome["grad_at"], "jour": day})
        token = next_page_token(payload)
        if not token:
            break
        follow = dict(config)
        follow["paginationToken"] = token
        payload = transactions(account, follow)
        if payload == "CAPPED":
            break
    print(f"    {day} : {len(found)} graduations")
    return found


def price_point(pool: str, moment: float) -> tuple[float | None, str]:
    payload = transfers(pool, {
        "limit": 100, "sortOrder": "asc",
        "filters": {"blockTime": {"gte": int(moment - exp.ACTIVITY_WINDOW)}}})
    if payload == "CAPPED":
        return None, "plafond"
    rows = rows_of(payload)
    if rows is None:
        return None, "perte"
    price, _, _ = rules.median_swap_price(rows, moment, exp.ACTIVITY_WINDOW)
    return (price, "actif") if price is not None else (None, "inactif")


def measure(record: dict) -> dict | None:
    """Quatre instants et la supply, pour un token hors echantillon."""
    supply, decimals, raw = rules.supply_of(token_supply(record["mint"]))
    points: dict[str, Any] = {}
    graduated = rules.to_float(record["grad_at"])
    for label in ("15 min", "60 min", "2 h", "3 h"):
        moment = graduated + exp.POINT_SECONDS[label]
        price, state = price_point(record["pool"], moment)
        if state == "plafond":
            return None
        entry: dict[str, Any] = {"actif": state == "actif",
                                 "prix_sol": price, "etat": state}
        if price is not None and supply > 0:
            usd = exp.sol_price_at(moment)
            if usd:
                entry["sol_usd"] = usd
                entry["mcap_usd"] = price * usd * supply
        points[label] = entry
    mcaps = [p["mcap_usd"] for p in points.values() if p.get("mcap_usd")]
    return {"mint": record["mint"], "pool": record["pool"],
            "grad_at": _iso(graduated), "jour": record["jour"],
            "status": OOS_STATUS, "stage": 2, "supply": supply or None,
            "decimals": decimals, "supply_raw": raw, "points": points,
            "mcap_max_usd": max(mcaps) if mcaps else None,
            "points_actifs": sum(1 for p in points.values()
                                 if p.get("actif")),
            "points_mesures": len(points),
            "updated_at": datetime.now(timezone.utc).isoformat()}


def wallet_buys(wallet: str, graduations: dict[str, dict]) -> list[dict]:
    """Achats du wallet, sur les journees hors echantillon."""
    start, _ = _day_bounds(OUT_SAMPLE_DAYS[0])
    _, end = _day_bounds(OUT_SAMPLE_DAYS[-1])
    signals: dict[str, float] = {}
    page_token = None
    for _ in range(WALLET_MAX_PAGES):
        config: dict[str, Any] = {
            "limit": 100, "sortOrder": "asc",
            "filters": {"blockTime": {"gte": int(start), "lte": int(end)}}}
        if page_token:
            config["paginationToken"] = page_token
        payload = transfers(wallet, config)
        if payload == "CAPPED":
            break
        rows = rows_of(payload)
        if rows is None:
            break
        if not rows:
            break
        for line in rows:
            mint = line.get("mint")
            if not isinstance(mint, str) or mint not in graduations:
                continue
            if line.get("toUserAccount") != wallet:
                continue
            when = rules.line_time(line)
            graduated = rules.to_float(graduations[mint]["grad_at"])
            if graduated <= when <= graduated + SIGNAL_WINDOW_S:
                signals[mint] = min(signals.get(mint, when), when)
        page_token = next_page_token(payload)
        if not page_token:
            break
    return [{"mint": mint, "when": when} for mint, when in signals.items()]


def section_3(wallets: list[str], account: str,
              rng: random.Random) -> dict:
    start_section("3", "Hors echantillon", SECTION3_CREDITS)
    print(f"  journees : {', '.join(OUT_SAMPLE_DAYS)} | {len(wallets)} "
          f"wallets pre-enregistres")

    graduations: dict[str, dict] = {}
    for day in OUT_SAMPLE_DAYS:
        for record in list_graduations(account, day):
            graduations[record["mint"]] = record
    print(f"  {len(graduations)} graduations hors echantillon")
    if not graduations:
        return {"graduations": 0}

    signalled: dict[str, dict] = {}
    for wallet in wallets:
        if not can_spend("getTransfersByAddress"):
            break
        for signal in wallet_buys(wallet, graduations):
            entry = signalled.setdefault(signal["mint"],
                                         {"wallets": [], "when": signal["when"]})
            entry["wallets"].append(wallet)
            entry["when"] = min(entry["when"], signal["when"])
    print(f"  {len(signalled)} token(s) signale(s) par au moins un wallet")

    others = [m for m in graduations if m not in signalled]
    rng.shuffle(others)
    benchmark = others[:BENCHMARK_SIZE]
    print(f"  benchmark : {len(benchmark)} graduation(s) tirees au hasard")

    try:
        deja = {r["mint"] for r in db.fetch_grad_paths(list(OUT_SAMPLE_DAYS))}
    except Exception as error:               # noqa: BLE001
        log.error("PERTE : relecture des trajectoires impossible : %s", error)
        deja = set()

    measured: dict[str, dict] = {}
    stopped = "termine"
    todo = [graduations[m] for m in list(signalled) + benchmark]
    for index, record in enumerate(todo, start=1):
        if record["mint"] in deja:
            continue
        if not can_spend("getTransfersByAddress"):
            stopped = "plafond"
            break
        row = measure(record)
        if row is None:
            stopped = "plafond"
            break
        db.upsert_grad_path(row)
        measured[record["mint"]] = row
        if index % CHECKPOINT_EVERY == 0:
            checkpoint("3", index)
            log.info("  section 3 : %d/%d tokens, %d credits", index,
                     len(todo), _credits)
    checkpoint("3", len(measured))
    print(f"  {len(measured)} token(s) mesures ({stopped})")

    return {"graduations": len(graduations), "signales": len(signalled),
            "benchmark": len(benchmark), "mesures": len(measured),
            "arret": stopped, "signaux": {m: d["wallets"]
                                          for m, d in signalled.items()},
            "credits": _credits - _section_start}


# ---------------------------------------------------------------------------
# SECTION 4 - Verdict, selon les regles fixees d'avance
# ---------------------------------------------------------------------------


def group_stats(rows: list[dict], entry_label: str,
                exit_label: str) -> dict:
    """Chaque token compte UNE FOIS, quel que soit le nombre de signaux."""
    kept = [r for r in rows if eligible(r, entry_label)
            and price_at(r, entry_label) > 0]
    if not kept:
        return {"n": 0}
    touches = sum(1 for r in kept if touches_x2(r, entry_label))
    prudent, optimistic = [], []
    for row in kept:
        outcome = outcome_of(row, entry_label, exit_label)
        if outcome:
            prudent.append(outcome[0])
            optimistic.append(outcome[1])
    low, high = wilson(touches, len(kept))
    return {"n": len(kept), "touches": touches,
            "part_touche": round(touches / len(kept), 4),
            "wilson_bas": round(low, 4), "wilson_haut": round(high, 4),
            "moyenne_prudente": round(statistics.fmean(prudent), 4)
            if prudent else 0.0,
            "moyenne_optimiste": round(statistics.fmean(optimistic), 4)
            if optimistic else 0.0}


def verdict_of(signal: dict, bench: dict) -> tuple[str, list[str]]:
    reasons = []
    if signal["n"] < MIN_VERDICT_N:
        manque = MIN_VERDICT_N - signal["n"]
        return "INCONCLUSIF", [f"{signal['n']} token(s) signales, il en "
                               f"faut {MIN_VERDICT_N} : {manque} de plus"]
    conditions = [
        (signal["part_touche"] >= GO_TOUCH_RATIO * bench["part_touche"],
         f"touche x2 {signal['part_touche']:.3f} vs "
         f"{GO_TOUCH_RATIO} x {bench['part_touche']:.3f}"),
        (signal["wilson_bas"] > bench["part_touche"],
         f"Wilson bas {signal['wilson_bas']:.3f} vs benchmark "
         f"{bench['part_touche']:.3f}"),
        (signal["moyenne_prudente"] > bench["moyenne_prudente"],
         f"moyenne prudente {signal['moyenne_prudente']:.3f} vs "
         f"{bench['moyenne_prudente']:.3f}"),
        (signal["moyenne_optimiste"] > 1.0,
         f"moyenne optimiste {signal['moyenne_optimiste']:.3f} vs 1"),
    ]
    for passed, text in conditions:
        reasons.append(("OK  " if passed else "NON ") + text)
    if all(passed for passed, _ in conditions):
        return "GO", reasons
    if signal["wilson_bas"] <= bench["part_touche"]:
        return "NO-GO", reasons
    return "INCONCLUSIF", reasons


def section_4(signalled: dict[str, list[str]], rows: list[dict]) -> dict:
    start_section("4", "Verdict, selon des regles fixees d'avance")
    oos = [r for r in rows if r.get("jour") in OUT_SAMPLE_DAYS]
    by_mint = {r["mint"]: r for r in oos}
    signal_rows = [by_mint[m] for m in signalled if m in by_mint]
    bench_rows = [r for r in oos if r["mint"] not in signalled]
    print(f"  {len(signal_rows)} token(s) signales | {len(bench_rows)} au "
          f"benchmark | chaque token compte UNE fois")
    coherent("signales + benchmark = mesures",
             len(signal_rows) + len(bench_rows) == len(oos),
             f"{len(signal_rows)} + {len(bench_rows)} pour {len(oos)}")

    outcome: dict[str, Any] = {}
    for entry_label in ENTRIES:
        for exit_label in EXITS:
            signal = group_stats(signal_rows, entry_label, exit_label)
            bench = group_stats(bench_rows, entry_label, exit_label)
            key = f"{entry_label} -> {exit_label}"
            if not signal.get("n") or not bench.get("n"):
                print(f"\n  --- {key} : population vide, non evaluable ---")
                outcome[key] = {"verdict": "INCONCLUSIF", "signal": signal,
                                "benchmark": bench}
                continue
            name, reasons = verdict_of(signal, bench)
            outcome[key] = {"verdict": name, "signal": signal,
                            "benchmark": bench, "raisons": reasons}
            print(f"\n  --- entree {entry_label}, sortie {exit_label} ---")
            print(f"    {'groupe':>10}{'n':>6}{'touche x2':>11}"
                  f"{'Wilson bas':>12}{'moy.prud':>10}{'moy.opt':>9}")
            for label, block in (("signal", signal), ("benchmark", bench)):
                print(f"    {label:>10}{block['n']:>6}"
                      f"{block['part_touche']:>11.3f}"
                      f"{block['wilson_bas']:>12.3f}"
                      f"{block['moyenne_prudente']:>10.3f}"
                      f"{block['moyenne_optimiste']:>9.3f}")
            print(f"    VERDICT : {name}")
            for reason in reasons:
                print(f"      {reason}")

    print("\n  --- par wallet (entree 15 min, sortie 2 h) ---")
    per_wallet: dict[str, list[dict]] = defaultdict(list)
    for mint, wallets in signalled.items():
        row = by_mint.get(mint)
        if row is None:
            continue
        for wallet in wallets:
            per_wallet[wallet].append(row)
    print(f"    {'wallet':>14}{'n':>5}{'touche x2':>11}{'moy.prud':>10}")
    per_wallet_out = {}
    for wallet, items in sorted(per_wallet.items(),
                                key=lambda kv: -len(kv[1]))[:15]:
        stats = group_stats(items, "15 min", "2 h")
        per_wallet_out[wallet] = stats
        if stats.get("n"):
            print(f"    {wallet[:12] + '..':>14}{stats['n']:>5}"
                  f"{stats['part_touche']:>11.3f}"
                  f"{stats['moyenne_prudente']:>10.3f}")
    return {"combinaisons": outcome, "par_wallet": per_wallet_out}


# ---------------------------------------------------------------------------
# Entree
# ---------------------------------------------------------------------------


def show_budget() -> None:
    print("\nCredits consommes :")
    for method, count in sorted(_calls.items()):
        cost = CREDIT_COST.get(method, 0)
        print(f"  {method:28} : {count:6d} appels -> {count * cost:>8,}")
    print(f"  {'TOTAL ce run':28} : {_credits:>8,}")
    print(f"  {'CUMULE':28} : {_credits + _credits_before:>8,} / "
          f"{MAX_CREDITS:,}")
    print("\nPar section :")
    for section, cost in sorted(_credits_by_section.items()):
        print(f"  section {section:2} : {cost:>8,} credits")
    print(f"  CoinGecko : {_gecko_calls} appel(s)")


def main() -> None:
    global _run_at, _credits_before
    setup_logging()
    diagnose_environment()
    helius.api_key()

    _run_at = datetime.now(timezone.utc).isoformat()
    print("\nExperience 2 : les wallets apportent-ils quelque chose ?")
    print(f"  echantillon : {', '.join(IN_SAMPLE_DAYS)}")
    print(f"  hors echantillon : {', '.join(OUT_SAMPLE_DAYS)}")
    print(f"  plafond CUMULE : {MAX_CREDITS:,} credits")

    if not check_tables():
        return
    _credits_before = consumed_before()
    print(f"  deja consomme par ce mode : {_credits_before:,} -> reste "
          f"{MAX_CREDITS - _credits_before:,}")
    if MAX_CREDITS - _credits_before <= 0:
        log.warning("Plafond cumule deja atteint : rien a faire.")
        log_run("run", "arret", {"raison": "plafond cumule"})
        return

    rows = [r for r in db.fetch_all_grad_paths()
            if (r.get("status") or "mesure") in ("mesure", OOS_STATUS)]
    in_sample = [r for r in rows if r.get("jour") in IN_SAMPLE_DAYS
                 and (r.get("status") or "mesure") == "mesure"]
    print(f"  {len(rows)} trajectoire(s) en base, {len(in_sample)} en "
          f"echantillon")

    rng = random.Random(RANDOM_SEED)
    results: dict[str, Any] = {}
    results["0"] = section_0(in_sample)
    log_run("0", "porte", results["0"])
    if not results["0"].get("porte"):
        print("\nARRET : la porte est fermee, aucun appel Helius n'a ete "
              "fait.")
        log_run("run", "arret", {"raison": "porte fermee"})
        return

    results["1"] = section_1(in_sample, rng)
    log_run("1", "acheteurs precoces", results["1"])

    results["2"] = section_2(in_sample, rng,
                             results["1"].get("echantillon", 0))
    log_run("2", "selection", results["2"])
    if not results["2"].get("signal"):
        print("\nARRET : aucun signal en echantillon, le placebo n'est pas "
              "battu. Rien ne justifie de depenser pour le hors "
              "echantillon.")
        log_run("run", "arret", {"raison": "placebo non battu"})
        return

    wallets = [w["wallet"] for w in results["2"]["retenus"]]
    log_run("2", "pre-enregistrement", {"wallets": wallets,
                                        "regles": PREREGISTERED_RULES})
    print(f"\n  PRE-ENREGISTREMENT ecrit : {len(wallets)} wallets et les "
          f"regles du verdict, AVANT de regarder le hors echantillon.")

    if not exp.load_sol_prices():
        log.warning("Prix du SOL indisponible : les capitalisations hors "
                    "echantillon seront absentes, donc l'eligibilite aussi.")
    accounts = exp.load_accounts()
    index = accounts.get("secours")
    if not index:
        log.error("Index des graduations inconnu : section 3 impossible.")
        log_run("run", "arret", {"raison": "index inconnu"})
        return

    results["3"] = section_3(wallets, index, rng)
    log_run("3", "hors echantillon", results["3"])

    fresh = [r for r in db.fetch_grad_paths(list(OUT_SAMPLE_DAYS))]
    results["4"] = section_4(results["3"].get("signaux") or {}, fresh)
    log_run("4", "verdict", results["4"])

    print("\n" + "=" * 74)
    print("RECAPITULATIF")
    print("=" * 74)
    show_budget()
    principal = (results["4"]["combinaisons"].get("15 min -> 2 h") or {})
    print(f"\nVerdict principal (15 min -> 2 h) : "
          f"{principal.get('verdict', 'non evalue')}")
    for key, block in results["4"]["combinaisons"].items():
        print(f"  {key:>18} : {block['verdict']}")
    if _incoherences:
        print(f"\n{len(_incoherences)} INCOHERENCE(S) :")
        for item in _incoherences:
            print(f"  - {item}")
    else:
        print("\nAucune incoherence.")

    log_run("recap", "recapitulatif", {
        "credits": _credits, "credits_cumules": _credits + _credits_before,
        "par_section": dict(_credits_by_section),
        "coingecko": _gecko_calls,
        "verdicts": {k: v["verdict"]
                     for k, v in results["4"]["combinaisons"].items()},
        "incoherences": list(_incoherences)})
    print(f"\n{RUN_LOG_TABLE} : run {_run_at} ecrit.")


if __name__ == "__main__":
    main()
