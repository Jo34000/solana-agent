# solana-agent

Detection de wallets Solana performants, puis alerte Telegram quand plusieurs
d'entre eux achetent le meme token a faible capitalisation.

Ce repo est construit par briques.

- **Phase 1** : trouver les tokens Solana "winners" (x5 ou plus depuis leur
  lancement) — `RUN_MODE=winners`.
- **Phase 2** : retrouver leurs premiers acheteurs et accumuler les wallets
  candidats — `RUN_MODE=discovery`.
- **Phase 3** : backtester ces wallets sur leur historique reel et n'activer
  que ceux qui performent — `RUN_MODE=validation`.

## Fichiers

| Fichier | Role |
| --- | --- |
| `config.py` | Seuils AJUSTABLES + diagnostic des variables d'environnement |
| `geckoterminal.py` | Client HTTP CoinGecko onchain (rate limit, retry, pertes explicites) |
| `supabase_client.py` | Lecture / upsert sur les tables `sol_*` |
| `main.py` | Point d'entree, dispatch via `RUN_MODE` |
| `find_winners.py` | Phase 1 : pipeline de discovery des winners |
| `helius.py` | Client HTTP Helius (throttle dedie, retry, pertes explicites) |
| `wallet_discovery.py` | Phase 2 : extraction des early buyers |
| `wallet_validation.py` | Phase 3 : backtest de validation des wallets |
| `wallet_validation_v2.py` | Phase 3 bis : backtest sur prix d'entree reel |
| `wallet_validation_v3.py` | Phase 3 ter : PnL realise en SOL |
| `probe_sort.py` | Sonde jetable : le tri de `/pools` est-il applique ? |
| `probe_helius.py` | Sonde jetable : forme des reponses Helius (phase 2) |
| `probe_transfers.py` | Sonde jetable : `getTransfersByAddress` (10 credits vs 100) |
| `probe_universe.py` | Sonde jetable : univers des gradues, faisabilite et cout |
| `probe_universe_v2.py` | Sonde jetable : courbe **derivee** (PDA), compte de migration, echantillon aleatoire |
| `probe_universe_v3.py` | Sonde jetable : mints d'une journee de graduations, prix des tokens morts |
| `probe_universe_v4.py` | Sonde jetable : liste datee des graduations, jointure par signature |
| `probe_universe_v5.py` | Sonde jetable : regle d'adresse de cotation, criblage a 3 points |
| `probe_universe_v6.py` | Sonde jetable : mint et pool depuis la transaction brute, regle **validee** |
| `probe_universe_v7.py` | Sonde jetable : liste reparee, graduation definie sans le signataire |
| `solana_addr.py` | base58 et derivation de PDA, sans dependance externe |

## Installation

```bash
python -m venv .venv && source .venv/bin/activate   # Python 3.13
pip install -r requirements.txt
```

## Variables d'environnement

Aucun secret n'est versionne. Toutes les variables sont lues via `os.environ` :

| Variable | Usage |
| --- | --- |
| `RUN_MODE` | `idle` (defaut), `winners`, `discovery`, `validation`, `validation_v2`, `validation_v3`, `probe`, `probe_helius`, `probe_transfers`, `probe_universe`, `probe_universe_v2`, `probe_universe_v3`, `probe_universe_v4`, `probe_universe_v5`, `probe_universe_v6`, `probe_universe_v7` |
| `FORCE_REMEASURE` | `true` pour refaire une mesure deja faite (voir plus bas) |
| `COINGECKO_API_KEY` | Cle Demo CoinGecko, envoyee en header `x-cg-demo-api-key` |
| `HELIUS_API_KEY` | Cle Helius — requise par `discovery`, `validation`, `probe_helius` |
| `SUPABASE_URL` | URL du projet Supabase |
| `SUPABASE_KEY` | Cle Supabase avec droit d'ecriture sur les tables `sol_*` |
| `MIGRATION_ACCOUNTS` | **Optionnelle**, `probe_universe_v3` a `v7` : adresses completes des comptes de migration, separees par des virgules. Absente -> la sonde les re-derive. |

En local, un fichier `.env` (git-ignore) suffit. Au demarrage, chaque variable
est loguee `presente` ou `ABSENTE`, et la source est annoncee explicitement :
`CoinGecko Demo (cle detectee)` ou `keyless (MODE DEGRADE)`. Il n'y a jamais de
bascule silencieuse en mode degrade.

## Execution

Point d'entree unique : `main.py`, qui lit `RUN_MODE`.

```bash
RUN_MODE=idle      python main.py   # defaut : diagnostic seul, 0 appel
RUN_MODE=winners   python main.py   # phase 1, tokens winners
RUN_MODE=discovery python main.py   # phase 2, early buyers
RUN_MODE=validation python main.py  # phase 3, backtest des wallets
RUN_MODE=validation_v2 python main.py # phase 3 bis, prix d'entree reel
RUN_MODE=validation_v3 python main.py # phase 3 ter, PnL realise en SOL
RUN_MODE=probe     python main.py   # sonde de tri (CoinGecko)
RUN_MODE=probe_helius python main.py # sonde Helius
RUN_MODE=probe_transfers python main.py # sonde getTransfersByAddress
RUN_MODE=probe_universe python main.py # sonde univers des gradues
RUN_MODE=probe_universe_v2 python main.py # sonde univers v2, courbe derivee
RUN_MODE=probe_universe_v3 python main.py # sonde univers v3, mints d'une journee
RUN_MODE=probe_universe_v4 python main.py # sonde univers v4, liste datee
RUN_MODE=probe_universe_v5 python main.py # sonde univers v5, adresse de cotation
RUN_MODE=probe_universe_v6 python main.py # sonde univers v6, pool valide
RUN_MODE=probe_universe_v7 python main.py # sonde univers v7, liste reparee
```

| `RUN_MODE` | Effet |
| --- | --- |
| `idle` (**defaut**, valeur vide ou absente incluse) | diagnostic seul, **aucun appel API**, aucune ecriture |
| `winners` | phase 1 : tokens winners |
| `discovery` | phase 2 : early buyers des winners |
| `validation` | phase 3 : backtest des wallets candidats |
| `validation_v2` | phase 3 bis : backtest sur prix d'entree reel |
| `validation_v3` | phase 3 ter : PnL realise en SOL |
| `probe` | sonde de tri CoinGecko, le pipeline n'est pas lance |
| `probe_helius` | sonde Helius, le pipeline n'est pas lance |
| `probe_transfers` | sonde `getTransfersByAddress`, le pipeline n'est pas lance |
| `probe_universe` | sonde univers des gradues, le pipeline n'est pas lance |
| `probe_universe_v2` | sonde univers v2, le pipeline n'est pas lance |
| `probe_universe_v3` | sonde univers v3, ecrit dans `sol_run_log` uniquement |
| `probe_universe_v4` | sonde univers v4, **relit** `sol_run_log` et y ecrit |
| `probe_universe_v5` | sonde univers v5, relit la liste datee de la v4 |
| `probe_universe_v6` | sonde univers v6, regle validee avant usage |
| `probe_universe_v7` | sonde univers v7, liste reparee et graduation definie |
| autre valeur | erreur explicite au demarrage, pas de repli silencieux |

> **Railway** : la Start Command doit etre `python main.py`. Lancer
> `python find_winners.py` fonctionne toujours mais execute *toujours* le
> pipeline winners — un `RUN_MODE=probe` y serait sans effet, et le script
> le signale par un warning au lieu de tourner silencieusement.

### Garde-fous d'execution

Le 20/09 a 13:08, un simple demarrage de conteneur Railway a relance
`validation_v3` sur les 30 wallets deja mesures. **Chaque deploiement ou
redemarrage rejoue le mode en place**, et consomme du budget pour rien.

Trois garde-fous :

1. **`idle` est le mode par defaut**, valeur vide ou absente incluse. Il
   logue le diagnostic d'environnement, ne fait **aucun appel API**,
   n'ecrit rien, et sort avec le code 0. Un redemarrage inattendu ne coute
   donc plus rien.
2. **Les modes couteux sont idempotents.** `validation_v2` et
   `validation_v3` ignorent les wallets qu'ils ont deja mesures
   (`activation_reason = "mesure_v2"` / `"mesure_v3"`) et loguent combien.
   Chaque mode n'ignore **que ses propres mesures** : un wallet mesure par
   v2 reste candidat pour v3. `FORCE_REMEASURE=true` est la seule facon de
   refaire une mesure, et l'annonce par un warning.
3. **Toute fin de run logue `Fin de run (<mode>)`** et rend le code 0.

Le tri des wallets deja mesures se fait en Python, pas dans la requete
PostgREST : un `.neq` sur `activation_reason` exclurait aussi les lignes
`NULL`, c'est-a-dire les wallets jamais mesures.

Ce principe vaut pour tout futur mode couteux : **un mode relance par
erreur ne doit rien depenser.**

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

## Phase 2 : extraction des early buyers

`RUN_MODE=discovery`. Deux voies Helius, arretees apres la sonde du 18/09 :

| Voie | Role |
| --- | --- |
| A — `getTransactionsForAddress` (JSON-RPC) | les signatures, en ordre **ascendant** |
| C — `POST /v0/transactions` | l'enrichissement, par lots de 100 |

La voie A est la seule a remonter les transactions les plus anciennes d'une
adresse ; elle ne rend que des metadonnees (`signature`, `slot`, `err`...).
La voie C ajoute `tokenTransfers` et `nativeTransfers`. `meta` et
`transaction` bruts ne sont pas parses.

### Ce que fait un run

1. Selection : `sol_analyzed_tokens WHERE is_winner AND
   buyers_extracted_at IS NULL`.
2. Par mint : voie A sur `EARLY_TX_LIMIT` transactions, filtre `err == null`,
   puis voie C sur les signatures retenues.
3. **Slot de lancement** = slot de la toute premiere transaction du lot
   brut, avant le filtre `err` — une transaction echouee marque quand meme
   le bundle. Toute transaction a ce slot porte `is_bundle = true`.
4. Un achat est une entree de `tokenTransfers` dont le `mint` est la cible
   **et** dont `toUserAccount` est le `feePayer`. L'inverse est une vente,
   ignoree. Le montant en SOL est la somme des `nativeTransfers` partant du
   `feePayer`, en lamports / 1e9, `NULL` si absent.
5. `buy_rank` incremental par ordre d'apparition, un wallet ne comptant
   qu'une fois. Les transactions enrichies sont **reordonnees sur l'ordre
   ascendant de la voie A** : la voie C ne garantit pas de conserver
   l'ordre des signatures envoyees, et le rang en depend entierement.
6. Ecriture dans `sol_early_buys` en `ON CONFLICT DO NOTHING` sur
   `(mint, wallet)` : le premier achat seulement, un rang existant n'est
   jamais ecrase.
7. `buyers_extracted_at` est pose **meme a zero acheteur**, sinon le token
   serait rejoue a chaque run. Jamais en cas de **PERTE** en revanche : le
   token reste a traiter.

### Accumulation

`sol_smart_wallets` est recalcule depuis la **totalite** de
`sol_early_buys` (hors `is_bundle`, rang <= `EARLY_BUYER_MAX_RANK`), pas
depuis le seul run. Un wallet vu sur un winner cette semaine et sur un autre
la semaine prochaine voit son `winners_count` monter.

| Champ | Regle |
| --- | --- |
| `winners_count` | mints distincts |
| `best_rank` | rang minimum |
| `active` | `winners_count >= ACTIVATION_MIN_WINNERS` **ou** `best_rank <= ACTIVATION_TOP_RANK` |
| `activation_reason` | `recoupement`, `rang_bas`, ou `NULL` |

**Tous** les wallets sont enregistres, actifs ou non : c'est l'accumulation
inter-runs qui les fait basculer.

Dans la ligne de resume, `acheteurs bruts` et `hors bundle` portent sur le
run ; `wallets distincts` et `actifs` sont les totaux accumules, puisqu'ils
viennent du recalcul global.

## Phase 3 : backtest de validation

`RUN_MODE=validation`. `sol_early_buys` ne contient **que des winners** : un
wallet vu sur dix d'entre eux peut etre un excellent trader comme un sniper
qui achete tous les lancements, ses pertes etant invisibles par
construction. Le backtest reconstitue son historique d'achats reel.

### Ce que fait un run

1. Candidats : `sol_smart_wallets WHERE winners_count >=
   VALIDATION_MIN_WINNERS AND validated_at IS NULL`.
2. Par wallet : voie A Helius en ordre **descendant**, **paginee** via le
   `paginationToken` jusqu'a `VALIDATION_TARGET_AGE_DAYS` de profondeur, avec
   `VALIDATION_MAX_PAGES` comme garde-fou.
3. Seules les transactions dont le `blockTime` tombe dans la fenetre de
   maturite (`VALIDATION_MIN_TOKEN_AGE_DAYS` a `VALIDATION_TARGET_AGE_DAYS`)
   sont enrichies par la voie C, par lots de 100. Le filtre porte sur les
   metadonnees de la voie A, qui portent deja `blockTime` : inutile
   d'enrichir des transactions qu'on ecarterait ensuite.
4. Les transactions sont **remises en ordre chronologique** avant extraction :
   "premier achat par mint" doit designer la plus ancienne entree de la
   fenetre, pas la plus recente.
5. Un achat est une entree de `tokenTransfers` dont `toUserAccount` est le
   wallet et dont le mint n'est pas du bruit. Comme en phase 2, une vente
   dans la meme transaction n'annule pas une reception.
6. Seuls les achats **matures** sont mesurables : plus vieux que
   `VALIDATION_MIN_TOKEN_AGE_DAYS`. Parmi eux, les **30 plus recents**
   (`VALIDATION_MAX_TOKENS_PER_WALLET`). C'est un echantillon, jamais une
   selection sur la performance : trier par gain biaiserait mecaniquement le
   win rate. La ligne loguee donne la fenetre reellement mesuree
   (`199 achats, 47 matures, 30 echantillonnes (achats de 11 a 38 j)`).
   Moins de `VALIDATION_MIN_TOKENS` achats matures ->
   `historique_trop_recent`, rendu **sans aucun appel de marche**.
7. Par mint : pool le plus liquide via `/tokens/{mint}/pools`, puis OHLCV
   journalier sur 180 jours. `perf = max(high APRES l'achat) / close du jour
   d'achat`, **cappee a `PERF_CAP` avant toute mediane**.
8. Verdict : `active` si `tokens_evaluated >= VALIDATION_MIN_TOKENS` **et**
   `win_rate >= VALIDATION_MIN_WIN_RATE` **et**
   `rug_rate <= VALIDATION_MAX_RUG_RATE`. `validated_at` est ecrit dans tous
   les cas, echec compris — mais jamais en cas de **PERTE**. Trois raisons de
   non-activation sont distinguees : `backtest_echoue`,
   `historique_insuffisant` (assez d'achats matures, mais trop peu
   mesurables) et `historique_trop_recent` (pas assez d'achats matures).
9. L'ecriture se fait **wallet par wallet, au fil de l'eau** : un arret du
   service ne fait pas rejouer les wallets deja backtestes. Une ligne de
   progression est loguee toutes les 10 wallets.

### Les pertes sont comptees

Un token achete est classe dans cet ordre :

| Etat | Condition | Compte ? | Cout |
| --- | --- | --- | --- |
| **mort** | aucun pool trouve | oui, `perf = 0`, `rug` | 1 appel |
| **rug** | pool sous `MIN_POOL_LIQUIDITY_USD`, ou volume 24h nul | oui, `rug` ; OHLCV tente pour mesurer la perf atteinte avant la chute, `perf = 0` si indisponible | 2 appels |
| **vivant** | pool au-dessus du seuil | oui, perf mesuree normalement | 2 appels |

Le **seul** cas ou un token sort du decompte : un token *vivant* dont
l'OHLCV est vide ou corrompu, ou un echec reseau. Il est logue a part comme
`non mesurable`, distinct des rugs.

Ecarter les tokens a faible liquidite revenait a ne mesurer que les succes.
Le resume donne, par wallet et au total, le nombre de tokens morts / rugs /
vivants / non mesurables — sans quoi un win rate ne veut rien dire.

### Pourquoi la pagination

Diagnostic du 18/09 14:21. Amplitude couverte par **500 transactions** :

| Wallet | Couverture | Achats extraits |
| --- | --- | --- |
| `7ioEZjdG` | **3 heures** | 202 |
| `9EX53TQE` | 2,8 jours | 161 |
| `DXenfCJ4` | **3 heures** | 0 |

Ces wallets font des centaines de transactions par jour. Un appel unique ne
peut structurellement pas atteindre la fenetre 7-60 j ou se trouvent leurs
winners : tous rendaient `0 matures`.

La voie A renvoie `result = {"data": [...], "paginationToken": ...}`. La
boucle redemande la page suivante tant que la derniere transaction de la
page est plus recente que la profondeur visee, et s'arrete des que l'une de
ces trois conditions est vraie :

| Motif | Sens |
| --- | --- |
| `profondeur_atteinte` | la profondeur visee est couverte |
| `historique_epuise` | plus de `paginationToken`, tout l'historique est lu |
| `limite_pages` | **garde-fou atteint**, profondeur NON couverte |

Le troisieme cas est logue en **warning**, pas comme un succes : des achats
matures manquent peut-etre. Le resume final compte les wallets concernes.

> **Ordre de grandeur a garder en tete** : a 500 tx par page, 20 pages font
> 10 000 transactions. Pour un wallet a ~4 000 tx/jour comme `7ioEZjdG`,
> cela ne couvre que **2,5 jours** — la profondeur de 45 j demanderait
> environ 360 pages. Les wallets hyperactifs finiront donc en
> `limite_pages`. C'est le warning qui le dira, wallet par wallet.

### Le cout est la contrainte dominante

Le run du 18/09 a consomme **396 480 credits** sur le million mensuel du
free tier, pour 60 wallets sur 117 : `getTransactionsForAddress` coute 100
credits par appel et le run en a fait 2384.

Deux leviers, dans cet ordre :

1. **Arret anticipe de la pagination** (en place). Les achats sont extraits
   **au fil des pages** plutot qu'apres coup, ce qui permet de s'arreter des
   que `VALIDATION_MAX_TOKENS_PER_WALLET` achats matures **distincts** sont
   collectes. Les logs montraient `FatpigGT` lisant 20 pages pour 9516
   transactions dans la fenetre... et n'en garder que 30. Une page suffit
   quand les mints distincts sont nombreux : 1900 credits economises sur ce
   seul wallet.
2. **`getTransfersByAddress`**, annoncee a 10 credits par appel.
   `RUN_MODE=probe_transfers` la sonde. Aucune ecriture en base, aucune
   conclusion dans le code.

#### Acquis de la sonde, run du 19/09 06:26

| Point | Resultat |
| --- | --- |
| Forme de la reponse | `result = {data, paginationToken}`, identique a `getTransactionsForAddress` |
| `sortOrder` | `asc` et `desc` acceptes |
| `startTime` / `endTime` | **rejetes** (-32602) |
| Cle inconnue dans la config | fait rejeter l'objet **entier** — n'envoyer que des cles connues |
| Montant SOL dans une ligne | **absent** : une ligne = une jambe de transfert d'un mint |
| Depart en `sortOrder=asc` | ~36 j avant maintenant sur `7ioEZjdG`, deja dans la fenetre mature |

#### Ce que la sonde mesure encore

| Section | Question |
| --- | --- |
| Plafond de `limit` | 100, 500, 1000, 2000 sont tentes. Le nombre de pages par wallet, donc le budget, en depend directement. |
| Jambes par signature | Une signature est prise dans le resultat, puis passee a la voie C. Les lignes `getTransfersByAddress` et les `tokenTransfers` / `nativeTransfers` sont affiches cote a cote. Si la jambe wSOL apparait comme une ligne distincte partageant la signature, **le prix d'entree se calcule sans voie C**. |
| Anciennete des candidats | Un appel ascendant par wallet a `winners_count >= 2`, sans pagination. Distribution en quatre tranches : moins de 10 j, 10-45 j, 45-90 j, au-dela. Si la majorite tombe dans 10-45 j, la collecte ascendante atteint la fenetre mature des la premiere page. |

La derniere section coute un appel par candidat. Si la limite minimale est
rejetee, elle n'est tentee **qu'une fois** avant bascule sur une valeur
sure, pour ne pas bruler tous les appels sur un parametre invalide. Le
total d'appels Helius est affiche en fin de sonde, a recouper avec le
dashboard.

Quatre motifs d'arret de pagination, testes dans cet ordre a chaque page :

| Motif | Sens |
| --- | --- |
| `echantillon_complet` | assez d'achats matures distincts, on s'arrete |
| `profondeur_atteinte` | la profondeur visee est couverte |
| `historique_epuise` | plus de `paginationToken` |
| `limite_pages` | **garde-fou atteint**, profondeur NON couverte |

Une consequence a connaitre : en parcourant du plus recent au plus ancien,
un mint achete plusieurs fois est enregistre a sa **premiere rencontre**,
donc sur la page la plus recente ou il apparait. L'arret anticipe interdit
de connaitre son achat le plus ancien sans lire toutes les pages.

### Pourquoi la maturite

Run du 18/09 12:45, premier wallet : 23 tokens mesures, **18 morts, 5 rugs,
0 vivant**, win rate 0. L'echantillon "les 30 plus recents" couvrait les
derniers jours d'activite, ou deux biais se cumulent :

- un token achete il y a deux jours n'a pas eu le temps de performer ;
- un lancement pump.fun trop recent n'est pas encore indexe par
  GeckoTerminal, donc classe **MORT a tort**.

Et surtout, les winners qui ont fait de ce wallet un candidat datent de 7 a
60 jours : ils etaient systematiquement hors de la fenetre mesuree.

`VALIDATION_MIN_TOKEN_AGE_DAYS` ecarte les achats trop jeunes avant
l'echantillonnage. La ligne par wallet affiche l'age du plus ancien et du
plus recent achat retenu, ce qui permet de verifier que la fenetre mesuree
est bien celle des winners.

### Pas de plancher d'activite

Le seul filtre sur le volume de transactions est le plafond haut
(`VALIDATION_MAX_TX`, bot ou MEV). Cote ETH, exclure les wallets a faible
historique avait elimine exactement les traders experimentes recherches.

Ce plafond n'est applique que si Helius expose un compteur total dans sa
reponse. La sonde du 18/09 n'a vu que `data` dans `result` : si aucun
compteur n'est present, **aucun wallet n'est exclu comme bot** et le
compteur `exclus bot` du resume restera a zero.

### Cache et cout

Les donnees de marche sont mises en cache **par mint pour la duree du run** :
plusieurs wallets achetent les memes tokens, et chaque mint coute un a deux
appels CoinGecko. Le cache retient les trois etats, tokens morts compris,
mais **jamais une PERTE** — celle-ci doit pouvoir etre reessayee.

Le budget Helius est affiche au demarrage. La part CoinGecko depend du nombre
de mints distincts, inconnu a priori, et domine le temps de run : 2 appels a
2,1 s par mint.

### Limite connue

**`active` et `activation_reason` sont partages avec la phase 2.** Un run
`discovery` posterieur recalcule ces deux colonnes depuis le recoupement et
ecrasera le verdict du backtest. `validated_at` survit, donc le wallet ne
sera pas rebacktest. Enchainer `validation` apres `discovery`, jamais
l'inverse.

## Phase 3 bis : backtest v2 sur prix d'entree reel

`RUN_MODE=validation_v2`, a cote de `validation` sans y toucher. Trois
constats de la sonde du 19/09 07:17 le fondent.

### Ce que la sonde a etabli

| Point | Resultat |
| --- | --- |
| Plafond de `limit` | **100** (`must be in [1, 100]`) |
| Cout reel | 100 lignes = 57 signatures. **0,175 credit par transaction** contre 0,20 : un gain de **12 %**, pas d'un facteur 10 |
| Jambe SOL | **presente** dans `getTransfersByAddress`, sous le mint `So1111...1111` — dernier caractere **1**, pas 2. Le controle automatique concluait "absente" parce qu'il cherchait la mauvaise adresse |
| Anciennete des 117 candidats | mediane **89 j**, max 921 j. `<10j` 1, `10-45j` **30**, `45-90j` 28, `>90j` 58 |

La collecte ascendante n'est economique que pour les **30 wallets** dont
l'historique commence deja dans la fenetre 10-45 j. Ce sont eux que le
backtest v2 mesure.

### Le prix d'entree devient reel

Un achat est un groupe de lignes partageant une signature, comportant
**les deux jambes** :

```
ligne SOL   : mint dans SOL_MINTS et fromUserAccount == wallet   (SOL sortant)
ligne token : mint hors SOL_MINTS et toUserAccount == wallet     (token entrant)

prix_entree = somme(uiAmount des jambes SOL sortantes) / uiAmount de la jambe entrante
```

Un token qui entre **sans** SOL sortant n'est pas un achat : airdrop,
migration ou transfert. Il est compte a part comme
`reception sans contrepartie`, jamais comme une perte.

Les prix OHLCV sont en USD et le prix d'entree en SOL. La conversion passe
par le prix du SOL a la **date de l'achat**, lu sur le pool SOL le plus
liquide **ayant le SOL en base token** — sans cette condition l'OHLCV
donnerait le prix de l'autre jeton. Le pool retenu et deux prix
d'extremite sont logues au demarrage pour etre verifiables.

### Pas de look-ahead

Le pic est le `max(high)` des bougies **strictement posterieures** a la
date d'achat. La bougie contenant l'achat est exclue : l'utiliser
reviendrait a connaitre le haut du jour au moment ou l'on achete.

### Gagnants et perdants separes

`median_winner_x` et `median_loser_x` sont calculees **separement**. Un win
rate de 20 % avec des gagnants a x10 n'est pas un echec, et une mediane
globale le masque completement. `median_raw_perf` conserve la valeur avant
cap.

### Ce run n'active personne

`active = false`, `activation_reason = "mesure_v2"`. Les metriques et
`validated_at` sont ecrits, les seuils seront choisis **apres** avoir vu la
distribution.

Colonnes supplementaires sur `sol_smart_wallets` : `median_raw_perf`,
`median_winner_x`, `median_loser_x`.

## Phase 3 ter : PnL realise en SOL

`RUN_MODE=validation_v3`. Le run v2 du 19/09 a corrige le prix d'entree
mais laisse trois defauts, et la metrique elle-meme etait mauvaise.

### Pourquoi v2 ne suffit pas

| Defaut | Constat |
| --- | --- |
| **Biais de survie** | 58 erreurs 404 GeckoTerminal. Tous les wallets au-dessus de 50 % de win rate avaient un echantillon ampute : `AkQ4bcEV` 12 tokens sur 30 -> 75 %, `EqQpvukm` 20 sur 38 -> 75 %. Parmi les 24 wallets mesures sur 30 tokens, le meilleur win rate tombait a **40 %**. |
| **Perfs aberrantes** | `median_raw_perf` a x3483 et x99 : signature d'un prix d'entree calcule sur un montant SOL derisoire. |
| **Receptions non reconnues** | 1280 ecartees faute de contrepartie SOL, contre 889 tokens classes. |

Et surtout : `perf = pic apres achat / prix d'entree` mesure ce que le
wallet **aurait** gagne en vendant au sommet exact. Le seuil de gagnant a
x2 rangeait par ailleurs dans les perdants des tokens a +42 %.

### Ce que v3 mesure

Les ventes sont deja dans les donnees collectees : meme signature, jambe
token sortante, jambe SOL entrante — le **symetrique exact** de l'achat.

```
ACHAT : jambe SOL sortante du wallet + jambe d'un autre mint entrante
VENTE : jambe du mint sortante du wallet + jambe SOL entrante

pnl_x = sol_recupere / sol_investi
```

**Aucun appel GeckoTerminal, aucune conversion USD, aucun cap.** Les trois
defauts disparaissent avec la dependance aux prix.

### Trois regles qui comptent

1. **La collecte ne s'arrete pas a la fin de la fenetre de maturite.** Les
   ventes d'un achat mature lui sont posterieures par construction :
   `V3_MAX_PAGES` vaut le double de v2 et la pagination va jusqu'au bout de
   l'historique disponible.
2. **Une position court a partir de son ouverture.** Seules les ventes
   posterieures au premier achat mature sont comptees — une vente
   appartenant a un cycle anterieur gonflerait le PnL sans rien mesurer.
3. **Les positions ouvertes sont comptees mais exclues du calcul.** Leur
   valeur actuelle est inconnue sans prix ; les compter reviendrait a
   inventer un resultat. Une position est fermee a partir de 95 % des
   tokens revendus — 100 % exact est irrealiste (frais, poussieres,
   arrondis).

Un achat sous `V3_MIN_SOL_PER_BUY` est ecarte et compte a part comme
**achat poussiere** : c'est ce denominateur derisoire qui produisait les
x3483 de v2.

Les positions retenues sont les **plus anciennes** de la fenetre : ce sont
celles qui ont eu le plus de temps pour etre revendues, donc les plus
susceptibles d'etre fermees.

### Ce run n'active personne

`active = false`, `activation_reason = "mesure_v3"`.

Colonnes supplementaires sur `sol_smart_wallets` : `positions_fermees`,
`positions_ouvertes`, `win_rate_reel`, `median_pnl_x`, `median_gagnant_x`,
`median_perdant_x`, `sol_investi`, `sol_recupere`, `pnl_global_x`.

## La sonde `RUN_MODE=probe_universe_v7`

### Ce qui avait reellement arrete la v6

Le run de 10:08 s'est arrete apres une validation a **9/10**, et le seuil
a ete soupconne. **Il n'etait pas en cause** : la v6 teste
`exact >= VALIDATION_MIN` avec `VALIDATION_MIN = 9`, donc 9/10 validait
deja — la reproduction le confirme. Ce qui a ferme la suite, c'est la
**garde de coherence** « regle validee mais aucun pool trouve » : la liste
etait **vide** malgre **476** transactions a un pool unique, parce que
l'entree exigeait `isinstance(row["signature"], str)` et que la signature
**n'est pas a la racine** de la ligne brute.

La garde a donc fait exactement son travail — elle a refuse de laisser
passer une conclusion que les nombres contredisaient. C'est
l'**extraction** qui etait fausse, et c'est elle que la v7 repare.

### Section A : l'entonnoir d'extraction

Pour 3 transactions a un pool unique, la sonde affiche **chaque champ**
(cles de premier niveau, signature et son chemin, `blockTime` et son
chemin, mint, pool) et nomme l'etape ou l'entree disparait. La signature
est ensuite cherchee a quatre emplacements (`signature`,
`transaction.signatures[0]`, `transaction.signature`, `signatures[0]`), et
le chemin reellement utilise est logue.

L'entonnoir compte alors chaque perte par motif — `aucun_mint`,
`pool_absent`, `pools_multiples`, `signature_introuvable`,
`horodatage_introuvable`, `retenue` — avec l'invariant
« somme des motifs = transactions lues ».

La regle est **revalidee en format BRUT** (`getTransaction`), et non plus
seulement en format Enhanced : les 10 migrations connues doivent rendre
leur `pool_address`. Seuil **>= 9/10**, ecrit et logue comme tel.

### Section B : une graduation, sans regarder qui signe

> **Graduation** = `CREATE_POOL` dans lequel le compte de la **bonding
> curve** du mint (PDA `["bonding-curve", mint]`) voit son solde de ce
> mint **DIMINUER**.

Cette definition ne depend pas du signataire — ce qui compte, c'est que la
courbe cede ses tokens. Elle est appliquee aux entrees de la section A,
puis a **100 signatures tirees** parmi les 857 propres a `39azUYFW`, qui
se repartissent en quatre classes :

| Classe | Sens |
| --- | --- |
| (a) | migration deja dans la liste A |
| (a') | signee par le compte de frais **mais absente de A** — un trou d'extraction |
| (b) | migration signee par un **autre** compte, dont la liste est affichee |
| (c) | `CREATE_POOL` **sans** courbe : creation directe, pas une graduation |

Seules (b) et (a') s'ajoutent au total, et l'estimation sort avec son
**intervalle a 95 %**. `CATE` et `Martians` sont diagnostiques nommement :
qui les a signees, et leur courbe cede-t-elle bien ses tokens.

La sortie nomme les **comptes a lire pour etre complet**.

### Ce que la sonde ne paie pas

Les **857 signatures propres sont relues** dans `sol_run_log` (la v6 les y
a ecrites). Les transactions brutes des 10 tokens, recuperees pour la
validation, resservent au diagnostic de `CATE` et `Martians`. Les
criblages a 1, 2 et 3 points sont rejoues sur les points deja lus.

### Plafonds

```
getTransfersByAddress <= 400   getTransactionsForAddress <=  5
Enhanced (voie C)     <=   4   getTransaction            <= 30
CoinGecko             <=   5
```

`getTransaction` n'a pas de cout publie a cote des methodes Helius : la
facturation annonce **1 credit** pour un appel RPC standard et **10** pour
une lecture **archivale**. La sonde compte **10** par prudence — 30 appels
font 300 credits dans les deux cas.

## La sonde `RUN_MODE=probe_universe_v6`

Le run de 05:51 a trouve **un** pool qui rend des swaps, et compte **1 181
signatures** sur `39azUYFW` le 17/09 dont **857 absentes** de `9C4nRvhh`.
La v6 en tire une regle, la **valide**, puis s'en sert — ou s'arrete.

### Trois regles de conduite

1. **Une regle deduite est validee avant d'etre appliquee**, sur les cas
   dont on connait deja la reponse. Le taux de validation est affiche.
2. **Toute conclusion imprimee est verifiee contre ses propres nombres.**
   Une conclusion qui les contredit s'affiche `INCOHERENT` et **n'est pas
   reutilisee** plus loin. Le recapitulatif les liste toutes.
3. **Une section qui depend d'une regle non validee s'arrete.**

### La regle mint + pool

Sur chaque transaction brute (`getTransactionsForAddress` en `full`), les
soldes `meta.preTokenBalances` et `meta.postTokenBalances` sont compares
par compte :

- le **mint gradue** est celui, hors SOL / WSOL / stablecoins, dont un
  compte voit son solde **augmenter** ;
- le **pool** est le `owner` qui voit augmenter **a la fois** un compte de
  ce mint **et** un compte WSOL. Un seul owner doit remplir les deux
  conditions, et les transactions a 0, 1 ou plusieurs candidats sont
  comptees.

La meme logique mint / owner / signe est ensuite appliquee, **au format
Enhanced** (`accountData.tokenBalanceChanges`), aux migrations des 10
tokens de `sol_analyzed_tokens` dont `pool_address` est connu : le pool
trouve doit etre **egal** a celui de la base. **En dessous de 9/10, les
sections C et D s'arretent.**

### Les quatre sections

| # | Mesure |
| --- | --- |
| A | La regle ci-dessus, sa validation 10/10, et la concordance des mints avec la liste Enhanced de la v4. |
| B | **Que sont les 857 signatures propres a `39azUYFW` ?** 100 tirees au hasard, un appel Enhanced, distribution `type` / `source`, `feePayer` des `CREATE_POOL` / `PUMP_AMM`. Sortie : le **nombre reel de graduations** du 17/09, avec son **intervalle de confiance a 95 %** (score de Wilson, sans dependance). |
| C | 30 mints tires dans la liste de A, trajectoire 8 points sur le **pool valide**, taux de succes du prix par classe, distribution des capitalisations. |
| D | Criblages a **1 point** (+6 h), **2 points** (+1 h, +24 h) et **3 points** (+30 min, +6 h, +24 h) compares a la trajectoire complete : manques et retenus a tort par seuil. Budget mensuel de chaque variante, sur toute la population **et sur la moitie tiree au hasard**. |

Chaque variante de criblage est un **sous-ensemble** des 8 points : sa
capitalisation vue ne peut pas depasser celle de la trajectoire complete,
et un « retenu a tort » est donc impossible. C'est verifie a chaque ligne,
et une violation s'affiche `INCOHERENT`.

### Ce que la sonde ne paie pas

La section D est gratuite : ses points sont deja lus par la section C. Les
adresses des comptes et la liste datee de la v4 sont relues dans
`sol_run_log`.

### Deux ecarts signales avant de coder

1. **Les 857 signatures ne sont pas persistees** : la v5 n'a ecrit que
   leurs nombres. La journee de `39azUYFW` est donc re-scannee (environ
   250 credits), et **cette fois la liste part dans `sol_run_log`**.
2. **`meta.preTokenBalances` n'est pas garanti** dans la reponse `full` :
   si le champ manque, la sonde affiche le **premier element brut** et
   s'arrete la, au lieu de conclure sur du vide.

### Plafonds

```
getTransfersByAddress <= 400   getTransactionsForAddress <=  5
Enhanced (voie C)     <=   6   CoinGecko                 <=  5
```

Une seule page de bougies horaires suffit ici : 41 jours de couverture
pour un echantillon vieux de 4 jours.

## La sonde `RUN_MODE=probe_universe_v5`

Le run de 20:08 a valide la **liste datee de 324 graduations** (PDA 3/3 a
**0 s** d'ecart), de type `CREATE_POOL` / `PUMP_AMM`, avec `9C4nRvhh` en
`feePayer`. Restait ce qui bloque depuis trois sondes : **ou lire le
prix**.

### La regle d'adresse est deduite, pas devinee

Trois sondes ont essaye une adresse au jugé — le pool, le mint, le
« coffre ». La v5 fait l'inverse : elle part des **10 tokens PumpSwap dont
`pool_address` est connu**, recupere leur transaction de migration, et
cherche **ou** cette adresse apparait dans le payload enrichi. Les chemins
sont normalises (`accountData[*].account`, `tokenTransfers[*].toUserAccount`,
…) : l'intersection des trois tokens **est** la regle, et elle est ecrite
telle quelle dans le log.

Elle est ensuite **verifiee** sur 3 mints du 17/09 : l'adresse obtenue
doit rendre des swaps a T+5 min. Si rien ne se degage, la sonde teste
**chaque compte** de la transaction de migration un par un et logue lequel
repond. **Sans adresse qui fonctionne, la section D n'est pas executee** —
mesurer un prix sur une adresse muette ne mesure rien.

### Les cinq autres sections

| # | Mesure |
| --- | --- |
| B | Un seul compte marque-t-il toutes les graduations ? Intersection et union des signatures du 17/09 entre les deux comptes : **l'union est le total reel** du jour. |
| C | La journee en **un appel** `getTransactionsForAddress` full / limit 1000, soit **100 credits**. Rend-il les mints ? Concordance **mint par mint** avec la liste Enhanced de la v4, et voie a retenir en croisiere. |
| D | 30 mints tires au hasard, trajectoire 8 points, **taux de succes du prix par classe** mort / vivant, distribution des capitalisations max. |
| E | **Le criblage a 3 points** (+30 min, +6 h, +24 h) : combien de tokens seraient mal classes, **dans les deux sens**, a quatre seuils de capitalisation. |
| F | Cout d'une journee listee, du criblage, de la trajectoire, de la courbe. **Plan en deux temps** sur toute la population, compare a l'echantillonnage. |

### Ce que la v5 ne paie pas

- **La liste datee et le cout de la courbe** sont relus dans
  `sol_run_log` (run `probe_universe_v4`). Repli explicite et logue si
  elle manque : re-scan de la journee plus jointure Enhanced.
- **La section E ne coute rien.** Ses 3 points sont un sous-ensemble des 8
  deja lus en section D : elle les **rejoue** sur les mesures existantes.
  Le cout annonce pour le criblage est celui d'un run de production, pas
  une depense de la sonde.
- **Les `feePayer`** de la section B viennent des transactions deja
  enrichies en section A.

### Deux ecarts signales avant de coder

1. **La part des `CREATE_POOL` / `PUMP_AMM` non couverte n'est pas
   mesurable** dans ces plafonds : il faudrait scanner le programme
   PumpSwap entier, dont le volume depasse de loin les 1000 transactions
   par appel. A la place, la section B mesure l'intersection et l'union
   des deux comptes connus, et regarde les `feePayer` deja enrichis : un
   `feePayer` qui n'est **aucun** des deux comptes est une preuve directe
   d'un troisieme chemin.
2. **Le ticket ne fixe pas le seuil de capitalisation** de la section E :
   la sonde en teste **quatre** (25 k, 50 k, 100 k, 250 k $) et donne la
   distribution, plutot que d'en inventer un.

### Plafonds

```
getTransfersByAddress <= 600   getTransactionsForAddress <= 20
Enhanced (voie C)     <=   6   CoinGecko                 <= 10
```

## La sonde `RUN_MODE=probe_universe_v4`

Le run de 19:11 a etabli : **frais de migration a 0,0015 SOL** sur 86 % des
lignes, **324 graduations** le 17/09, **8/10 tokens retrouves** a +/- 120 s
dont 7 a la signature pres, et **683 transactions** de la journee en **un
seul** appel `getTransactionsForAddress` en `full` / `limit 1000`.

Restait le livrable : la **liste datee** `[mint, horodatage, signature]`.

### Le bug de la v3

La v3 cherchait le mint **sur les lignes du compte de frais**. Or une
ligne de frais ne porte **que du SOL** : `token_mint_of` renvoyait `None`
pour chacune, la liste sortait vide, et la section C s'arretait — ce
qu'elle devait faire, mais pour la mauvaise raison. Les 341 mints de la
voie Enhanced etaient un **ensemble** fusionne sur tous les lots, sans
jointure : personne ne pouvait dire s'ils correspondaient aux 324
signatures.

La v4 **joint par signature** : pour chacune, Enhanced rend ses
`tokenTransfers`, on ecarte SOL, WSOL et les stablecoins, et on retient le
mint du **plus gros** transfert. Le nombre de signatures rendant 0, 1 ou
plusieurs candidats est logue, et un exemple complet est affiche quand il
y en a plusieurs.

### Ce que la sonde etablit

| # | Mesure |
| --- | --- |
| A | **Couverture temporelle** : date de la premiere transaction du compte de frais, puis recherche des absents sur le compte de secours. Un token gradue **avant** que le compte existe n'est pas un defaut d'appariement, et la sonde le dit. Le critere « signature portant un mint distinct » est **retire** : une ligne de frais ne porte que du SOL, il ne pouvait jamais etre vrai. |
| B | **La liste datee**, plus le croisement avec le **montant du frais** : les signatures a 0,0150 SOL rendent-elles un mint aussi souvent que celles a 0,0015 ? Un ecart de 20 points ou plus signerait **deux evenements differents**. Puis **validation** sur 3 mints : le PDA de leur bonding curve doit avoir sa **derniere** transaction a l'heure annoncee (la courbe est videe a la migration). |
| C | Echantillon aleatoire de 30 mints, seed loguee. Trajectoire 8 points, **taux de succes du prix par classe** mort / vivant. Courbe lue sur 10. Sans liste : **section arretee**, jamais de repli. |
| D | Cout par journee listee, par token (trajectoire), par token (courbe), et le budget mensuel a trois taux d'echantillonnage. |

### Sonde et regime de croisiere

Chaque appel est compte dans l'un des deux regimes :

- **sonde** : l'exploration, payee **une fois** (premiere transaction du
  compte, appariement des 10 tokens, validation des PDA) ;
- **croisiere** : ce qu'un run quotidien **repaierait** (scan de la
  journee, jointure Enhanced, trajectoires, courbes).

Seul le second entre dans la projection mensuelle, et le recapitulatif le
rappelle explicitement.

### Ce que la v4 relit au lieu de le repayer

`sol_run_log` sert enfin : les **adresses completes** des comptes, que la
v3 y a ecrites, sont relues au lieu d'etre re-derivees. En revanche les
**324 signatures n'avaient pas ete persistees** — seul leur nombre l'etait
— donc la journee est re-scannee (environ 70 credits), et cette fois la
liste complete part dans la table.

## La sonde `RUN_MODE=probe_universe_v3`

Le run v2 de 15:35 a etabli trois choses, qui ne sont plus remesurees : la
**PDA de courbe est correcte (10/10)**, `filters.status = "succeeded"` est
accepte, `solMode` accepte `merged` et `separate`. Restait l'essentiel :
**une ligne du compte de migration vaut-elle une graduation**, **quel est
le mint** de chacune, et **comment se comporte le prix sur des tokens
morts**.

C'est la premiere sonde qui **ecrit** : `sol_run_log`, et rien d'autre.

### Pourquoi `sol_run_log`

Le 20/09, la sonde avait trouve un compte de migration et l'avait affiche
en `9C4nRvhh..`. Au ticket suivant, **l'adresse complete n'existait plus
nulle part** : une sonde qui n'ecrit rien fait recommencer le ticket
suivant a zero. `sol_run_log` garde une ligne par section, avec sa mesure
en `jsonb` — dont la **liste datee des mints** d'une journee de
graduations.

```sql
create table if not exists sol_run_log (
  id       bigserial primary key,
  run_mode text not null,
  run_at   timestamptz not null default now(),
  section  text,
  label    text,
  payload  jsonb
);
```

L'ecriture est testee **au demarrage, avant la moindre depense de
credits** : table absente -> la sonde affiche ce SQL et s'arrete.

### Quatre sections et un dimensionnement

| # | Mesure |
| --- | --- |
| A | Le compte de migration marque-t-il les graduations ? Fenetre de +/- 2 min autour des 10 graduations **deja datees**, signature comparee a celle de la creation du pool. Distribution des montants SOL de la journee : un montant fixe dominant signerait un frais de migration. Conclusion explicite. |
| B | Les mints d'une journee, par **deux voies comparees sur les memes signatures** : `getTransactionsForAddress` en `transactionDetails: "full"` contre Enhanced par lots de 100. Mints obtenus, appels, credits, cout par graduation. Sortie : la **liste datee** des mints gradues. |
| C | 30 mints tires au hasard **dans la liste de B** (seed loguee). Trajectoire 8 points, prix en SOL, mediane des swaps. Classement mort / vivant et **taux de succes du prix par classe** — le point a etablir. Courbe lue sur 10 d'entre eux. |
| D | Prix du SOL sur **au moins 120 jours** en bougies horaires (plusieurs pages), repli journalier **explicite et logue** au-dela. Le compteur de conversions hors couverture doit finir a **0**. |
| E | Gradues par jour, credits pour lister une journee, credits par token (trajectoire, courbe), puis le budget mensuel a **trois taux d'echantillonnage** : toutes les graduations, une sur trois, une sur dix. |

Si la section B ne produit pas de liste, **la section C s'arrete** : pas de
repli sur l'echantillon des sondes precedentes, qui ne serait plus
aleatoire et rendrait le taux de succes par classe ininterpretable.

### Plafonds

```
getTransfersByAddress <= 800   getTransactionsForAddress <= 20
Enhanced (voie C)     <=  10   CoinGecko                 <= 10
```

### Trois ecarts signales avant de coder

1. **`9C4nRvhh` et `39azUYFW` sont des prefixes de 8 caracteres**, pas des
   adresses : on n'interroge pas Helius avec un prefixe. La sonde
   **re-derive** les comptes recurrents puis les **rapparie** par prefixe.
   `MIGRATION_ACCOUNTS` (adresses completes, separees par des virgules)
   court-circuite cette re-derivation quand on les aura.
2. **`filters.tokenAccounts` et `transactionDetails: "full"` n'ont jamais
   ete valides** — et le 17/09 avait etabli que cette methode ne rend que
   `signature`, `slot`, `err`, `blockTime`. Les cles sont donc testees
   **une par une** avant d'etre combinees, message de rejet brut affiche :
   une cle inconnue fait rejeter tout l'objet.
3. **324 lignes ne sont pas 324 graduations.** Ce sont des *lignes de
   transfert* : une graduation en produit plusieurs (jambe SOL, jambe
   token). La section A donne lignes, signatures et mints distincts, et
   conclut separement sur « une signature = une graduation » et sur « une
   ligne = une graduation ».

## La sonde `RUN_MODE=probe_universe_v2`

Le run `probe_universe` du 20/09 13:25 a valide **une** chose : le prix a
un instant donne se reconstruit a **1,11 appel par point obtenu**. Trois
sections n'ont pas mesure ce qu'elles devaient, et cette sonde les refait.
Aucune ecriture en base, cles masquees, `HELIUS_API_KEY` absente -> la
sonde leve.

### Quatre sections

| # | Mesure |
| --- | --- |
| A | Syntaxe corrigee : `filters.status = "succeeded"` (le 20/09 envoyait `"success"`), et `solMode` compare `"merged"` (defaut) a `"separate"` sur **une meme signature de swap**, lignes cote a cote. |
| B | La bonding curve est **derivee**, plus devinee : PDA de seeds `["bonding-curve", mint]` sous le programme pump.fun. Creation = 1re transaction de la courbe, graduation = 1re transaction du pool PumpSwap. Toute duree **< 1 min** est signalee comme suspecte. Lecture ascendante jusqu'a la graduation (plafond 50 pages, logue s'il est atteint). |
| C | Le compte **propre aux migrations** : comptes recurrents a la creation, exclusion des programmes et mints, criblage a 1 appel par compte sur 1 h (sature -> compte de trading), confirmation par 3 transactions portant pump.fun **et** PumpSwap, puis pagination de **deux journees completes**. |
| D | Echantillon **reellement aleatoire** : 30 mints tires (seed loguee) dans les graduations de C, morts compris. Prix = **mediane des swaps de la page**, achats et ventes, en SOL. Mort = prix a 24 h **< 30 %** du prix a la graduation, et le taux de succes du prix est donne **separement pour morts et vivants**. |

### Pourquoi la section 6 du 20/09 lisait 0 a 1 transaction

La courbe etait devinee : *contrepartie la plus frequente des 50 premieres
jambes du mint*. Apres graduation, cette contrepartie est tres souvent le
**pool PumpSwap**, pas la courbe — et toutes ses lignes sont alors
posterieures a la graduation, donc la boucle sortait des la page 1 avec 0
acheteur. La sonde v2 ne l'affirme pas : elle **compte**, pour chaque
token, les lignes anterieures a la graduation cote PDA et cote
heuristique, et affiche les deux.

`solana_addr.py` fournit la derivation sans aucune dependance nouvelle :
base58 et PDA (`sha256(seeds || bump || program_id ||
"ProgramDerivedAddress")`, bump descendant, premiere adresse **hors courbe
ed25519**) sont reimplementes en Python pur.

### Plafonds et couts

```
getTransfersByAddress <= 1000   getTransactionsForAddress <= 40
Enhanced (voie C)     <=    5   CoinGecko                 <= 10
```

Deux corrections de comptabilite par rapport au 20/09 :

1. **Enhanced n'est plus a 0 credit** mais a **100** (documentation
   Helius, *"Credit cost: 100 credits per call"*). Le 20/09 sous-estimait
   la facture de sa section 4.
2. Les deux journees de la section C sont paginees avec
   `getTransfersByAddress` (**10** credits) et non
   `getTransactionsForAddress` (**100**) : c'est dix fois moins cher, et
   les lignes portent le **mint**, dont la section D a besoin pour tirer
   son echantillon. Le plafond de 40 `getTransactionsForAddress` est
   reserve au criblage des comptes.

Le prix du SOL est ici **horaire** (2 appels CoinGecko, 1000 bougies), et
non journalier comme dans `wallet_validation_v2` : les capitalisations de
la section D ne portent plus l'imprecision intra-journaliere du 20/09.

### Ce qui a ete signale avant de coder

- Les "comptes recurrents du 20/09" ne sont **nulle part** : la sonde
  n'ecrit rien et ses sorties ne sont pas persistees. La section C les
  recalcule, en **un seul** appel Enhanced pour les trois signatures au
  lieu de trois.
- **baton** n'est identifiable que par son symbole. S'il n'est pas dans
  l'echantillon, la sonde le **dit** au lieu de faire semblant ; le
  diagnostic *pourquoi aucun prix* (mints de la page, presence d'une jambe
  SOL, nombre de swaps valorisables) s'applique de toute facon a tout
  token sans prix.
- Si la section C ne retient aucun compte de migration, la section D se
  rabat sur les tokens de la section B **en annoncant** que l'echantillon
  n'est alors ni aleatoire ni representatif.

## La sonde `RUN_MODE=probe_universe`

Mesure de **faisabilite et de cout**, pas de trading. Question posee : un
humain recevant une alerte avec 5 a 60 min de latence dispose-t-il d'une
fenetre exploitable sur les tokens gradues ? Avant de chercher des wallets,
il faut savoir si l'univers est listable retroactivement et a quel prix.

Aucune ecriture en base. Cles masquees. `HELIUS_API_KEY` absente -> la
sonde leve.

### Six sections

| # | Mesure |
| --- | --- |
| 1 | Syntaxe des filtres, **une cle a la fois** (une cle inconnue fait rejeter tout l'objet). Preuve que le filtre est applique : dates dans la plage, montants au-dessus du seuil. `solMode` compare les lignes d'une meme signature avec et sans. |
| 2 | Prix a un instant donne sur un pool gradue, reconstruit par `jambe SOL / jambe token`. Controle contre l'OHLCV **horaire** CoinGecko. |
| 3 | Bonding curve retrouvee depuis les premieres jambes du mint, prix avant graduation, duree creation -> graduation. |
| 4 | **Section cle** : peut-on reconstituer les gradues d'une journee passee ? Comptes recurrents a la creation des pools, puis `new_pools` CoinGecko en alternative. |
| 5 | Trajectoire d'un echantillon aleatoire (seed loguee), morts compris, a 8 instants apres graduation. Taux de succes **morts vs vivants** : c'est la population que GeckoTerminal perdait en 404. |
| 6 | Cout des premiers acheteurs : toutes les jambes de la courbe, de la creation a la graduation. |

### Plafonds

Plafonds **globaux** et **par section**. Un plafond atteint coupe la
section avec un warning et la sonde passe a la suivante — jamais d'arret
silencieux.

```
getTransfersByAddress <= 1500   getTransactionsForAddress <= 60
Enhanced (voie C)     <=   10   CoinGecko                 <= 40
```

Le cout en credits est affiche au demarrage et en fin de run, par methode,
**a recouper avec le dashboard Helius**. Le resume final projette combien
de tokens tiennent dans 1M credits/mois pour (a) lister, (b) suivre en
trajectoire, (c) suivre avec leurs premiers acheteurs.

### Trois ecarts releves avant ecriture

1. **PumpSwap n'est plus collecte depuis le 18/09** : `PREFERRED_DEXES` ne
   contient que `meteora`, `raydium-clmm` et `raydium`.
   `sol_analyzed_tokens` peut donc n'en contenir aucun. Le repli va
   chercher les pools PumpSwap **directement** via `dex_pools("pumpswap")`
   plutot que de basculer sur "le DEX le plus represente", ou il n'y a pas
   de bonding curve et ou les sections 3 et 6 mesureraient autre chose. Si
   meme ce repli echoue, un **avertissement explicite** le dit.
2. **Le prix du SOL reutilise de `wallet_validation_v2` est en bougies
   journalieres** : les capitalisations intra-journalieres de la section 5
   en heritent d'une imprecision, rappelee dans le resume.
3. **`new_pools` n'est pas filtrable par DEX** et la pagination est
   plafonnee a 10 pages sur le plan Demo. Le filtrage PumpSwap se fait
   cote client, et la sonde **mesure** l'amplitude reellement couverte au
   lieu de la supposer.

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

## Volume de winners

La base **s'alimente par accumulation** : chaque run n'analyse que les mints
absents de `sol_analyzed_tokens` depuis moins de `ANALYZED_TTL_DAYS`, si bien
que des runs hebdomadaires empilent des winners nouveaux au lieu de
re-mesurer les memes. Le compte d'un run isole n'est donc pas un objectif a
atteindre : le run logue `winners ce run : N` a titre informatif, sans
avertissement. Le meme principe vaut en phase 2 pour `sol_smart_wallets`.
