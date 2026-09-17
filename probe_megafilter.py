"""Sonde jetable : que vaut reellement /pools/megafilter ?

Script d'observation, jamais appele par le pipeline. Lance via
RUN_MODE=probe (voir main.py) ou directement.

Question posee : cet endpoint pourrait-il remplacer toute l'etape de
collecte (new_pools + trending_pools + pools) ?

Methode : une baseline sans filtre, puis un appel par filtre/tri candidat.
Un parametre est dit IGNORE quand il renvoie exactement le meme jeu de pools
que la baseline, ou quand les lignes renvoyees violent la contrainte
demandee. C'est le seul test fiable : l'API ne signale pas les parametres
qu'elle n'a pas compris.

On observe et on affiche. On ne conclut rien ici.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import geckoterminal as gt
from config import (
    MIN_LIQUIDITY_USD,
    NETWORK,
    diagnose_environment,
    setup_logging,
)

ENDPOINT = f"/networks/{NETWORK}/pools/megafilter"

# Filtres volontairement larges : on sonde le comportement de l'endpoint,
# pas la pertinence des resultats.
BASE_FILTERS: dict[str, Any] = {
    "reserve_in_usd_min": MIN_LIQUIDITY_USD,
    "fdv_usd_min": 100_000,
    "fdv_usd_max": 50_000_000,
}

# (libelle, parametres ajoutes a la baseline)
PROBES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("filtres liquidite + fdv", dict(BASE_FILTERS)),
    ("tri pool_created_at_desc", {**BASE_FILTERS, "sort": "pool_created_at_desc"}),
    ("tri h24_volume_usd_desc", {**BASE_FILTERS, "sort": "h24_volume_usd_desc"}),
    ("age pool_created_hour_min", {**BASE_FILTERS, "pool_created_hour_min": 168}),
    ("age pool_created_hour_max", {**BASE_FILTERS, "pool_created_hour_max": 1440}),
)


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _created_at(pool: dict) -> datetime | None:
    raw = pool.get("attributes", {}).get("pool_created_at")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _ids(pools: list[dict]) -> list[str]:
    return [p.get("id", "") for p in pools]


def _preview(value: object, depth: int = 0) -> str:
    """Rend la forme d'un objet JSON sans deverser tout son contenu."""
    pad = "  " * depth
    if isinstance(value, dict):
        lines = [f"{pad}{{"]
        for key, sub in value.items():
            if isinstance(sub, (dict, list)):
                lines.append(f"{pad}  {key} :")
                lines.append(_preview(sub, depth + 2))
            else:
                lines.append(f"{pad}  {key} : {sub!r}")
        lines.append(f"{pad}}}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return f"{pad}[] (vide)"
        return f"{pad}[{len(value)} elements] premier :\n" + _preview(value[0], depth + 1)
    return f"{pad}{value!r}"


def _fetch(params: dict[str, Any]) -> list[dict] | None:
    payload = gt.get(ENDPOINT, {**params, "include": "base_token"})
    if payload is None:
        return None
    data = payload.get("data")
    return data if isinstance(data, list) else None


def _describe_ages(pools: list[dict]) -> str:
    now = datetime.now(timezone.utc)
    ages = [
        (now - c).total_seconds() / 86400.0
        for c in (_created_at(p) for p in pools)
        if c is not None
    ]
    if not ages:
        return "age : aucune date exploitable"
    return f"age j : min {min(ages):.1f} / max {max(ages):.1f} ({len(ages)} dates)"


def _sort_respected(pools: list[dict]) -> str:
    """Le tri par date de creation est-il reellement applique ?"""
    dates = [c for c in (_created_at(p) for p in pools) if c is not None]
    if len(dates) < 2:
        return "tri : pas assez de dates pour juger"
    desc = all(a >= b for a, b in zip(dates, dates[1:]))
    asc = all(a <= b for a, b in zip(dates, dates[1:]))
    if desc:
        return "tri : dates decroissantes (tri par date APPLIQUE)"
    if asc:
        return "tri : dates croissantes"
    return "tri : dates NON ordonnees (tri par date sans effet visible)"


def _filters_respected(pools: list[dict], params: dict[str, Any]) -> str:
    """Les lignes renvoyees respectent-elles les bornes demandees ?"""
    breaches: list[str] = []
    checks = (
        ("reserve_in_usd_min", "reserve_in_usd", lambda v, b: v < b),
        ("fdv_usd_min", "fdv_usd", lambda v, b: v < b),
        ("fdv_usd_max", "fdv_usd", lambda v, b: v > b),
    )
    for param, attr, violates in checks:
        if param not in params:
            continue
        bound = float(params[param])
        bad = sum(
            1 for p in pools
            if violates(_to_float(p.get("attributes", {}).get(attr)), bound)
        )
        if bad:
            breaches.append(f"{param} viole par {bad} lignes")
    return "bornes : " + ("; ".join(breaches) if breaches else "toutes respectees")


def main() -> None:
    setup_logging()
    diagnose_environment()

    print(f"\nEndpoint sonde : {ENDPOINT}")
    print("Un parametre est dit IGNORE s'il rend le meme jeu de pools que la")
    print("baseline, ou si les lignes violent la contrainte demandee.\n")

    baseline = _fetch({})
    if baseline is None:
        print("Baseline indisponible (voir les logs PERTE). Sonde interrompue.")
        return
    if not baseline:
        print("Baseline vide : rien a comparer. Sonde interrompue.")
        return
    baseline_ids = set(_ids(baseline))
    print(f"BASELINE (aucun filtre) : {len(baseline)} pools")
    print(f"  {_describe_ages(baseline)}\n")

    for label, params in PROBES:
        pools = _fetch(params)
        if pools is None:
            print(f"{label:28} : PERTE, non concluant")
            continue
        ids = set(_ids(pools))
        identical = ids == baseline_ids
        verdict = "identique a la baseline (parametre probablement IGNORE)" \
            if identical else "jeu de pools different (parametre PRIS EN COMPTE)"
        print(f"{label:28} : {len(pools)} pools - {verdict}")
        print(f"  {_describe_ages(pools)}")
        print(f"  {_filters_respected(pools, params)}")
        if "sort" in params or any(k.startswith("pool_created") for k in params):
            print(f"  {_sort_respected(pools)}")
        print(f"  params envoyes : {json.dumps(params)}")

    print("\n--- Structure brute d'un element ---")
    print(_preview(baseline[0]))


if __name__ == "__main__":
    main()
