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
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from supabase import Client, create_client

from config import ANALYZED_TABLE

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
