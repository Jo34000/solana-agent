"""Acces Supabase a la table sol_analyzed_tokens.

Regle non negociable : aucune exception d'ecriture n'est avalee. Un except
qui retourne [] fait croire a un succes alors que la table (RLS, colonne
manquante, cle en lecture seule) est restee vide. Ici, ca leve.

Colonnes attendues cote Supabase :
    mint            text  (cle primaire / unique)
    symbol          text
    name            text
    pool_address    text
    dex             text         (identifiant DEX : raydium, pumpswap...)
    pool_created_at timestamptz  (date de creation du pool retenu)
    fdv_usd         numeric      (NULL si absent du payload, jamais 0)
    liquidity_usd   numeric
    volume_24h_usd  numeric
    perf_x          numeric      (entree fin du 1er jour -> pic ulterieur)
    perf_x_launch   numeric      (open de lancement -> pic, pour comparaison)
    peak_at         timestamptz
    is_winner       boolean
    rejected_reason text
    analyzed_at     timestamptz
    buyers_extracted_at timestamptz  (phase 2 : NULL = a traiter)

Colonnes attendues sur sol_early_buys :
    mint         text     -- contrainte unique sur le couple (mint, wallet)
    wallet       text
    buy_rank     integer  (1, 2, 3... par ordre d'apparition)
    is_bundle    boolean  (transaction dans le slot de lancement)
    sol_amount   numeric  (NULL si non determinable)
    signature    text
    slot         bigint
    block_time   timestamptz
    extracted_at timestamptz

Colonnes attendues sur sol_smart_wallets :
    wallet            text  (cle primaire / unique)
    winners_count     integer
    winner_tokens     jsonb ou text[]
    best_rank         integer
    active            boolean
    activation_reason text
    updated_at        timestamptz
    -- phase 3, backtest de validation :
    validated_at      timestamptz  (NULL = a backtester)
    tokens_evaluated  integer
    win_rate          numeric
    median_perf       numeric
    rug_rate          numeric
    -- phase 3 bis, backtest v2 sur prix d'entree reel :
    median_raw_perf   numeric  (avant cap)
    median_winner_x   numeric  (mediane des seuls gagnants)
    median_loser_x    numeric  (mediane des seuls perdants)
    -- phase 3 ter, PnL realise en SOL :
    positions_fermees int
    positions_ouvertes int
    win_rate_reel     numeric  (part des positions fermees a pnl_x > 1)
    median_pnl_x      numeric
    median_gagnant_x  numeric
    median_perdant_x  numeric
    sol_investi       numeric
    sol_recupere      numeric
    pnl_global_x      numeric  (sol_recupere / sol_investi, fermees seules)

Colonnes attendues sur sol_run_log, journal des sondes :
    run_mode text         (probe_universe_v3, ...)
    run_at   timestamptz  (horodatage du run, identique pour toutes ses lignes)
    section  text         (A, B, C, D, E, run)
    label    text         (intitule court de la mesure)
    payload  jsonb        (la mesure elle-meme, relisable par le run suivant)

    create table if not exists sol_run_log (
      id       bigserial primary key,
      run_mode text not null,
      run_at   timestamptz not null default now(),
      section  text,
      label    text,
      payload  jsonb
    );

C'est la SEULE table qu'une sonde ecrit. Elle existe parce qu'une sonde qui
n'ecrit rien perd ses acquis : le 20/09, les comptes recurrents trouves
n'etaient plus nulle part au ticket suivant.

Colonnes attendues sur sol_grad_paths, trajectoires des graduations :
    mint           text  (cle primaire)
    pool           text  (owner valide par la regle des sondes)
    signature      text
    signer         text  (feePayer de la migration)
    grad_at        timestamptz
    jour           date
    status         text  (mesure, variante_non_couverte, ...)
    stage          int   (1 = trois premiers instants, 2 = trajectoire)
    supply         numeric
    points         jsonb (par instant : prix_sol, mcap_usd, actif)
    mcap_max_usd   numeric
    points_actifs  int
    points_mesures int
    updated_at     timestamptz

    create table if not exists sol_grad_paths (
      mint           text primary key,
      pool           text,
      signature      text,
      signer         text,
      grad_at        timestamptz,
      jour           date,
      status         text,
      stage          int,
      supply         numeric,
      points         jsonb,
      mcap_max_usd   numeric,
      points_actifs  int,
      points_mesures int,
      updated_at     timestamptz default now()
    );

Un token sans echange autour d'un instant n'est pas une perte : son point
vaut null avec actif = false. C'est une mesure, et elle compte.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from supabase import Client, create_client

from config import (
    ANALYZED_TABLE,
    EARLY_BUYS_TABLE,
    GRAD_PATHS_TABLE,
    RUN_LOG_TABLE,
    SMART_WALLETS_TABLE,
)

log = logging.getLogger("solana-agent")

_PAGE_SIZE = 1000
_client: Client | None = None


def get_client() -> Client:
    """Client Supabase memoise. Leve si les variables manquent."""
    global _client
    if _client is not None:
        return _client

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL et/ou SUPABASE_KEY absentes : impossible d'ecrire "
            "en base, arret."
        )
    _client = create_client(url, key)
    return _client


def fetch_recent_mints(ttl_days: int) -> set[str]:
    """Mints analyses il y a moins de ttl_days. Leve en cas d'erreur."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat()
    mints: set[str] = set()
    offset = 0

    while True:
        response = (
            get_client()
            .table(ANALYZED_TABLE)
            .select("mint")
            .gte("analyzed_at", cutoff)
            .range(offset, offset + _PAGE_SIZE - 1)
            .execute()
        )
        rows = response.data or []
        mints.update(row["mint"] for row in rows if row.get("mint"))
        if len(rows) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE

    log.info(
        "Supabase : %d mints deja analyses dans les %d derniers jours",
        len(mints), ttl_days,
    )
    return mints


def upsert_analyzed_tokens(rows: list[dict]) -> int:
    """Upsert par mint. Retourne le nombre de lignes confirmees par la base.

    Leve si l'ecriture echoue, et alerte si la base confirme moins de lignes
    que demande (symptome typique d'une policy RLS silencieuse).
    """
    if not rows:
        log.info("Supabase : aucune ligne a ecrire dans %s", ANALYZED_TABLE)
        return 0

    response = (
        get_client()
        .table(ANALYZED_TABLE)
        .upsert(rows, on_conflict="mint")
        .execute()
    )
    written = len(response.data or [])
    log.info(
        "Supabase : %d/%d lignes upsertees dans %s",
        written, len(rows), ANALYZED_TABLE,
    )
    if written < len(rows):
        raise RuntimeError(
            f"Ecriture partielle dans {ANALYZED_TABLE} : {written} confirmees "
            f"sur {len(rows)} envoyees (verifier les policies RLS)."
        )
    return written


def fetch_winners(limit: int = 500) -> list[dict]:
    """Winners deja enregistres, du plus performant au moins performant."""
    response = (
        get_client()
        .table(ANALYZED_TABLE)
        .select("mint, symbol, perf_x, peak_at, liquidity_usd, pool_address")
        .eq("is_winner", True)
        .order("perf_x", desc=True)
        .limit(limit)
        .execute()
    )
    rows = response.data or []
    log.info("Supabase : %d winners en base", len(rows))
    return rows


# ---------------------------------------------------------------------------
# Phase 2 : early buyers
# ---------------------------------------------------------------------------


def fetch_winners_to_process(limit: int = 1000) -> list[dict]:
    """Winners dont les acheteurs n'ont pas encore ete extraits."""
    response = (
        get_client()
        .table(ANALYZED_TABLE)
        .select("mint, symbol, peak_at")
        .eq("is_winner", True)
        .is_("buyers_extracted_at", "null")
        .limit(limit)
        .execute()
    )
    rows = response.data or []
    log.info("Supabase : %d winners a traiter", len(rows))
    return rows


def insert_early_buys(rows: list[dict]) -> int:
    """Premier achat par (mint, wallet). Un rang existant n'est PAS ecrase.

    ignore_duplicates : les lignes deja presentes sont ignorees cote base,
    donc moins de lignes confirmees que de lignes envoyees est NORMAL ici
    et ne doit pas lever.
    """
    if not rows:
        log.info("Supabase : aucun early buy a ecrire")
        return 0

    response = (
        get_client()
        .table(EARLY_BUYS_TABLE)
        .upsert(rows, on_conflict="mint,wallet", ignore_duplicates=True)
        .execute()
    )
    written = len(response.data or [])
    log.info(
        "Supabase : %d nouvelles lignes dans %s (%d envoyees, le reste deja "
        "connu)", written, EARLY_BUYS_TABLE, len(rows),
    )
    return written


def mark_buyers_extracted(mint: str) -> None:
    """Marque un token comme traite. A n'appeler qu'en l'absence de PERTE."""
    now = datetime.now(timezone.utc).isoformat()
    (
        get_client()
        .table(ANALYZED_TABLE)
        .update({"buyers_extracted_at": now})
        .eq("mint", mint)
        .execute()
    )


def fetch_early_buys(max_rank: int) -> list[dict]:
    """Tous les achats early hors bundle, tous runs confondus.

    C'est cette lecture globale qui fait l'accumulation : un wallet vu sur
    un winner cette semaine et sur un autre la semaine prochaine voit son
    winners_count monter.
    """
    rows: list[dict] = []
    offset = 0
    while True:
        response = (
            get_client()
            .table(EARLY_BUYS_TABLE)
            .select("mint, wallet, buy_rank")
            .eq("is_bundle", False)
            .lte("buy_rank", max_rank)
            .range(offset, offset + _PAGE_SIZE - 1)
            .execute()
        )
        page = response.data or []
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE

    log.info(
        "Supabase : %d achats early (hors bundle, rang <= %d) en base",
        len(rows), max_rank,
    )
    return rows


def upsert_smart_wallets(rows: list[dict]) -> int:
    """Remplace l'etat de chaque wallet. Leve si l'ecriture est partielle."""
    if not rows:
        log.info("Supabase : aucun wallet a ecrire")
        return 0

    written = 0
    for start in range(0, len(rows), _PAGE_SIZE):
        batch = rows[start:start + _PAGE_SIZE]
        response = (
            get_client()
            .table(SMART_WALLETS_TABLE)
            .upsert(batch, on_conflict="wallet")
            .execute()
        )
        written += len(response.data or [])

    log.info(
        "Supabase : %d/%d wallets upsertes dans %s",
        written, len(rows), SMART_WALLETS_TABLE,
    )
    if written < len(rows):
        raise RuntimeError(
            f"Ecriture partielle dans {SMART_WALLETS_TABLE} : {written} "
            f"confirmees sur {len(rows)} envoyees (verifier les policies RLS)."
        )
    return written


# ---------------------------------------------------------------------------
# Phase 3 : backtest de validation
# ---------------------------------------------------------------------------


def fetch_wallets_to_validate(min_winners: int, limit: int = 2000) -> list[dict]:
    """Candidats au backtest : assez de winners, pas encore valides."""
    response = (
        get_client()
        .table(SMART_WALLETS_TABLE)
        .select("wallet, winners_count, best_rank")
        .gte("winners_count", min_winners)
        .is_("validated_at", "null")
        .order("winners_count", desc=True)
        .limit(limit)
        .execute()
    )
    rows = response.data or []
    log.info(
        "Supabase : %d wallets a backtester (winners_count >= %d)",
        len(rows), min_winners,
    )
    return rows


def update_wallet_validation(rows: list[dict]) -> int:
    """Ecrit le verdict du backtest. Leve si l'ecriture est partielle."""
    if not rows:
        log.info("Supabase : aucun verdict de validation a ecrire")
        return 0

    written = 0
    for start in range(0, len(rows), _PAGE_SIZE):
        batch = rows[start:start + _PAGE_SIZE]
        response = (
            get_client()
            .table(SMART_WALLETS_TABLE)
            .upsert(batch, on_conflict="wallet")
            .execute()
        )
        written += len(response.data or [])

    log.info(
        "Supabase : %d/%d verdicts ecrits dans %s",
        written, len(rows), SMART_WALLETS_TABLE,
    )
    if written < len(rows):
        raise RuntimeError(
            f"Ecriture partielle dans {SMART_WALLETS_TABLE} : {written} "
            f"confirmees sur {len(rows)} envoyees (verifier les policies RLS)."
        )
    return written


# ---------------------------------------------------------------------------
# Journal des sondes
# ---------------------------------------------------------------------------


def insert_run_log(run_mode: str, run_at: str, section: str, label: str,
                   payload: dict) -> None:
    """Ecrit une mesure dans sol_run_log. Leve si la base ne confirme rien.

    Une sonde qui perd ses acquis fait recommencer le ticket suivant a
    zero : cette ecriture est la seule que les sondes s'autorisent, et son
    echec n'est jamais avale.
    """
    response = (
        get_client()
        .table(RUN_LOG_TABLE)
        .insert({
            "run_mode": run_mode,
            "run_at": run_at,
            "section": section,
            "label": label,
            "payload": payload,
        })
        .execute()
    )
    if not (response.data or []):
        raise RuntimeError(
            f"Ecriture non confirmee dans {RUN_LOG_TABLE} (section {section}, "
            f"{label}) : verifier que la table existe et ses policies RLS."
        )


def fetch_run_log(run_mode: str, section: str, limit: int = 5) -> list[dict]:
    """Dernieres mesures d'une sonde, la plus recente d'abord.

    C'est la raison d'etre de sol_run_log : une sonde relit ce que la
    precedente a etabli au lieu de le repayer.
    """
    response = (
        get_client()
        .table(RUN_LOG_TABLE)
        .select("run_at, section, label, payload")
        .eq("run_mode", run_mode)
        .eq("section", section)
        .order("run_at", desc=True)
        .limit(limit)
        .execute()
    )
    rows = response.data or []
    log.info("Supabase : %d ligne(s) relues dans %s (%s / section %s)",
             len(rows), RUN_LOG_TABLE, run_mode, section)
    return rows


# ---------------------------------------------------------------------------
# Trajectoires des graduations
# ---------------------------------------------------------------------------


def upsert_grad_path(row: dict) -> None:
    """Ecrit UNE trajectoire. Leve si la base ne confirme rien.

    L'ecriture est faite token par token, au fil de l'eau : un arret au
    plafond de credits ne perd rien de ce qui precede.
    """
    response = (
        get_client()
        .table(GRAD_PATHS_TABLE)
        .upsert(row, on_conflict="mint")
        .execute()
    )
    if not (response.data or []):
        raise RuntimeError(
            f"Ecriture non confirmee dans {GRAD_PATHS_TABLE} pour "
            f"{row.get('mint')} : verifier que la table existe et ses "
            f"policies RLS."
        )


def fetch_grad_paths(days: list[str]) -> list[dict]:
    """Toutes les lignes deja ecrites pour ces journees."""
    if not days:
        return []
    rows: list[dict] = []
    start = 0
    while True:
        response = (
            get_client()
            .table(GRAD_PATHS_TABLE)
            .select("mint, pool, signature, signer, grad_at, jour, status, "
                    "stage, supply, points, mcap_max_usd, points_actifs, "
                    "points_mesures")
            .in_("jour", days)
            .range(start, start + _PAGE_SIZE - 1)
            .execute()
        )
        page = response.data or []
        rows += page
        if len(page) < _PAGE_SIZE:
            break
        start += _PAGE_SIZE
    log.info("Supabase : %d trajectoire(s) deja en base sur %d journee(s)",
             len(rows), len(days))
    return rows
