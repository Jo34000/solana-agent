"""Sonde jetable v2 : refaire les trois mesures ratees du 20/09.

Script d'observation, jamais appele par le pipeline. Lance via
RUN_MODE=probe_universe_v2 ou directement. Aucune ecriture en base,
aucune conclusion de trading.

Le run du 20/09 13:25 a valide UNE chose : le prix a un instant donne se
reconstruit a 1,11 appel par point obtenu. Trois sections n'ont pas mesure
ce qu'elles devaient, et c'est tout l'objet de cette sonde :

  - la syntaxe de deux filtres etait fausse (filters.status, solMode) ;
  - la bonding curve etait DEVINEE par heuristique, d'ou une section 6
    qui lisait 0 a 1 transaction par token ;
  - le compte propre aux migrations n'a jamais ete isole, donc l'univers
    des gradues n'a jamais ete liste, donc l'echantillon "aleatoire" ne
    l'etait pas.

Contrainte permanente : offres gratuites. Helius free tier (1M
credits/mois), CoinGecko Demo (cle PARTAGEE avec l'agent ETH, donc appels
minimises), Supabase gratuit.

Ecarts releves AVANT ecriture, et ce qui a ete decide :

  1. Le cout Enhanced est desormais renseigne : 100 credits par appel
     (documentation Helius, "Credit cost: 100 credits per call", valable
     pour POST /v0/transactions comme pour /v0/addresses/{a}/transactions).
     Le 20/09 affichait 0, ce qui sous-estimait la facture de la section 4.
  2. Les "comptes recurrents du 20/09" ne sont nulle part : la sonde
     n'ecrit rien en base et ses sorties ne sont pas persistees. La
     section C les RECALCULE (memes trois pools, un seul appel Enhanced
     pour les trois signatures au lieu de trois) avant d'appliquer les
     exclusions demandees.
  3. Les deux journees de la section C sont paginees avec
     getTransfersByAddress et non getTransactionsForAddress : 10 credits
     par appel au lieu de 100, et surtout les lignes portent le MINT, dont
     la section D a besoin pour tirer son echantillon. Le plafond de 40
     getTransactionsForAddress est reserve au criblage des comptes.
  4. La section D a besoin du prix du SOL HORAIRE : le pipeline n'a que
     des bougies journalieres (wallet_validation_v2). La sonde charge donc
     ses propres bougies horaires, en 2 appels CoinGecko, et ne touche pas
     a v2.
  5. "baton" n'est identifiable que par son symbole : la sonde le cherche
     dans l'echantillon et, s'il en est absent, le dit au lieu de faire
     semblant. Le diagnostic "pourquoi aucun prix" est de toute facon
     applique a TOUT token sans prix, baton compris s'il est present.
"""

from __future__ import annotations

import json
import logging
import os
import random
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

import geckoterminal as gt
import helius
import solana_addr
import supabase_client as db
from config import (
    ANALYZED_TABLE,
    SOL_MINTS,
    diagnose_environment,
    setup_logging,
)

log = logging.getLogger("solana-agent")

# --- Programmes -----------------------------------------------------------
PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PREFIX = "pAMMBay6"
PUMPFUN_PREFIX = "6EF8rrec"

# Comptes a exclure du criblage de la section C : programmes et mints, qui
# sont presents dans toutes les transactions et ne distinguent rien.
EXCLUDED_PREFIXES = (
    "6EF8rrec",    # pump.fun
    "pAMMBay6",    # PumpSwap AMM
    "Tokenz",      # Token-2022
    "Tokenkeg",    # SPL Token
    "ATokenGP",    # Associated Token Account program
    "ComputeB",    # ComputeBudget
    "SysvarRe",    # sysvars
    "1111",        # System program et derives
    "So1111",      # SOL / WSOL
)

# --- Plafonds -------------------------------------------------------------
# Imposes par le ticket, puis repartis par section. Une section qui atteint
# son plafond s'arrete avec un warning et la sonde passe a la suivante.
CAPS_GLOBAL = {
    "getTransfersByAddress": 1000,
    "getTransactionsForAddress": 40,
    "enhanced": 5,
    "coingecko": 10,
}
CAPS_SECTION = {
    "A": {"getTransfersByAddress": 6, "getTransactionsForAddress": 4},
    "B": {"getTransfersByAddress": 400},
    "C": {"getTransfersByAddress": 215, "getTransactionsForAddress": 30,
          "enhanced": 4},
    "D": {"getTransfersByAddress": 365, "coingecko": 6},
}

# Couts unitaires. Enhanced n'est plus a 0 : documentation Helius, 100
# credits par appel. getTransfersByAddress et getTransactionsForAddress
# restent a recouper avec le dashboard.
CREDIT_COST = {
    "getTransfersByAddress": 10,
    "getTransactionsForAddress": 100,
    "enhanced": 100,     # doc Helius : "Credit cost: 100 credits per call"
    "getTokenSupply": 1,
    "coingecko": 0,      # quota separe, pas des credits Helius
}

MONTHLY_CREDITS = 1_000_000

TOKENS_SECTION_B = 10
SAMPLE_SIZE = 30
CURVE_MAX_PAGES = 50        # plafond demande par le ticket
DAY_MAX_PAGES = 100
SCREEN_MAX_ACCOUNTS = 12
RANDOM_SEED = 20260921

# Journees paginees par la section C (UTC).
DAY_A = "2026-09-17"
DAY_B = "2026-09-10"

# Points de trajectoire apres graduation, en secondes. Le point 0 sert de
# reference a la regle de mort ( < 30 % a 24 h ) et n'est pas un horizon.
REFERENCE_POINT = ("graduation", 0)
TRAJECTORY_POINTS = (
    ("5 min", 300), ("15 min", 900), ("30 min", 1800), ("1 h", 3600),
    ("3 h", 10800), ("6 h", 21600), ("24 h", 86400), ("7 j", 604800),
)
DEAD_RATIO = 0.30
SUSPECT_DURATION_S = 60     # creation -> graduation en moins d'une minute

_calls: Counter = Counter()
_section_calls: Counter = Counter()
_current_section = "?"
_capped: set[tuple[str, str]] = set()


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def start_section(letter: str, title: str) -> None:
    global _current_section, _section_calls
    _current_section = letter
    _section_calls = Counter()
    print("\n" + "=" * 74)
    print(f"SECTION {letter} - {title}")
    print("=" * 74)


def can_spend(method: str) -> bool:
    """Reste-t-il du budget pour un appel ? Plafond atteint -> warning."""
    if _calls[method] >= CAPS_GLOBAL.get(method, 10**9):
        key = ("*", method)
        if key not in _capped:
            _capped.add(key)
            log.warning("PLAFOND GLOBAL atteint : %s (%d appels), sections "
                        "suivantes amputees", method, CAPS_GLOBAL[method])
        return False
    limit = CAPS_SECTION.get(_current_section, {}).get(method)
    if limit is not None and _section_calls[method] >= limit:
        key = (_current_section, method)
        if key not in _capped:
            _capped.add(key)
            log.warning("PLAFOND de section %s atteint : %s (%d appels), "
                        "section interrompue", _current_section, method, limit)
        return False
    return True


def _spend(method: str) -> None:
    _calls[method] += 1
    _section_calls[method] += 1


def _masked(text: str) -> str:
    key = os.environ.get("HELIUS_API_KEY", "").strip()
    return text.replace(key, "***") if key else text


def _as_json(value: Any) -> str:
    try:
        return _masked(json.dumps(value, indent=2, ensure_ascii=False,
                                  default=str))
    except (TypeError, ValueError):
        return _masked(repr(value))


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _amount(line: dict) -> float:
    for key in ("uiAmount", "tokenAmount", "amount"):
        if key in line:
            value = _to_float(line.get(key))
            if value:
                return value
    return 0.0


def _line_time(line: dict) -> float:
    return _to_float(line.get("timestamp") or line.get("blockTime"))


def _iso(moment: float) -> str:
    try:
        return datetime.fromtimestamp(moment, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return repr(moment)


def _day_bounds(day: str) -> tuple[float, float]:
    """(debut, fin) d'une journee UTC donnee en AAAA-MM-JJ."""
    start = datetime.fromisoformat(day + "T00:00:00+00:00").timestamp()
    return start, start + 86400


# ---------------------------------------------------------------------------
# Appels comptabilises
# ---------------------------------------------------------------------------


def transfers(address: str, config: dict) -> Any | None:
    """getTransfersByAddress brut, payload tel quel (erreur comprise)."""
    if not can_spend("getTransfersByAddress"):
        return "CAPPED"
    _spend("getTransfersByAddress")
    return helius.rpc("getTransfersByAddress", [address, config])


def transactions(address: str, config: dict) -> Any | None:
    if not can_spend("getTransactionsForAddress"):
        return "CAPPED"
    _spend("getTransactionsForAddress")
    return helius.rpc("getTransactionsForAddress", [address, config])


def enhanced(signatures: list[str]) -> list[dict] | None:
    if not can_spend("enhanced"):
        return None
    _spend("enhanced")
    return helius.enrich_signatures(signatures)


def token_supply(mint: str) -> dict | None:
    _spend("getTokenSupply")
    return helius.get_token_supply(mint)


def gecko(call, *args, **kwargs):
    if not can_spend("coingecko"):
        return None
    _spend("coingecko")
    return call(*args, **kwargs)


def rows_of(payload: Any) -> list[dict] | None:
    """Liste de result, quelle que soit sa forme. None si erreur/absente."""
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


def next_page_token(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    return helius.pagination_token(result)


def group_by_signature(lines: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for line in lines:
        if isinstance(line, dict) and isinstance(line.get("signature"), str):
            groups[line["signature"]].append(line)
    return groups


def swap_price(lines: list[dict]) -> tuple[float, str] | None:
    """Prix d'un swap : jambe SOL / jambe token. (prix en SOL, mint).

    Achats et ventes donnent la meme grandeur : le sens n'est pas filtre.
    """
    sol = 0.0
    token_amount = 0.0
    mint = None
    for line in lines:
        line_mint = line.get("mint")
        if not isinstance(line_mint, str):
            continue
        if line_mint in SOL_MINTS:
            sol += _amount(line)
        elif mint is None or line_mint == mint:
            mint = line_mint
            token_amount += _amount(line)
    if sol > 0 and token_amount > 0 and mint:
        return sol / token_amount, mint
    return None


def token_mint_of(lines: list[dict]) -> str | None:
    """Mint non-SOL majoritaire d'un groupe de lignes."""
    counts: Counter = Counter()
    for line in lines:
        mint = line.get("mint")
        if isinstance(mint, str) and mint and mint not in SOL_MINTS:
            counts[mint] += 1
    return counts.most_common(1)[0][0] if counts else None


# ---------------------------------------------------------------------------
# Prix du SOL, bougies HORAIRES
# ---------------------------------------------------------------------------

WSOL_MINT = "So11111111111111111111111111111111111111112"
_sol_hourly: list[list] = []
_sol_misses = 0


def load_sol_hourly() -> bool:
    """Bougies horaires du SOL : 2 appels CoinGecko, 1000 bougies (~41 j).

    Le pipeline n'a que du journalier (wallet_validation_v2) : une mcap
    intra-journaliere en heriterait d'une imprecision. La sonde charge donc
    ses propres bougies plutot que de reutiliser v2.
    """
    global _sol_hourly

    result = gecko(gt.token_pools, WSOL_MINT)
    if result is None:
        log.error("PERTE : pools du SOL indisponibles, pas de conversion USD")
        return False
    pools, _ = result

    best = None
    best_liquidity = -1.0
    for pool in pools:
        attributes = pool.get("attributes") or {}
        base_id = (pool.get("relationships", {}).get("base_token", {})
                   .get("data", {}).get("id", ""))
        if not base_id.endswith(WSOL_MINT):
            continue
        liquidity = _to_float(attributes.get("reserve_in_usd"))
        if liquidity > best_liquidity:
            best, best_liquidity = pool, liquidity
    if best is None:
        log.error("PERTE : aucun pool avec le SOL en base token")
        return False

    attributes = best.get("attributes") or {}
    address = attributes.get("address") or best.get("id", "").split("_", 1)[-1]
    candles = gecko(gt.ohlcv, address, "hour", 1000)
    if not candles:
        log.error("PERTE : bougies horaires du SOL indisponibles")
        return False
    _sol_hourly = sorted(candles, key=lambda c: _to_float(c[0]))
    print(f"prix du SOL : {len(_sol_hourly)} bougies HORAIRES, "
          f"{_iso(_to_float(_sol_hourly[0][0]))[:16]} -> "
          f"{_iso(_to_float(_sol_hourly[-1][0]))[:16]}")
    return True


def sol_price_at(moment: float) -> float | None:
    """Cloture de la bougie horaire couvrant l'instant. None hors couverture."""
    global _sol_misses
    if not _sol_hourly:
        return None
    closest = min(_sol_hourly, key=lambda c: abs(_to_float(c[0]) - moment))
    if abs(_to_float(closest[0]) - moment) > 3600:
        _sol_misses += 1
        return None
    price = _to_float(closest[4])
    return price or None


# ---------------------------------------------------------------------------
# Echantillon de depart (sections A et B)
# ---------------------------------------------------------------------------


def pick_tokens(count: int) -> tuple[list[dict], str]:
    """(tokens PumpSwap, provenance). Meme regle que le 20/09.

    PumpSwap n'est plus collecte depuis le 18/09 : le repli va chercher les
    pools directement chez GeckoTerminal plutot que de basculer sur un DEX
    sans bonding curve, ou les sections B et D mesureraient autre chose.
    """
    response = (
        db.get_client()
        .table(ANALYZED_TABLE)
        .select("mint, symbol, dex, pool_address, pool_created_at")
        .not_.is_("pool_address", "null")
        .limit(2000)
        .execute()
    )
    rows = [r for r in (response.data or []) if r.get("pool_address")]
    distribution = Counter(r.get("dex") or "?" for r in rows)
    print(f"sol_analyzed_tokens : {len(rows)} tokens avec un pool")
    print(f"  repartition par DEX : {dict(distribution.most_common(8))}")

    pump = [r for r in rows if (r.get("dex") or "").startswith("pumpswap")]
    if len(pump) >= count:
        print(f"  -> {count} tokens PumpSwap pris en base")
        return pump[:count], "sol_analyzed_tokens/pumpswap"

    print(f"  seulement {len(pump)} tokens PumpSwap en base")
    result = gecko(gt.dex_pools, "pumpswap", 1, "h24_volume_usd_desc")
    extra: list[dict] = []
    if result:
        pools, _ = result
        for pool in pools:
            attributes = pool.get("attributes") or {}
            base = (pool.get("relationships", {}).get("base_token", {})
                    .get("data", {}).get("id", ""))
            mint = base.split("_", 1)[-1] if "_" in base else None
            address = attributes.get("address")
            if mint and address:
                extra.append({
                    "mint": mint,
                    "symbol": (attributes.get("name") or "?").split("/")[0].strip(),
                    "dex": "pumpswap",
                    "pool_address": address,
                    "pool_created_at": attributes.get("pool_created_at"),
                })
    merged = pump + [e for e in extra if e["mint"] not in
                     {p["mint"] for p in pump}]
    if merged:
        print(f"  -> repli GeckoTerminal : {len(extra)} pools PumpSwap, "
              f"echantillon de {min(count, len(merged))}")
        return merged[:count], "geckoterminal/dex_pools(pumpswap)"

    print("  -> AUCUN pool PumpSwap accessible. Les sections B et C ne "
          "peuvent pas mesurer de bonding curve.")
    return [], "aucune"


# ---------------------------------------------------------------------------
# SECTION A - Corrections de syntaxe
# ---------------------------------------------------------------------------


def _status_proof(rows: list[dict]) -> str:
    """Le filtre de statut a-t-il ecarte les transactions en erreur ?"""
    failed = sum(1 for r in rows if isinstance(r, dict) and r.get("err"))
    return f"{len(rows)} elements, dont {failed} en erreur (champ err)"


def section_a(reference: str) -> dict:
    start_section("A", "Corrections de syntaxe")
    print("Une cle a la fois : acquis du 19/09, une cle inconnue fait "
          "rejeter tout l'objet de config.\n")
    accepted: dict[str, Any] = {"status": {}, "solmode": {}}

    print("--- getTransactionsForAddress : filters.status ---")
    print("Le 20/09 envoyait \"success\" et se faisait rejeter. Le ticket "
          "donne \"succeeded\".")
    for value in (None, "succeeded", "success"):
        config: dict[str, Any] = {"limit": 20, "sortOrder": "desc"}
        if value is not None:
            config["filters"] = {"status": value}
        payload = transactions(reference, config)
        label = f'status="{value}"' if value else "sans filtre (reference)"
        if payload == "CAPPED":
            break
        print(f"\n> {label}")
        print(f"  config : {json.dumps(config)}")
        if payload is None:
            print("  PERTE, non concluant")
            continue
        if isinstance(payload, dict) and "error" in payload:
            print("  REJET, message brut :")
            print("    " + _as_json(payload).replace("\n", "\n    "))
            accepted["status"][str(value)] = False
            continue
        rows = rows_of(payload)
        if rows is None:
            print(f"  accepte, mais pas de liste : {_as_json(payload)[:300]}")
            accepted["status"][str(value)] = True
            continue
        print(f"  ACCEPTE : {_status_proof(rows)}")
        accepted["status"][str(value)] = True

    print()
    accepted["solmode"] = _section_a_solmode(reference)
    return accepted


def _section_a_solmode(reference: str) -> dict:
    """solMode : "merged" (defaut) contre "separate", sur UNE signature."""
    print("--- getTransfersByAddress : solMode merged vs separate ---")
    print("Le 20/09 envoyait solMode=True (booleen). Les valeurs attendues "
          "sont \"merged\" et \"separate\".")

    base = transfers(reference, {"limit": 100, "sortOrder": "desc"})
    rows_base = rows_of(base)
    if rows_base is None:
        print("  appel de reference indisponible, comparaison impossible")
        return {}

    target = None
    for signature, lines in group_by_signature(rows_base).items():
        if swap_price(lines):
            target = signature
            break
    if target is None:
        print("  aucune signature de swap sur la page de reference")
        return {}
    print(f"  signature de swap retenue : {target}")

    outcome: dict[str, Any] = {}
    captured: dict[str, list[dict]] = {}
    for mode in ("merged", "separate"):
        payload = transfers(reference, {"limit": 100, "sortOrder": "desc",
                                        "solMode": mode})
        if payload == "CAPPED":
            break
        if isinstance(payload, dict) and "error" in payload:
            outcome[mode] = "REJET"
            print(f"\n  solMode=\"{mode}\" : REJET")
            print("    " + _as_json(payload)[:400].replace("\n", "\n    "))
            continue
        rows = rows_of(payload)
        if rows is None:
            outcome[mode] = "sans liste"
            continue
        outcome[mode] = "ACCEPTE"
        groups = group_by_signature(rows)
        captured[mode] = groups.get(target) or []

    print()
    for mode in ("merged", "separate"):
        lines = captured.get(mode)
        if lines is None:
            continue
        if not lines:
            print(f"  {mode:8} : signature absente de cette page "
                  f"(mode {outcome.get(mode)})")
            continue
        print(f"  {mode:8} : {len(lines)} lignes")
        for line in lines:
            mint = line.get("mint")
            tag = " <- SOL/WSOL" if mint in SOL_MINTS else ""
            print(f"      mint={mint} montant={_amount(line)} "
                  f"de={str(line.get('fromUserAccount'))[:8]} "
                  f"vers={str(line.get('toUserAccount'))[:8]}{tag}")
    if captured.get("merged") is not None and captured.get("separate") is not None:
        diff = len(captured.get("separate") or []) - len(captured.get("merged") or [])
        print(f"  ecart de lignes separate - merged : {diff:+d}")
    return outcome


# ---------------------------------------------------------------------------
# Prix median d'une page de swaps
# ---------------------------------------------------------------------------


def tolerance_for(delta: float) -> float:
    """Ecart maximal accepte entre l'instant vise et le swap trouve."""
    return max(900.0, delta * 0.25)


def page_price(address: str, moment: float,
               tolerance: float) -> tuple[float | None, int, float, int]:
    """(prix median en SOL, nb de swaps, ecart median, appels consommes).

    Le prix est la MEDIANE des swaps de la page, achats et ventes
    confondus : un swap isole peut etre une miette ou un slippage extreme.
    """
    payload = transfers(address, {
        "limit": 100, "sortOrder": "asc",
        "filters": {"blockTime": {"gte": int(moment)}},
    })
    if payload == "CAPPED":
        return None, 0, 0.0, 0
    rows = rows_of(payload)
    if rows is None:
        return None, 0, 0.0, 1
    prices: list[float] = []
    gaps: list[float] = []
    for lines in group_by_signature(rows).values():
        stamps = [_line_time(line) for line in lines if _line_time(line) > 0]
        when = max(stamps) if stamps else 0.0
        if when and when - moment > tolerance:
            continue
        priced = swap_price(lines)
        if priced:
            prices.append(priced[0])
            gaps.append(max(0.0, when - moment))
    if not prices:
        return None, 0, 0.0, 1
    return statistics.median(prices), len(prices), statistics.median(gaps), 1


def diagnose_no_price(address: str, label: str, extra: str = "") -> None:
    """Pourquoi aucun prix ? Mints de la page, jambe SOL, signatures."""
    print(f"    diagnostic {label} ({address[:8]}...) {extra}")
    payload = transfers(address, {"limit": 100, "sortOrder": "desc"})
    if payload == "CAPPED":
        print("      plafond atteint, diagnostic impossible")
        return
    if isinstance(payload, dict) and "error" in payload:
        print("      REJET : " + _as_json(payload)[:200].replace("\n", " "))
        return
    rows = rows_of(payload)
    if rows is None:
        print("      PERTE ou payload inattendu")
        return
    if not rows:
        print("      0 ligne : l'adresse n'a aucun transfert connu d'Helius")
        return
    mints: Counter = Counter()
    for line in rows:
        mint = line.get("mint")
        if isinstance(mint, str):
            mints[mint] += 1
    groups = group_by_signature(rows)
    priced = sum(1 for lines in groups.values() if swap_price(lines))
    sol_lines = sum(count for mint, count in mints.items() if mint in SOL_MINTS)
    print(f"      {len(rows)} lignes, {len(groups)} signatures, "
          f"{priced} swap(s) valorisable(s)")
    print(f"      jambes SOL/WSOL : {sol_lines}")
    print("      mints vus : " + ", ".join(
        f"{m[:8]}..x{n}" for m, n in mints.most_common(5)))
    if not sol_lines:
        print("      -> aucune jambe SOL : le token est cote contre autre "
              "chose (USDC ?) ou l'adresse n'est pas le pool de cotation")


# ---------------------------------------------------------------------------
# SECTION B - Bonding curve : deriver au lieu de deviner
# ---------------------------------------------------------------------------


def heuristic_curve(mint: str) -> str | None:
    """L'heuristique du 20/09 : contrepartie la plus frequente du mint."""
    payload = transfers(mint, {"limit": 50, "sortOrder": "asc"})
    rows = rows_of(payload)
    if not rows:
        return None
    counterparts: Counter = Counter()
    for line in rows:
        if line.get("mint") != mint:
            continue
        for field in ("fromUserAccount", "toUserAccount"):
            value = line.get(field)
            if isinstance(value, str) and value:
                counterparts[value] += 1
    return counterparts.most_common(1)[0][0] if counterparts else None


def first_transfer_time(address: str) -> float | None:
    """Date de la premiere transaction connue d'une adresse."""
    payload = transfers(address, {"limit": 1, "sortOrder": "asc"})
    rows = rows_of(payload)
    if not rows:
        return None
    stamps = [_line_time(line) for line in rows if _line_time(line) > 0]
    return min(stamps) if stamps else None


def lines_before(address: str, end: float) -> tuple[int, int]:
    """(lignes datees avant end sur la 1re page ascendante, total lignes)."""
    payload = transfers(address, {"limit": 100, "sortOrder": "asc"})
    rows = rows_of(payload)
    if not rows:
        return 0, 0
    inside = sum(1 for line in rows
                 if 0 < _line_time(line) <= end)
    return inside, len(rows)


def early_buyers(curve: str, mint: str, created: float,
                 end: float) -> dict:
    """Lecture ascendante de la courbe, de la creation a la graduation."""
    buyers: dict[str, float] = {}
    signatures: set[str] = set()
    calls = 0
    page_token = None
    stopped = "historique epuise"

    for page in range(CURVE_MAX_PAGES):
        config: dict[str, Any] = {"limit": 100, "sortOrder": "asc"}
        if page_token:
            config["paginationToken"] = page_token
        payload = transfers(curve, config)
        if payload == "CAPPED":
            stopped = "plafond de budget"
            break
        calls += 1
        rows = rows_of(payload)
        if not rows:
            break
        past_end = False
        for line in rows:
            when = _line_time(line)
            if when and when > end:
                past_end = True
                continue
            signature = line.get("signature")
            if isinstance(signature, str):
                signatures.add(signature)
            if line.get("mint") != mint:
                continue
            buyer = line.get("toUserAccount")
            if isinstance(buyer, str) and buyer and buyer != curve:
                buyers.setdefault(buyer, when)
        if past_end:
            stopped = "graduation atteinte"
            break
        page_token = next_page_token(payload)
        if not page_token:
            break
    else:
        stopped = f"plafond de {CURVE_MAX_PAGES} pages"
        log.warning("Plafond de %d pages atteint sur la courbe %s : les "
                    "chiffres de ce token sont des minorants",
                    CURVE_MAX_PAGES, curve[:8])

    windows: Counter = Counter()
    for when in buyers.values():
        delta = when - created if when else None
        if delta is None or delta < 0:
            continue
        if delta <= 60:
            windows["60 s"] += 1
        if delta <= 300:
            windows["5 min"] += 1
        if delta <= 1800:
            windows["30 min"] += 1
    return {"buyers": len(buyers), "signatures": len(signatures),
            "calls": calls, "stopped": stopped, "windows": windows}


EARLY_BUYER_TOKENS = 8

WHY_ZERO = """Pourquoi la section 6 du 20/09 lisait 0 a 1 transaction par token.
Trois causes possibles, que cette section MESURE au lieu de les affirmer :

  (a) mauvaise adresse. La courbe etait DEVINEE : "contrepartie la plus
      frequente des 50 premieres jambes du mint". Apres graduation, cette
      contrepartie est tres souvent le pool PumpSwap ou un compte de token,
      pas la courbe.
  (b) fenetre incoherente. La creation venait du premier transfert du MINT
      et la graduation du premier transfert du POOL. Si l'adresse devinee
      est le pool, toutes ses lignes sont posterieures a la graduation :
      past_end est vrai des la page 1, la boucle sort avec 0 acheteur.
  (c) filtre trop etroit. Seules les lignes portant exactement le mint
      etaient comptees, et l'acheteur lu sur toUserAccount.

La mesure : pour chaque token, nombre de lignes anterieures a la
graduation sur la PREMIERE page ascendante, cote PDA et cote heuristique.
Si le PDA en a et l'heuristique non, (a) et (b) sont etablies."""


def section_b(tokens: list[dict]) -> dict:
    start_section("B", "Bonding curve : deriver au lieu de deviner")
    print(WHY_ZERO)
    print(f"\nPDA = sha256(seeds || bump || program || "
          f"\"ProgramDerivedAddress\"), seeds [\"bonding-curve\", mint], "
          f"programme {PUMPFUN_PROGRAM[:8]}...\n")

    derived = matched = 0
    durations: list[float] = []
    suspects: list[str] = []
    graduations: dict[str, float] = {}
    curve_lines: list[tuple[str, int, int]] = []
    prices_ok = prices_try = 0
    no_price: list[str] = []
    per_token_calls: list[int] = []
    buyers_counts: list[int] = []
    windows: Counter = Counter()
    total_buyers = 0
    capped_tokens = 0
    baton_seen = False

    for index, token in enumerate(tokens):
        mint = token["mint"]
        symbol = token.get("symbol") or "?"
        if symbol.lower() == "baton":
            baton_seen = True
        try:
            pda, bump = solana_addr.bonding_curve_address(mint, PUMPFUN_PROGRAM)
        except ValueError as error:
            log.warning("  %-10s : PDA inderivable (%s)", symbol, error)
            continue
        derived += 1

        heuristic = heuristic_curve(mint)
        same = heuristic == pda
        if same:
            matched += 1

        created = first_transfer_time(pda)
        graduated = first_transfer_time(token["pool_address"])
        if graduated:
            graduations[mint] = graduated

        print(f"\n  {symbol:>10} | PDA {pda[:8]}.. (bump {bump}) | "
              f"heuristique {(heuristic or 'aucune')[:8]}.. | "
              f"{'IDENTIQUES' if same else 'DIFFERENTES'}")

        if created and graduated and graduated > created:
            duration = graduated - created
            durations.append(duration)
            flag = ""
            if duration < SUSPECT_DURATION_S:
                flag = "  <-- SUSPECT : moins d'une minute"
                suspects.append(symbol)
            print(f"             creation {_iso(created)[:19]} -> graduation "
                  f"{_iso(graduated)[:19]} = {duration / 60:.1f} min{flag}")
        else:
            print("             duree indeterminable (creation "
                  f"{created is not None}, graduation {graduated is not None})")

        # Diagnostic (a)/(b) : qui a des lignes AVANT la graduation ?
        end = graduated or (created + 86400 if created else 0)
        if end:
            pda_inside, pda_total = lines_before(pda, end)
            heur_inside, heur_total = (0, 0)
            if heuristic and heuristic != pda:
                heur_inside, heur_total = lines_before(heuristic, end)
            curve_lines.append((symbol, pda_inside, heur_inside))
            print(f"             lignes avant graduation (1re page) : "
                  f"PDA {pda_inside}/{pda_total} | "
                  f"heuristique {heur_inside}/{heur_total}")

        # Prix avant graduation, sur la courbe derivee.
        got = 0
        if created and graduated and graduated > created:
            span = graduated - created
            for step in range(3):
                moment = created + span * (step + 1) / 4
                price, count, _, spent = page_price(
                    pda, moment, tolerance_for(span / 4))
                prices_try += 1
                if spent == 0:
                    break
                if price is not None:
                    got += 1
                    prices_ok += 1
            print(f"             prix avant graduation : {got}/3 "
                  f"(median de {3} instants)")
        if got == 0:
            no_price.append(symbol)
            diagnose_no_price(pda, f"courbe de {symbol}",
                              f"dex={token.get('dex')}")

        # Refaire la section 6 sur la courbe DERIVEE.
        if index < EARLY_BUYER_TOKENS and created:
            outcome = early_buyers(pda, mint, created,
                                   graduated or created + 86400)
            per_token_calls.append(outcome["calls"])
            buyers_counts.append(outcome["buyers"])
            total_buyers += outcome["buyers"]
            windows.update(outcome["windows"])
            if "pages" in outcome["stopped"]:
                capped_tokens += 1
            print(f"             courbe lue : {outcome['signatures']} tx, "
                  f"{outcome['buyers']} acheteurs distincts, "
                  f"{outcome['calls']} appels ({outcome['stopped']})")

    print(f"\nPDA derives : {derived}/{len(tokens)} | identiques a "
          f"l'heuristique du 20/09 : {matched}")
    if durations:
        print(f"creation -> graduation : mediane "
              f"{statistics.median(durations) / 60:.1f} min | min "
              f"{min(durations) / 60:.1f} min | max "
              f"{max(durations) / 3600:.1f} h")
    if suspects:
        print(f"SUSPECTS (< 1 min, donc date de creation ou de graduation "
              f"fausse) : {', '.join(suspects)}")
    if prices_try:
        print(f"prix avant graduation : {prices_ok}/{prices_try} "
              f"({100 * prices_ok / prices_try:.0f} %)")
    if no_price:
        print(f"tokens sans aucun prix : {', '.join(no_price)}")
    if not baton_seen:
        print("baton n'est PAS dans l'echantillon de cette sonde : son cas "
              "n'est pas diagnostique nommement, le diagnostic ci-dessus "
              "s'applique a tout token sans prix.")
    if per_token_calls:
        print(f"appels par token (courbe entiere) : mediane "
              f"{statistics.median(per_token_calls):.0f} | max "
              f"{max(per_token_calls)}")
    if buyers_counts:
        print(f"acheteurs distincts avant graduation : mediane "
              f"{statistics.median(buyers_counts):.0f} | max "
              f"{max(buyers_counts)}")
    if total_buyers:
        print("part des acheteurs dans les premieres :")
        for label in ("60 s", "5 min", "30 min"):
            share = 100 * windows[label] / total_buyers
            print(f"    {label:>7} : {windows[label]:4d} ({share:5.1f} %)")
    if capped_tokens:
        log.warning("%d token(s) ont atteint le plafond de %d pages",
                    capped_tokens, CURVE_MAX_PAGES)

    return {"derived": derived, "matched": matched, "durations": durations,
            "suspects": suspects, "graduations": graduations,
            "curve_lines": curve_lines, "prices_ok": prices_ok,
            "prices_try": prices_try, "no_price": no_price,
            "calls": per_token_calls, "buyers": buyers_counts,
            "windows": dict(windows), "capped": capped_tokens}


# ---------------------------------------------------------------------------
# SECTION C - Trouver le compte propre aux migrations
# ---------------------------------------------------------------------------


def is_excluded(account: str, mints: set[str]) -> bool:
    """Programme, sysvar, mint du token ou pool : ne distingue rien."""
    if account in mints:
        return True
    return any(account.startswith(prefix) for prefix in EXCLUDED_PREFIXES)


def program_ids(transaction: dict) -> set[str]:
    """Programmes appeles par une transaction enrichie, inner compris."""
    found: set[str] = set()
    for instruction in transaction.get("instructions") or []:
        if not isinstance(instruction, dict):
            continue
        program = instruction.get("programId")
        if isinstance(program, str):
            found.add(program)
        for inner in instruction.get("innerInstructions") or []:
            if isinstance(inner, dict) and isinstance(inner.get("programId"), str):
                found.add(inner["programId"])
    return found


def recurring_accounts(tokens: list[dict]) -> tuple[Counter, set[str]]:
    """Comptes presents a la creation de 3 pools. UN seul appel Enhanced."""
    signatures: list[str] = []
    mints = {t["mint"] for t in tokens} | {t["pool_address"] for t in tokens}
    for token in tokens[:3]:
        payload = transfers(token["pool_address"], {"limit": 1,
                                                    "sortOrder": "asc"})
        rows = rows_of(payload)
        if not rows:
            continue
        signature = rows[0].get("signature")
        if isinstance(signature, str):
            signatures.append(signature)

    seen: Counter = Counter()
    if not signatures:
        print("  aucune signature de creation recuperee")
        return seen, mints
    enriched = enhanced(signatures)
    if not enriched:
        print("  enrichissement indisponible : comptes recurrents inconnus")
        return seen, mints

    for transaction in enriched:
        accounts: set[str] = set()
        fee_payer = transaction.get("feePayer")
        if isinstance(fee_payer, str):
            accounts.add(fee_payer)
        for entry in transaction.get("accountData") or []:
            if isinstance(entry, dict) and isinstance(entry.get("account"), str):
                accounts.add(entry["account"])
        accounts |= program_ids(transaction)
        for account in accounts:
            seen[account] += 1
    print(f"  {len(signatures)} transactions de creation enrichies "
          f"(1 appel Enhanced), {len(seen)} comptes distincts")
    return seen, mints


def screen_account(account: str, start: float,
                   end: float) -> tuple[int, list[str], str]:
    """(transactions sur la fenetre, signatures, note). 1 appel."""
    config: dict[str, Any] = {
        "limit": 1000, "sortOrder": "desc",
        "transactionDetails": "signatures",
        "filters": {"blockTime": {"gte": int(start), "lte": int(end)}},
    }
    payload = transactions(account, config)
    if payload == "CAPPED":
        return -1, [], "plafond"
    if isinstance(payload, dict) and "error" in payload:
        # Une cle peut faire rejeter tout l'objet : repli sans
        # transactionDetails, la fenetre restant filtree cote serveur.
        config.pop("transactionDetails")
        payload = transactions(account, config)
        if payload == "CAPPED":
            return -1, [], "plafond"
        if isinstance(payload, dict) and "error" in payload:
            return -1, [], "rejet : " + _as_json(payload)[:120].replace("\n", " ")
    rows = rows_of(payload)
    if rows is None:
        return -1, [], "PERTE"
    signatures: list[str] = []
    for row in rows:
        if isinstance(row, str):
            signatures.append(row)
        elif isinstance(row, dict) and isinstance(row.get("signature"), str):
            signatures.append(row["signature"])
    return len(rows), signatures, ""


def scan_day(account: str, day: str) -> dict:
    """Pagination d'une journee complete : mints gradues et appels."""
    start, end = _day_bounds(day)
    found: dict[str, dict] = {}
    signatures: set[str] = set()
    calls = 0
    page_token = None
    stopped = "journee couverte"

    for _ in range(DAY_MAX_PAGES):
        config: dict[str, Any] = {
            "limit": 100, "sortOrder": "asc",
            "filters": {"blockTime": {"gte": int(start), "lte": int(end)}},
        }
        if page_token:
            config["paginationToken"] = page_token
        payload = transfers(account, config)
        if payload == "CAPPED":
            stopped = "plafond de budget"
            break
        calls += 1
        rows = rows_of(payload)
        if rows is None:
            stopped = "PERTE"
            break
        if not rows:
            break
        for signature, lines in group_by_signature(rows).items():
            signatures.add(signature)
            mint = token_mint_of(lines)
            if not mint or mint in found:
                continue
            stamps = [_line_time(line) for line in lines if _line_time(line) > 0]
            pool = None
            for line in lines:
                if line.get("mint") in SOL_MINTS:
                    candidate = line.get("toUserAccount")
                    if isinstance(candidate, str) and candidate != account:
                        pool = candidate
                        break
            found[mint] = {"time": max(stamps) if stamps else start,
                           "pool": pool, "signature": signature}
        page_token = next_page_token(payload)
        if not page_token:
            break
    else:
        stopped = f"plafond de {DAY_MAX_PAGES} pages"
        log.warning("Journee %s : plafond de %d pages, le compte est un "
                    "minorant", day, DAY_MAX_PAGES)

    return {"day": day, "mints": found, "signatures": len(signatures),
            "calls": calls, "stopped": stopped}


def section_c(tokens: list[dict]) -> dict:
    start_section("C", "Trouver le compte propre aux migrations")
    print("Objectif : un compte present a CHAQUE graduation et a rien "
          "d'autre. Les programmes et les mints sont exclus, les comptes "
          "satures sont des comptes de trading.\n")

    print("--- comptes recurrents a la creation de 3 pools ---")
    seen, mints = recurring_accounts(tokens)
    shared = [account for account, count in seen.items() if count >= 2]
    kept = [account for account in shared if not is_excluded(account, mints)]
    print(f"  presents sur >= 2 pools : {len(shared)} | apres exclusion des "
          f"programmes et mints : {len(kept)}")
    for account in kept[:SCREEN_MAX_ACCOUNTS]:
        print(f"    {account} ({seen[account]}/3)")

    now = datetime.now(timezone.utc).timestamp()
    hour_start, hour_end = now - 3600, now
    print(f"\n--- criblage sur 1 h ({_iso(hour_start)[:16]} -> "
          f"{_iso(hour_end)[:16]}) ---")
    print("  sature (>= 900 tx/h) -> compte de trading, ecarte")

    candidates: list[tuple[str, int, list[str]]] = []
    for account in kept[:SCREEN_MAX_ACCOUNTS]:
        count, signatures, note = screen_account(account, hour_start, hour_end)
        if count < 0:
            print(f"    {account[:8]}.. : non mesurable ({note})")
            continue
        verdict = "SATURE, ecarte" if count >= 900 else (
            "candidat" if 5 <= count <= 300 else "volume hors cible")
        print(f"    {account[:8]}.. : {count:4d} tx/h -> {verdict}")
        if verdict == "candidat":
            candidates.append((account, count, signatures))

    chosen = None
    if candidates:
        chosen = _confirm_candidate(candidates)
    if chosen is None:
        print("\n  AUCUN compte de migration retenu : la liste des gradues "
              "d'une journee n'est pas reconstituable par cette voie.")
        return {"kept": kept, "candidates": [c[0] for c in candidates],
                "chosen": None, "days": {}}

    print(f"\n--- pagination de 2 journees completes sur {chosen[:8]}.. ---")
    print("  getTransfersByAddress et non getTransactionsForAddress : "
          "10 credits au lieu de 100, et les lignes portent le mint.")
    days: dict[str, dict] = {}
    for day in (DAY_A, DAY_B):
        outcome = scan_day(chosen, day)
        days[day] = outcome
        print(f"  {day} : {len(outcome['mints'])} mints distincts, "
              f"{outcome['signatures']} transactions, {outcome['calls']} "
              f"appels ({outcome['stopped']})")
    return {"kept": kept, "candidates": [c[0] for c in candidates],
            "chosen": chosen, "days": days}


def _confirm_candidate(
    candidates: list[tuple[str, int, list[str]]],
) -> str | None:
    """3 transactions echantillon doivent porter pump.fun ET PumpSwap."""
    print("\n--- confirmation : 3 transactions doivent porter pump.fun "
          "ET PumpSwap ---")
    for account, count, signatures in candidates:
        sample = signatures[:3]
        if not sample:
            print(f"    {account[:8]}.. : aucune signature echantillon")
            continue
        enriched = enhanced(sample)
        if not enriched:
            print(f"    {account[:8]}.. : enrichissement indisponible")
            continue
        both = 0
        for transaction in enriched:
            programs = program_ids(transaction)
            has_pump = any(p.startswith(PUMPFUN_PREFIX) for p in programs)
            has_swap = any(p.startswith(PUMPSWAP_PREFIX) for p in programs)
            if has_pump and has_swap:
                both += 1
        print(f"    {account[:8]}.. : {both}/{len(enriched)} transactions "
              f"portent les deux programmes ({count} tx/h)")
        if both >= 2:
            print(f"  -> compte de migration retenu : {account}")
            return account
    return None


# ---------------------------------------------------------------------------
# SECTION D - Echantillon reellement aleatoire
# ---------------------------------------------------------------------------


def build_population(days: dict[str, dict]) -> tuple[list[dict], str]:
    """Population de tirage : les graduations d'une journee complete.

    La journee la plus ancienne est preferee : tous les horizons, 7 jours
    compris, y sont echus. Un horizon non echu n'est pas un echec de prix.
    """
    for day in (DAY_B, DAY_A):
        outcome = days.get(day) or {}
        mints = outcome.get("mints") or {}
        if len(mints) >= 5:
            population = [{"mint": mint, **data} for mint, data in mints.items()]
            return population, f"graduations du {day} (section C)"
    return [], "aucune"


def measure_token(entry: dict) -> dict:
    """Trajectoire d'un token : prix median a chaque horizon, en SOL."""
    now = datetime.now(timezone.utc).timestamp()
    graduated = _to_float(entry.get("time"))
    mint = entry["mint"]
    addresses = [a for a in (entry.get("pool"), mint) if a]
    calls = 0

    # Adresse de cotation : le pool candidat, sinon le mint. La premiere qui
    # donne un prix sert pour toute la serie.
    address = addresses[0] if addresses else mint
    reference = None
    for candidate in addresses:
        price, _, _, spent = page_price(candidate, graduated,
                                        tolerance_for(3600))
        calls += spent
        if price is not None:
            address, reference = candidate, price
            break

    prices: dict[str, float] = {}
    due = 0
    obtained = 0
    best_mcap = 0.0
    supply = _to_float((token_supply(mint) or {}).get("uiAmount"))

    for label, delta in TRAJECTORY_POINTS:
        moment = graduated + delta
        if moment > now:
            continue
        due += 1
        price, _, _, spent = page_price(address, moment, tolerance_for(delta))
        calls += spent
        if price is None:
            continue
        obtained += 1
        prices[label] = price
        usd = sol_price_at(moment)
        if usd and supply > 0:
            best_mcap = max(best_mcap, price * usd * supply)

    verdict = "indetermine"
    if reference and "24 h" in prices:
        verdict = "mort" if prices["24 h"] < DEAD_RATIO * reference else "vivant"
    elif reference and due and not obtained:
        verdict = "muet"
    return {"mint": mint, "address": address, "reference": reference,
            "prices": prices, "due": due, "obtained": obtained,
            "calls": calls, "supply": supply, "mcap": best_mcap,
            "verdict": verdict}


def section_d(days: dict[str, dict], fallback: list[dict],
              fallback_graduations: dict[str, float]) -> dict:
    start_section("D", "Echantillon reellement aleatoire")

    population, source = build_population(days)
    if not population:
        population = [
            {"mint": token["mint"], "time": fallback_graduations[token["mint"]],
             "pool": token["pool_address"]}
            for token in fallback
            if token["mint"] in fallback_graduations
        ]
        source = "REPLI : tokens de la section B"
        print("  ATTENTION : la section C n'a pas fourni de population. "
              "L'echantillon est celui de la section B, donc NON aleatoire "
              "et biaise vers les tokens survivants deja collectes.")
    if not population:
        print("  aucune population : section non mesurable")
        return {}

    rng = random.Random(RANDOM_SEED)
    sample = rng.sample(population, min(SAMPLE_SIZE, len(population)))
    print(f"source : {source}")
    print(f"seed aleatoire : {RANDOM_SEED} | population {len(population)} | "
          f"tires {len(sample)} (morts compris)")
    print(f"prix = MEDIANE des swaps de la page, achats et ventes, en SOL. "
          f"Mort = prix a 24 h < {DEAD_RATIO:.0%} du prix a la graduation.\n")

    results: list[dict] = []
    for entry in sample:
        outcome = measure_token(entry)
        results.append(outcome)
        log.info("  %s.. : %s | %d/%d points | mcap max %.0f $",
                 entry["mint"][:8], outcome["verdict"],
                 outcome["obtained"], outcome["due"], outcome["mcap"])

    by_verdict: Counter = Counter(r["verdict"] for r in results)
    print(f"\nclassement : {dict(by_verdict)}")

    print("taux de succes du prix, par classe :")
    for verdict in ("vivant", "mort", "muet", "indetermine"):
        group = [r for r in results if r["verdict"] == verdict]
        if not group:
            continue
        due = sum(r["due"] for r in group)
        obtained = sum(r["obtained"] for r in group)
        rate = 100 * obtained / due if due else 0
        print(f"    {verdict:>12} : {obtained:4d}/{due:4d} points "
              f"({rate:5.1f} %) sur {len(group)} tokens")

    calls = [r["calls"] for r in results]
    if calls:
        print(f"appels par token : mediane {statistics.median(calls):.1f} | "
              f"max {max(calls)}")

    mcaps = sorted(r["mcap"] for r in results if r["mcap"] > 0)
    if mcaps:
        print("capitalisations max observees, brut (aucune conclusion de "
              "trading) :")
        for label, value in (
            ("min", mcaps[0]),
            ("p25", mcaps[len(mcaps) // 4]),
            ("mediane", statistics.median(mcaps)),
            ("p75", mcaps[3 * len(mcaps) // 4]),
            ("max", mcaps[-1]),
        ):
            print(f"    {label:>8} : {value:>14,.0f} $")
        buckets: Counter = Counter()
        for value in mcaps:
            if value < 10_000:
                buckets["< 10 k$"] += 1
            elif value < 100_000:
                buckets["10-100 k$"] += 1
            elif value < 1_000_000:
                buckets["100 k-1 M$"] += 1
            else:
                buckets["> 1 M$"] += 1
        print("    distribution : " + ", ".join(
            f"{k} {v}" for k, v in buckets.items()))
        print(f"    ({len(results) - len(mcaps)} token(s) sans "
              f"capitalisation : pas de prix ou supply inconnue)")
    if _sol_misses:
        log.warning("%d conversion(s) USD hors couverture des bougies "
                    "horaires du SOL", _sol_misses)

    return {"source": source, "seed": RANDOM_SEED,
            "sample": len(sample), "population": len(population),
            "verdicts": dict(by_verdict), "calls": calls, "mcaps": mcaps,
            "results": results}


# ---------------------------------------------------------------------------
# SECTION E - Recapitulatif
# ---------------------------------------------------------------------------


def credits_spent() -> int:
    return sum(_calls[method] * CREDIT_COST.get(method, 0) for method in _calls)


def show_budget(title: str) -> None:
    print(f"\n{title}")
    for method, count in sorted(_calls.items()):
        cost = CREDIT_COST.get(method, 0)
        cap = CAPS_GLOBAL.get(method)
        cap_text = f" / plafond {cap}" if cap else ""
        print(f"  {method:28} : {count:5d} appels{cap_text}"
              f" -> {count * cost:>7,} credits ({cost}/appel)")
    print(f"  {'TOTAL':28} : {credits_spent():>7,} credits")


def final_recap(results: dict) -> None:
    print("\n" + "=" * 74)
    print("SECTION E - RECAPITULATIF (a copier-coller)")
    print("=" * 74)

    provenance = results.get("provenance", "?")
    print(f"\nEchantillon de depart : {provenance}")

    a = results.get("a") or {}
    if a:
        status = a.get("status") or {}
        print("\nSyntaxe (A) :")
        for value, ok in status.items():
            print(f"  filters.status={value:12} : "
                  f"{'ACCEPTE' if ok else 'REJETE'}")
        print(f"  solMode : {a.get('solmode') or 'non mesure'}")

    b = results.get("b") or {}
    median_b = statistics.median(b["calls"]) if b.get("calls") else 0
    if b:
        durations = b.get("durations") or []
        print(f"\nBonding curve (B) : {b['derived']} PDA derives, "
              f"{b['matched']} identiques a l'heuristique du 20/09")
        if durations:
            print(f"  creation -> graduation : mediane "
                  f"{statistics.median(durations) / 60:.1f} min")
        if b.get("suspects"):
            print(f"  durees suspectes (< 1 min) : {', '.join(b['suspects'])}")
        print(f"  prix avant graduation : {b['prices_ok']}/{b['prices_try']}")
        if b.get("buyers"):
            print(f"  acheteurs distincts avant graduation : mediane "
                  f"{statistics.median(b['buyers']):.0f}")
        print(f"  appels par token (courbe entiere) : mediane {median_b:.0f}")

    c = results.get("c") or {}
    days = c.get("days") or {}
    per_day_counts = [len(d.get("mints") or {}) for d in days.values()]
    per_day_calls = [d.get("calls", 0) for d in days.values()]
    graduations_per_day = statistics.median(per_day_counts) if per_day_counts else 0
    calls_per_day = statistics.median(per_day_calls) if per_day_calls else 0
    print("\nCompte de migration (C) :")
    if c.get("chosen"):
        print(f"  retenu : {c['chosen']}")
        for day, outcome in days.items():
            print(f"  {day} : {len(outcome.get('mints') or {})} graduations, "
                  f"{outcome.get('calls')} appels ({outcome.get('stopped')})")
    else:
        print(f"  AUCUN compte retenu ({len(c.get('candidates') or [])} "
              f"candidat(s) teste(s)) : l'univers n'est pas listable par "
              f"cette voie.")

    d = results.get("d") or {}
    median_d = statistics.median(d["calls"]) if d.get("calls") else 0
    if d:
        print(f"\nTrajectoire (D) : {d.get('sample')} tokens tires "
              f"(seed {d.get('seed')}) sur {d.get('source')}")
        print(f"  classement : {d.get('verdicts')}")
        print(f"  appels par token : mediane {median_d:.1f}")
        mcaps = d.get("mcaps") or []
        if mcaps:
            print(f"  capitalisation max mediane : "
                  f"{statistics.median(mcaps):,.0f} $")

    show_budget("Consommation reelle de cette sonde :")

    print(f"\nPROJECTION sur {MONTHLY_CREDITS:,} credits/mois")
    print("  Couts unitaires utilises : "
          f"getTransfersByAddress {CREDIT_COST['getTransfersByAddress']}, "
          f"getTransactionsForAddress {CREDIT_COST['getTransactionsForAddress']}, "
          f"Enhanced {CREDIT_COST['enhanced']} (doc Helius).")

    unit = CREDIT_COST["getTransfersByAddress"]
    if graduations_per_day and calls_per_day:
        listing_day = calls_per_day * unit
        listing_month = listing_day * 30
        remaining = MONTHLY_CREDITS - listing_month
        print(f"  (a) gradues par jour            : "
              f"{graduations_per_day:.0f}")
        print(f"  (a) credits pour les lister     : "
              f"{listing_day:,.0f} / jour, {listing_month:,.0f} / mois")
        print(f"      budget restant              : {remaining:,.0f} credits")
    else:
        remaining = MONTHLY_CREDITS
        print("  (a) gradues par jour            : non mesure (section C "
              "sans compte de migration)")

    if median_d:
        cost_d = median_d * unit
        print(f"  (b) credits par token suivi     : {cost_d:,.0f} "
              f"({median_d:.1f} appels)")
        if remaining > 0:
            print(f"      tokens suivables par mois   : "
                  f"{remaining / cost_d:,.0f}")
            if graduations_per_day:
                share = 100 * (remaining / cost_d) / (graduations_per_day * 30)
                print(f"      soit {share:.1f} % des gradues du mois")
    if median_b:
        cost_bc = (median_b + median_d) * unit
        print(f"  (c) + premiers acheteurs        : {cost_bc:,.0f} par token "
              f"({median_b:.0f} + {median_d:.1f} appels)")
        if remaining > 0 and cost_bc:
            print(f"      tokens complets par mois    : "
                  f"{remaining / cost_bc:,.0f}")

    print("\n  Le prix du SOL est ici HORAIRE : les capitalisations de la "
          "section D\n  ne portent plus l'imprecision journaliere du 20/09.")
    if _capped:
        print(f"\n  {len(_capped)} plafond(s) atteint(s) : les chiffres "
              "correspondants sont des MINORANTS.")
        for section, method in sorted(_capped):
            print(f"    section {section} : {method}")


# ---------------------------------------------------------------------------
# Entree
# ---------------------------------------------------------------------------


def main() -> None:
    setup_logging()
    diagnose_environment()
    present = bool(os.environ.get("HELIUS_API_KEY", "").strip())
    log.info("  %-18s : %s", "HELIUS_API_KEY",
             "presente" if present else "ABSENTE")
    helius.api_key()  # leve si absente

    print("\nSonde univers v2 : refaire les trois mesures ratees du 20/09.")
    print("Aucune ecriture en base, aucune conclusion de trading.")
    print(f"Plafonds globaux : {CAPS_GLOBAL}")
    show_budget("Cout unitaire retenu :")

    if not load_sol_hourly():
        log.warning("Prix du SOL horaire indisponible : les capitalisations "
                    "de la section D seront sautees.")

    tokens, provenance = pick_tokens(TOKENS_SECTION_B)
    if not tokens:
        log.error("Aucun token exploitable, sonde interrompue.")
        return

    results: dict[str, Any] = {"provenance": provenance}
    results["a"] = section_a(tokens[0]["pool_address"])
    results["b"] = section_b(tokens)
    results["c"] = section_c(tokens)
    results["d"] = section_d(
        (results["c"] or {}).get("days") or {},
        tokens,
        (results["b"] or {}).get("graduations") or {},
    )

    final_recap(results)


if __name__ == "__main__":
    main()
