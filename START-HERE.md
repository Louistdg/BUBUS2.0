# BUBUS2.0

V2 d'INVEST-AI, construite sur les sources courantes du 9 octobre 2026.
Publication publique : les métadonnées de provenance et les chemins privés de la base source sont conservés hors de ce dépôt.

Lancement Windows : `bubus.cmd`, puis http://127.0.0.1:8790/. Au premier
démarrage, le script crée un environnement Python isolé `.venv` et installe
`requirements.txt`; les dépendances ne sont pas ajoutées au Python global.

L'aperçu Railway utilise séparément `python -m bubus.cloud_preview`. Il sert
uniquement l'interface et les données de marché publiques derrière
l'authentification HTTP Basic, refuse les écritures et n'expose aucune route de
portefeuille privé, de clé ou d'ordre. Les variables `BUBUS_PREVIEW_USERNAME`
et `BUBUS_PREVIEW_PASSWORD` sont obligatoires (mot de passe d'au moins 24
caractères); l'accès extérieur exige HTTPS via le proxy. Le coffre Windows et
les clés Crypto.com ne sont jamais copiés vers Railway.

Objectif : surveillance des marchés réels et trading automatique sur Crypto.com
Exchange sous une politique financière explicitement configurée. Aucun faux cours
ni portefeuille de démonstration dans l'interface.

Le package `invest_ai` conserve les fondations et tests hérités. Le nouveau produit
et son interface se trouvent dans `src/bubus`. Ne pas copier de base, clé ou coffre
depuis INVEST-AI. L'entrée Windows Credential Manager de BUBUS2.0 est distincte.

L'objectif de trading réel est en cours de construction. L'interface ne doit pas
annoncer que l'exécution est opérationnelle tant que ses contrôles ne le prouvent
pas. Les limites financières non précisées restent absentes, jamais déduites d'un
solde ou de « 10000 » sans unité. Les tests utilisent uniquement des ressources
temporaires isolées. Aucun ordre réel pour tester sans autorisation spécifique.

## Données et limites actuelles

- Crypto.com Exchange fournit les marchés Spot disponibles par REST, actualisés
  toutes les 15 secondes. Le bouton « Suivre tous les marchés Spot » souscrit
  l’univers disponible au WebSocket en lots et répartit les paires sur plusieurs
  connexions; les ticks de moins de 30 secondes remplacent le dernier trade REST
  affiché. Dernier trade, meilleur bid et meilleur ask restent séparés. Les
  cotations incomplètes ou croisées ne sont jamais déclarées utilisables pour Risk.
  Les marchés sans tick récent gardent le cours REST identifié comme tel.
- Le tableau recoupe les paires avec `public/get-instruments` : seules les paires
  `CCY_PAIR` déclarées tradables reçoivent les incréments officiels de prix et
  de quantité. Si le catalogue ne répond pas, les cours restent visibles mais
  les règles sont marquées indisponibles; aucun incrément n'est déduit.
- L’action d’évaluation archive une preuve uniquement pour une paire de la liste
  Spot actuelle, avec un bid/ask complet et frais. Le prix médian est un candidat
  de donnée seulement : il ne donne aucune autorisation à Risk et n’active jamais
  les ordres.
- CoinGecko affiche une vue agrégée globale, rafraîchie chaque minute; elle ne
  représente pas le prix d'exécution de Crypto.com.
- L'onglet Marchés peut comparer trois règles historiques (achat-conservation,
  croisement SMA 3/10, cassure + volume) sur les bougies Crypto.com chargées.
  Le calcul sépare chronologiquement contexte et test, affiche trois fenêtres,
  drawdown, volatilité et coûts configurés. Il utilise un indice de capital
  normalisé, ne persiste pas les essais et ne produit ni signal, ni recommandation,
  ni autorité d'ordre. Les résultats historiques ne valident pas une stratégie.
- Après une lecture Crypto.com réussie, le dernier relevé est conservé dans
  `data/desk.sqlite3` (maximum 1 000 relevés). Il reste une observation locale,
  en lecture seule qui inclut balances, positions dérivées et ordres ouverts;
  elle n'est pas encore une source autoritaire recevable par Risk et n'autorise
  aucun ordre.
- La page Portefeuille lit aussi les transactions Crypto.com des dernières 24 h
  et conserve un historique local hashé. Pour toute page de 100 lignes, elle
  scinde récursivement l'intervalle jusqu'à 16 requêtes maximum, en bornes
  disjointes, puis conserve les pages terminales séparément. Une plage d'une
  milliseconde encore plafonnée, ou des intervalles non interrogés après le
  plafond de requêtes, restent explicitement incomplets dans le lot archivé.
  Une couverture complète vaut uniquement pour la période demandée; elle ne
  prouve pas l'exhaustivité de l'historique du compte. Les devises des P&L/frais
  et les flux de capital ne sont pas encore rapprochés; ces transactions ne
  servent donc pas à calculer le P&L Risk ni à autoriser les ordres. Une lecture
  en direct du compte a confirmé les variantes `OFFCHAIN_DEPOSIT`, les références
  optionnelles vides et le marqueur `NULL_VAL` pour le côté absent; elles sont
  normalisées explicitement afin que l'événement n'invalide pas toute la page.
  L’interface permet de choisir les dates pour relire jusqu’à sept jours à la
  fois, y compris les périodes précédentes. Les lectures restent bornées et
  manuelles; un historique plus long demande plusieurs lots, et une période
  complète dans l’API ne prouve pas les soldes d’ouverture ou l’exhaustivité
  globale du compte.
- La page Portefeuille récupère séparément les exécutions confirmées avec
  `private/get-trades` (quantité, prix, identifiants d’ordre/trade, frais et devise
  des frais), les conserve localement avec empreinte et les affiche sur 24 h
  après connexion; une relecture historique permet de choisir la même période
  que les journaux de transactions, jusqu’à sept jours par lot.
  L’API REST limite cette route à une requête par seconde et 100 lignes par
  réponse; une plage plafonnée est scindée en intervalles disjoints (16 requêtes
  maximum). Les pages terminales et intervalles non couverts sont conservés
  ensemble sous un identifiant de lot. La couverture complète vaut uniquement
  pour la période demandée et ne prouve pas l’exhaustivité de l’historique du
  compte. Le contrôle de rapprochement déduplique les pages, puis recoupe les
  références de trades, d’ordres et le
  sens avec les journaux `TRADING` qui couvrent la même fenêtre. Ce contrôle
  d’identité ne prouve pas les mouvements signés, l’exhaustivité des frais ou les
  soldes d’ouverture/clôture : les trades ne sont pas une source P&L ou Risk et
  n’accordent aucune autorisation d’ordre.
- L’historique privé Crypto.com `user-balance-history` est lu séparément en
  points H1 et conservé localement avec son empreinte. Il ne contient que le cash
  total d’une devise; Crypto.com signale des écarts temporaires possibles avec
  son interface. BUBUS2.0 ne l’utilise ni comme equity ni pour le P&L ou Risk.
- Après lecture du compte, BUBUS2.0 demande également le barème Spot maker/taker
  effectif du compte, en lecture seule. Le taux sert d'observation sourcée; il n'est
  pas encore consommé par le registre de politique Risk.
- Une estimation Spot candidate peut être demandée dans la page Portefeuille en
  choisissant explicitement EUR, USD, GBP ou CHF. Elle valorise une seule fois les
  soldes wallet détaillés avec des snapshots de carnet Crypto.com vérifiés et les
  ordres LIMIT ouverts séparément. Elle refuse les snapshots incomplets/périmés,
  les positions dérivées, la marge, l'emprunt et les cours manquants. Ce résultat
  reste un candidat non autoritaire, sans PnL historique et sans droit d'ordre.
- Chaque estimation Spot candidate acceptée est archivée localement dans
  `data/desk.sqlite3` (`cryptocom_portfolio_valuation_candidates`, 500 entrées
  conservées; l’API `/api/desk/portfolio-valuations` affiche les 100 dernières).
  Ces lignes sont des **observations ponctuelles**, pas un calcul de **P&L**,
  de rendement ou de performance. Les variations entre deux valorisations ne
  neutralisent **pas** tous les dépôts, retraits, conversions et frais : les
  flux n’ont pas encore été intégralement rapprochés. Ne jamais calculer un P&L
  à partir de la différence brute entre ces observations. Chacune conserve son
  horodatage, la devise, le détail et les références/empreintes des preuves.
  La lecture contrôle les empreintes, la cohérence des montants et les drapeaux
  de non-autorité; une ligne invalide parmi celles demandées entraîne le refus
  de toute la réponse API (HTTP 503), sans afficher de chiffres partiels. Aucun historique
  de valorisation ne doit entrer dans **Risk**, le calcul des limites ou le
  chemin **signal → autorisation → ordre**. Les ordres restent désactivés.
- Le contrôle **Rapprochement du portefeuille** est accessible dans Portefeuille
  et via `GET /api/desk/reconciliation` (lecture locale, même origine).
  Il contrôle les dernières valorisations, le dernier relevé du compte, la série
  cash et jusqu'à 50 pages conservées de journaux et d’exécutions. Il cite les IDs, fenêtres
  temporelles et empreintes SHA-256 de ces preuves, classe sans les sommer
  dépôts, retraits, conversions, trades et frais, déduplique les événements
  identiques, recoupe trade_id/order_id/sens dans les fenêtres communes et signale
  les identités contradictoires. Il recherche aussi les trous
  temporels entre les deux dernières valorisations et le plafonnement à 100
  lignes d'une page. Un enregistrement invalide provoque HTTP 503 : aucun
  rapprochement partiel n'est déclaré fiable.
  **Aucun P&L, gain/perte, dépôt net ou frais net n'est calculé** à partir des
  journaux existants : leurs montants n'établissent pas les mouvements signés
  par actif et devise, les deux jambes des conversions, la devise des frais,
  l'exhaustivité de la pagination, les soldes d'ouverture et de clôture.
  Le rapport de rapprochement est un calcul de contrôle déterministe sur les
  données locales déjà persistées, identifié par une empreinte et non une
  nouvelle source financière autoritaire. Il reste `BLOCKED_INCOMPLETE`
  ou `BLOCKED_CONTRADICTORY`, avec `pnl: null` et tous les drapeaux
  Risk/exécution à `false`. Les observations cash ne sont pas de l'equity.
  Avant tout calcul financier il faut une chaîne vérifiable de mouvements
  débit/crédit, flux externes, frais, conversions et références de transactions
  non tronquées, avec rapprochement chronologique des soldes.
  `src/bubus/quantity_ledger.py` prépare le contrôle arithmétique exact
  `ouverture + mouvements signés = clôture` par actif : mouvements à deux
  jambes pour trades/conversions, références externes pour dépôts/retraits,
  frais comme débits et rejet des doublons. Ses tests utilisent des données
  synthétiques; les observations Crypto.com actuelles ne remplissent pas encore
  son contrat d'entrée. Même lorsqu'il retourne `MATCHED_ARITHMETIC_ONLY`,
  cela ne prouve ni l'exhaustivité des mouvements, ni un P&L, et n'autorise
  **jamais** Risk ou l'exécution.
- La page Automatisation conserve aussi un brouillon des limites Risk choisies
  explicitement (devise, capital, maximum par ordre, perte journalière). Le
  brouillon est append-only, hashé et affiché `PENDING_OWNER_REVIEW`; il ne devient
  pas une politique approuvée et ne peut armer ou envoyer un ordre.
- **Mission Risk — précontrôles locaux uniquement** : l’onglet Automatisation
  affiche l'audit `GET /api/desk/risk-readiness`, en lecture seule,
  accessible seulement depuis l'origine locale. L'audit recoupe le brouillon
  explicite, la dernière observation Crypto.com, les valorisations candidates
  et le rapport de rapprochement. Il contrôle les empreintes SHA-256, les
  identifiants, la devise et les drapeaux de non-autorité. Une preuve corrompue
  provoque HTTP 503, et non une autorisation ni un affichage partiel trompeur.
  L'absence de brouillon ne crée **aucun capital ou plafond par défaut**.
  Le module `src/bubus/risk_readiness.py` permet aussi des comparaisons
  **synthétiques** de montant par ordre aux plafonds **choisis par l'utilisateur**;
  même une comparaison `PASS_LIMITS_ONLY` n'est pas une décision Risk.
  Aucun P&L journalier authentifié, équité disponible utilisable, réservation
  atomique, contrôle complet des frais, provenance Risk approuvée, politique
  Risk active ou approbation humaine transactionnelle n'est établi ici.
  L'API renvoie **toujours** `status: BLOCKED`, `armed: false`,
  `orders_enabled: false`, `usable_for_risk: false` et
  `usable_for_execution: false`; un éventuel branchement inattendu
  d'une passerelle d'ordres est remonté comme alerte, jamais comme succès.
  Le moteur pur hérité `invest_ai.risk` conserve ses tests historiques mais
  **ses décisions ACCEPT sur fixtures ne sont pas des autorisations de marché**.
  Aucun endpoint d'activation ou de création d'ordre n'est ajouté. Le
  `CryptoComSpotOrderAdapter` de BUBUS2.0 reste créé sans passerelle
  d'autorisation Risk/Policy/humaine; aucun ordre réel n'a été testé.
- **Mission chaîne d'ordres — audit et durcissement sans exécution** :
  `GET /api/desk/order-chain-audit` donne un inventaire **bloquant** des
  barrières manquantes, dans l'onglet Automatisation. Cette API est en lecture
  seule et d'origine locale; elle recalcule d'abord les preuves Risk, refusant
  toute altération (HTTP 503). Le rapport conserve `status: BLOCKED`,
  `orders_enabled: false`, `can_submit: false`,
  `retry_automatically: false` et `recovery_ready: false`.
  `src/bubus/order_chain_audit.py` permet des **replays synthétiques** : il
  détecte deux POST du même `client_oid`, un changement
  d'empreinte de l'intention, une réponse réseau incertaine, un accusé de
  réception confondu avec une exécution, des identités incompatibles lors
  du `get-order-detail`, une régression de quantités exécutées et un statut
  final redevenu actif. Le seul accès privé de statut est
  `POST /api/cryptocom/orders/readback`, local et même origine : il exige une
  clé enregistrée dans le coffre en lecture seule, un `client_oid` déjà présent
  dans le journal durable, et un état `ATTEMPTED`, `UNCERTAIN` ou
  `ACKNOWLEDGED`. Il vérifie la réponse du `get-order-detail` puis l'ajoute au
  journal. Il ne crée aucun ordre, ne relit jamais un identifiant inconnu et
  n'autorise aucun réessai. Un statut terminal
  peut inclure une **exécution partielle** (ex. CANCELED avec volume exécuté);
  il faut toujours rapprocher ces mouvements et frais du compte.
  L'adaptateur Spot existant refuse désormais les identifiants de retour
  invalides, statuts Exchange inconnus, montants non finis/hors bornes,
  horodatages malformés ou futurs (marge d'horloge de cinq secondes pour les
  réponses horodatées), et frais avec devise explicitement invalide.
  La devise des frais absente reste `None`, jamais convertie implicitement.
  Une protection **en mémoire par instance** bloque les soumissions répétées
  d'un même `client_oid` après un refus ou une réponse incertaine; elle est
  thread-safe mais **ne résiste PAS** à un redémarrage ni à plusieurs processus.
  Cette protection n'est donc **pas** une idempotence de production.
  Une panne réseau, un timeout, un HTTP 5xx ou une réponse inconnue ne
  prouvent jamais qu'aucun ordre n'a été exécuté. **Ne jamais renvoyer
  automatiquement** la même intention : relire l'état par `client_oid`,
  comparer les identités et les cumuls, conserver les états incertains comme
  bloqués. Un résultat "introuvable" n'est pas une autorisation de réessai.
  Les exigences non réalisées restent : route d'envoi d'ordre, approbation
  Risk/Politique/humaine atomique et à usage unique, réservations atomiques, rapprochement
  exhaustif des soldes/fills/frais, réconciliation après crash et contrôle QA
  indépendant avant tout déploiement. La relecture manuelle ne constitue pas
  une reprise automatique après crash et le compte n'a pas encore de tentatives
  réelles consignées.
  Les tests d'échec, de concurrence et de récupération utilisent uniquement
  des **fausses passerelles et réponses locales**. Aucun ordre réel n'est
  envoyé et aucune clé dotée du droit d'ordre n'a été lue ou créée.
- **Mission 5 — journal transactionnel durable de simulation hors ligne** :
  `src/bubus/order_journal.py` crée un fichier SQLite **distinct**
  `data/offline-order-journal.sqlite3` (ou dans le répertoire local configuré
  au lancement). Il enregistre uniquement des **intentions fictives**
  `CryptoComSpotLimitOrder` validées, leurs empreintes canoniques et des
  événements explicitement **synthétiques**. Il ne stocke jamais les
  identifiants secrets API et n'effectue aucun appel réseau ni ordre.
  - `client_oid` est UNIQUE au niveau SQLite et reste réservé après
    redémarrage; un même UUID, même avec une intention identique, est refusé.
    Les transactions `BEGIN IMMEDIATE`, le journal WAL et `synchronous=FULL`
    sérialisent les écritures locales et renforcent leur durabilité.
  - Les événements sont ajoutés dans un ordre strict (`INTENT_RECORDED`,
    `SIMULATED_ATTEMPT_RESERVED`, `RESPONSE_UNCERTAIN`,
    `ACK_OBSERVED`, `READBACK_OBSERVED`, `READBACK_FAILED`).
    Chaque entrée possède un numéro continu et une empreinte liée à
    l'empreinte précédente; le compteur et la tête sont vérifiés. Les
    écritures/suppressions directes des événements sont empêchées par des
    triggers SQLite ordinaires. Il ne s'agit **pas** d'une preuve
    cryptographique d'authenticité : une personne pouvant altérer la base et
    recalculer toutes les empreintes peut contourner ce mécanisme.
  - Une interruption entre la réservation et la réponse reste
    `BLOCKED_UNCERTAIN` après réouverture. Un ACK ne prouve pas un fill; une
    lecture de statut n'accorde ni approbation Risk, ni droit de réémettre.
    Les quantités contradictoires restent visibles comme bloquées. Une
    transition invalide ou une corruption refusée **avant COMMIT** est annulée
    par `ROLLBACK`.
  - La méthode `audit()` vérifie **toutes** les entrées persistées, même si
    l'interface n'affiche que les vingt plus récentes. L'API
    `GET /api/desk/order-journal` et le panneau
    « Journal durable des essais hors ligne » d'Automatisation sont en
    **lecture seule**, sur l'origine locale et renvoient HTTP 503 sur une
    corruption. Aucun POST de simulation ou d'ordre n'est exposé.
  - Tous les états retournent `can_submit: false`,
    `can_retry_automatically: false`, `orders_enabled: false` et
    `usable_for_execution: false`; le seul mode de reprise est **examen
    manuel**. Les tests incluent un processus Python distinct, plusieurs
    connexions concurrentes, corruption, idempotence après redémarrage et
    relecture d'une réponse incertaine.
  Ce journal **n'est jamais injecté** dans `CryptoComSpotOrderAdapter`, ni
  utilisé comme `OrderAuthorizationGate`. Le **vrai** journal de production,
  l'idempotence multiprocessus associée au broker, les réservations de fonds,
  la validation humaine et la réconciliation authentifiée restent à développer
  puis à auditer avant d'envisager le trading réel. **Ne jamais interpréter
  ce résultat comme l'autorisation d'activer des ordres.**
- **Mission 6 — approbations Risk et réservations atomiques en simulation** :
  `src/bubus/offline_risk_reservations.py` contient une seconde base SQLite
  **entièrement fictive**, `data/offline-risk-reservations.sqlite3`, séparée
  du journal d'ordres de la mission 5, des relevés Crypto.com et de Risk réel.
  Aucune politique de capital n'est créée par défaut : un scénario doit
  déclarer explicitement la devise, le capital, le plafond par ordre, la
  perte quotidienne fictive, sa limite et le coussin de frais. Ces valeurs
  sont **des fixtures de test**, jamais les soldes ou performances du compte.
  Les scénarios enregistrés sont immuables; pour recommencer des tests,
  utiliser une **nouvelle base de simulation** et ne jamais écraser les preuves
  existantes. Les approbations ont un identifiant UUID unique, sont liées
  à un `client_oid`, à l'empreinte canonique d'une intention Spot LIMIT,
  à une révision de scénario et à une échéance maximale de trente minutes.
  Le champ d'approbateur est une **étiquette synthétique**, pas une identité
  authentifiée ni une signature humaine.
  Seuls les ordres fictifs **BUY** dont la devise de cotation correspond au
  scénario peuvent réserver du capital. Les SELL exigeraient des stocks
  d'actifs fiables et sont donc refusés. Le montant bloqué est calculé en
  `Decimal`, frais fictifs explicitement inclus, arrondi **vers le haut**
  à huit décimales. Ni le budget ni le plafond par ordre ne peuvent être
  dépassés; la simulation refuse les devises différentes, les expirations,
  les montants non finis et une perte journalière fictive atteignant la
  limite choisie.
  `consume_and_reserve_simulated` fait une transaction SQLite
  `BEGIN IMMEDIATE` : **une approbation consommée et sa réservation de
  capital sont inscrites ensemble**, avec un reçu de consommation
  immuable et vérifié. Les UUID et reçus sont uniques et persistants après
  un redémarrage, les écritures concurrentes ne peuvent pas consommer le
  même budget deux fois; en cas d'arrêt **avant COMMIT**, la transaction
  incomplète est annulée. Après réservation, aucun montant n'est jamais
  libéré automatiquement, même si l'approbation expire ou si un statut
  d'ordre reste incertain.
  `GET /api/desk/offline-risk-reservations` expose uniquement l'audit
  local **en lecture seule**, également visible dans Automatisation sous
  « Réservations Risk — simulation ». La réponse refuse toute incohérence
  des montants, de la configuration, des intentions et des reçus (HTTP 503).
  Les valeurs monétaires affichées sont clairement indiquées comme
  fictives; quand aucun scénario n'existe, le capital vaut `null`.
  Les méthodes de simulation ne sont **pas** exposées en POST.
  Chaque résultat reste `BLOCKED_SIMULATION_ONLY`, avec
  `can_submit: false`, `can_retry_automatically: false`,
  `orders_enabled: false` et `usable_for_execution: false`.
  **Aucune réservation du compte réel ni autorisation Risk authentifiée** :
  ce moteur n'implémente jamais `OrderAuthorizationGate`, ne fabrique pas
  d'`OrderAuthorization` et n'est injecté dans aucun adaptateur d'ordre.
  L'approbation humaine réelle, les mouvements/balances authentifiés,
  l'atomicité **du circuit d'exécution réel** entre le journal d'ordres et Risk, les réservations
  de capital par actif, les plafonds globaux inter-comptes, la reprise
  authentifiée après incident et le contrôle QA indépendant restent
  obligatoires avant d'envisager toute mise en marché.
  Les empreintes SHA-256 locales ne constituent pas une preuve externe contre
  une altération malveillante de la base. Les tests utilisent exclusivement
  des données synthétiques, deux connexions concurrentes, plusieurs
  processus, des arrêts brutaux et des corruptions simulées.
- **Mission 7 — preuve atomique commune, banc hors ligne seulement** :
  `src/bubus/offline_trade_transaction.py` réunit le journal fictif et les
  réservations fictives dans `data/offline-trade-transaction.sqlite3`, une
  base distincte. `record_and_reserve` enregistre l'intention, consomme son
  approbation synthétique à usage unique, réserve le capital fictif et ajoute
  le reçu lié dans **une seule transaction SQLite `BEGIN IMMEDIATE`**. Avant
  le commit, les deux journaux complets sont revérifiés; toute erreur annule
  tous les changements. Les tests couvrent le rollback après écriture, les
  demandes concurrentes, le refus de sur-réservation et la détection d'une
  altération. `GET /api/desk/offline-trade-transaction` expose uniquement un
  audit local; aucun POST d'intention, d'approbation ou d'ordre n'existe.
  Cette preuve est **entièrement synthétique**, n'est pas une approbation
  humaine, ne réserve aucun fonds réel et n'est pas reliée au Risk de
  production ni à l'adaptateur Crypto.com. Le circuit réel reste bloqué.
- Crypto.com accepte jusqu'à 512 abonnements observés par connexion; 568 paires
  Spot ont été confirmées sur deux connexions dans le parcours de l'application
  (561 ticks distincts reçus pendant l'essai). La documentation consultée
  décrit les canaux par instrument mais ne précise pas ce plafond numérique.
