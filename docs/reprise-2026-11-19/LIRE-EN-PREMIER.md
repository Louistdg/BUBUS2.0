# Reprendre BUBUS au 19 novembre 2026

Application hébergée sur Railway, projet bubus, service BUBUS2.0. Dépôt Louistdg/BUBUS2.0, branche feat/appli-android-discussion-ia. Dernière révision fonctionnelle ddc11e7, suivie du correctif de test fdb2e62.

## Ce que Louis peut utiliser

- [Marchés](https://bubus20-production.up.railway.app/#markets) affiche les cryptos CoinGecko et six actions Yahoo Finance avec leur dernière heure de cotation. La page interroge les cours des actions chaque minute quand elle est visible. Une bourse fermée peut renvoyer le dernier cours de séance.
- [Mon patrimoine](https://bubus20-production.up.railway.app/patrimoine) interroge Revolut X en lecture seule pour les soldes et les transactions. La page redemande un relevé toutes les 30 secondes quand elle est visible. Les cours crypto de valorisation viennent de Revolut X, avec un taux de change Yahoo si nécessaire. Une valorisation incomplète doit apparaître comme telle.
- [Connexions](https://bubus20-production.up.railway.app/#settings) propose six thèmes, dont Prune et Sable. Le choix est conservé dans le navigateur.
- Louis indique avoir renseigné sa clé OpenRouter. Une réponse réelle de l’agent reste à confirmer depuis son interface.

## Travaux à reprendre

1. Alertes sur téléphone. Les seuils actuels restent dans le navigateur. Pour fonctionner application fermée, prévoir une surveillance sur Railway, un stockage persistant, la permission de notification du téléphone et un test de franchissement de seuil avec l’application fermée. Vérifier aussi que le même franchissement produit une seule notification.
2. Discussion. Vérifier qu’une ancienne conversation peut être rouverte après « Nouvelle discussion », un rechargement et une reconnexion. Louis a signalé qu’il perdait l’accès aux conversations précédentes.
3. Abonnement ChatGPT. Pour le service hébergé, vérifier l’approbation OpenAI de BUBUS pour l’usage de l’abonnement et la séparation des comptes. Le parcours local possède des exigences distinctes. Référence demandée par Louis dans Louistdg/tarvos-zig, fichiers src/chatgpt_plan.zig et src/chatgpt-plan.mjs. Après consentement personnel dans le navigateur, tester un appel d’outil sur un fichier fictif et vérifier le code exact dans la réponse. [Guide officiel](https://developers.openai.com/siwc/quickstart)
4. Achat et vente. L’application hébergée affiche les données en lecture seule. Louis a demandé pourquoi il ne pouvait pas passer d’ordre. Clarifier le parcours souhaité avant toute activation du trading réel.

## Vérifications et points d’entrée

Depuis la racine du dépôt, dans PowerShell :

~~~powershell
$env:PYTHONPATH='src'
python -m pytest tests/bubus_v2/test_desk.py tests/bubus_v2/test_cloud_app.py -q
~~~

Le dernier passage combiné a réussi 67 tests et signalé une assertion qui attendait encore quatre thèmes. Après correction, le test concerné a réussi seul. Le déploiement de la version fonctionnelle ddc11e7 a réussi. La réception d’une notification sur le téléphone de Louis reste à démontrer.

- Cours et calcul en euros : src/bubus/valuation.py. Accès Revolut X : src/bubus/revolutx.py.
- Routes et contrôle d’accès en ligne : src/bubus/server.py et src/bubus/cloud_preview.py.
- Marchés et thèmes : src/bubus/ui/index.html, desk.js et desk.css. Patrimoine et seuils : finance.html et finance.js dans le même dossier.
- Lors de la vérification, comparer l’heure et la devise affichées sur PC et téléphone. Garder les clés, jetons, soldes et transactions hors des traces partagées.
