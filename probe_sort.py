"""Sonde jetable : le tri de /networks/solana/pools est-il applique ?

Script d'observation, jamais appele par le pipeline. Lance via
RUN_MODE=probe (voir main.py) ou directement.

Motif : au run du 17/09, la source pools_volume a rendu un age median de
0,4 j avec sort=h24_volume_usd_desc, exactement comme sans tri. Hypothese :
le parametre est ignore par l'API, ou porte un autre nom.

Methode : 3 appels sur /networks/solana/pools?page=1, identiques a
l'exception du parametre de tri. Si les trois rendent la meme premiere
adresse, le tri n'est pas applique.

Remplace l'ancienne sonde megafilter : cet endpoint est reserve aux plans
payants et n'est pas exploitable sur la cle Demo.

On observe et on affiche. On ne conclut rien ici.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any

import geckoterminal as gt
from config import NETWORK, diagnose_environment, setup_logging

ENDPOINT = f"/networks/{NETWORK}/pools"

# (libelle, parametres de tri ajoutes). Le premier sert de reference.
VARIANTS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("a. sans parametre de tri", {}),
    ("b. sort=h24_volume_usd_desc", {"sort": "h24_volume_usd_desc"}),
    ("c. order=h24_volume_usd_desc", {"order": "h24_volume_usd_desc"}),
)


def _created_at(pool: dict) -> datetime | None:
    raw = pool.get("attributes", {}).get("pool_created_at")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _median_age_days(pools: list[dict]) -> str:
    now = datetime.now(timezone.utc)
    ages = [
        (now - c).total_seconds() / 86400.0
        for c in (_created_at(p) for p in pools)
        if c is not None
    ]
    if not ages:
        return "n/a"
    return f"{statistics.median(ages):.1f}".replace(".", ",")


def _address(pool: dict) -> str:
    return (
        pool.get("attributes", {}).get("address")
        or pool.get("id", "")
        or "?"
    )


def _fetch_page_one(params: dict[str, Any]) -> list[dict] | None:
    payload = gt.get(ENDPOINT, {"page": 1, **params})
    if payload is None:
        return None
    data = payload.get("data")
    return data if isinstance(data, list) else None


def main() -> None:
    setup_logging()
    diagnose_environment()

    print(f"\nSonde tri : {ENDPOINT}?page=1, 3 appels\n")

    results: list[tuple[str, list[str]]] = []
    for label, params in VARIANTS:
        pools = _fetch_page_one(params)
        if pools is None:
            print(f"{label:30} : PERTE, non concluant")
            continue
        if not pools:
            print(f"{label:30} : 0 pool")
            continue
        addresses = [_address(p) for p in pools]
        results.append((label, addresses))
        print(f"{label:30} : {len(pools)} pools")
        print(f"  premier    : {addresses[0]}")
        print(f"  dernier    : {addresses[-1]}")
        print(f"  age median : {_median_age_days(pools)} j")

    print()
    if len(results) < 2:
        print("Pas assez d'appels exploitables pour comparer.")
        return

    firsts = {addresses[0] for _, addresses in results}
    if len(firsts) == 1:
        print("VERDICT : meme premiere adresse partout -> LE TRI EST IGNORE.")
    else:
        print("VERDICT : premieres adresses differentes -> un tri est applique.")
        for label, addresses in results:
            print(f"  {label:30} -> {addresses[0]}")

    # L'ordre complet est une preuve plus forte que la seule premiere adresse.
    reference_label, reference = results[0]
    for label, addresses in results[1:]:
        if addresses == reference:
            verdict = f"sequence IDENTIQUE a '{reference_label}'"
        elif set(addresses) == set(reference):
            verdict = "memes pools, ordre different"
        else:
            verdict = "jeu de pools different"
        print(f"  {label:30} : {verdict}")


if __name__ == "__main__":
    main()
