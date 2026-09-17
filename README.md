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
| `probe_sort.py` | Sonde jetable : le tri de `/pools` est-il applique ? |

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
RUN_MODE=probe   python main.py   # sonde de tri uniquement
```

| `RUN_MODE` | Effet |
| --- | --- |
| `winners` (defaut, valeur vide incluse) | pipeline de discovery |
| `probe` | sonde de tri, le pipeline n'est pas lance |
| autre valeur | erreur explicite au demarrage, pas de repli silencieux |

> **Railway** : la Start Command doit etre `python main.py`. Lancer
> `python find_winners.py` fonctionne toujours mais execute *toujours* le
> pipeline winners — un `RUN_MODE=probe` y serait sans effet, et le script
> le signale par un warning au lieu de tourner silencieusement.

## Pipeline

1. **Collecte** — 12 sources, 101 appels (~3,5 min de throttle) :
   `trending_pools` sur les 4 durees (5m, 1h, 6h, 24h), p. 1-5 chacune ;
   puis les pools de chaque DEX retenu en `sort=h24_volume_usd_desc`,
   p. 1-10. Jamais de page > 10 : la pagination au dela est reservee aux
   plans payants et le client refuse l'appel.
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

## Pourquoi cette topologie de collecte

La collecte se recalibre a chaque run sur l'**age median mesure par
source** : une source dont la mediane est structurellement hors de la
fenetre 7-60 j est retiree, pas ajustee.

Mesures du run du 17/09 (1400 pools) :

| Source | Age median | Decision |
| --- | --- | --- |
| `dex_orca` | 404,1 j | retiree |
| `trending_1h` | 87,8 j | conservee, marginale |
| `dex_meteora` | 43,3 j | conservee |
| `trending_6h` | 39,5 j | conservee |
| `trending_24h` | 28,6 j | conservee |
| `dex_raydium` | 5,9 j | conservee |
| `trending_5m` | 1,6 j | conservee, marginale |
| `pools_volume` | 0,4 j | retiree |
| `dex_pumpswap` | 0,3 j | retiree |
| `new_pools` (run precedent) | 0,0 j | retiree |

Retirer `pools_volume`, `dex_pumpswap` et `dex_orca` libere 30 appels,
reinvestis dans six DEX supplementaires a mesurer : `raydium-clmm`,
`meteora-damm-v2`, `meteora-dbc`, `bags-fm`, `heaven`, `boop-fun`. Aucune
hypothese sur leur productivite — c'est leur age median au prochain run qui
tranchera.

`pools_volume` a rendu le meme age median (0,4 j) avec et sans
`sort=h24_volume_usd_desc`, d'ou le soupcon que le parametre de tri est
ignore. C'est ce que mesure `RUN_MODE=probe` (voir plus bas).

Les identifiants de DEX **ne sont pas codes en dur** : `resolve_dexes()`
appelle `/networks/solana/dexes` au demarrage et ne retient que les ids
reellement presents dans la reponse, en **correspondance exacte**. Un DEX
souhaite mais absent est ignore avec un warning, jamais remplace par un id
approchant : un repli substituerait silencieusement un autre DEX a celui
qu'on veut mesurer. Si l'appel echoue, la collecte par DEX est desactivee
pour le run et les autres sources continuent.

## La sonde `RUN_MODE=probe`

`probe_sort.py` repond a une seule question : le parametre de tri de
`/networks/solana/pools` est-il applique ? Trois appels sur `page=1`, sans
tri, avec `sort=`, avec `order=`. Pour chacun : premiere adresse, derniere
adresse, age median de la page. Meme premiere adresse partout -> le tri est
ignore, et c'est logue comme tel. La comparaison des sequences completes
sert de preuve plus forte que la seule premiere adresse.

Elle remplace l'ancienne sonde megafilter : cet endpoint est reserve aux
plans payants et n'est pas exploitable sur la cle Demo.

La deduplication par mint devient critique ici : les 9 sources se recoupent
largement. Le pool le plus liquide de chaque token est conserve, les autres
sont comptes en `doublon_pool`.

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
  a 30 req/min et **partagee** avec l'agent ETH. Ne pas reduire l'intervalle :
  la collecte coute 101 appels, plus un appel OHLCV par candidat retenu.
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
