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
| `main.py` | Point d'entree, dispatch via `RUN_MODE` |
| `find_winners.py` | Pipeline de discovery |
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
| `RUN_MODE` | `winners` (defaut) ou `probe` — voir Execution |
| `COINGECKO_API_KEY` | Cle Demo CoinGecko, envoyee en header `x-cg-demo-api-key` |
| `SUPABASE_URL` | URL du projet Supabase |
| `SUPABASE_KEY` | Cle Supabase avec droit d'ecriture sur `sol_analyzed_tokens` |

En local, un fichier `.env` (git-ignore) suffit. Au demarrage, chaque variable
est loguee `presente` ou `ABSENTE`, et la source est annoncee explicitement :
`CoinGecko Demo (cle detectee)` ou `keyless (MODE DEGRADE)`. Il n'y a jamais de
bascule silencieuse en mode degrade.

## Execution

Point d'entree unique : `main.py`, qui lit `RUN_MODE`.

```bash
RUN_MODE=winners python main.py   # defaut : pipeline de discovery
RUN_MODE=probe   python main.py   # sonde megafilter uniquement
```

| `RUN_MODE` | Effet |
| --- | --- |
| `winners` (defaut, valeur vide incluse) | pipeline de discovery |
| `probe` | sonde megafilter, le pipeline n'est pas lance |
| autre valeur | erreur explicite au demarrage, pas de repli silencieux |

> **Railway** : la Start Command doit etre `python main.py`. Lancer
> `python find_winners.py` fonctionne toujours mais execute *toujours* le
> pipeline winners — un `RUN_MODE=probe` y serait sans effet, et le script
> le signale par un warning au lieu de tourner silencieusement.

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
6. **OHLCV journalier 60 j** par candidat, deux metriques (voir plus bas).
   Moins de 3 bougies -> `ohlcv_insuffisant` ; `close` du 1er jour a zero ->
   `ohlcv_invalide`. Sans crash dans les deux cas.
7. **Upsert de tous les candidats analyses**, winners comme non-winners : ce
   sont les seconds qui alimentent la memoire.

Un OHLCV **perdu** (echec reseau apres retries) n'est pas ecrit en base : le
token sera retente au prochain run plutot qu'enterre dans la memoire.

Les tokens **ecartes en pre-filtre** (etapes 2 a 4) ne sont pas ecrits non
plus, et c'est volontaire : un token rejete aujourd'hui parce que son pool a
moins de `MIN_POOL_AGE_DAYS` sera eligible dans quelques jours. L'ecrire avec
un TTL de 60 jours l'enterrerait. Seuls les candidats reellement **analyses**
(OHLCV recupere) alimentent la memoire.

## Les deux mesures de performance

```
perf_x_launch = max(high) / premier open non nul
perf_x        = max(high des bougies d'index >= 1) / close de la bougie d'index 0
```

`perf_x_launch` est la mesure historique. Pour un token lance sur bonding
curve, le premier open est le prix de depart de la courbe, proche de zero :
la valeur est mecaniquement enorme et ne discrimine rien.

`perf_x` mesure ce qu'un acheteur entre a la fin du premier jour aurait pu
faire. **C'est elle qui determine `is_winner`**, et `peak_at` suit son pic.

Les deux sont ecrites en base pour permettre de comparer leur distribution
sur donnees reelles avant de fixer `WINNER_MULTIPLE`. `perf_x_launch` est
calculee des qu'elle est calculable, y compris quand `perf_x` ne l'est pas.

## Lire l'entonnoir de collecte

Chaque run logue une ligne par source avec l'age median des pools retournes,
puis une ligne de synthese du filtrage :

```
new_pools        : 200 pools, age median 0,8 j
filtrage : doublon_pool 120 | bruit 8 | age_trop_jeune 210 | age_trop_vieux 0 | ...
```

Les motifs sont mutuellement exclusifs et verifient l'invariant
`collectes = somme(motifs hors deja_analyse) + dedupliques`. Ventiler
`age_trop_jeune` et `age_trop_vieux` separement est le point cle : c'est ce
qui dit si la fenetre d'age est mal placee ou si les endpoints collectent a
cote de la cible.

## Table `sol_analyzed_tokens`

Colonnes attendues : `mint` (unique), `symbol`, `name`, `pool_address`,
`dex`, `pool_created_at`, `fdv_usd`, `liquidity_usd`, `volume_24h_usd`,
`perf_x`, `perf_x_launch`, `peak_at`, `is_winner`, `rejected_reason`,
`analyzed_at`.

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
