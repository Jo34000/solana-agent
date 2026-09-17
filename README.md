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
| `probe_helius.py` | Sonde jetable : forme des reponses Helius (phase 2) |

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
| `HELIUS_API_KEY` | Cle Helius — requise par `RUN_MODE=probe_helius` uniquement |
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
RUN_MODE=probe   python main.py   # sonde de tri (CoinGecko)
RUN_MODE=probe_helius python main.py   # sonde Helius (phase 2)
```

| `RUN_MODE` | Effet |
| --- | --- |
| `winners` (defaut, valeur vide incluse) | pipeline de discovery |
| `probe` | sonde de tri CoinGecko, le pipeline n'est pas lance |
| `probe_helius` | sonde Helius, le pipeline n'est pas lance |
| autre valeur | erreur explicite au demarrage, pas de repli silencieux |

> **Railway** : la Start Command doit etre `python main.py`. Lancer
> `python find_winners.py` fonctionne toujours mais execute *toujours* le
> pipeline winners — un `RUN_MODE=probe` y serait sans effet, et le script
> le signale par un warning au lieu de tourner silencieusement.

## Pipeline

1. **Collecte** — 6 sources, 61 appels (~2,1 min de throttle) :
   `trending_pools` sur 3 durees (1h, 6h, 24h), p. 1-10 chacune ; puis les
   pools de 3 DEX en `sort=h24_volume_usd_desc`, p. 1-10. Jamais de
   page > 10 : la pagination au dela est reservee aux plans payants et le
   client refuse l'appel.
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

La collecte a ete calibree en trois iterations sur l'**age median mesure par
source** : une source dont la mediane est structurellement hors de la
fenetre 7-60 j est retiree, pas ajustee. Etat arrete au run du 17/09 19:18.

**Sources conservees**, 10 pages chacune :

| Source | Age median |
| --- | --- |
| `trending_1h` | 49,9 j |
| `dex_meteora` | 41,7 j |
| `trending_6h` | 21,0 j |
| `trending_24h` | 21,0 j |
| `dex_raydium-clmm` | 15,9 j |
| `dex_raydium` | 5,3 j |

**Sources retirees**, avec l'age median qui les a disqualifiees :

| Source | Age median | |
| --- | --- | --- |
| `dex_boop-fun` | 504,0 j | 17 pools seulement |
| `dex_orca` | 404,1 j | |
| `dex_bags-fm` | 3,2 j | |
| `pools_volume` | 0,4 j | le tri par volume n'y a rien change |
| `dex_meteora-damm-v2` | 0,3 j | |
| `dex_meteora-dbc` | 0,3 j | |
| `dex_pumpswap` | 0,3 j | |
| `trending_5m` | 0,1 j | |
| `new_pools` | 0,0 j | reviendra pour une logique d'accumulation |

Le budget passe de 101 a 61 appels, et les trois sources `trending_*`
doublent de 5 a 10 pages avec les appels liberes.

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

## La sonde `RUN_MODE=probe_helius`

Premiere brique de la **phase 2** : retrouver les premiers acheteurs des
winners de la phase 1. `probe_helius.py` ne fait que sonder — aucune
ecriture en base, aucun parsing metier, aucune notion d'acheteur ni de rang.

### Acquis du run du 17/09 20:22

- `getTransactionsForAddress` (JSON-RPC) repond en HTTP 200 et renvoie
  `result = {"data": [...]}`, **pas une liste**. La sonde testait
  `isinstance(result, list)` et concluait a un echec sur des donnees
  presentes : bug corrige.
- **L'ordre ascendant est confirme** : `blockTime` et `slot` croissants.
  C'est ce qui rend cette voie exploitable pour des early buyers.
  (`transactionIndex` se remet a zero a chaque slot : il est croissant
  dans un slot, pas d'un slot a l'autre.)
- Les elements renvoyes ne portent que `signature`, `slot`,
  `transactionIndex`, `err`, `memo`, `blockTime`, `confirmationStatus` :
  pas de `tokenTransfers`. **Une etape d'enrichissement est necessaire.**
- La voie REST par adresse (`/v0/addresses/{addr}/transactions`) renvoyait
  l'ordre **descendant** : inutilisable ici. Elle n'est plus testee.

### Ce que la sonde fait maintenant

| Voie | Objet |
| --- | --- |
| A | `getTransactionsForAddress`, corps de requete logue, ordre verifie |
| A-bis | la meme methode peut-elle rendre les transactions completes ? |
| C | enrichissement par signature, `POST /v0/transactions` |
| D | transactions partageant le slot de la premiere |

La voie A-bis essaie plusieurs noms de parametre (`encoding`,
`transactionDetails`, `showTransactionDetails`). **Aucun n'est certain** :
un rejet est une information, et le message brut de Helius donne le nom
correct. Chaque variante logue la config envoyee, le code HTTP et, si elle
passe, les cles obtenues au-dela des metadonnees.

La voie C prend les 5 premieres signatures avec `err == null` et les poste
a l'API Enhanced Transactions. Elle logue la structure complete d'une
transaction (JSON indente, tronque a 4000 caracteres) puis nomme les champs
reperes : signataire, transferts avec les champs portant source,
destination, montant et mint, programme et type.

Le recapitulatif final dit `donnees exploitables` en fonction de la
**presence de donnees**, jamais du type Python renvoye par l'API — c'est
exactement ce qui avait produit le faux negatif.

`HELIUS_API_KEY` absente dans ce mode : la sonde **leve**, elle ne continue
pas. La cle n'apparait jamais dans les logs, URL et corps de requete sont
masques. Le throttle Helius (0,5 s) est **dedie** et independant de celui
de CoinGecko : free tier a 10 req/s, la sonde fait une dizaine d'appels.
