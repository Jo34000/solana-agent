"""Second diagnostic : que sont les transferts geants du pool ?

La cause du "0 achat" est etablie : avec le filtre `mint`, la reponse ne
contient QUE des lignes de ce mint. Aucune jambe SOL ne peut donc
partager la signature, et la condition `sol > 0` rejetait tout. Le
premier diagnostic a aussi montre des transferts enormes vers quelques
destinataires : avant de les compter comme des achats, il faut savoir ce
qu'ils sont.

Trois transactions sont lues en entier, et deux comptes sont identifies.
Aucune ecriture hors sol_run_log. Plafond : 6 appels RPC standard.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import exp1_window as exp
import graduations as rules
import helius
import supabase_client as db
from config import RUN_LOG_TABLE, diagnose_environment, setup_logging

log = logging.getLogger("solana-agent")

RUN_MODE = "exp2_diag2"
MAX_CALLS = exp._env_int("DIAG2_CALLS", 6)

# Cout prudent : getTransaction vaut 1 credit en standard et 10 en
# archival. On compte 10, quitte a surestimer.
COST = {"getTransaction": 10, "getAccountInfo": 1}

# Depot de migration pump.fun, en jetons. Sert a exprimer un transfert
# en PART du depot plutot qu'en valeur absolue.
MIGRATION_DEPOSIT = 206_900_000.0

CASES: tuple[tuple[str, str], ...] = (
    ("yuud : transfert geant, destinataire = signataire de la graduation",
     "4prioniocXxgBNTJxSy4eXmwVgbpk4jvK9SGdbnq3axLA9ZDurSHeAusJmLbjr7ZF3sM"
     "gfipJPvvkX2qvKk6XDo7"),
    ("6vSq : transfert geant, autre destinataire",
     "eztTpPqtQiXGGYCdCmVUvvH9d9TajKmvemXx9K84mnanx9PGsBrq9XnAVPtEgaDXBV95"
     "yZhRRxH8xVUWxXDyMtn"),
    ("H22a : achat de taille ordinaire, pour comparer",
     "3kPEFaiqP5uNaBi9MJUcyfEqUjraF6iSZL4Y77iBjywFYzngvvRanmGpXRymZBj6uiTi"
     "C1798PRHrHinq7Vho5kF"),
)

ACCOUNTS: tuple[str, ...] = (
    "27HFmP7ccLadGswvQfvea4o3juLw75cPF4V6jWpHM3MX",
    "8N4QDR8m54PuV2KgHSu39QRHrNooNEK667hBeKVokZoc",
)

_calls = 0
_credits = 0
_run_at = ""


def _json(value: Any, limit: int = 600) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= limit else text[:limit] + " ... [tronque]"


def call(method: str, params: list) -> Any:
    global _calls, _credits
    if _calls >= MAX_CALLS:
        log.warning("Plafond de %d appels atteint", MAX_CALLS)
        return "CAPPED"
    _calls += 1
    _credits += COST.get(method, 1)
    return helius.rpc(method, params)


def result_of(payload: Any, what: str) -> Any:
    """None n'est pas une absence : c'est une perte, et elle se dit."""
    if payload == "CAPPED":
        return None
    if payload is None:
        log.error("PERTE : %s abandonne, aucune reponse", what)
        return None
    if isinstance(payload, dict) and "error" in payload:
        log.error("PERTE : %s rejete : %s", what, str(payload["error"])[:200])
        return None
    if not isinstance(payload, dict):
        log.error("PERTE : %s, payload inattendu", what)
        return None
    return payload.get("result")


# ---------------------------------------------------------------------------
# Etiquetage des comptes a partir de sol_grad_paths
# ---------------------------------------------------------------------------


def labels_of(paths: list[dict], mints: set[str]) -> dict[str, str]:
    """pool, signataire, mint, courbe : ce qu'on sait deja, gratuitement."""
    labels: dict[str, str] = {}
    for row in paths:
        mint = row.get("mint")
        if mint not in mints:
            continue
        labels[mint] = f"MINT du token ({str(mint)[:8]})"
        if row.get("pool"):
            labels[row["pool"]] = "POOL"
        if row.get("signer"):
            labels[row["signer"]] = "SIGNATAIRE DE LA GRADUATION"
        curve = rules.curve_of(mint) if isinstance(mint, str) else None
        if curve:
            labels[curve] = "COURBE (bonding curve)"
    return labels


def account_keys(transaction: dict) -> list[dict]:
    message = (transaction.get("transaction") or {}).get("message") or {}
    keys = message.get("accountKeys") or []
    out = []
    for index, key in enumerate(keys):
        if isinstance(key, dict):
            out.append({"index": index, "pubkey": key.get("pubkey"),
                        "signer": bool(key.get("signer")),
                        "writable": bool(key.get("writable"))})
        else:
            out.append({"index": index, "pubkey": key, "signer": False,
                        "writable": False})
    loaded = (transaction.get("meta") or {}).get("loadedAddresses") or {}
    for kind in ("writable", "readonly"):
        for key in loaded.get(kind) or []:
            out.append({"index": len(out), "pubkey": key, "signer": False,
                        "writable": kind == "writable"})
    return out


def instructions_of(transaction: dict) -> tuple[list[str], list[str]]:
    """(programmes invoques, noms d'instructions quand le parsing les donne)."""
    message = (transaction.get("transaction") or {}).get("message") or {}
    meta = transaction.get("meta") or {}
    blocks = list(message.get("instructions") or [])
    for inner in meta.get("innerInstructions") or []:
        blocks.extend(inner.get("instructions") or [])
    programs: list[str] = []
    names: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        program = block.get("programId") or block.get("program")
        if program and program not in programs:
            programs.append(str(program))
        parsed = block.get("parsed")
        if isinstance(parsed, dict) and parsed.get("type"):
            names.append(f"{block.get('program') or program}."
                         f"{parsed['type']}")
        elif isinstance(parsed, str):
            names.append(parsed)
    return programs, names


def sol_deltas(transaction: dict, keys: list[dict]) -> list[tuple[str, float]]:
    meta = transaction.get("meta") or {}
    before = meta.get("preBalances") or []
    after = meta.get("postBalances") or []
    out = []
    for key in keys:
        index = key["index"]
        if index >= len(before) or index >= len(after):
            continue
        delta = (rules.to_float(after[index]) - rules.to_float(before[index]))
        if delta:
            out.append((str(key["pubkey"]),
                        delta / rules.LAMPORTS_PER_SOL))
    return out


def token_deltas(transaction: dict) -> list[tuple[str, str, float]]:
    """(proprietaire, mint, variation) depuis pre/postTokenBalances."""
    meta = transaction.get("meta") or {}
    before: dict[int, tuple[str, str, float]] = {}
    for entry in meta.get("preTokenBalances") or []:
        before[entry.get("accountIndex")] = (
            entry.get("owner") or "", entry.get("mint") or "",
            rules.to_float((entry.get("uiTokenAmount") or {}).get(
                "uiAmountString")))
    out = []
    seen = set()
    for entry in meta.get("postTokenBalances") or []:
        index = entry.get("accountIndex")
        seen.add(index)
        owner = entry.get("owner") or ""
        mint = entry.get("mint") or ""
        after = rules.to_float((entry.get("uiTokenAmount") or {}).get(
            "uiAmountString"))
        previous = before.get(index)
        delta = after - (previous[2] if previous else 0.0)
        if delta:
            out.append((owner, mint, delta))
    for index, (owner, mint, amount) in before.items():
        if index not in seen and amount:
            out.append((owner, mint, -amount))
    return out


# ---------------------------------------------------------------------------
# Une transaction
# ---------------------------------------------------------------------------


def examine(label: str, signature: str, paths: list[dict]) -> dict:
    print("\n" + "-" * 74)
    print(f"{label}")
    print(f"  signature {signature}")
    payload = call("getTransaction",
                   [signature, {"encoding": "jsonParsed",
                                "maxSupportedTransactionVersion": 0}])
    transaction = result_of(payload, f"getTransaction {signature[:12]}")
    if not isinstance(transaction, dict):
        print("  PERTE ou transaction introuvable : rien n'est conclu ici")
        return {"signature": signature, "erreur": "sans reponse"}

    keys = account_keys(transaction)
    signers = [k["pubkey"] for k in keys if k["signer"]]
    programs, names = instructions_of(transaction)
    tokens = token_deltas(transaction)
    mints = {mint for _, mint, _ in tokens if mint}
    marks = labels_of(paths, mints)

    def mark(address: str) -> str:
        return f"  [{marks[address]}]" if address in marks else ""

    lamports = sol_deltas(transaction, keys)
    when = rules.to_float(transaction.get("blockTime"))
    print(f"  bloc {transaction.get('slot')} | {_iso(when)}")
    print(f"  signataires : {[s + mark(str(s)) for s in signers]}")
    print(f"  programmes invoques ({len(programs)}) :")
    for program in programs:
        print(f"    {program}{mark(program)}")
    print(f"  instructions nommees : {names if names else 'aucune parsee'}")

    print("  variations de SOL :")
    for address, delta in lamports:
        print(f"    {address:<45}{delta:+18.9f} SOL{mark(address)}")
    print("  variations de tokens (proprietaire, mint, variation) :")
    for owner, mint, delta in tokens:
        share = delta / MIGRATION_DEPOSIT
        print(f"    {owner:<45}{delta:+20.6f}  part du depot "
              f"{share:+.1%}  mint {str(mint)[:8]}..{mark(owner)}")
    return {"signature": signature, "label": label,
            "slot": transaction.get("slot"), "blockTime": when,
            "signataires": signers, "programmes": programs,
            "instructions": names,
            "sol": [[a, d] for a, d in lamports],
            "tokens": [[o, m, d] for o, m, d in tokens],
            "etiquettes": marks}


def _iso(moment: float) -> str:
    try:
        return datetime.fromtimestamp(moment, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return repr(moment)


def identify(address: str) -> dict:
    print(f"\n  compte {address}")
    payload = call("getAccountInfo", [address, {"encoding": "jsonParsed"}])
    result = result_of(payload, f"getAccountInfo {address[:12]}")
    value = (result or {}).get("value") if isinstance(result, dict) else None
    if not isinstance(value, dict):
        print("    PERTE ou compte inexistant : rien n'est conclu")
        return {"compte": address, "erreur": "sans reponse"}
    owner = value.get("owner")
    data = value.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    info = (parsed or {}).get("info") if isinstance(parsed, dict) else None
    print(f"    proprietaire (programme) : {owner}")
    print(f"    executable {value.get('executable')} | lamports "
          f"{value.get('lamports')} | espace "
          f"{data.get('space') if isinstance(data, dict) else '?'}")
    if isinstance(info, dict):
        print(f"    type : {(parsed or {}).get('type')}")
        print(f"    info : {_json(info)}")
    else:
        print("    aucune donnee parsee : ce n'est pas un compte de token "
              "standard")
    return {"compte": address, "owner": owner,
            "executable": value.get("executable"),
            "type": (parsed or {}).get("type") if parsed else None,
            "info": info if isinstance(info, dict) else None}


def main() -> None:
    global _run_at
    setup_logging()
    diagnose_environment()
    helius.api_key()
    _run_at = datetime.now(timezone.utc).isoformat()

    print("\nSecond diagnostic : les transferts geants du pool")
    print(f"  plafond : {MAX_CALLS} appels RPC standard, aucune ecriture "
          f"hors {RUN_LOG_TABLE}")
    try:
        paths = db.fetch_all_grad_paths()
    except Exception as error:               # noqa: BLE001
        log.error("PERTE : sol_grad_paths illisible (%s) : les comptes ne "
                  "seront pas etiquetes", error)
        paths = []

    transactions = [examine(label, signature, paths)
                    for label, signature in CASES]

    print("\n" + "-" * 74)
    print("COMPTES A IDENTIFIER")
    accounts = [identify(address) for address in ACCOUNTS]

    print("\n" + "=" * 74)
    print("RECAPITULATIF")
    print("=" * 74)
    print(f"  {len(transactions)} transaction(s) lue(s), "
          f"{len(accounts)} compte(s) identifie(s)")
    print(f"  appels : {_calls}/{MAX_CALLS} | credits (comptes en "
          f"archival) : {_credits}")
    payload = {"transactions": transactions, "comptes": accounts,
               "appels": _calls, "credits": _credits,
               "depot_migration": MIGRATION_DEPOSIT}
    try:
        db.insert_run_log(RUN_MODE, _run_at, "1", "transactions", payload)
        print(f"  ecrit dans {RUN_LOG_TABLE} ({RUN_MODE} / section 1)")
    except Exception as error:               # noqa: BLE001
        log.error("PERTE : diagnostic non ecrit dans %s : %s", RUN_LOG_TABLE,
                  error)
        raise
    print("\nSTOP : aucune collecte n'est lancee dans ce passage.")


if __name__ == "__main__":
    main()
