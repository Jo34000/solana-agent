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

# --- Phase 2 : extraction des early buyers -------------------------------

# AJUSTABLE - transactions remontees par mint (voie A, ordre ascendant).
EARLY_TX_LIMIT = 200

# AJUSTABLE - rang maximum pour qu'un achat compte comme "early".
EARLY_BUYER_MAX_RANK = 50

# AJUSTABLE - nombre de winners distincts a partir duquel un wallet est
# active par recoupement.
ACTIVATION_MIN_WINNERS = 2

# AJUSTABLE - rang a partir duquel un wallet est active sur un seul winner.
ACTIVATION_TOP_RANK = 20

# --- Phase 3 : backtest de validation ------------------------------------

# AJUSTABLE - perimetre des candidats a backtester.
VALIDATION_MIN_WINNERS = 2

# AJUSTABLE - transactions remontees par wallet (historique recent).
VALIDATION_TX_LIMIT = 500

# AJUSTABLE - minimum de tokens mesures pour oser statuer sur un wallet.
VALIDATION_MIN_TOKENS = 3

# AJUSTABLE - seuils de validation.
VALIDATION_MIN_WIN_RATE = 0.40
VALIDATION_MAX_RUG_RATE = 0.50

# AJUSTABLE - plafond haut d'activite : au-dela, bot ou MEV, exclu. Il n'y a
# volontairement PAS de plancher : cote ETH, exclure les wallets a faible
# historique avait elimine exactement les traders experimentes recherches.
VALIDATION_MAX_TX = 50_000

# AJUSTABLE - cap applique a chaque performance AVANT toute mediane : un x300
# isole ne doit pas porter le verdict d'un wallet.
PERF_CAP = 20.0

# AJUSTABLE - sous cette liquidite, le token est considere comme rugge. Il
# est COMPTE dans le backtest, jamais ecarte : c'est la perte qu'on cherche
# precisement a mesurer.
MIN_POOL_LIQUIDITY_USD = 5_000

# AJUSTABLE - echantillon de tokens mesures par wallet. Ce sont les plus
# RECENTS des achats MATURES qui sont gardes, jamais les plus performants :
# trier sur le gain biaiserait le win rate.
VALIDATION_MAX_TOKENS_PER_WALLET = 30

# AJUSTABLE - profondeur d'historique visee par la pagination de la voie A.
# Mesure du 18/09 : 500 transactions couvrent 3 HEURES chez un wallet tres
# actif. Sans pagination, la fenetre ou se trouvent les winners n'est jamais
# atteinte.
VALIDATION_TARGET_AGE_DAYS = 45

# AJUSTABLE - garde-fou : nombre maximum de pages lues par wallet.
VALIDATION_MAX_PAGES = 20

# AJUSTABLE - age minimum d'un achat pour etre mesurable. En dessous, deux
# biais se cumulent : le token n'a pas eu le temps de performer, et un
# lancement pump.fun trop recent n'est pas encore indexe par GeckoTerminal,
# donc classe MORT a tort.
VALIDATION_MIN_TOKEN_AGE_DAYS = 10

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
EARLY_BUYS_TABLE = "sol_early_buys"
SMART_WALLETS_TABLE = "sol_smart_wallets"

# Seuil de performance a partir duquel un token compte comme gagnant dans le
# backtest (x2 sur le prix d'entree).
VALIDATION_WIN_MULTIPLE = 2.0

ENV_VARS = (
    "COINGECKO_API_KEY",
    "HELIUS_API_KEY",
    "SUPABASE_URL",
    "SUPABASE_KEY",
)

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
    log.info("--- Early buyers : %d tx/mint | early <= rang %d | "
             "activation >= %d winners ou rang <= %d ---",
             EARLY_TX_LIMIT, EARLY_BUYER_MAX_RANK,
             ACTIVATION_MIN_WINNERS, ACTIVATION_TOP_RANK)
    log.info("--- Validation : %d tx/page, %d pages max, profondeur visee "
             "%d j | fenetre mesuree %d-%d j | >= %d tokens | win rate >= %s "
             "| rug rate <= %s | cap x%s ---",
             VALIDATION_TX_LIMIT, VALIDATION_MAX_PAGES,
             VALIDATION_TARGET_AGE_DAYS, VALIDATION_MIN_TOKEN_AGE_DAYS,
             VALIDATION_TARGET_AGE_DAYS, VALIDATION_MIN_TOKENS,
             VALIDATION_MIN_WIN_RATE, VALIDATION_MAX_RUG_RATE, PERF_CAP)
    return has_key
