"""Experience 1 : correction des unites, puis lecture fine.

Lecture de sol_grad_paths. Le SEUL appel autorise est getTokenSupply sur
les tokens suspects, sous un plafond de SUPPLY_CREDITS.

Une courbe pump.fun gradue a reserve fixe : la capitalisation EN SOL a la
graduation doit etre tres resserree. Toute dispersion est un defaut de
mesure, pas un fait de marche, et la section 1 la traque.

Ecarts releves AVANT ecriture, et ce qui a ete decide :

  1. La regle d'activite a +/- 10 min NE PEUT PAS etre appliquee aux
     donnees existantes : les points enregistres ne portent pas l'ecart
     au temps vise. Pire, l'ancienne tolerance etait PROPORTIONNELLE
     (max(15 min, 25 % de l'horizon)), donc a 7 jours un swap 42 HEURES
     apres l'instant vise comptait comme "actif". La section 2 mesure
     donc ce qui est mesurable sur ces donnees, en affichant la fenetre
     reellement utilisee horizon par horizon ; exp1_window enregistre
     desormais ecart_s a chaque point, et applique une fenetre FIXE.
  2. La correction des capitalisations est un simple RAPPORT : la
     capitalisation stockee vaut prix x taux x supply, donc la corriger
     revient a la multiplier par supply_corrigee / supply_stockee. Aucun
     prix du SOL n'est redemande. Les lignes dont la supply valait zero
     n'ont aucune capitalisation a corriger : elles sont comptees a part,
     jamais remises a zero.
  3. La matrice ne porte que sur les etapes 2 COMPLETES, comme en v1 :
     melanger les incompletes transformerait un point NON MESURE en
     inactif, c'est-a-dire en zero.
  4. ROUND_TRIP_COST s'applique en MULTIPLICATIF : un aller-retour a 3 %
     laisse 0,97 du multiple brut. C'est la convention retenue, ecrite
     ici pour qu'elle ne soit pas devinee.
"""

from __future__ import annotations

import logging
import os
import statistics
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import exp1_window as exp
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

RUN_MODE = "exp1_matrix_v2"

SUPPLY_CREDITS = exp._env_int("SUPPLY_CREDITS", 3_000)
SUPPLY_COST = 1                      # getTokenSupply : 1 credit
OUTLIER_BAND = exp._env_float("OUTLIER_BAND", 0.30)
ROUND_TRIP_COST = exp._env_float("ROUND_TRIP_COST", 0.03)
LEGACY_ENTRY_USD = 60_000.0
PUMPFUN_SUPPLY = 1_000_000_000.0     # supply affichee d'un token pump.fun
PUMPFUN_DECIMALS = 6

STAGE1_LABELS = tuple(label for label, _ in exp.STAGE1_POINTS)
STAGE2_LABELS = tuple(label for label, _ in exp.STAGE2_POINTS)

CONDITION_LATENCY = "15 min"
CONDITION_HORIZONS = ("60 min", "2 h", "3 h", "6 h")
ENTRY_BUCKETS = ((1.5, "< x1,5"), (3.0, "x1,5-3"), (10.0, "x3-10"),
                 (float("inf"), "> x10"))
MOMENTUM_BUCKETS = ((0.8, "< 0,8"), (1.2, "0,8-1,2"),
                    (float("inf"), "> 1,2"))
TOUCH_LEVELS = (1.5, 2.0, 5.0)

_credits = 0
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


def bucket_of(value: float, buckets) -> str:
    for edge, label in buckets:
        if value < edge:
            return label
    return buckets[-1][1]


def stats_of(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {"n": len(values), "mediane": round(statistics.median(values), 3),
            "p10": round(exp.percentile(values, 0.10), 3),
            "p90": round(exp.percentile(values, 0.90), 3)}


def net(multiple: float) -> float:
    """Un aller-retour a ROUND_TRIP_COST laisse (1 - cout) du brut."""
    return multiple * (1.0 - ROUND_TRIP_COST)


def price_of(row: dict, label: str) -> float:
    return rules.to_float(exp.point_of(row, label).get("prix_sol"))


def mcap_of(row: dict, label: str) -> float:
    return rules.to_float(exp.point_of(row, label).get("mcap_usd"))


def grad_label(row: dict) -> str | None:
    """Premier instant de l'etape 1 qui porte un prix."""
    for label in STAGE1_LABELS:
        if price_of(row, label) > 0:
            return label
    return None


def stage2_complete(row: dict) -> bool:
    points = row.get("points") or {}
    for label in STAGE2_LABELS:
        entry = points.get(label)
        if not isinstance(entry, dict) or entry.get("etat") not in (
                "actif", "inactif"):
            return False
    return True


# ---------------------------------------------------------------------------
# SECTION 1 - Controle des capitalisations
# ---------------------------------------------------------------------------


def supply_credits_left() -> int:
    return SUPPLY_CREDITS - _credits


def reread_supply(mint: str) -> dict | None:
    global _credits
    if supply_credits_left() < SUPPLY_COST:
        return None
    _credits += SUPPLY_COST
    value = helius.get_token_supply(mint)
    if value is None:
        return None
    ui, decimals, raw = rules.supply_of(value)
    return {"ui": ui, "decimals": decimals, "raw": raw}


def diagnose(row: dict, fresh: dict) -> tuple[str, float]:
    """(cause, supply corrigee en unites affichees)."""
    stored = rules.to_float(row.get("supply"))
    ui, decimals, raw = fresh["ui"], fresh["decimals"], fresh["raw"]
    if not stored:
        return "supply_absente", ui
    if raw and abs(stored - raw) <= 0.01 * raw:
        return "supply_brute_stockee", ui
    if decimals != PUMPFUN_DECIMALS:
        return f"decimales_{decimals}", ui
    if ui and abs(ui - PUMPFUN_SUPPLY) > 0.1 * PUMPFUN_SUPPLY:
        return "supply_hors_norme_pumpfun", ui
    if ui and abs(stored - ui) > 0.01 * ui:
        return "supply_stockee_erronee", ui
    return "supply_correcte_donc_prix", ui


def section_1(rows: list[dict]) -> dict:
    start_section("1", "Controle des capitalisations")
    print("  Une courbe pump.fun gradue a reserve fixe : la capitalisation "
          "EN SOL a la graduation doit etre tres resserree.")
    measured = [r for r in rows if (r.get("status") or "mesure") == "mesure"]

    sols: list[float] = []
    per_row: dict[str, float] = {}
    sans_supply = 0
    sans_prix = 0
    for row in measured:
        supply = rules.to_float(row.get("supply"))
        label = grad_label(row)
        if not supply:
            sans_supply += 1
            continue
        if not label:
            sans_prix += 1
            continue
        value = price_of(row, label) * supply
        per_row[row["mint"]] = value
        sols.append(value)

    print(f"  {len(measured)} lignes mesurees | {len(sols)} capitalisations "
          f"calculables | {sans_supply} sans supply | {sans_prix} sans prix")
    if not sols:
        print("  aucune capitalisation calculable : section interrompue")
        return {"calculables": 0}

    median = statistics.median(sols)
    p5, p95 = exp.percentile(sols, 0.05), exp.percentile(sols, 0.95)
    print("\n  capitalisation a la graduation, en SOL :")
    print(f"    p5 {p5:,.1f} | mediane {median:,.1f} | p95 {p95:,.1f}")
    spread = (p95 / p5) if p5 else float("inf")
    print(f"    p95 / p5 = {spread:,.1f}")
    if spread > 3:
        print("    -> la distribution N'EST PAS resserree : il y a un defaut "
              "de mesure, pas un fait de marche")
    else:
        print("    -> distribution resserree, conforme a une reserve fixe")

    low, high = median * (1 - OUTLIER_BAND), median * (1 + OUTLIER_BAND)
    suspects = [mint for mint, value in per_row.items()
                if not low <= value <= high]
    print(f"    hors de +/-{OUTLIER_BAND:.0%} de la mediane : "
          f"{len(suspects)}/{len(per_row)} "
          f"({len(suspects) / len(per_row):.1%})")

    # --- relecture des suspects ------------------------------------------
    budget = supply_credits_left() // SUPPLY_COST
    print(f"\n  relecture de la supply : {min(len(suspects), budget)} "
          f"token(s) sur {len(suspects)} suspects "
          f"(plafond {SUPPLY_CREDITS} credits)")
    by_mint = {r["mint"]: r for r in measured}
    causes: Counter = Counter()
    corrections: dict[str, float] = {}
    for mint in suspects[:budget]:
        fresh = reread_supply(mint)
        if fresh is None:
            causes["relecture_impossible"] += 1
            continue
        cause, corrected = diagnose(by_mint[mint], fresh)
        causes[cause] += 1
        stored = rules.to_float(by_mint[mint].get("supply"))
        if corrected and stored and abs(corrected - stored) > 0.01 * corrected:
            corrections[mint] = corrected
    if len(suspects) > budget:
        log.warning("%d suspect(s) non relus faute de credits : leur "
                    "capitalisation reste celle d'origine",
                    len(suspects) - budget)
    print(f"  causes : {dict(causes.most_common())}")
    print(f"  {len(corrections)} supply(s) corrigee(s)")

    # --- recalcul : un simple rapport ------------------------------------
    before = exp.base_rates(measured, STAGE1_LABELS)
    corrected_rows = []
    for row in measured:
        new_supply = corrections.get(row["mint"])
        if not new_supply:
            corrected_rows.append(row)
            continue
        stored = rules.to_float(row.get("supply")) or 1.0
        ratio = new_supply / stored
        points = {}
        for label, entry in (row.get("points") or {}).items():
            if isinstance(entry, dict) and entry.get("mcap_usd"):
                entry = {**entry, "mcap_usd": entry["mcap_usd"] * ratio}
            points[label] = entry
        corrected_rows.append({**row, "points": points,
                               "supply": new_supply})
    after = exp.base_rates(corrected_rows, STAGE1_LABELS)

    print("\n  taux de base, AVANT et APRES correction :")
    print(f"    {'palier':>14}{'avant':>12}{'apres':>12}")
    for threshold in before:
        print(f"    {threshold:>14}{before[threshold]['tokens']:>7} "
              f"({before[threshold]['part']:5.2%}){after[threshold]['tokens']:>7}"
              f" ({after[threshold]['part']:5.2%})")
        coherent(f"taux de base plausible ({threshold})",
                 after[threshold]["part"] <= 1.0,
                 f"{after[threshold]['part']:.2%}")

    return {"calculables": len(sols), "mediane_sol": round(median, 2),
            "p5_sol": round(p5, 2), "p95_sol": round(p95, 2),
            "dispersion": round(spread, 2), "suspects": len(suspects),
            "relus": sum(causes.values()), "causes": dict(causes),
            "corriges": len(corrections), "sans_supply": sans_supply,
            "avant": before, "apres": after, "credits": _credits,
            "_rows": corrected_rows}


# ---------------------------------------------------------------------------
# Le coeur : une cellule, tous ses chiffres
# ---------------------------------------------------------------------------


def legacy_window(delta: float) -> float:
    """Fenetre REELLEMENT utilisee pour les donnees existantes."""
    return max(900.0, delta * 0.25)


def auditable(entry: dict) -> bool:
    """Le point porte-t-il son ecart au temps vise ?"""
    return isinstance(entry, dict) and entry.get("ecart_s") is not None


def active_at(row: dict, label: str) -> bool:
    """Actif selon la regle unique, quand les donnees le permettent.

    Un point qui porte son ecart est filtre a +/- ACTIVITY_WINDOW. Un
    point qui ne le porte pas garde le verdict enregistre, et la section
    2 dit combien de points sont dans ce cas.
    """
    entry = exp.point_of(row, label)
    if not entry.get("actif"):
        return False
    if auditable(entry):
        return rules.to_float(entry.get("ecart_s")) <= exp.ACTIVITY_WINDOW
    return True


def above(row: dict, latency: str) -> bool:
    return (active_at(row, latency)
            and mcap_of(row, latency) >= LEGACY_ENTRY_USD)


def horizon_stats(population: list[dict], latency: str, label: str) -> dict:
    """Tous les chiffres d'une cellule (latence, horizon)."""
    seconds = exp.POINT_SECONDS[latency]
    target = exp.POINT_SECONDS[label]
    returns: list[float] = []
    inactive = 0
    drawdowns: list[float] = []
    peaks: list[float] = []
    for row in population:
        entry_price = price_of(row, latency)
        if entry_price <= 0:
            continue
        if active_at(row, label):
            returns.append(price_of(row, label) / entry_price)
        else:
            inactive += 1
        between = [price_of(row, mid) / entry_price
                   for mid, mid_delta in exp.ALL_POINTS
                   if seconds < mid_delta <= target
                   and active_at(row, mid) and price_of(row, mid) > 0]
        if between:
            drawdowns.append(min(between))
            peaks.append(max(between))
    total = len(returns) + inactive
    gross_mean = statistics.fmean(returns) if returns else 0.0
    zero_mean = sum(returns) / total if total else 0.0
    touched = {level: (sum(1 for peak in peaks if peak >= level) / len(peaks))
               if peaks else 0.0 for level in TOUCH_LEVELS}
    return {
        "actifs": len(returns), "inactifs": inactive,
        "mediane": round(statistics.median(returns), 3) if returns else 0.0,
        "mediane_nette": round(net(statistics.median(returns)), 3)
        if returns else 0.0,
        "part_x2": round(sum(1 for r in returns if net(r) >= 2.0)
                         / len(returns), 4) if returns else 0.0,
        "part_x2_brut": round(sum(1 for r in returns if r >= 2.0)
                              / len(returns), 4) if returns else 0.0,
        "part_perte": round(sum(1 for r in returns if net(r) <= 0.3)
                            / len(returns), 4) if returns else 0.0,
        "moyenne_exclus": round(gross_mean, 3),
        "moyenne_exclus_nette": round(net(gross_mean), 3),
        "moyenne_zero": round(zero_mean, 3),
        "moyenne_zero_nette": round(net(zero_mean), 3),
        "creux_median": round(statistics.median(drawdowns), 3)
        if drawdowns else 0.0,
        "creux_p10": round(exp.percentile(drawdowns, 0.10), 3)
        if drawdowns else 0.0,
        "max_median": round(statistics.median(peaks), 3) if peaks else 0.0,
        "max_p90": round(exp.percentile(peaks, 0.90), 3) if peaks else 0.0,
        "touche": {f"x{level:g}": round(share, 4)
                   for level, share in touched.items()},
        "_returns": returns,
    }


def horizons_after(latency: str) -> list[str]:
    seconds = exp.POINT_SECONDS[latency]
    return [label for label, delta in exp.ALL_POINTS if delta > seconds]


# ---------------------------------------------------------------------------
# SECTION 2 - Definition unique de l'activite
# ---------------------------------------------------------------------------


def section_2(rows: list[dict]) -> dict:
    start_section("2", "Definition unique de l'activite")
    print(f"  Regle : un swap dans +/- {exp.ACTIVITY_WINDOW / 60:.0f} min de "
          f"l'instant vise, la MEME a tous les horizons.")
    points_total = sum(len(r.get("points") or {}) for r in rows)
    points_auditables = sum(1 for r in rows
                            for entry in (r.get("points") or {}).values()
                            if auditable(entry))
    print(f"  points portant leur ecart : {points_auditables}/{points_total}")
    if points_auditables < points_total:
        print("  Les points sans ecart gardent leur verdict d'origine : la "
              "fenetre alors utilisee etait PROPORTIONNELLE, soit")
        for label, delta in exp.ALL_POINTS:
            print(f"      {label:>8} : +/- {legacy_window(float(delta)) / 60:6.0f} "
                  f"min")
        print("  A 7 jours, un swap 42 heures apres l'instant vise comptait "
              "donc comme actif. exp1_window enregistre desormais l'ecart.")

    matrix: dict[str, Any] = {}
    for latency in exp.LATENCIES:
        population = [r for r in rows if above(r, latency)]
        print(f"\n  --- latence {latency} : {len(population)} token(s) "
              f"au-dessus de {LEGACY_ENTRY_USD:,.0f} $ ---")
        if not population:
            matrix[latency] = {"population": 0, "horizons": {}}
            continue
        print(f"    {'horizon':>8}{'actifs':>8}{'inactifs':>10}"
              f"{'med.brut':>10}{'med.net':>9}{'moy.ex':>9}{'moy.0':>8}")
        horizons: dict[str, Any] = {}
        for label in horizons_after(latency):
            cell = horizon_stats(population, latency, label)
            cell.pop("_returns", None)
            horizons[label] = cell
            coherent(f"effectif ({latency}/{label})",
                     cell["actifs"] + cell["inactifs"] <= len(population),
                     f"{cell['actifs']} + {cell['inactifs']} pour "
                     f"{len(population)}")
            print(f"    {label:>8}{cell['actifs']:>8}{cell['inactifs']:>10}"
                  f"{cell['mediane']:>10.2f}{cell['mediane_nette']:>9.2f}"
                  f"{cell['moyenne_exclus']:>9.2f}{cell['moyenne_zero']:>8.2f}")
        matrix[latency] = {"population": len(population),
                           "horizons": horizons}
    return {"points_auditables": points_auditables,
            "points_total": points_total, "matrice": matrix}


# ---------------------------------------------------------------------------
# SECTION 3 - Creux et maximum avant l'horizon
# ---------------------------------------------------------------------------


def section_3(rows: list[dict]) -> dict:
    start_section("3", "Creux et maximum avant l'horizon")
    print("  Creux et maximum sont pris sur les points MESURES entre "
          "l'entree et l'horizon, rapportes au prix d'entree.")
    outcome: dict[str, Any] = {}
    for latency in exp.LATENCIES:
        population = [r for r in rows if above(r, latency)]
        if not population:
            continue
        print(f"\n  --- latence {latency} ---")
        print(f"    {'horizon':>8}{'creux med':>11}{'creux p10':>11}"
              f"{'max med':>9}{'max p90':>9}"
              + "".join(f"{'touche x' + f'{level:g}':>12}"
                        for level in TOUCH_LEVELS))
        per_horizon: dict[str, Any] = {}
        for label in horizons_after(latency):
            cell = horizon_stats(population, latency, label)
            cell.pop("_returns", None)
            per_horizon[label] = {
                "creux_median": cell["creux_median"],
                "creux_p10": cell["creux_p10"],
                "max_median": cell["max_median"],
                "max_p90": cell["max_p90"], "touche": cell["touche"],
            }
            coherent(f"creux <= max ({latency}/{label})",
                     cell["creux_median"] <= cell["max_median"]
                     or not cell["max_median"],
                     f"creux {cell['creux_median']} > max "
                     f"{cell['max_median']}")
            print(f"    {label:>8}{cell['creux_median']:>11.2f}"
                  f"{cell['creux_p10']:>11.2f}{cell['max_median']:>9.2f}"
                  f"{cell['max_p90']:>9.2f}"
                  + "".join(f"{cell['touche'][f'x{level:g}']:>12.1%}"
                            for level in TOUCH_LEVELS))
        outcome[latency] = per_horizon
    return outcome


# ---------------------------------------------------------------------------
# SECTION 4 - Frais
# ---------------------------------------------------------------------------


def section_4(rows: list[dict]) -> dict:
    start_section("4", f"Frais : aller-retour a {ROUND_TRIP_COST:.1%}")
    print("  Convention : le net vaut le brut x "
          f"{1 - ROUND_TRIP_COST:.2f}. Elle est ecrite, pas devinee.")
    outcome: dict[str, Any] = {}
    for latency in exp.LATENCIES:
        population = [r for r in rows if above(r, latency)]
        if not population:
            continue
        print(f"\n  --- latence {latency} ---")
        print(f"    {'horizon':>8}{'med.brut':>10}{'med.net':>9}"
              f"{'>=x2 brut':>11}{'>=x2 net':>10}{'moy.0 net':>11}")
        per_horizon: dict[str, Any] = {}
        for label in horizons_after(latency):
            cell = horizon_stats(population, latency, label)
            cell.pop("_returns", None)
            per_horizon[label] = {
                "mediane": cell["mediane"],
                "mediane_nette": cell["mediane_nette"],
                "part_x2_brut": cell["part_x2_brut"],
                "part_x2": cell["part_x2"],
                "moyenne_zero_nette": cell["moyenne_zero_nette"],
            }
            print(f"    {label:>8}{cell['mediane']:>10.2f}"
                  f"{cell['mediane_nette']:>9.2f}"
                  f"{cell['part_x2_brut']:>11.1%}{cell['part_x2']:>10.1%}"
                  f"{cell['moyenne_zero_nette']:>11.2f}")
        outcome[latency] = per_horizon
    return outcome


# ---------------------------------------------------------------------------
# SECTION 5 - Deux conditionnements
# ---------------------------------------------------------------------------


def entry_multiple(row: dict) -> float:
    """Capitalisation a l'entree, en multiple de celle a la graduation."""
    label = grad_label(row)
    if not label:
        return 0.0
    reference = mcap_of(row, label)
    if reference <= 0:
        return 0.0
    return mcap_of(row, CONDITION_LATENCY) / reference


def momentum(row: dict) -> float:
    """Prix a 15 min / prix a 5 min. Zero si l'un des deux manque."""
    early = price_of(row, "5 min")
    late = price_of(row, CONDITION_LATENCY)
    if early <= 0 or late <= 0:
        return 0.0
    return late / early


def conditioned(population: list[dict], key, buckets, title: str) -> dict:
    print(f"\n  --- {title} ---")
    groups: dict[str, list[dict]] = {label: [] for _, label in buckets}
    ignored = 0
    for row in population:
        value = key(row)
        if value <= 0:
            ignored += 1
            continue
        groups[bucket_of(value, buckets)].append(row)
    if ignored:
        print(f"    {ignored} token(s) sans valeur de conditionnement, "
              f"ecartes (jamais comptes a zero)")
    print(f"    {'case':>10}{'horizon':>9}{'n':>5}{'med.net':>9}"
          f"{'>=x2':>8}{'<=x0.3':>9}{'moy.0 net':>11}")
    outcome: dict[str, Any] = {}
    for _, label in buckets:
        rows_in = groups[label]
        outcome[label] = {"effectif": len(rows_in), "horizons": {}}
        if not rows_in:
            print(f"    {label:>10}{'-':>9}{0:>5}")
            continue
        for horizon in CONDITION_HORIZONS:
            cell = horizon_stats(rows_in, CONDITION_LATENCY, horizon)
            cell.pop("_returns", None)
            outcome[label]["horizons"][horizon] = {
                "n": cell["actifs"], "inactifs": cell["inactifs"],
                "mediane_nette": cell["mediane_nette"],
                "part_x2": cell["part_x2"], "part_perte": cell["part_perte"],
                "moyenne_zero_nette": cell["moyenne_zero_nette"],
            }
            print(f"    {label:>10}{horizon:>9}{cell['actifs']:>5}"
                  f"{cell['mediane_nette']:>9.2f}{cell['part_x2']:>8.1%}"
                  f"{cell['part_perte']:>9.1%}"
                  f"{cell['moyenne_zero_nette']:>11.2f}")
    total = sum(len(g) for g in groups.values()) + ignored
    coherent(f"somme des cases ({title})", total == len(population),
             f"{total} classes pour {len(population)} tokens")
    return outcome


def section_5(rows: list[dict]) -> dict:
    start_section("5", f"Deux conditionnements, latence {CONDITION_LATENCY}")
    population = [r for r in rows if above(r, CONDITION_LATENCY)]
    print(f"  population : {len(population)} token(s) au-dessus du seuil a "
          f"T+{CONDITION_LATENCY}")
    print(f"  horizons : {', '.join(CONDITION_HORIZONS)}")
    if not population:
        print("  population vide : section non calculable")
        return {}
    by_entry = conditioned(population, entry_multiple, ENTRY_BUCKETS,
                           "par capitalisation d'entree, en multiple de "
                           "celle a la graduation")
    by_momentum = conditioned(population, momentum, MOMENTUM_BUCKETS,
                              "par elan : prix a 15 min / prix a 5 min")
    return {"population": len(population), "par_entree": by_entry,
            "par_elan": by_momentum}


# ---------------------------------------------------------------------------
# Entree
# ---------------------------------------------------------------------------


def main() -> None:
    global _run_at
    setup_logging()
    diagnose_environment()
    _run_at = datetime.now(timezone.utc).isoformat()
    print("\nExperience 1 : correction des unites, puis lecture fine.")
    print(f"  source : {GRAD_PATHS_TABLE}")
    print(f"  seul appel autorise : getTokenSupply, plafond "
          f"{SUPPLY_CREDITS:,} credits")
    print(f"  bande de suspicion : +/-{OUTLIER_BAND:.0%} autour de la "
          f"mediane")
    print(f"  frais d'aller-retour : {ROUND_TRIP_COST:.1%}")
    if os.environ.get("HELIUS_API_KEY", "").strip():
        helius.api_key()
    else:
        log.warning("HELIUS_API_KEY absente : aucune supply ne sera relue, "
                    "la section 1 diagnostiquera sans corriger.")

    rows = db.fetch_all_grad_paths()
    if not rows:
        log.error("Aucune trajectoire en base : rien a calculer.")
        return

    results: dict[str, Any] = {}
    results["1"] = section_1(rows)
    corrected = results["1"].pop("_rows", [])

    complete = [r for r in corrected if stage2_complete(r)]
    print(f"\n  matrice : {len(complete)} token(s) a l'etape 2 complete "
          f"sur {len(corrected)} mesures")
    print("  un eligible non mesure reste absent : ni perte, ni zero.")

    results["2"] = section_2(complete)
    results["3"] = section_3(complete)
    results["4"] = section_4(complete)
    results["5"] = section_5(complete)

    print("\n" + "=" * 74)
    print("RECAPITULATIF")
    print("=" * 74)
    first = results["1"]
    print(f"\nCapitalisation a la graduation : mediane "
          f"{first.get('mediane_sol', 0):,.1f} SOL | p95/p5 "
          f"{first.get('dispersion', 0):,.1f}")
    print(f"  {first.get('suspects', 0)} suspect(s), "
          f"{first.get('relus', 0)} relu(s), "
          f"{first.get('corriges', 0)} corrige(s) "
          f"({_credits} credits)")
    print(f"  causes : {first.get('causes')}")
    print(f"Matrice : {len(complete)} trajectoires completes")
    print(f"Points portant leur ecart : "
          f"{results['2'].get('points_auditables')}/"
          f"{results['2'].get('points_total')}")
    if _incoherences:
        print(f"\n{len(_incoherences)} INCOHERENCE(S) :")
        for item in _incoherences:
            print(f"  - {item}")
    else:
        print("\nAucune incoherence.")

    payload = {"capitalisations": results["1"], "activite": results["2"],
               "creux_et_max": results["3"], "frais": results["4"],
               "conditionnements": results["5"],
               "credits": _credits, "round_trip_cost": ROUND_TRIP_COST,
               "incoherences": list(_incoherences)}
    try:
        db.insert_run_log(RUN_MODE, _run_at, "recap", "lecture fine", payload)
        print(f"\n{RUN_LOG_TABLE} : run {_run_at} ecrit.")
    except Exception as error:               # noqa: BLE001
        log.error("PERTE : recapitulatif non ecrit dans %s : %s",
                  RUN_LOG_TABLE, error)
        raise


if __name__ == "__main__":
    main()
