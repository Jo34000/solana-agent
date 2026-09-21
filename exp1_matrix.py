"""Experience 1 : la matrice, calculee sur les donnees deja ecrites.

AUCUN appel API. Lecture de sol_grad_paths uniquement.

Le run du 21/09 a ete arrete a la main pendant l'etape 2 : l'etape 1 est
complete (2 972 graduations), l'etape 2 est partielle. Ce mode dit ce que
ces donnees permettent de conclure, et ce qu'elles ne permettent pas.

Regle qui gouverne tout le module : un token ELIGIBLE mais NON MESURE
n'est ni une perte ni un zero. Il est absent de la matrice, et son
absence est comptee a part. Un token mesure dont le prix manque a un
horizon, lui, est un inactif : c'est une mesure.

Ecarts releves AVANT ecriture :

  1. Le seuil de 60 000 $ est celui sous lequel les donnees ont ete
     COLLECTEES. Les runs futurs utilisent un multiple de la
     capitalisation a la graduation ; ce module garde le seuil en dollars
     pour relire ce qui existe, sans quoi il mesurerait autre chose que
     ce qui a ete preleve.
  2. mcap_max_usd couvre TOUS les points d'une ligne, etape 2 comprise :
     l'utiliser pour les taux de base de l'etape 1 melangerait les deux
     etapes et gonflerait le resultat des seuls tokens suivis. Les taux
     de base sont donc recalcules sur les instants de l'etape 1 seuls.
  3. L'ordre de traitement de l'etape 2 etait celui d'insertion, donc
     CHRONOLOGIQUE : la section 1 le verifie sur les donnees au lieu de
     l'affirmer depuis le code.
"""

from __future__ import annotations

import logging
import statistics
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import exp1_window as exp
import graduations as rules
import supabase_client as db
from config import (
    GRAD_PATHS_TABLE,
    RUN_LOG_TABLE,
    diagnose_environment,
    setup_logging,
)

log = logging.getLogger("solana-agent")

RUN_MODE = "exp1_matrix"

# Seuil sous lequel les donnees existantes ont ete prelevees.
LEGACY_ENTRY_USD = 60_000.0

STAGE1_LABELS = tuple(label for label, _ in exp.STAGE1_POINTS)
STAGE2_LABELS = tuple(label for label, _ in exp.STAGE2_POINTS)

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


def mcap_at(row: dict, label: str) -> float:
    return rules.to_float(exp.point_of(row, label).get("mcap_usd"))


def above_legacy(row: dict, latency: str) -> bool:
    """Regle sous laquelle les donnees ont ete prelevees : 60 000 $."""
    return mcap_at(row, latency) >= LEGACY_ENTRY_USD


def eligible(row: dict) -> bool:
    """Au-dessus du seuil a l'un des trois instants de l'etape 1."""
    return any(mcap_at(row, label) >= LEGACY_ENTRY_USD
               for label in exp.LATENCIES)


def stage2_complete(row: dict) -> bool:
    """Les sept instants de l'etape 2 sont MESURES (actifs ou inactifs)."""
    points = row.get("points") or {}
    for label in STAGE2_LABELS:
        entry = points.get(label)
        if not isinstance(entry, dict):
            return False
        if entry.get("etat") not in ("actif", "inactif"):
            return False
    return True


def quartiles(values: list[float]) -> str:
    if not values:
        return "aucune valeur"
    ordered = sorted(values)
    return (f"min {ordered[0]:,.0f} | p25 {exp.percentile(ordered, 0.25):,.0f}"
            f" | mediane {statistics.median(ordered):,.0f} | p75 "
            f"{exp.percentile(ordered, 0.75):,.0f} | max {ordered[-1]:,.0f}")


# ---------------------------------------------------------------------------
# SECTION 1 - Etat des lieux
# ---------------------------------------------------------------------------


def section_1(rows: list[dict]) -> dict:
    start_section("1", "Etat des lieux")
    by_status: Counter = Counter(r.get("status") or "?" for r in rows)
    by_stage: Counter = Counter(int(rules.to_float(r.get("stage")))
                                for r in rows)
    print(f"  {len(rows)} ligne(s) dans {GRAD_PATHS_TABLE}")
    print(f"  par statut : {dict(by_status)}")
    print(f"  par etape  : {dict(sorted(by_stage.items()))}")

    measured = [r for r in rows if (r.get("status") or "mesure") == "mesure"]
    print(f"  mesurees : {len(measured)}")
    coherent("statuts = total", sum(by_status.values()) == len(rows),
             f"{sum(by_status.values())} pour {len(rows)} lignes")

    print(f"\n  part au-dessus de {LEGACY_ENTRY_USD:,.0f} $ par instant :")
    per_latency: dict[str, Any] = {}
    for latency in exp.LATENCIES:
        active = [r for r in measured if exp.point_of(r, latency).get("actif")]
        above = [r for r in measured if above_legacy(r, latency)]
        per_latency[latency] = {"actifs": len(active), "au_dessus": len(above),
                                "part": round(len(above) / len(measured), 5)
                                if measured else 0}
        print(f"    {latency:>8} : {len(above):5d} au-dessus "
              f"({len(above) / max(len(measured), 1):6.2%}) | "
              f"{len(active):5d} actifs")
        coherent(f"au-dessus <= actifs ({latency})",
                 len(above) <= len(active),
                 f"{len(above)} au-dessus pour {len(active)} actifs")

    eligibles = [r for r in measured if eligible(r)]
    complete = [r for r in eligibles if stage2_complete(r)]
    partial = [r for r in eligibles if not stage2_complete(r)]
    print(f"\n  eligibles (au-dessus a l'un des trois) : {len(eligibles)}")
    print(f"    etape 2 COMPLETE   : {len(complete)} "
          f"({len(complete) / max(len(eligibles), 1):.1%})")
    print(f"    etape 2 incomplete : {len(partial)}")
    coherent("complets + partiels = eligibles",
             len(complete) + len(partial) == len(eligibles),
             f"{len(complete)} + {len(partial)} pour {len(eligibles)}")

    # --- representativite -------------------------------------------------
    print("\n  le sous-ensemble mesure est-il representatif ?")
    print("    par journee :")
    days = sorted({str(r.get("jour")) for r in eligibles})
    per_day: dict[str, Any] = {}
    for day in days:
        total = [r for r in eligibles if str(r.get("jour")) == day]
        done = [r for r in total if stage2_complete(r)]
        share = len(done) / len(total) if total else 0
        per_day[day] = {"eligibles": len(total), "mesures": len(done),
                        "part": round(share, 4)}
        print(f"      {day} : {len(done):4d}/{len(total):4d} mesures "
              f"({share:6.1%})")

    entry_done = [max((mcap_at(r, label) for label in exp.LATENCIES),
                      default=0.0) for r in complete]
    entry_left = [max((mcap_at(r, label) for label in exp.LATENCIES),
                      default=0.0) for r in partial]
    print("    par capitalisation a l'entree :")
    print(f"      mesures    : {quartiles(entry_done)}")
    print(f"      non mesures: {quartiles(entry_left)}")

    # --- l'ordre etait-il chronologique ? ---------------------------------
    ordered = sorted(eligibles, key=lambda r: str(r.get("grad_at") or ""))
    quarter = max(1, len(ordered) // 4)
    shares = []
    for index in range(4):
        chunk = ordered[index * quarter:(index + 1) * quarter]
        if not chunk:
            continue
        done = sum(1 for r in chunk if stage2_complete(r))
        shares.append(done / len(chunk))
    print("    part mesuree par quart chronologique : "
          + ", ".join(f"{share:.0%}" for share in shares))
    chronological = (len(shares) >= 2 and shares[0] >= 0.8
                     and shares[-1] <= 0.2)
    if chronological:
        print("    -> L'ORDRE ETAIT CHRONOLOGIQUE : l'echantillon mesure "
              "n'est PAS un tirage au hasard des eligibles, il en est le "
              "debut. Toute lecture par journee ou par heure en herite.")
    else:
        print("    -> pas de gradient chronologique marque")

    return {"lignes": len(rows), "par_statut": dict(by_status),
            "par_etape": {str(k): v for k, v in sorted(by_stage.items())},
            "mesurees": len(measured), "par_latence": per_latency,
            "eligibles": len(eligibles), "complets": len(complete),
            "partiels": len(partial), "par_jour": per_day,
            "quarts_chronologiques": [round(s, 4) for s in shares],
            "ordre_chronologique": chronological,
            "_complets": complete, "_mesurees": measured}


# ---------------------------------------------------------------------------
# SECTIONS 2 et 3
# ---------------------------------------------------------------------------


def section_2(complete: list[dict], eligibles: int) -> dict:
    start_section("2", "La matrice, sur les etapes 2 COMPLETES seulement")
    print(f"  population : {len(complete)} token(s) dont l'etape 2 est "
          f"complete, sur {eligibles} eligibles")
    print("  un eligible non mesure n'est NI une perte NI un zero : il est "
          "absent de la matrice.")
    if not complete:
        print("  aucune trajectoire complete : matrice non calculable")
        return {}
    matrix = exp.compute_matrix(complete, above_legacy)
    for latency, block in matrix.items():
        coherent(f"au-dessus <= actives ({latency})",
                 block["au_dessus"] <= block["actives"],
                 f"{block['au_dessus']} au-dessus pour {block['actives']} "
                 f"actives")
        for label, cell in (block.get("horizons") or {}).items():
            coherent(f"effectif de la cellule ({latency}/{label})",
                     cell["n"] + cell["inactifs"] <= block["au_dessus"],
                     f"{cell['n']} + {cell['inactifs']} pour "
                     f"{block['au_dessus']} au-dessus")
    return {"population": len(complete), "eligibles": eligibles,
            "matrice": matrix}


def section_3(measured: list[dict]) -> dict:
    start_section("3", "Taux de base, sur l'etape 1 complete")
    print(f"  {len(measured)} graduations mesurees, instants de l'etape 1 "
          f"uniquement ({', '.join(STAGE1_LABELS)})")
    base = exp.base_rates(measured, STAGE1_LABELS)
    for threshold, stats in base.items():
        print(f"    >= {threshold:>12} $ : {stats['tokens']:5d} tokens "
              f"({stats['part']:7.3%})")
    values = list(base.values())
    for first, second in zip(values, values[1:]):
        coherent("taux de base decroissants",
                 second["tokens"] <= first["tokens"],
                 f"{second['tokens']} au-dessus du palier superieur pour "
                 f"{first['tokens']} en dessous")
    return {"population": len(measured), "taux_de_base": base}


def main() -> None:
    global _run_at
    setup_logging()
    diagnose_environment()
    _run_at = datetime.now(timezone.utc).isoformat()
    print("\nExperience 1 : calcul de la matrice, AUCUN appel API.")
    print(f"  source : {GRAD_PATHS_TABLE}")
    print(f"  seuil d'entree relu : {LEGACY_ENTRY_USD:,.0f} $ "
          f"(celui sous lequel les donnees ont ete prelevees)")

    rows = db.fetch_all_grad_paths()
    if not rows:
        log.error("Aucune trajectoire en base : rien a calculer.")
        return

    results: dict[str, Any] = {}
    results["1"] = section_1(rows)
    complete = results["1"].pop("_complets", [])
    measured = results["1"].pop("_mesurees", [])
    results["2"] = section_2(complete, results["1"]["eligibles"])
    results["3"] = section_3(measured)

    print("\n" + "=" * 74)
    print("RECAPITULATIF")
    print("=" * 74)
    print(f"\nEtape 1 : {results['1']['mesurees']} graduations mesurees")
    print(f"Eligibles : {results['1']['eligibles']} | etape 2 complete : "
          f"{results['1']['complets']}")
    if results["1"]["ordre_chronologique"]:
        print("La matrice porte sur le DEBUT de la fenetre, pas sur un "
              "echantillon aleatoire : les runs futurs traitent l'etape 2 "
              "dans un ordre aleatoire pour corriger cela.")
    if _incoherences:
        print(f"\n{len(_incoherences)} INCOHERENCE(S) :")
        for item in _incoherences:
            print(f"  - {item}")
    else:
        print("\nAucune incoherence.")

    payload = {"etat": results["1"], "matrice": results["2"],
               "taux_de_base": results["3"],
               "incoherences": list(_incoherences)}
    try:
        db.insert_run_log(RUN_MODE, _run_at, "recap", "matrice", payload)
        print(f"\n{RUN_LOG_TABLE} : run {_run_at} ecrit.")
    except Exception as error:               # noqa: BLE001
        log.error("PERTE : recapitulatif non ecrit dans %s : %s",
                  RUN_LOG_TABLE, error)
        raise


if __name__ == "__main__":
    main()
