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
| `probe_sort.py` | Sonde jetable : le tri de `/pools` est-il applique ? |
| `probe_helius.py` | Sonde jetable : forme des reponses Helius (phase 2) |
| `probe_transfers.py` | Sonde jetable : `getTransfersByAddress` (10 credits vs 100) |

## Installation

```bash
python -m venv .venv && source .venv/bin/activate   # Python 3.13
pip install -r requirements.txt
```

## Variables d'environnement

Aucun secret n'est versionne. Toutes les variables sont lues via `os.environ` :

| Variable | Usage |
| --- | --- |
| `RUN_MODE` | `winners` (defaut), `discovery`, `validation`, `validation_v2`, `probe`, `probe_helius`, `probe_transfers` |
| `COINGECKO_API_KEY` | Cle Demo CoinGecko, envoyee en header `x-cg-demo-api-key` |
| `HELIUS_API_KEY` | Cle Helius — requise par `discovery`, `validation`, `probe_helius` |
| `SUPABASE_URL` | URL du projet Supabase |
| `SUPABASE_KEY` | Cle Supabase avec droit d'ecriture sur les tables `sol_*` |

En local, un fichier `.env` (git-ignore) suffit. Au demarrage, chaque variable
est loguee `presente` ou `ABSENTE`, et la source est annoncee explicitement :
`CoinGecko Demo (cle detectee)` ou `keyless (MODE DEGRADE)`. Il n'y a jamais de
bascule silencieuse en mode degrade.

## Execution

Point d'entree unique : `main.py`, qui lit `RUN_MODE`.

```bash
RUN_MODE=winners   python main.py   # defaut : phase 1, tokens winners
RUN_MODE=discovery python main.py   # phase 2, early buyers
RUN_MODE=validation python main.py  # phase 3, backtest des wallets
RUN_MODE=validation_v2 python main.py # phase 3 bis, prix d'entree reel
RUN_MODE=probe     python main.py   # sonde de tri (CoinGecko)
RUN_MODE=probe_helius python main.py # sonde Helius
RUN_MODE=probe_transfers python main.py # sonde getTransfersByAddress
```

| `RUN_MODE` | Effet |
| --- | --- |
| `winners` (defaut, valeur vide incluse) | phase 1 : tokens winners |
| `discovery` | phase 2 : early buyers des winners |
| `validation` | phase 3 : backtest des wallets candidats |
| `validation_v2` | phase 3 bis : backtest sur prix d'entree reel |
| `probe` | sonde de tri CoinGecko, le pipeline n'est pas lance |
| `probe_helius` | sonde Helius, le pipeline n'est pas lance |
| `probe_transfers` | sonde `getTransfersByAddress`, le pipeline n'est pas lance |
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
