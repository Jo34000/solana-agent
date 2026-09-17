# solana-agent

Detection de wallets Solana performants, puis alerte Telegram quand plusieurs
d'entre eux achetent le meme token a faible capitalisation.

Ce repo est construit par briques. **Phase 1 (seule livree a ce jour)** :
trouver les tokens Solana "winners" (x5 ou plus depuis leur lancement) qui
serviront ensuite a identifier les wallets.

## Fichiers

| Fichier | Role |
| --- | --- |
| `config.py` | Seuils AJUSTABLES + diagnostic des variables d'environnement |
| `geckoterminal.py` | Client HTTP CoinGecko onchain (rate limit, retry, pertes explicites) |
| `supabase_client.py` | Lecture / upsert sur `sol_analyzed_tokens` |
| `find_winners.py` | Pipeline de discovery (point d'entree) |
| `probe_megafilter.py` | Sonde jetable sur `/pools/megafilter`, independante du pipeline |

## Installation

```bash
python -m venv .venv && source .venv/bin/activate   # Python 3.13
pip install -r requirements.txt
```

## Variables d'environnement

Aucun secret n'est versionne. Trois variables sont lues via `os.environ` :

| Variable | Usage |
| --- | --- |
| `COINGECKO_API_KEY` | Cle Demo CoinGecko, envoyee en header `x-cg-demo-api-key` |
| `SUPABASE_URL` | URL du projet Supabase |
| `SUPABASE_KEY` | Cle Supabase avec droit d'ecriture sur `sol_analyzed_tokens` |

En local, un fichier `.env` (git-ignore) suffit. Au demarrage, chaque variable
est loguee `presente` ou `ABSENTE`, et la source est annoncee explicitement :
`CoinGecko Demo (cle detectee)` ou `keyless (MODE DEGRADE)`. Il n'y a jamais de
bascule silencieuse en mode degrade.

## Execution

```bash
python find_winners.py      # pipeline de discovery
python probe_megafilter.py  # sonde exploratoire, un seul appel API
```

## Pipeline

1. **Collecte** — `new_pools` (p. 1-10), `trending_pools` (p. 1-5), `pools` (p. 1-10).
2. **Deduplication par mint**, pas par pool : un token a souvent plusieurs
   pools, on garde le plus liquide.
3. **Exclusion du bruit** : SOL, wSOL, USDC, USDT et les LST (JitoSOL, mSOL,
   bSOL, INF).
4. **Pre-filtres** sur le payload de liste, sans appel supplementaire : age du
   pool, liquidite actuelle, volume 24h actuel. Les deux derniers forment le
   filtre anti-rug — un rug a une liquidite a zero aujourd'hui. Aucun filtre
   sur le drawdown : sur Solana un vrai winner fait -85% en routine.
5. **Memoire** : les mints analyses depuis moins de `ANALYZED_TTL_DAYS` sont
   ecartes, sinon chaque run reanalyse les memes tokens.
6. **OHLCV journalier 60 j** par candidat :
   `perf_x = max(high) / premier open non nul`, `peak_at` = date du max.
   OHLCV vide, open a zero ou moins de 3 bougies -> `rejected_reason =
   "ohlcv_invalide"`, sans crash.
7. **Upsert de tous les candidats analyses**, winners comme non-winners : ce
   sont les seconds qui alimentent la memoire.

Un OHLCV **perdu** (echec reseau apres retries) n'est pas ecrit en base : le
token sera retente au prochain run plutot qu'enterre dans la memoire.

## Table `sol_analyzed_tokens`

Colonnes attendues : `mint` (unique), `symbol`, `name`, `pool_address`,
`dex`, `pool_created_at`, `fdv_usd`, `liquidity_usd`, `volume_24h_usd`,
`perf_x`, `peak_at`, `is_winner`, `rejected_reason`, `analyzed_at`.

`dex`, `pool_created_at` et `fdv_usd` viennent du payload des endpoints de
liste (aucun appel supplementaire) et sont ecrites pour **tous** les
candidats analyses, winners comme rejetes : elles serviront a calibrer la
fenetre de mcap cible et le seuil d'age. Ces colonnes etant nullables, un
payload incomplet passerait l'upsert sans erreur — le run logue donc un
avertissement quand `dex` ou `fdv_usd` manquent. Un `fdv_usd` absent est
ecrit `NULL`, jamais `0`, pour ne pas fausser la calibration.

## Contraintes de conception

- **Rate limit** : 2,1 s minimum entre deux appels. La cle Demo est plafonnee
  a 30 req/min et **partagee** avec un autre service.
- **Pertes explicites** : tout appel abandonne apres retries logue
  `PERTE : <endpoint> abandonne apres N tentatives` et retourne `None`. Jamais
  de liste vide silencieuse.
- **Ecritures verifiees** : aucune exception d'ecriture n'est avalee, et une
  ecriture partielle leve. Un `except` qui retourne `[]` fait croire a un
  succes alors que la table (RLS) reste vide.
- **Logs** : une synthese par etape, jamais une ligne par enregistrement.
- **Aucun etat sur le filesystem** : Railway est ephemere, tout ce qui doit
  survivre va en base.

## Objectif de volume

Cible : **50 winners ou plus**. Sur Ethereum, 7 winners avaient donne zero
recoupement entre early buyers, ce qui bloquait toute la suite. Si le run
termine sous la cible, il logue un avertissement : relancer, ou assouplir les
seuils AJUSTABLES de `config.py`.
