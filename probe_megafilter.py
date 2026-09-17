"""Sonde jetable : a quoi ressemble /pools/megafilter ?

Script independant, jamais appele par find_winners.py. Il fait UN seul appel
et affiche brut la structure de la reponse et le nombre de resultats.

But : savoir si cet endpoint pourrait, a lui seul, remplacer l'etape de
collecte (new_pools + trending_pools + pools). On ne conclut rien ici :
on regarde, on decide ailleurs.

Usage : python probe_megafilter.py
"""

from __future__ import annotations

import json

import geckoterminal as gt
from config import (
    MIN_LIQUIDITY_USD,
    NETWORK,
    diagnose_environment,
    setup_logging,
)

# Filtres volontairement larges : on sonde la forme de la reponse, pas la
# pertinence des resultats.
PARAMS = {
    "reserve_in_usd_min": MIN_LIQUIDITY_USD,
    "fdv_usd_min": 100_000,
    "fdv_usd_max": 50_000_000,
    "sort": "pool_created_at_desc",
    "include": "base_token",
}


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


def main() -> None:
    setup_logging()
    diagnose_environment()

    endpoint = f"/networks/{NETWORK}/pools/megafilter"
    print(f"\nAppel : {endpoint}")
    print(f"Params : {json.dumps(PARAMS, indent=2)}\n")

    payload = gt.get(endpoint, PARAMS)
    if payload is None:
        print("Aucune reponse exploitable (voir les logs PERTE ci-dessus).")
        return

    data = payload.get("data")
    included = payload.get("included") or []
    print("Cles racine       :", list(payload.keys()))
    print("Nombre de pools   :", len(data) if isinstance(data, list) else "n/a")
    print("Nombre d'inclus   :", len(included))
    print("Meta              :", json.dumps(payload.get("meta"), indent=2))

    if isinstance(data, list) and data:
        print("\n--- Structure du premier pool ---")
        print(_preview(data[0]))
    if included:
        print("\n--- Structure du premier 'included' ---")
        print(_preview(included[0]))


if __name__ == "__main__":
    main()
