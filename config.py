"""Configuration centrale et diagnostic de démarrage.

Tous les seuils de filtrage vivent ici : ils seront recalibres sur donnees
reelles apres les premiers runs. Aucun secret n'est stocke dans ce fichier.
"""

from __future__ import annotations

import logging
import os
import sys

try:  # le .env est pratique en local, absent sur Railway
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dependance optionnelle a l'execution
    pass

# --------------------------------------------------------------------------
# PARAMETRES AJUSTABLES
# Ce ne sont PAS des constantes definitives : ils servent de point de depart
# et devront etre recalibres a partir des donnees collectees.
# --------------------------------------------------------------------------

# AJUSTABLE - age minimum du pool : en dessous, pas assez d'historique OHLCV
# pour juger d'une performance.
MIN_POOL_AGE_DAYS = 7

# AJUSTABLE - age maximum du pool : au dela, les early buyers sont trop loin
# dans le passe pour etre exploitables.
MAX_POOL_AGE_DAYS = 60

# AJUSTABLE - liquidite ACTUELLE minimale. Filtre anti-rug : un rug a une
# liquidite proche de zero aujourd'hui.
MIN_LIQUIDITY_USD = 25_000

# AJUSTABLE - volume 24h ACTUEL minimal. Second volet du filtre anti-rug :
# un token mort ne s'echange plus.
MIN_VOLUME_24H_USD = 10_000

# AJUSTABLE - multiple a partir duquel un token est considere comme winner.
WINNER_MULTIPLE = 5.0

# AJUSTABLE - duree de memoire : un mint analyse il y a moins de N jours
# n'est pas re-analyse.
ANALYZED_TTL_DAYS = 60

# --------------------------------------------------------------------------
# Constantes techniques (non ajustables a la volee)
# --------------------------------------------------------------------------

NETWORK = "solana"
GECKOTERMINAL_BASE_URL = "https://api.coingecko.com/api/v3/onchain"

# La cle Demo est plafonnee a 30 req/min ET partagee avec un autre service :
# on reste volontairement sous la moitie du plafond.
MIN_REQUEST_INTERVAL_S = 2.1
MAX_RETRIES = 3

ANALYZED_TABLE = "sol_analyzed_tokens"

ENV_VARS = ("COINGECKO_API_KEY", "SUPABASE_URL", "SUPABASE_KEY")

log = logging.getLogger("solana-agent")


def setup_logging(level: int = logging.INFO) -> None:
    """Configure un format de log compact et lisible."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def coingecko_api_key() -> str | None:
    key = os.environ.get("COINGECKO_API_KEY", "").strip()
    return key or None


def diagnose_environment() -> bool:
    """Logge l'etat de chaque variable d'environnement attendue.

    Retourne True si la cle CoinGecko est presente (mode nominal), False si
    l'on tourne en keyless. Le mode degrade est toujours annonce
    explicitement : jamais de bascule silencieuse.
    """
    log.info("--- Diagnostic environnement ---")
    for name in ENV_VARS:
        present = bool(os.environ.get(name, "").strip())
        log.info("  %-18s : %s", name, "presente" if present else "ABSENTE")

    has_key = coingecko_api_key() is not None
    if has_key:
        log.info("Source : CoinGecko Demo (cle detectee)")
    else:
        log.warning("Source : keyless (MODE DEGRADE)")
        log.warning(
            "  -> quota tres bas et endpoints restreints, les resultats "
            "seront partiels."
        )
    log.info("--- Parametres : age %d-%d j | liq >= %s$ | vol24h >= %s$ | "
             "winner >= x%s | ttl %d j ---",
             MIN_POOL_AGE_DAYS, MAX_POOL_AGE_DAYS,
             f"{MIN_LIQUIDITY_USD:,}", f"{MIN_VOLUME_24H_USD:,}",
             WINNER_MULTIPLE, ANALYZED_TTL_DAYS)
    return has_key
