"""Experience 1 : cloture. Valider les rendements, puis chercher une regle.

Aucun appel Helius. Lecture de sol_grad_paths. Seuls appels autorises :
CoinGecko Demo (endpoints onchain / GeckoTerminal), sous plafond, avec
une pause entre deux appels. Aucune ecriture hors sol_run_log.

Les niveaux de capitalisation en dollars sont faux — ecart p95/p5 de dix
millions a la graduation. L'hypothese est que l'erreur est un facteur
CONSTANT par token, et donc que les RATIOS de prix d'un meme token sont
justes. Toute la section 2 en depend : si la section 1 ne valide pas, la
section 2 ne conclut rien.

Ecarts releves AVANT ecriture, et ce qui a ete decide :

  1. LA CAUSE DU BUG EST IDENTIFIEE PAR LECTURE, avant meme le run.
     amount_of acceptait le champ `amount` tel quel. Or Helius rend le SOL
     natif en LAMPORTS (x1e9) et un montant de token parfois en unites
     brutes (x10^decimals). Divisee par la jambe opposee, une jambe lue
     dans la mauvaise unite donne un prix faux d'un facteur exactement
     egal a une PUISSANCE DE 10 — la signature que la section 1 cherche.
     graduations.py convertit desormais tout champ brut et compte quel
     champ a servi ; la section 1 confirmera ou infirmera sur les donnees.
  2. Un token peut etre a la fois "calme" et "mort" : la classification
     est donc ORDONNEE, mort d'abord, et l'ordre est ecrit ici. Un token
     dont l'elan est inferieur a 0,8 sans etre mort n'entre dans aucune
     strate, et il est compte a part.
  3. Les points mesures sont ESPACES (5, 15, 60 min, 2 h, 3 h...). Un
     objectif touche entre deux points est manque : la regle R1 est donc
     PRUDENTE, elle sous-estime les sorties reussies. Le rappel est
     imprime en tete de section 2.
  4. GeckoTerminal rend des bougies en USD ; nos prix sont en SOL. La
     comparaison porte sur des RATIOS d'un meme token, ou le taux de
     change se simplifie — sauf s'il bouge beaucoup en trois heures, ce
     que la tolerance de 15 % absorbe.
"""

from __future__ import annotations

import logging
import math
import os
import random
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import exp1_window as exp
import geckoterminal as gt
import graduations as rules
import supabase_client as db
from config import (
    GRAD_PATHS_TABLE,
    RUN_LOG_TABLE,
    coingecko_api_key,
    diagnose_environment,
    setup_logging,
)

log = logging.getLogger("solana-agent")

RUN_MODE = "exp1_close"

MAX_CALLS = exp._env_int("CLOSE_MAX_CALLS", 40)
PAUSE_S = exp._env_float("CLOSE_PAUSE_S", 2.5)
ROUND_TRIP_COST = exp._env_float("ROUND_TRIP_COST", 0.03)
RANDOM_SEED = exp._env_int("RANDOM_SEED", 20260928)

CANDLE_SECONDS = 300
COVER_SECONDS = 4 * 3600
AGREE_BAND = 0.15
DEAD_RATIO = 0.3
VALIDATION_MIN = 18
VALIDATION_TOTAL = 20

STRATA = (("calmes", 10), ("deja pompes", 5), ("morts a 3 h", 5))
CALM_LOW, CALM_HIGH = 0.8, 1.2

LATENCIES = ("5 min", "15 min", "60 min")
EXITS = ("2 h", "3 h")
TAKE_PROFITS = (1.5, 2.0)
STOP_LOSS = 0.7
FILTERS = ("tous", "calmes", "calmes < x1,5")
COMPARE_HORIZONS = ("2 h", "3 h")

_calls = 0
_last_call = 0.0
_incoherences: list[str] = []
_run_at = ""


def coherent(label: str, condition: bool, detail: str) -> bool:
    if condition:
        return True
    _incoherences.append(f"{label} : {detail}")
    print(f"    INCOHERENT - {label} : {detail}")
    log.warning("INCOHERENCE : %s (%s)", label, detail)
    return False


def start_section(number: str, title: str) -> None:
    print("\n" + "=" * 74)
    print(f"SECTION {number} - {title}")
    print("=" * 74)


def calls_left() -> int:
    return MAX_CALLS - _calls


def fetch_candles(pool: str, graduated: float) -> list[list] | None:
    """Bougies de 5 minutes couvrant la graduation a +4 h. UN appel."""
    global _calls, _last_call
    if calls_left() <= 0:
        return None
    waited = time.monotonic() - _last_call
    if _last_call and waited < PAUSE_S:
        time.sleep(PAUSE_S - waited)
    _calls += 1
    _last_call = time.monotonic()
    return gt.ohlcv(pool, "minute",
                    limit=int(COVER_SECONDS / CANDLE_SECONDS) + 12,
                    before=int(graduated + COVER_SECONDS + CANDLE_SECONDS),
                    aggregate=int(CANDLE_SECONDS / 60))


def close_at(candles: list[list], moment: float) -> float:
    """Cloture de la bougie qui CONTIENT l'instant."""
    best = 0.0
    for candle in candles:
        start = rules.to_float(candle[0])
        if start <= moment < start + CANDLE_SECONDS:
            return rules.to_float(candle[4])
        if start <= moment:
            best = max(best, start)
    for candle in candles:                   # repli : la derniere avant
        if rules.to_float(candle[0]) == best and best:
            return rules.to_float(candle[4])
    return 0.0


def price_of(row: dict, label: str) -> float:
    entry = exp.point_of(row, label)
    if not entry.get("actif"):
        return 0.0
    return rules.to_float(entry.get("prix_sol"))


def last_active_before(row: dict, seconds: float) -> float:
    """Dernier prix actif connu avant un instant. Sert a la variante haute."""
    best = 0.0
    best_delta = -1.0
    for label, delta in exp.ALL_POINTS:
        if delta <= seconds and price_of(row, label) > 0 and delta > best_delta:
            best, best_delta = price_of(row, label), float(delta)
    return best


def momentum(row: dict) -> float:
    early, late = price_of(row, "5 min"), price_of(row, "15 min")
    return late / early if early > 0 and late > 0 else 0.0


def strate_of(row: dict) -> str | None:
    """Classification ORDONNEE : mort d'abord, un token pouvant etre les deux."""
    entry = price_of(row, "15 min")
    if entry <= 0:
        return None
    late = price_of(row, "3 h")
    if late > 0 and late / entry <= DEAD_RATIO:
        return "morts a 3 h"
    elan = momentum(row)
    if elan > CALM_HIGH:
        return "deja pompes"
    if CALM_LOW <= elan <= CALM_HIGH:
        return "calmes"
    return None


def complete(row: dict) -> bool:
    points = row.get("points") or {}
    for label, _ in exp.STAGE2_POINTS:
        entry = points.get(label)
        if not isinstance(entry, dict) or entry.get("etat") not in (
                "actif", "inactif"):
            return False
    return True


# ---------------------------------------------------------------------------
# SECTION 1 - Validation externe des rendements
# ---------------------------------------------------------------------------


def compare_token(row: dict, candles: list[list]) -> dict:
    """Nos ratios contre ceux de GeckoTerminal, sur le meme token."""
    graduated = 0.0
    raw = row.get("grad_at")
    if isinstance(raw, str):
        try:
            graduated = datetime.fromisoformat(
                raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            graduated = 0.0
    entry_ours = price_of(row, "15 min")
    entry_theirs = close_at(candles, graduated + exp.POINT_SECONDS["15 min"])
    outcome: dict[str, Any] = {"mint": row["mint"], "horizons": {}}
    if entry_ours <= 0 or entry_theirs <= 0:
        outcome["verdict"] = "sans prix d'entree"
        return outcome

    agreements = []
    for label in COMPARE_HORIZONS:
        ours_price = price_of(row, label)
        theirs_price = close_at(candles, graduated + exp.POINT_SECONDS[label])
        if theirs_price <= 0:
            outcome["horizons"][label] = {"verdict": "absent chez eux"}
            continue
        ours = ours_price / entry_ours if ours_price > 0 else 0.0
        theirs = theirs_price / entry_theirs
        both_dead = ours <= DEAD_RATIO and theirs <= DEAD_RATIO
        close = (min(ours, theirs) / max(ours, theirs) >= 1 - AGREE_BAND
                 if max(ours, theirs) > 0 else False)
        agree = bool(both_dead or close)
        agreements.append(agree)
        outcome["horizons"][label] = {
            "notre_ratio": round(ours, 4), "leur_ratio": round(theirs, 4),
            "accord": agree, "deux_morts": both_dead}
    outcome["verdict"] = ("accord" if agreements and all(agreements)
                          else "desaccord" if agreements
                          else "non comparable")
    # Diagnostic du facteur : notre capitalisation contre la leur.
    supply = rules.to_float(row.get("supply"))
    ours_mcap = rules.to_float(exp.point_of(row, "15 min").get("mcap_usd"))
    if supply > 0 and entry_theirs > 0 and ours_mcap > 0:
        outcome["facteur"] = ours_mcap / (entry_theirs * supply)
    return outcome


def section_1(rows: list[dict], rng: random.Random) -> dict:
    start_section("1", "Validation externe des rendements")
    print(f"  seed {RANDOM_SEED} | plafond {MAX_CALLS} appels | pause "
          f"{PAUSE_S} s | accord a +/-{AGREE_BAND:.0%} ou deux ratios "
          f"<= {DEAD_RATIO}")
    if coingecko_api_key() is None:
        log.warning("Cle CoinGecko absente : la validation externe va "
                    "probablement echouer, et rien ne sera valide en "
                    "silence.")

    pools: dict[str, list[dict]] = {name: [] for name, _ in STRATA}
    hors_strate = 0
    for row in rows:
        name = strate_of(row)
        if name is None:
            hors_strate += 1
            continue
        pools[name].append(row)
    print(f"\n  strates disponibles : "
          f"{ {name: len(items) for name, items in pools.items()} }")
    print(f"  hors strate (elan < 0,8 sans etre mort) : {hors_strate}")

    for items in pools.values():
        rng.shuffle(items)

    results: list[dict] = []
    missing = 0
    per_strate: Counter = Counter()
    for name, wanted in STRATA:
        queue = list(pools[name])
        kept = 0
        while kept < wanted and queue and calls_left() > 0:
            row = queue.pop(0)
            candles = fetch_candles(row["pool"], _grad_at(row))
            if not candles:
                missing += 1
                print(f"    {row['mint'][:8]}.. ({name}) : absent de "
                      f"GeckoTerminal, remplace")
                continue
            outcome = compare_token(row, candles)
            outcome["strate"] = name
            results.append(outcome)
            per_strate[name] += 1
            kept += 1
            marks = {label: block.get("accord")
                     for label, block in outcome["horizons"].items()}
            print(f"    {row['mint'][:8]}.. ({name:>12}) : "
                  f"{outcome['verdict']:>16} {marks}")
        if kept < wanted:
            log.warning("Strate %s : %d/%d seulement (plafond d'appels ou "
                        "strate epuisee)", name, kept, wanted)

    agreed = sum(1 for r in results if r["verdict"] == "accord")
    tested = len(results)
    print(f"\n  accord : {agreed}/{tested} (cible {VALIDATION_MIN}/"
          f"{VALIDATION_TOTAL})")
    print(f"  tokens absents de GeckoTerminal : {missing}")
    print(f"  appels consommes : {_calls}/{MAX_CALLS}")
    validated = agreed >= VALIDATION_MIN and tested >= VALIDATION_MIN
    coherent("accord <= testes", agreed <= tested,
             f"{agreed} accords pour {tested} testes")
    print(f"  VERDICT : {'VALIDE' if validated else 'NON VALIDE'}")
    if not validated and tested < VALIDATION_TOTAL:
        print(f"    (seuls {tested} tokens compares : un verdict positif "
              f"exigeait {VALIDATION_MIN} accords)")

    factors = [r["facteur"] for r in results if r.get("facteur")]
    diagnosis = _diagnose(factors)
    return {"testes": tested, "accords": agreed, "valide": validated,
            "absents": missing, "par_strate": dict(per_strate),
            "hors_strate": hors_strate, "appels": _calls,
            "facteurs": diagnosis,
            "details": [{k: v for k, v in r.items()} for r in results]}


def _grad_at(row: dict) -> float:
    raw = row.get("grad_at")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(
                raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return rules.to_float(raw)


def _diagnose(factors: list[float]) -> dict:
    """Les facteurs se regroupent-ils sur des puissances de 10 ?"""
    print("\n  --- diagnostic : notre capitalisation / la leur ---")
    if not factors:
        print("    aucun facteur calculable")
        return {}
    ordered = sorted(factors)
    print(f"    n {len(ordered)} | min {ordered[0]:.3g} | mediane "
          f"{statistics.median(ordered):.3g} | max {ordered[-1]:.3g}")
    powers: Counter = Counter()
    for value in factors:
        if value <= 0:
            continue
        exponent = round(_log10(value))
        powers[exponent] += 1
    print(f"    exposants arrondis : {dict(sorted(powers.items()))}")
    dominant, count = powers.most_common(1)[0] if powers else (0, 0)
    share = count / len(factors) if factors else 0
    if share >= 0.9 and dominant != 0:
        print(f"    -> {share:.0%} des facteurs valent 10^{dominant} : "
              f"c'est une erreur d'UNITE, pas de marche.")
        if dominant in (9, -9):
            print("       10^9 = lamports lus comme des SOL.")
        elif dominant in (6, -6):
            print("       10^6 = unites brutes lues comme des unites "
                  "affichees (6 decimales).")
        print("       amount_of convertit desormais tout champ brut : les "
              "runs futurs ne peuvent plus melanger les unites.")
    elif powers:
        print("    -> les facteurs ne se regroupent pas sur une puissance "
              "de 10 unique : l'erreur n'est pas un facteur constant "
              "commun, elle varie par token.")
    return {"n": len(factors), "mediane": round(statistics.median(factors), 6),
            "exposants": {str(k): v for k, v in sorted(powers.items())},
            "dominant": dominant, "part_dominante": round(share, 4)}


def _log10(value: float) -> float:
    return math.log10(value)


# ---------------------------------------------------------------------------
# SECTION 2 - Regles de sortie, calcul local
# ---------------------------------------------------------------------------

MIN_EFFECTIF = exp._env_int("MIN_EFFECTIF", 30)


def net(value: float) -> float:
    return value * (1.0 - ROUND_TRIP_COST)


def grad_price(row: dict) -> float:
    """Prix a la graduation, ou le premier instant actif a defaut."""
    for label, _ in exp.STAGE1_POINTS:
        price = price_of(row, label)
        if price > 0:
            return price
    return 0.0


def simulate(row: dict, latency: str, exit_label: str,
             take_profit: float | None = None,
             stop: float | None = None) -> tuple[float, float] | None:
    """(rendement brut prudent, rendement brut optimiste).

    Une sortie sur objectif se fait AU PRIX DE L'OBJECTIF ; une sortie sur
    stop se fait au PRIX DU POINT qui le franchit, pas au stop : entre
    deux points mesures, la chute est deja consommee.
    """
    entry = price_of(row, latency)
    if entry <= 0:
        return None
    start = exp.POINT_SECONDS[latency]
    end = exp.POINT_SECONDS[exit_label]
    for label, delta in exp.ALL_POINTS:
        if not start < delta <= end:
            continue
        price = price_of(row, label)
        if price <= 0:
            continue
        ratio = price / entry
        if take_profit is not None and ratio >= take_profit:
            return take_profit, take_profit
        if stop is not None and ratio <= stop:
            return ratio, ratio
    final = price_of(row, exit_label)
    if final > 0:
        return final / entry, final / entry
    last = last_active_before(row, end)
    return 0.0, (last / entry if last > 0 else 0.0)


def passes(row: dict, latency: str, name: str) -> bool:
    if name == "tous":
        return True
    elan = momentum(row)
    if not CALM_LOW <= elan <= CALM_HIGH:
        return False
    if name == "calmes":
        return True
    reference = grad_price(row)
    entry = price_of(row, latency)
    return bool(reference > 0 and entry > 0 and entry / reference < 1.5)


def combination(rows: list[dict], latency: str, exit_label: str,
                rule: str, take_profit: float | None,
                stop: float | None, filtre: str) -> dict:
    prudent: list[float] = []
    optimistic: list[float] = []
    for row in rows:
        if not passes(row, latency, filtre):
            continue
        outcome = simulate(row, latency, exit_label, take_profit, stop)
        if outcome is None:
            continue
        prudent.append(net(outcome[0]))
        optimistic.append(net(outcome[1]))
    if not prudent:
        return {"n": 0, "latence": latency, "sortie": exit_label,
                "regle": rule, "filtre": filtre}
    return {
        "n": len(prudent), "latence": latency, "sortie": exit_label,
        "regle": rule, "filtre": filtre,
        "moyenne_prudente": round(statistics.fmean(prudent), 4),
        "moyenne_optimiste": round(statistics.fmean(optimistic), 4),
        "mediane": round(statistics.median(prudent), 4),
        "part_gagnante": round(sum(1 for v in prudent if v > 1.0)
                               / len(prudent), 4),
        "part_perte": round(sum(1 for v in prudent if v <= 0.3)
                            / len(prudent), 4),
    }


def rule_set() -> list[tuple[str, float | None, float | None]]:
    rules_list: list[tuple[str, float | None, float | None]] = [
        ("R0 vente a H", None, None)]
    for take in TAKE_PROFITS:
        rules_list.append((f"R1 TP x{take:g}", take, None))
    for take in TAKE_PROFITS:
        rules_list.append((f"R2 TP x{take:g} + stop {STOP_LOSS:g}", take,
                           STOP_LOSS))
    return rules_list


def section_2(rows: list[dict]) -> dict:
    start_section("2", "Regles de sortie, sur les trajectoires en base")
    print("  RAPPEL : les points mesures sont espaces (5, 15, 60 min, 2 h, "
          "3 h...). Un objectif touche ENTRE deux points est manque : R1 et "
          "R2 sous-estiment donc les sorties reussies. Elles sont prudentes.")
    print(f"  frais d'aller-retour {ROUND_TRIP_COST:.0%} | inactif a H : "
          f"deux variantes, prix 0 (prudente) et dernier prix actif "
          f"(optimiste)")

    outcomes: list[dict] = []
    for latency in LATENCIES:
        for exit_label in EXITS:
            print(f"\n  --- entree T+{latency}, sortie forcee a {exit_label} "
                  f"---")
            print(f"    {'regle':>26}{'filtre':>14}{'n':>6}{'moy.prud':>10}"
                  f"{'moy.opt':>9}{'mediane':>9}{'gagne':>8}{'<=x0.3':>8}")
            for rule, take, stop in rule_set():
                for filtre in FILTERS:
                    cell = combination(rows, latency, exit_label, rule, take,
                                       stop, filtre)
                    outcomes.append(cell)
                    if not cell["n"]:
                        print(f"    {rule:>26}{filtre:>14}{0:>6}")
                        continue
                    coherent(f"parts <= 1 ({rule}/{filtre})",
                             cell["part_gagnante"] <= 1.0
                             and cell["part_perte"] <= 1.0,
                             f"{cell['part_gagnante']}, {cell['part_perte']}")
                    coherent(f"prudente <= optimiste ({rule}/{filtre})",
                             cell["moyenne_prudente"]
                             <= cell["moyenne_optimiste"] + 1e-9,
                             f"{cell['moyenne_prudente']} > "
                             f"{cell['moyenne_optimiste']}")
                    print(f"    {rule:>26}{filtre:>14}{cell['n']:>6}"
                          f"{cell['moyenne_prudente']:>10.3f}"
                          f"{cell['moyenne_optimiste']:>9.3f}"
                          f"{cell['mediane']:>9.3f}"
                          f"{cell['part_gagnante']:>8.1%}"
                          f"{cell['part_perte']:>8.1%}")
    return {"combinaisons": outcomes}


# ---------------------------------------------------------------------------
# SECTION 3 - La barre a battre
# ---------------------------------------------------------------------------


def section_3(outcomes: list[dict], validated: bool) -> dict:
    start_section("3", "La barre a battre")
    usable = [c for c in outcomes if c["n"] >= MIN_EFFECTIF]
    ignored = sum(1 for c in outcomes if 0 < c["n"] < MIN_EFFECTIF)
    print(f"  {len(usable)} combinaison(s) d'effectif >= {MIN_EFFECTIF} | "
          f"{ignored} ecartee(s) pour effectif trop faible")
    if not usable:
        print("  aucune combinaison d'effectif suffisant : rien a classer")
        return {"classement": [], "verdict": None}

    best = sorted(usable, key=lambda c: -c["moyenne_prudente"])[:5]
    print(f"\n  {'rang':>5}{'latence':>9}{'sortie':>8}{'regle':>26}"
          f"{'filtre':>14}{'n':>6}{'moy.prud':>10}")
    for rank, cell in enumerate(best, start=1):
        print(f"  {rank:>5}{cell['latence']:>9}{cell['sortie']:>8}"
              f"{cell['regle']:>26}{cell['filtre']:>14}{cell['n']:>6}"
              f"{cell['moyenne_prudente']:>10.3f}")

    winner = best[0]
    above = winner["moyenne_prudente"] > 1.0
    print("\n  VERDICT :")
    if not validated:
        print("    La section 1 n'a PAS valide les rendements : ces chiffres "
              "ne sont pas exploitables, et ils ne sont pas repris dans le "
              "recapitulatif.")
    elif above:
        print(f"    OUI : {winner['latence']} / {winner['sortie']} / "
              f"{winner['regle']} / {winner['filtre']} rend "
              f"{winner['moyenne_prudente']:.3f} net en moyenne prudente "
              f"sur {winner['n']} tokens, donc au-dessus de 1.")
    else:
        print(f"    NON : la meilleure combinaison rend "
              f"{winner['moyenne_prudente']:.3f} net en moyenne prudente, "
              f"donc en dessous de 1. Aucune regle testee ne bat le fait de "
              f"ne rien faire.")
    coherent("le classement est decroissant",
             all(best[i]["moyenne_prudente"] >= best[i + 1]["moyenne_prudente"]
                 for i in range(len(best) - 1)), "ordre rompu")
    return {"classement": best, "verdict": bool(above),
            "exploitable": bool(validated), "ecartees": ignored}


def main() -> None:
    global _run_at
    setup_logging()
    diagnose_environment()
    _run_at = datetime.now(timezone.utc).isoformat()
    print("\nExperience 1 : cloture. Valider les rendements, puis chercher "
          "une regle.")
    print(f"  source : {GRAD_PATHS_TABLE} | aucun appel Helius")
    print(f"  CoinGecko : {MAX_CALLS} appels au plus, {PAUSE_S} s de pause")
    if os.environ.get("HELIUS_API_KEY"):
        print("  (HELIUS_API_KEY presente mais NON utilisee par ce mode)")

    rows = [r for r in db.fetch_all_grad_paths()
            if (r.get("status") or "mesure") == "mesure"]
    ready = [r for r in rows if complete(r)]
    print(f"  {len(rows)} trajectoire(s) mesurees, {len(ready)} a l'etape 2 "
          f"complete")
    if not ready:
        log.error("Aucune trajectoire complete : rien a valider.")
        return

    rng = random.Random(RANDOM_SEED)
    results: dict[str, Any] = {}
    results["1"] = section_1(ready, rng)
    validated = bool(results["1"].get("valide"))
    results["2"] = section_2(ready)
    results["3"] = section_3(results["2"]["combinaisons"], validated)

    print("\n" + "=" * 74)
    print("RECAPITULATIF")
    print("=" * 74)
    first = results["1"]
    print(f"\nValidation externe : {first['accords']}/{first['testes']} "
          f"-> {'VALIDE' if validated else 'NON VALIDE'} "
          f"({first['appels']} appels CoinGecko)")
    if first.get("facteurs"):
        print(f"  facteur de capitalisation : mediane "
              f"{first['facteurs'].get('mediane')} | exposants "
              f"{first['facteurs'].get('exposants')}")
    print(f"  champs de montant utilises : {rules.amount_fields() or 'aucun'}")
    if validated:
        classement = results["3"].get("classement") or []
        if classement:
            best = classement[0]
            print(f"\nMeilleure regle : {best['latence']} / {best['sortie']} "
                  f"/ {best['regle']} / {best['filtre']} -> "
                  f"{best['moyenne_prudente']:.3f} net sur {best['n']} tokens")
        print(f"Une regle bat-elle le fait de ne rien faire : "
              f"{'OUI' if results['3'].get('verdict') else 'NON'}")
    else:
        print("\nLes regles de sortie ne sont PAS reprises : leurs "
              "rendements ne sont pas valides.")
    if _incoherences:
        print(f"\n{len(_incoherences)} INCOHERENCE(S) :")
        for item in _incoherences:
            print(f"  - {item}")
    else:
        print("\nAucune incoherence.")

    payload = {"validation": results["1"],
               "regles": results["2"]["combinaisons"] if validated else [],
               "classement": results["3"],
               "exploitable": validated,
               "champs_montant": rules.amount_fields(),
               "round_trip_cost": ROUND_TRIP_COST,
               "incoherences": list(_incoherences)}
    try:
        db.insert_run_log(RUN_MODE, _run_at, "recap", "cloture", payload)
        print(f"\n{RUN_LOG_TABLE} : run {_run_at} ecrit.")
    except Exception as error:               # noqa: BLE001
        log.error("PERTE : recapitulatif non ecrit dans %s : %s",
                  RUN_LOG_TABLE, error)
        raise


if __name__ == "__main__":
    main()
