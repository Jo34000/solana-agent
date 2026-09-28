"""Diagnostic de la section 1 de l'experience 2 : pourquoi 0 achat ?

Le run du 25/09 a 09:23 a lu 800 tokens, depense 32 290 credits, tronque
547 tokens au plafond de pages... et n'a ecrit AUCUN achat. Une requete
qui renvoie des lignes et une extraction qui n'en retient aucune ne se
distinguent pas dans le recapitulatif : cette sonde les separe.

Elle refait la requete de la section 1 EXACTEMENT comme elle etait codee
le 25/09 (la config est recopiee ici, pas importee, pour que la
correction de exp2_wallets ne change pas ce qu'on mesure), et compte, a
chaque etage du filtre, combien de lignes survivent. La cause est le
premier etage ou il n'en reste aucune.

Aucune ecriture en base hors sol_run_log. Plafond : 10 appels Helius,
soit 100 credits.
"""

from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timezone
from typing import Any

import exp1_window as exp
import exp2_wallets as x
import graduations as rules
import helius
import supabase_client as db
from config import RUN_LOG_TABLE, diagnose_environment, setup_logging

log = logging.getLogger("solana-agent")

RUN_MODE = "exp2_diag"
MAX_CALLS = exp._env_int("DIAG_CALLS", 10)
TOKENS_WANTED = 3
SHOW_LINES = 5
SEED = exp._env_int("DIAG_SEED", 20260928)

# Le run invalide a corriger dans le journal.
BAD_RUN = "2026-09-25 09:23"

_calls = 0
_run_at = ""


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------


def _json(value: Any, limit: int = 1200) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= limit else text[:limit] + " ... [tronque]"


def log_run(section: str, label: str, payload: dict) -> None:
    try:
        db.insert_run_log(RUN_MODE, _run_at, section, label, payload)
    except Exception as error:               # noqa: BLE001
        log.error("PERTE : %s non ecrit dans %s : %s", label, RUN_LOG_TABLE,
                  error)
        raise


def write_correction() -> None:
    """Le verdict du 25/09 etait faux : le journal doit le dire.

    La ligne est ecrite sous run_mode exp2_wallets, a cote du run qu'elle
    corrige, pour que toute relecture de ce mode la trouve.
    """
    payload = {
        "run": BAD_RUN,
        "constat": "section 1 : 800 tokens, 32 290 credits, 0 achat ecrit",
        "verdict_affiche": "aucun signal en echantillon, placebo non battu",
        "correction": "section 2 invalide (0 achat) : mesure vide, pas un "
                      "verdict. Le placebo n'a pas ete calcule.",
    }
    db.insert_run_log("exp2_wallets", _run_at, "2", "correction", payload)
    print(f"  correction ecrite dans {RUN_LOG_TABLE} "
          f"(exp2_wallets / section 2) : {payload['correction']}")


def call_transfers(address: str, config: dict) -> Any:
    global _calls
    if _calls >= MAX_CALLS:
        return "CAPPED"
    _calls += 1
    return helius.rpc("getTransfersByAddress", [address, config])


def config_of(mint: str, graduated: float) -> dict:
    """La config du 25/09, a la cle pres. Recopiee, pas importee."""
    return {"limit": 100, "sortOrder": "asc",
            "mint": mint,
            "filters": {"blockTime": {"gte": int(graduated),
                                      "lte": int(graduated
                                                 + x.SIGNAL_WINDOW_S)}}}


def source_fields(line: dict) -> dict:
    """Tous les champs d'emission presents, sans en supposer aucun."""
    keys = ("fromUserAccount", "fromTokenAccount", "source", "from",
            "fromAddress", "sender", "owner")
    return {key: line[key] for key in keys if line.get(key)}


def target_fields(line: dict) -> dict:
    keys = ("toUserAccount", "toTokenAccount", "destination", "to",
            "toAddress", "receiver")
    return {key: line[key] for key in keys if line.get(key)}


def pool_accounts_of(lines: list[dict], pool: str, mint: str) -> set[str]:
    """Le pool, sa courbe, et tout compte de token dont il est proprietaire.

    Rien n'est devine : un compte n'entre ici que si une ligne dit
    explicitement que son proprietaire est le pool.
    """
    accounts = {pool}
    curve = rules.curve_of(mint)
    if curve:
        accounts.add(curve)
    for line in lines:
        if line.get("fromUserAccount") == pool and line.get(
                "fromTokenAccount"):
            accounts.add(line["fromTokenAccount"])
        if line.get("toUserAccount") == pool and line.get("toTokenAccount"):
            accounts.add(line["toTokenAccount"])
    return accounts


# ---------------------------------------------------------------------------
# Le trace d'une ligne, avec la regle du 25/09
# ---------------------------------------------------------------------------


def decision_of(line: dict, pool: str, mint: str) -> str:
    """Ce que la section 1 du 25/09 faisait de cette ligne."""
    if not isinstance(line.get("signature"), str):
        return ("REJET : pas de signature A LA RACINE, group_by_signature "
                "jette la ligne")
    line_mint = line.get("mint")
    if line_mint in rules.SOL_MINTS:
        return "jambe SOL, comptee dans le prix"
    if line_mint != mint:
        return f"REJET : autre mint ({str(line_mint)[:12]})"
    source = line.get("fromUserAccount")
    destination = line.get("toUserAccount")
    if source != pool:
        return f"REJET : emetteur != pool (fromUserAccount={str(source)[:12]})"
    if not isinstance(destination, str):
        return "REJET : pas de toUserAccount"
    if x.excluded_wallet(destination, pool):
        return f"REJET : destinataire exclu ({destination[:12]})"
    return "ACHAT (si une jambe SOL partage la signature)"


def funnel(lines: list[dict], pool: str, mint: str) -> dict:
    """Combien de lignes survivent a chaque etage, regle du 25/09."""
    accounts = pool_accounts_of(lines, pool, mint)
    counts = {
        "lignes": len(lines),
        "signature a la racine": 0,
        "signature retrouvee ailleurs": 0,
        "jambes du mint": 0,
        "emetteur == pool": 0,
        "emetteur dans les comptes du pool": 0,
        "destinataire utilisable": 0,
    }
    sources: dict[str, int] = {}
    with_sol: set[str] = set()
    mint_legs: dict[str, list[dict]] = {}
    for line in lines:
        signature, path = rules.extract_signature(line)
        if path == "signature":
            counts["signature a la racine"] += 1
        elif signature:
            counts["signature retrouvee ailleurs"] += 1
        if line.get("mint") in rules.SOL_MINTS and signature:
            with_sol.add(signature)
        if line.get("mint") != mint:
            continue
        counts["jambes du mint"] += 1
        source = line.get("fromUserAccount") or line.get("fromTokenAccount")
        sources[str(source)] = sources.get(str(source), 0) + 1
        if line.get("fromUserAccount") == pool:
            counts["emetteur == pool"] += 1
        if (line.get("fromUserAccount") in accounts
                or line.get("fromTokenAccount") in accounts):
            counts["emetteur dans les comptes du pool"] += 1
            destination = (line.get("toUserAccount")
                           or line.get("toTokenAccount"))
            if isinstance(destination, str) and not x.excluded_wallet(
                    destination, pool):
                counts["destinataire utilisable"] += 1
                if signature:
                    mint_legs.setdefault(signature, []).append(line)
    counts["signatures avec jambe SOL"] = len(with_sol)
    counts["achats, regle du 25/09"] = sum(
        1 for signature, legs in mint_legs.items()
        if signature in with_sol
        and any(leg.get("fromUserAccount") == pool for leg in legs))
    counts["achats, regle corrigee"] = sum(
        1 for signature in mint_legs if signature in with_sol)
    counts["achats corriges, sans exiger la jambe SOL"] = len(mint_legs)
    return {"compteurs": counts, "comptes_du_pool": sorted(accounts),
            "emetteurs": sorted(sources.items(), key=lambda kv: -kv[1])[:5]}


def cause_of(counts: dict) -> str:
    """Le premier etage ou il ne reste plus rien. Nomme, pas devine."""
    if counts["lignes"] == 0:
        return ("aucune ligne renvoyee : la fenetre, le filtre ou le pool "
                "sont en cause, pas l'extraction")
    if counts["signature a la racine"] == 0:
        return ("la signature n'est PAS a la racine des lignes : "
                "group_by_signature jetait TOUTES les lignes")
    if counts["jambes du mint"] == 0:
        return ("aucune ligne ne porte le mint du token : la cle `mint` ou "
                "l'adresse interrogee est en cause")
    if counts["emetteur == pool"] == 0:
        return ("l'emetteur n'est JAMAIS le pool : le token sort d'un "
                "compte de token du pool, pas du pool lui-meme")
    if counts["signatures avec jambe SOL"] == 0:
        return ("aucune signature ne porte de jambe SOL : la condition "
                "sol > 0 rejetait tous les groupes")
    if counts["destinataire utilisable"] == 0:
        return "tous les destinataires sont exclus par EXCLUDED_PREFIXES"
    if counts["achats, regle du 25/09"] == 0:
        return ("les etages passent un a un mais aucun groupe ne les passe "
                "TOUS : jambe SOL et jambe token ne partagent pas la "
                "signature")
    return "aucune cause identifiee par ces compteurs sur cette page"


# ---------------------------------------------------------------------------
# Un token
# ---------------------------------------------------------------------------


def examine(row: dict) -> dict | None:
    mint, pool = row["mint"], row["pool"]
    graduated = x._grad_at(row)
    config = config_of(mint, graduated)
    print("\n" + "-" * 74)
    print(f"TOKEN {mint}")
    print(f"  jour {row.get('jour')} | pool {pool}")
    print(f"  grad_at {x._iso(graduated)} ({int(graduated)})")
    print(f"  signature de graduation {row.get('signature')}")
    print(f"  signer {row.get('signer')}")
    print(f"  courbe derivee (compte de token connu) {rules.curve_of(mint)}")
    print(f"  config envoyee : {_json(config)}")

    payload = call_transfers(pool, config)
    if payload == "CAPPED":
        log.warning("Plafond de %d appels atteint : token non examine",
                    MAX_CALLS)
        return None
    error = x.error_of(payload)
    if error:
        print(f"  REPONSE EN ERREUR : {error}")
        return {"mint": mint, "erreur": error}
    lines = x.rows_of(payload)
    if lines is None:
        print("  PERTE : payload inattendu, ce n'est PAS une absence de "
              "lignes")
        print(f"  payload : {_json(payload)}")
        return {"mint": mint, "erreur": "payload inattendu"}

    token = x.next_page_token(payload)
    first = rules.line_time(lines[0]) if lines else 0.0
    last = rules.line_time(lines[-1]) if lines else 0.0
    print(f"  {len(lines)} ligne(s) | page suivante : "
          f"{'oui' if token else 'non'}")
    if lines:
        print(f"  premiere {x._iso(first)} | derniere {x._iso(last)}")
        print(f"  dans la fenetre demandee : "
              f"{graduated <= first and last <= graduated + x.SIGNAL_WINDOW_S}")
        print(f"  cles d'une ligne : {sorted(lines[0].keys())}")
        print(f"  ligne 1 entiere : {_json(lines[0])}")

    for index, line in enumerate(lines[:SHOW_LINES], start=1):
        print(f"\n    ligne {index} | {x._iso(rules.line_time(line))}")
        print(f"      emetteur      : {_json(source_fields(line), 300)}")
        print(f"      destinataire  : {_json(target_fields(line), 300)}")
        print(f"      mint          : {line.get('mint')}")
        print(f"      montant       : {rules.amount_of(line)} "
              f"(brut {line.get('amount')}, decimales "
              f"{line.get('decimals')})")
        print(f"      signature     : {rules.extract_signature(line)}")
        print(f"      DECISION      : {decision_of(line, pool, mint)}")

    detail = funnel(lines, pool, mint)
    print("\n    entonnoir de la section 1 (regle du 25/09) :")
    for label, value in detail["compteurs"].items():
        print(f"      {label:<42} {value}")
    print(f"    comptes du pool retenus : {detail['comptes_du_pool']}")
    print(f"    emetteurs les plus frequents des jambes du mint : "
          f"{detail['emetteurs']}")
    cause = cause_of(detail["compteurs"])
    print(f"    CAUSE sur ce token : {cause}")
    dense = len(lines) >= 100 and bool(token)
    print(f"    ce token serait {'TRONQUE' if dense else 'non tronque'} "
          f"au plafond de {x.BUY_MAX_PAGES} pages")
    return {"mint": mint, "pool": pool, "lignes": len(lines),
            "page_suivante": bool(token), "tronque": dense,
            "premiere": x._iso(first), "derniere": x._iso(last),
            "compteurs": detail["compteurs"],
            "comptes_du_pool": detail["comptes_du_pool"],
            "emetteurs": detail["emetteurs"], "cause": cause}


# ---------------------------------------------------------------------------
# Entree
# ---------------------------------------------------------------------------


def main() -> None:
    global _run_at
    setup_logging()
    diagnose_environment()
    helius.api_key()
    _run_at = datetime.now(timezone.utc).isoformat()

    print("\nDiagnostic de la section 1 de l'experience 2")
    print(f"  plafond : {MAX_CALLS} appels Helius "
          f"({MAX_CALLS * 10} credits), aucune ecriture hors "
          f"{RUN_LOG_TABLE}")
    write_correction()

    rows = [r for r in db.fetch_all_grad_paths()
            if (r.get("status") or "mesure") == "mesure"]
    population = [r for r in rows
                  if r.get("jour") in x.IN_SAMPLE_DAYS and x.complete(r)
                  and x.eligible(r, "15 min") and r.get("pool")]
    print(f"  {len(population)} token(s) de la meme population que la "
          f"section 1 (seed {SEED})")
    if not population:
        log.error("Population vide : rien a diagnostiquer.")
        log_run("1", "diagnostic", {"erreur": "population vide"})
        return

    random.Random(SEED).shuffle(population)
    results: list[dict] = []
    dense = 0
    sparse = 0
    # On veut 1 token tronque et 2 non tronques. Le melange ne se connait
    # qu'apres l'appel : on continue a tirer tant qu'il n'y est pas, sous
    # le plafond d'appels.
    for row in population:
        if _calls >= MAX_CALLS:
            break
        if len(results) >= TOKENS_WANTED and dense >= 1 and sparse >= 2:
            break
        detail = examine(row)
        if detail is None:
            break
        results.append(detail)
        if detail.get("tronque"):
            dense += 1
        elif "erreur" not in detail:
            sparse += 1

    print("\n" + "=" * 74)
    print("RECAPITULATIF")
    print("=" * 74)
    print(f"  {len(results)} token(s) examines, {dense} tronque(s), "
          f"{sparse} non tronque(s)")
    if dense < 1 or sparse < 2:
        log.warning("Melange demande non atteint (1 tronque, 2 non "
                    "tronques) sous le plafond de %d appels : %d tronque(s), "
                    "%d non tronque(s)", MAX_CALLS, dense, sparse)
    causes = sorted({r.get("cause", "") for r in results if r.get("cause")})
    for cause in causes:
        print(f"  CAUSE : {cause}")
    if len(causes) > 1:
        log.warning("Plusieurs causes distinctes selon les tokens : la "
                    "correction doit les couvrir toutes.")
    print(f"  appels Helius : {_calls}/{MAX_CALLS} "
          f"({_calls * 10} credits)")
    log_run("1", "diagnostic", {"tokens": results, "causes": causes,
                                "appels": _calls, "credits": _calls * 10,
                                "tronques": dense, "non_tronques": sparse})
    print(f"  diagnostic ecrit dans {RUN_LOG_TABLE} "
          f"({RUN_MODE} / section 1)")


if __name__ == "__main__":
    main()
