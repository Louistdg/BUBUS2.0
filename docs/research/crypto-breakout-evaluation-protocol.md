# Protocole de recherche — stratégie de cassure Spot Crypto.com

**Date de cadrage : 10 octobre 2026**

**Statut : protocole de recherche proposé, aucune performance validée, aucun ordre réel**

## 1. Question étudiée

Évaluer si la règle expérimentale de cassure Spot `BREAKOUT_VOLUME_TOP_BOOK_V0` conserve un résultat net positif et robuste après coûts, sur des périodes et marchés qui n'ont pas servi à régler ses paramètres.

Ce protocole ne fixe ni capital de trading réel ni autorisation d'exécution. Les simulations utilisent un indice de capital normalisé et restent séparées de l'approbation de politique Risk.

## 2. Univers de départ

Univers candidat : **BTC_USD, ETH_USD, SOL_USD et XRP_USD**, Spot uniquement, sans levier. Les paires USD sont retenues afin d'éviter d'introduire dès le départ une conversion USD/EUR ou une paire stablecoin.

Un contrôle public Crypto.com effectué le 10 octobre 2026 vers 14:38 UTC a observé les volumes notionnels 24 h et spreads suivants :

| Paire | Notionnel 24 h affiché en USD | Spread bid/ask observé |
|---|---:|---:|
| BTC_USD | 138,37 M$ | 0,00 bps |
| ETH_USD | 65,01 M$ | 0,04 bps |
| SOL_USD | 9,18 M$ | 0,90 bps |
| XRP_USD | 6,70 M$ | 0,71 bps |

Ces chiffres sont un instantané, pas une preuve de liquidité durable; le spread nul de BTC_USD doit être traité comme une observation possiblement arrondie. Garder une paire dans l'étude seulement si elle satisfait, sur la période de calibration, les critères préétablis suivants : médiane du notionnel quotidien d'au moins **5 M USD**, données journalières valides au moins **99 %** du temps et spread observé en PAPER dans les seuils configurés. Les règles de sélection sont calculées sans consulter le holdout final. Si XRP ou SOL échoue, l'exclure selon cette règle et documenter la raison; ne pas le remplacer en regardant les performances du holdout.

## 3. Périodes et données

- **Bougies principales : 1 h**, UTC, du 1 janvier 2022 au 30 septembre 2026, ou depuis la première date réellement disponible pour chaque paire. Conserver les trous comme trous; ne pas interpoler OHLCV.
- **Bougies de contrôle : 1 jour**, sur la même période, pour la classification de régime et les contrôles d'intégrité.
- **Développement/réglage :** 1 janvier 2022–31 décembre 2024.
- **Validation chronologique :** 1 janvier–31 décembre 2025; ne pas régler les paramètres sur cette période.
- **Holdout final :** 1 janvier–30 septembre 2026; le consulter une seule fois après gel du code, des paramètres, des coûts et des règles d'exclusion.
- Chaque décision au temps `t` ne peut employer que des données dont `available_at <= t`. La cassure calculée à la clôture d'une bougie ne peut être exécutée avant l'ouverture suivante.

Le chargeur actuellement intégré à BUBUS récupère et conserve au plus 120 bougies par paire/période. Des appels publics directs testés aujourd'hui acceptent `start_ts`/`end_ts` et renvoient au plus 300 bougies par requête; l'API a renvoyé une fenêtre historique demandée en 2024 et a tronqué une requête pluriannuelle aux 300 bougies récentes. Une collecte par pages arrière semble donc nécessaire. Avant le backtest, elle doit être implémentée et validée contre les bornes, doublons, trous, timestamps UTC et empreintes des réponses brutes. Les quatre années ne sont pas encore présentes dans le dépôt.

Pour chaque page archivée, garder : fournisseur et endpoint, paire, période, paramètres exacts, heure UTC de collecte, corps JSON brut ou son emplacement durable, SHA-256, première/dernière bougie, nombre reçu, doublons/trous détectés et version du parseur. N'utiliser que des bougies fermées, OHLCV positif/valide, sans barres futures ou dupliquées.

**Limite de couverture à respecter :** l'API historique fournit OHLCV, pas un carnet d'ordres historique. La variante historique ne pourra tester que le noyau cassure/prix/volume. Elle ne validera pas les filtres spread et déséquilibre top-of-book de `BREAKOUT_VOLUME_TOP_BOOK_V0`. Ces filtres ne pourront être évalués en conditions observées qu'à partir d'une collecte prospective de quotes/carnets horodatés et empreintés. Ne pas présenter le proxy historique comme le backtest complet de la stratégie.

## 4. Comparaison sans avantage indu

Comparer sur les mêmes paires, bougies, dates, règles de disponibilité et modèle de coûts :

1. **Ne rien faire :** rester en USD, rendement 0 %, aucun coût. C'est le contrôle de base.
2. **Buy-and-hold :** achat au début de chaque période évaluée puis conservation, avec coût d'entrée et de sortie cohérent.
3. **SMA 3/10 :** référence simple déjà présente dans le comparateur.
4. **Cassure prix/volume :** règle historique explicitement versionnée. Ne pas mélanger ses sorties avec celles du signal live détaillé.

Réutiliser les mêmes instants d'exécution et capital normalisé. Les signaux à la clôture remplissent à l'ouverture suivante; comptabiliser immédiatement les coûts et l'exposition. Aucun capital ou solde Crypto.com n'est lu.

### Frais et glissement

La grille officielle consultée le 10 octobre 2026 affiche pour le niveau Spot 1 sans solde CRO **0,25 % maker / 0,50 % taker**. Utiliser **0,50 % par côté exécuté** comme cas de base conservateur (une entrée et une sortie coûtent chacune des frais), sans créditer de réduction personnelle, CRO, promotion ou fill maker supposé. La grille changeant, enregistrer sa copie/version avec le rapport.

Présenter une sensibilité de glissement de **5, 10, 25 et 50 bps par côté**, en plus des frais; ces valeurs sont des scénarios de stress, pas des mesures observées. En PAPER, mesurer le spread et la différence entre prix de référence au signal, prix simulé et cotation ultérieure. Ne pas compter le spread deux fois : préciser pour chaque modèle s'il est inclus dans le prix d'exécution ou ajouté au glissement.

Les pertes stop/target intrabougie demandent une convention conservatrice. Si stop et cible sont tous deux touchés dans la même bougie et que l'ordre des ticks est inconnu, supposer le stop exécuté en premier; appliquer un slippage de gap si l'ouverture dépasse le stop. Documenter aussi toute limite de fill des ordres LIMIT.

## 5. Mesures et critères de robustesse

Rapporter séparément pour chaque paire, stratégie, période, régime et scénario de coûts :

- rendement net cumulé, rendement annualisé seulement si la fenêtre est suffisante, et rendement relatif au buy-and-hold;
- drawdown maximal sur la courbe de valeur, avec traitement conservateur des extrêmes intrabougie;
- nombre d'allers-retours terminés, taux gagnant, gain/perte moyen, frais et glissement cumulés;
- durée médiane et moyenne des positions, temps exposé, nombre de signaux et signaux refusés;
- résultats avant/après coûts et sensibilité aux hypothèses de frais/glissement;
- résultat sur tendances haussières/baissières et périodes de volatilité haute/basse. Les régimes sont dérivés uniquement de données passées (par exemple position par rapport à SMA 200 jours et volatilité réalisée passée), sans réétiqueter avec des informations futures.

Un résultat est **inconclusif**, pas positif, si le nombre de trades est trop faible, si le résultat net devient négatif avec le coût de base, si l'essentiel du gain vient d'un seul actif/régime, ou si un léger changement de paramètre inverse le résultat. Exiger une analyse de stabilité autour des paramètres retenus sur le seul segment de développement; ne pas optimiser sur validation/holdout.

## 6. Revue du simulateur avant tout résultat

Le comparateur actuel `src/bubus/research.py` n'est pas encore apte à produire ce rapport :

- il ne teste pas le signal détaillé avec carnet/spread;
- sa règle breakout est différente (10 bougies et 1,5× volume moyen, contre seuils médian et carnet distincts dans le module stratégie);
- les calculs de rendement, de frontière de période, d'exposition et de drawdown doivent être corrigés et couverts par des tests à résultats attendus;
- les trois sous-fenêtres actuelles sont des tranches du même test, pas trois échantillons indépendants;
- buy-and-hold n'est pas une référence flat/cash.

Avant tout rapport, écrire des tests déterministes pour entrée à la frontière, premier rendement de test, signal à la clôture / fill à l'open suivant, frais d'entrée/sortie, position ouverte en fin de test, gaps, stop et cible touchés dans une bougie, drawdown, disponibilité point-in-time et scénario cash. Vérifier manuellement au moins une petite série dont le résultat exact est calculable.

## 7. PAPER prospectif, sans transmission

Le dépôt possède un simulateur pur `src/invest_ai/execution/simulator.py` et un scanner live qui rapporte des observations, mais ils ne sont pas reliés au flux d'étude BUBUS. Le simulateur PAPER reçoit une snapshot explicite; il ne collecte pas lui-même le marché et n'établit pas une courbe persistante de stratégie.

Après validation des tests historiques, raccorder un journal PAPER local append-only qui enregistre pour chaque signal : règle/version, paire, bougie close et heure de disponibilité, bid/ask et tailles, spread, snapshot hash, décision, fill hypothétique avec frais/slippage, sortie, résultat net et raison de non-fill. Aucun endpoint d'ordre, outbox réelle ou credential n'est utilisé.

Observer au moins **90 jours** et viser **50 allers-retours terminés agrégés** avant une revue; si le seuil de trades n'est pas atteint, prolonger et déclarer la preuve insuffisante. Comparer les références d'entrée/sortie avec les cotations réellement observées après signal, en conservant les non-fills et les données manquantes. Toute lacune de flux, horloge, carnet incomplet ou prix périmé invalide l'observation correspondante, jamais en la remplaçant par un prix reconstruit favorable.

## 8. Ordre de travail et sortie attendue

1. Corriger et tester le simulateur sur séries synthétiques à résultats exacts.
2. Ajouter une collecte historique paginée, traçable et persistée; vérifier la couverture de chaque paire et période.
3. Implémenter le rapport équitable et le protocole de séparation/régimes/coûts; geler les paramètres avant validation et holdout.
4. Produire les résultats par période et paire avec limites visibles; résultat possible : négatif ou inconclusif.
5. Seulement après cela, raccorder le mode PAPER prospectif, sans pouvoir de transmission, et observer la durée/le nombre de trades prévus ci-dessus.

Le rapport final doit inclure les SHA du code et du jeu de données, les requêtes/source, empreintes, dates, couverture, règles d'exclusion, paramètres, frais, sensibilités, résultats par segment, tests du simulateur, nombre de trades PAPER, non-fills et limites. Aucun résultat ne vaut approbation Risk, autorisation d'ordre ou promesse de gain.
