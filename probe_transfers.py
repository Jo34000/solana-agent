"""Sonde jetable : que vaut getTransfersByAddress ?

Script d'observation, jamais appele par le pipeline. Lance via
RUN_MODE=probe_transfers (voir main.py) ou directement.

Motif : le run de validation du 18/09 a consomme 396 480 credits sur le
million mensuel, pour 60 wallets sur 117. getTransactionsForAddress coute
100 credits par appel et le run en a fait 2384. getTransfersByAddress est
annoncee a 10 credits et concue pour l'historique de transferts — elle n'a
jamais ete testee ici.

Deux wallets DEJA BACKTESTES, pour pouvoir comparer aux resultats connus.

Acquis du run du 19/09 06:26 :
  - result = {data, paginationToken}, meme forme que
    getTransactionsForAddress ;
  - sortOrder asc et desc acceptes ;
  - startTime / endTime REJETES (-32602), et toute cle inconnue fait
    rejeter l'objet de config ENTIER. On n'envoie donc que des cles
    connues ;
  - aucun champ de montant SOL dans une ligne : une ligne = une jambe de
    transfert d'un mint ;
  - sortOrder=asc sur 7ioEZjdG demarre ~36 jours avant maintenant, deja
    dans la fenetre mature 10-45 j.

Trois questions restent ouvertes, traitees ci-dessous : le plafond de
limit, la presence de plusieurs jambes par signature, et l'anciennete des
candidats.

La sonde DECRIT, elle n'interprete pas : aucun parsing metier, aucune
ecriture en base. Les messages d'erreur bruts de Helius sont affiches tels
quels — ce sont eux qui donneront les noms exacts de parametres.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import helius
import supabase_client as db
from config import SMART_WALLETS_TABLE, diagnose_environment, setup_logging

log = logging.getLogger("solana-agent")

RPC_METHOD = "getTransfersByAddress"
TX_LIMIT = 20
JSON_TRUNCATE = 4000

# Premier wallet : adresse complete connue. Second : seul le prefixe l'est,
# l'adresse complete est reprise depuis sol_smart_wallets au demarrage.
WALLET_FULL = "7ioEZjdGdciB2jS79Rc8yqrsYanf2Q1YejHjNcQZLkER"
WALLET_PREFIX = "EYSBahi9"

# Options tentees pour le tri et le filtre temporel. Aucun de ces noms n'est
# certain : un rejet est une information, le message brut donnera le nom
# correct.
VARIANTS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("sortOrder=asc", {"sortOrder": "asc"}),
    ("sortOrder=desc", {"sortOrder": "desc"}),
    ("startTime / endTime", {"startTime": 0, "endTime": 2_000_000_000}),
)

# Cles candidates, cherchees n'importe ou dans un element.
FIELD_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("mint", ("mint", "tokenMint", "token_mint", "mintAddress")),
    ("direction / type", ("direction", "type", "transferType", "kind")),
    ("montant token", ("tokenAmount", "amount", "uiAmount", "rawTokenAmount")),
    ("montant SOL", ("lamports", "nativeAmount", "solAmount", "fee")),
    ("source", ("fromUserAccount", "source", "from", "fromAddress", "sender")),
    ("destination", ("toUserAccount", "destination", "to", "toAddress",
                     "receiver")),
    ("timestamp", ("timestamp", "blockTime", "block_time", "time")),
    ("slot", ("slot", "blockSlot")),
)

# Valeurs de limit a sonder : c'est ce plafond qui fixe le nombre de pages
# par wallet, donc le budget.
LIMIT_PROBES = (100, 500, 1000, 2000)

# Limite minimale pour le profil d'anciennete : un seul element suffit,
# seule sa date compte. Repli si la valeur est rejetee.
MIN_LIMIT = 1
FALLBACK_LIMIT = 100

WSOL_MINT = "So11111111111111111111111111111111111111112"

# Tranches d'anciennete, en jours.
AGE_BUCKETS = ((0, 10), (10, 45), (45, 90), (90, float("inf")))

LIST_KEYS = ("data", "items", "transfers", "result", "value")
TOKEN_KEYS = ("paginationToken", "nextToken", "cursor", "before", "after")


def _truncate(text: str, limit: int = JSON_TRUNCATE) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [tronque a {limit} caracteres]"


def _as_json(value: Any) -> str:
    try:
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(value)


def _masked(text: str) -> str:
    key = os.environ.get("HELIUS_API_KEY", "").strip()
    return text.replace(key, "***") if key else text


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _readable(value: Any) -> str:
    if isinstance(value, (int, float)) and 1_000_000_000 < value < 20_000_000_000:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    return repr(value)


def _short(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else _as_json(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _walk(value: Any, names: tuple[str, ...], path: str = "",
          found: list[tuple[str, Any]] | None = None,
          depth: int = 0) -> list[tuple[str, Any]]:
    if found is None:
        found = []
    if depth > 6 or len(found) > 20:
        return found
    if isinstance(value, dict):
        for key, sub in value.items():
            here = f"{path}.{key}" if path else key
            if key in names:
                found.append((here, sub))
            _walk(sub, names, here, found, depth + 1)
    elif isinstance(value, list):
        for index, sub in enumerate(value[:2]):
            _walk(sub, names, f"{path}[{index}]", found, depth + 1)
    return found


def _extract_list(result: Any) -> tuple[list | None, str]:
    if isinstance(result, list):
        return result, "result"
    if isinstance(result, dict):
        for key in LIST_KEYS:
            value = result.get(key)
            if isinstance(value, list):
                return value, f"result.{key}"
    return None, ""


def _order_verdict(items: list, field: str) -> str:
    values = [
        item.get(field) for item in items
        if isinstance(item, dict) and isinstance(item.get(field), (int, float))
    ]
    if len(values) < 2:
        return f"{field} : moins de 2 valeurs, ordre indeterminable"
    ascending = all(a <= b for a, b in zip(values, values[1:]))
    descending = all(a >= b for a, b in zip(values, values[1:]))
    if ascending and not descending:
        verdict = "ASCENDANT"
    elif descending and not ascending:
        verdict = "DESCENDANT"
    elif ascending and descending:
        verdict = "constant"
    else:
        verdict = "NON MONOTONE"
    return f"{field} : {verdict} ({values[0]} -> {values[-1]})"


def resolve_second_wallet() -> str | None:
    """Adresse complete du second wallet, depuis sol_smart_wallets."""
    try:
        response = (
            db.get_client()
            .table(SMART_WALLETS_TABLE)
            .select("wallet")
            .like("wallet", f"{WALLET_PREFIX}%")
            .limit(2)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 - on veut le message brut
        print(f"  lecture Supabase impossible : {type(exc).__name__} {exc}")
        return None
    rows = response.data or []
    if not rows:
        print(f"  aucun wallet commencant par '{WALLET_PREFIX}' en base")
        return None
    if len(rows) > 1:
        print(f"  ATTENTION : {len(rows)} wallets commencent par "
              f"'{WALLET_PREFIX}', le premier est retenu")
    return rows[0]["wallet"]


def _call(address: str, config: dict[str, Any]) -> tuple[Any, str]:
    """(payload, corps de requete envoye)."""
    body = {
        "jsonrpc": "2.0",
        "id": "sonde",
        "method": RPC_METHOD,
        "params": [address, config],
    }
    sent = _masked(_as_json(body))
    payload = helius.rpc(RPC_METHOD, [address, config])
    return payload, sent


def probe_wallet(address: str) -> None:
    print("=" * 72)
    print(f"{RPC_METHOD} sur {address}")
    print("=" * 72)

    payload, sent = _call(address, {"limit": TX_LIMIT})
    print(f"corps de requete envoye :\n{_truncate(sent, 800)}")

    if payload is None:
        print("PERTE : pas de reponse exploitable (voir les logs ci-dessus)")
        return
    if not isinstance(payload, dict) or "error" in payload:
        print("REJET, message brut renvoye par Helius :")
        print(_truncate(_as_json(payload), 1500))
        return

    print("code HTTP : 200")
    result = payload.get("result")
    print(f"cles de result : "
          f"{sorted(result.keys()) if isinstance(result, dict) else type(result).__name__}")

    items, path = _extract_list(result)
    if items is None:
        print("aucune liste trouvee dans result. Payload brut :")
        print(_truncate(_as_json(payload)))
        return
    print(f"liste trouvee en : {path}")
    print(f"elements retournes : {len(items)}")

    if isinstance(result, dict):
        tokens = [(k, result[k]) for k in TOKEN_KEYS if result.get(k)]
        print(f"token de pagination : "
              f"{tokens if tokens else 'aucun parmi ' + str(list(TOKEN_KEYS))}")

    if not items:
        return

    print("\n--- structure COMPLETE du premier element ---")
    print(_truncate(_as_json(items[0])))

    print("\n--- champs reperes ---")
    for label, names in FIELD_GROUPS:
        matches = _walk(items[0], names)
        if not matches:
            print(f"  {label:18} : aucun champ de nom {list(names)}")
            continue
        for key, value in matches[:3]:
            print(f"  {label:18} : {key} = {_short(value)}")

    print("\n--- ordre et amplitude ---")
    for field in ("timestamp", "blockTime", "slot"):
        if any(isinstance(i, dict) and field in i for i in items):
            print(f"  {_order_verdict(items, field)}")
    stamps = [
        _to_float(item.get("timestamp") or item.get("blockTime"))
        for item in items if isinstance(item, dict)
    ]
    stamps = [s for s in stamps if s > 0]
    if stamps:
        now = datetime.now(timezone.utc).timestamp()
        print(f"  plus ancien : {_readable(min(stamps))} "
              f"({(now - min(stamps)) / 86400:.2f} j)")
        print(f"  plus recent : {_readable(max(stamps))} "
              f"({(now - max(stamps)) / 86400:.2f} j)")
        print(f"  amplitude   : {(max(stamps) - min(stamps)) / 86400:.2f} j")


def probe_variants(address: str) -> None:
    """Tri et filtre temporel : acceptes ou rejetes ?"""
    print("\n" + "=" * 72)
    print("Tri et filtre temporel (sur le premier wallet seulement)")
    print("Aucun de ces noms n'est certain : un rejet donne le nom correct.")
    print("=" * 72)

    for label, extra in VARIANTS:
        config = {"limit": TX_LIMIT, **extra}
        payload, _ = _call(address, config)
        print(f"\n> {label}")
        print(f"  config envoyee : {json.dumps(config)}")
        if payload is None:
            print("  PERTE, non concluant")
            continue
        if not isinstance(payload, dict) or "error" in payload:
            print("  REJET, message brut :")
            print("    " + _truncate(_as_json(payload), 700).replace("\n", "\n    "))
            continue
        items, path = _extract_list(payload.get("result"))
        if not items:
            print(f"  accepte, mais aucun element ({path})")
            continue
        print(f"  ACCEPTE : {len(items)} elements en {path}")
        for field in ("timestamp", "blockTime", "slot"):
            if any(isinstance(i, dict) and field in i for i in items):
                print(f"    {_order_verdict(items, field)}")


def probe_limits(address: str) -> None:
    """OBJECTIF 1 : quel est le plafond de limit ?

    C'est ce chiffre qui fixe le nombre de pages par wallet, donc le
    budget. Sans lui aucune estimation n'est possible.
    """
    print("\n" + "=" * 72)
    print("Plafond de limit")
    print("=" * 72)

    for limit in LIMIT_PROBES:
        payload, _ = _call(address, {"limit": limit})
        print(f"\n> limit = {limit}")
        if payload is None:
            print("  PERTE, non concluant")
            continue
        if not isinstance(payload, dict) or "error" in payload:
            print("  REJET, message brut complet :")
            print("    " + _as_json(payload).replace("\n", "\n    "))
            continue
        items, path = _extract_list(payload.get("result"))
        if items is None:
            print("  accepte, mais aucune liste dans result")
            continue
        print(f"  ACCEPTE : {len(items)} elements renvoyes en {path}")
        if len(items) < limit:
            print(f"  -> moins d'elements que demande : soit le plafond reel "
                  f"est {len(items)}, soit le wallet n'en a pas plus")


def probe_signature_legs(address: str) -> None:
    """OBJECTIF 2 : plusieurs jambes par signature ?

    Si getTransfersByAddress expose la jambe SOL/wSOL de l'achat sous forme
    d'une ligne distincte partageant la meme signature, le prix d'entree se
    calcule sans voie C. Sinon la voie C ciblee reste necessaire.
    """
    print("\n" + "=" * 72)
    print("Jambes par signature : getTransfersByAddress vs voie C")
    print("=" * 72)

    payload, _ = _call(address, {"limit": 100})
    if payload is None or not isinstance(payload, dict) or "error" in payload:
        print("appel de reference indisponible, section sautee")
        if isinstance(payload, dict):
            print(_truncate(_as_json(payload), 700))
        return
    items, _ = _extract_list(payload.get("result"))
    if not items:
        print("aucun element, section sautee")
        return

    # On prend la signature la PLUS REPRESENTEE : c'est elle qui repond le
    # mieux a la question.
    counts: dict[str, int] = {}
    for item in items:
        signature = item.get("signature") if isinstance(item, dict) else None
        if isinstance(signature, str):
            counts[signature] = counts.get(signature, 0) + 1
    if not counts:
        print("aucun champ 'signature' dans les elements, section sautee")
        return

    signature, occurrences = max(counts.items(), key=lambda kv: kv[1])
    uniques = sum(1 for n in counts.values() if n == 1)
    print(f"{len(items)} lignes pour {len(counts)} signatures distinctes")
    print(f"  signatures a une seule ligne : {uniques}")
    print(f"  maximum de lignes pour une signature : {occurrences}")
    print(f"\nsignature examinee : {signature} ({occurrences} ligne(s))")

    print("\n--- lignes getTransfersByAddress portant cette signature ---")
    legs = [i for i in items if isinstance(i, dict) and i.get("signature") == signature]
    for index, leg in enumerate(legs, start=1):
        mint = leg.get("mint")
        print(f"  ligne {index} : mint={mint} "
              f"{'(wSOL)' if mint == WSOL_MINT else ''}")
        for key in ("tokenAmount", "amount", "uiAmount", "direction", "type",
                    "fromUserAccount", "toUserAccount"):
            if key in leg:
                print(f"      {key:18} = {_short(leg[key])}")

    print("\n--- voie C (POST /v0/transactions) sur cette seule signature ---")
    enriched = helius.enrich_signatures([signature])
    if enriched is None:
        print("  PERTE sur la voie C")
        return
    if not enriched:
        print("  aucune transaction renvoyee")
        return
    transaction = enriched[0]
    for field in ("tokenTransfers", "nativeTransfers"):
        value = transaction.get(field)
        print(f"  {field} : "
              f"{len(value) if isinstance(value, list) else type(value).__name__}")
        print("    " + _truncate(_as_json(value), 1500).replace("\n", "\n    "))

    mints_a = {l.get("mint") for l in legs}
    print(f"\n  mints vus cote getTransfersByAddress : {sorted(m for m in mints_a if m)}")
    print(f"  wSOL present cote getTransfersByAddress : "
          f"{'OUI' if WSOL_MINT in mints_a else 'NON'}")


def _fetch_candidates(min_winners: int = 2) -> list[str]:
    """Wallets de sol_smart_wallets avec assez de winners. Lecture seule."""
    try:
        response = (
            db.get_client()
            .table(SMART_WALLETS_TABLE)
            .select("wallet")
            .gte("winners_count", min_winners)
            .limit(5000)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 - on veut le message brut
        print(f"  lecture Supabase impossible : {type(exc).__name__} {exc}")
        return []
    return [
        row["wallet"] for row in (response.data or [])
        if isinstance(row.get("wallet"), str)
    ]


def probe_candidate_ages() -> None:
    """OBJECTIF 3 : a quelle anciennete demarre l'historique indexe ?

    Un appel ascendant par candidat, limite minimale, sans pagination. Si la
    majorite tombe dans 10-45 j, la collecte ascendante atteint la fenetre
    mature des la premiere page et le budget s'effondre.
    """
    print("\n" + "=" * 72)
    print("Profil d'anciennete des candidats (winners_count >= 2)")
    print("=" * 72)

    wallets = _fetch_candidates()
    if not wallets:
        print("aucun candidat, section sautee")
        return
    print(f"{len(wallets)} candidats, 1 appel chacun en sortOrder=asc")

    limit = MIN_LIMIT
    now = datetime.now(timezone.utc).timestamp()
    buckets = {label: 0 for label in ("<10j", "10-45j", "45-90j", ">90j")}
    unreadable = 0
    lost = 0
    ages: list[float] = []

    for index, wallet in enumerate(wallets):
        payload, _ = _call(wallet, {"limit": limit, "sortOrder": "asc"})

        # Si la limite minimale est rejetee, on le dit une fois et on
        # bascule sur une valeur sure plutot que de bruler tous les appels.
        if (index == 0 and isinstance(payload, dict) and "error" in payload
                and limit != FALLBACK_LIMIT):
            print(f"  limit={limit} rejete, message brut :")
            print("    " + _truncate(_as_json(payload), 500).replace("\n", "\n    "))
            limit = FALLBACK_LIMIT
            print(f"  -> bascule sur limit={limit} pour les {len(wallets)} appels")
            payload, _ = _call(wallet, {"limit": limit, "sortOrder": "asc"})

        if payload is None:
            lost += 1
            continue
        if not isinstance(payload, dict) or "error" in payload:
            unreadable += 1
            continue
        items, _ = _extract_list(payload.get("result"))
        if not items:
            unreadable += 1
            continue

        stamps = [
            _to_float(i.get("timestamp") or i.get("blockTime"))
            for i in items if isinstance(i, dict)
        ]
        stamps = [s for s in stamps if s > 0]
        if not stamps:
            unreadable += 1
            continue

        age = (now - min(stamps)) / 86400
        ages.append(age)
        for (low, high), label in zip(AGE_BUCKETS, buckets):
            if low <= age < high:
                buckets[label] += 1
                break

    print(f"\nplus ancienne activite indexee, {len(ages)} wallets mesures :")
    total = len(ages) or 1
    for label, count in buckets.items():
        share = 100 * count / total
        print(f"  {label:8} : {count:4d}  ({share:5.1f}%)  "
              f"{'#' * int(share / 2)}")
    if unreadable or lost:
        print(f"  non exploitables : {unreadable} | PERTE : {lost}")
    if ages:
        ordered = sorted(ages)
        median = ordered[len(ordered) // 2]
        print(f"  min {min(ages):.1f} j | mediane {median:.1f} j | "
              f"max {max(ages):.1f} j")


def main() -> None:
    setup_logging()
    diagnose_environment()
    present = bool(os.environ.get("HELIUS_API_KEY", "").strip())
    log.info("  %-18s : %s", "HELIUS_API_KEY", "presente" if present else "ABSENTE")
    helius.api_key()

    print(f"\nSonde {RPC_METHOD} : 2 wallets deja backtestes, aucune ecriture "
          f"en base.")
    print("Aucune conclusion tiree ici : la sonde decrit ce qu'elle recoit.\n")

    print(f"Second wallet : resolution du prefixe '{WALLET_PREFIX}'...")
    second = resolve_second_wallet()
    print(f"  -> {second or 'non resolu, ce wallet sera saute'}\n")

    probe_wallet(WALLET_FULL)
    probe_variants(WALLET_FULL)
    if second:
        print()
        probe_wallet(second)

    probe_limits(WALLET_FULL)
    probe_signature_legs(WALLET_FULL)
    probe_candidate_ages()

    calls, losses = helius.request_stats()
    print("\n" + "=" * 72)
    print(f"Appels Helius effectues : {calls} (pertes {losses})")
    print("A recouper avec le dashboard Helius pour connaitre le cout reel "
          "par appel.")


if __name__ == "__main__":
    main()
