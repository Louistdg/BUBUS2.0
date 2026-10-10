# BUBUS2.0

Interface locale de suivi des marchés et de recherche sur données de marché, reliée à Crypto.com Exchange. Le projet comprend aussi des fondations de portefeuille en lecture seule, de contrôles Risk fail-closed et un adaptateur Spot LIMIT.

> **État important : le trading automatique réel n’est pas opérationnel.** La passerelle d’autorisation Risk/politique/approbation humaine n’est pas raccordée à l’envoi d’ordres. Les contrôles de portefeuille et de rapprochement restent incomplets. Ne configurez pas de clé de trading pour tenter d’activer des ordres.

## Démarrage local

Sous Windows, lancez `bubus.cmd`. Au premier démarrage, le script crée un environnement Python isolé et installe les dépendances. Consultez [START-HERE.md](START-HERE.md) pour les limites fonctionnelles et les consignes de lancement.

## Sécurité et données

- Les clés et secrets ne doivent jamais être commités. Utilisez uniquement le coffre local prévu par l’application.
- `.env`, bases locales, données, journaux, sauvegardes et notes de reprise sont exclus par Git.
- Aucune clé ou donnée de compte n’est requise pour les tests.
- L’aperçu cloud éventuel est séparé, en lecture seule et ne constitue pas le service de trading.

## Tests

Les tests utilisent des réponses et données locales/synthétiques. Ils ne prouvent ni la rentabilité d’une stratégie ni l’aptitude à exécuter des ordres en production.

À la préparation de cette publication, un lot ciblé a donné 85 tests réussis et 3 échecs dans `tests/bubus_v2/test_research.py`; le projet n’est donc pas présenté comme validé. Le test local `tests/llm/test_benchmark.py` et son corpus ont été omis : ils dépendaient d’une base privée absente du dépôt.
