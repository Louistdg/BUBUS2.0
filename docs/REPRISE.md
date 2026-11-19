# BUBUS 2.0 — Dossier de reprise

*État au 19 novembre 2026 (mis à jour après le retrait de Crypto.com et l'ajout d'OpenRouter). Ce dossier est destiné à la personne qui reprend le projet.*

## 1. Le projet en deux phrases

BUBUS est l'outil personnel de suivi des marchés (cryptos et actions) de Louis.
Il existe en deux versions :

- une **version PC** (Windows, `bubus.cmd`), complète et locale ;
- une **version en ligne** sur Railway, installable comme appli sur son téléphone Android (PWA).

Le propriétaire ne parle pas anglais : **toute l'interface et tous les messages doivent être en français**, avec des mots simples.

## 2. Règles à ne jamais casser

1. **Aucun ordre de bourse.** Le trading réel est bloqué (`orders_enabled: false` partout). BUBUS lit, il n'achète et ne vend rien.
2. **Lecture seule sur les comptes** (Revolut X) : uniquement des clés d'API en lecture, sans retrait ni trading.
   **Crypto.com est retiré** (décision de Louis, 19/11/2026) : seul Revolut X sera branché.
3. **Aucun secret dans le code ni dans un message.**
   - Sur le PC, les clés vont dans le coffre-fort de Windows (DPAPI ou `cmdkey`).
   - En ligne, elles vont dans les **Variables** de Railway.
   - Les clés en ligne doivent être **dédiées à BUBUS**.
4. **Pas de conseil d'investissement personnalisé.** L'IA et la note de risque décrivent ce qui s'est passé ; elles ne recommandent rien.
5. **Version en ligne** : connexion obligatoire (identifiant + mot de passe d'au moins 24 caractères), et seules les routes de la liste blanche sont exposées (`_AUTHENTICATED_GET_PATHS` dans `cloud_preview.py`).

## 3. Où est le code

- **Dépôt GitHub** : `Louistdg/BUBUS2.0`.
- **Branche de travail** : `feat/appli-android-discussion-ia`. Tout y est poussé. **Elle n'est pas encore fusionnée dans `main`**, et la demande de fusion (PR) n'a pas été créée.
- **Autre branche** : `origin/ccr-00cafae5-fjss7k` corrige 3 tests de `test_research`. Elle n'est pas fusionnée.
- **Railway déploie automatiquement** la branche `feat/appli-android-discussion-ia` à chaque envoi (`git push`).

Commits de la branche, du plus récent au plus ancien :

| Commit | Contenu |
|---|---|
| `69de6bc` | Ma liste : cryptos et actions avec note de risque 1 à 5 |
| `0eff5cb` | Discussion en ligne : explication sur la connexion ChatGPT |
| `eecedae` | Connexion ChatGPT (version PC), finitions mobiles |
| `f331d9a` | Guide Railway |
| `0487cf7` | Appli Android installable et discussion IA |

### Fichiers importants (`src/bubus/`)

| Fichier | Rôle |
|---|---|
| `server.py` | Serveur HTTP local (`DeskHandler`), toutes les routes `/api/desk/*`. `make_server()` assemble les services. |
| `cloud_preview.py` | Version en ligne : page de connexion, cookie de session signé (HMAC, 30 jours), limitation des essais, liste blanche des routes, bandeau « BUBUS EN LIGNE ». |
| `assistant_chat.py` | Discussion IA : OpenRouter (modèle par défaut `anthropic/claude-sonnet-4.6`), Claude (SDK `anthropic`, modèle `claude-opus-5-5`) ou OpenAI, quota de messages par jour, relevé joint aux questions (« Ma liste » + CoinGecko). |
| `ai_keys.py` | Clé OpenRouter saisie dans l'appli PC, chiffrée par DPAPI (`data/openrouter-key.bin`), jamais renvoyée au navigateur. |
| `chatgpt_signin.py` | « Continuer avec ChatGPT » (PC seulement) : OAuth PKCE en boucle locale, jetons chiffrés par DPAPI, appel de `/v1/responses`. |
| `watch_risk.py` | « Ma liste » : cours Yahoo Finance (cryptos `BTC_USD` → `BTC-USD`, et actions), évolution, note de risque 1 à 5, recherche. |
| `store.py` | SQLite local : favoris crypto (`watchlist`), actions (`stock_watchlist`), journal d'activité. |
| `market.py` | Bougies Crypto.com (cache). Plus appelé par l'interface depuis le retrait de Crypto.com. |
| `ui/` | Interface : `index.html`, `desk.js`, `desk.css`, `manifest.webmanifest`, `sw.js`, icônes. |

### Documentation

- `docs/railway-appli-android.md` : installation sur Railway, variables, installation sur Android.
- `START-HERE.md` : vue d'ensemble de la version PC.

## 4. Lancer et tester

**Version PC** : double-cliquer sur `bubus.cmd`. L'interface s'ouvre sur `http://127.0.0.1:8790`.

**Copie locale de la version en ligne** (pour tester) :

```bash
PYTHONPATH=src BUBUS_PREVIEW_USERNAME=essai BUBUS_PREVIEW_PASSWORD=<24 caractères ou plus> \
BUBUS_PREVIEW_REQUIRE_HTTPS=false PORT=8795 python -m bubus.cloud_preview
```

**Tests** (Python 3.13) :

```bash
PYTHONPATH="src;." python -m pytest -q --continue-on-collection-errors
```

Résultat actuel : **818 réussis, 18 échecs et 2 erreurs de chargement, tous antérieurs à ce travail** :

- 15 échecs viennent de fichiers privés absents (`tests/assistant/test_dashboard.py`, `tests/test_knowledge_core.py`) ;
- 3 échecs sont dans `test_research`, corrigés sur la branche `ccr-00cafae5-fjss7k` ;
- les 2 erreurs de chargement sont `tests/policy/test_engine.py` et `tests/trading_engine/test_backtrader_benchmark.py`.

Tests du travail récent : `tests/bubus_v2/test_cloud_app.py`, `test_chatgpt_signin.py` et `test_watch_risk.py`.

## 5. Railway (version en ligne)

- **Compte** : le nouveau compte de Louis (connexion Google, avec abonnement).
- **Démarrage** : `python -m bubus.cloud_preview` ; vérification de santé sur `/healthz` ; port 8080.
- **Variables existantes** : `PYTHONPATH=src`, `BUBUS_PREVIEW_USERNAME`, `BUBUS_PREVIEW_PASSWORD`.
- **Le même compte Railway contient un autre projet (« SOCLE-V2 ») : ne pas y toucher.**
- **Un ancien projet BUBUS existe sur un autre compte Railway (Outlook)** : il faut l'arrêter ou le supprimer pour éviter de payer deux fois.

## 6. Ce qui marche aujourd'hui

- **Version en ligne** :
  - connexion et appli Android installable (manifeste, service worker, icônes) ;
  - affichage adapté au téléphone (barre de navigation en bas) ;
  - marchés : cryptos CoinGecko (Crypto.com est retiré de l'interface ; son code serveur reste en place mais n'est plus appelé).
- **Discussion IA** : prête. OpenRouter est accepté (variable `OPENROUTER_API_KEY` en ligne, case dans l'onglet Discussion sur le PC). **Aucune clé n'est encore branchée en ligne** (voir 7.1).
- **« Ma liste »**, sur PC et en ligne :
  - recherche par nom, cryptos et actions, via Yahoo Finance ;
  - prix, évolution sur 1 jour, 1 semaine et 1 mois ;
  - note de risque calculée ainsi :
    - agitation quotidienne sur environ 1 mois, ramenée à l'année : seuils 20, 35, 60 et 90 % ;
    - plus forte baisse sur 3 mois : seuils 10, 20, 35 et 50 % ;
    - la note retenue est la plus haute des deux.
- **Version PC** : bouton « Continuer avec ChatGPT » prêt.

## 7. À faire, par ordre d'importance

1. **Brancher une IA en ligne.** Ajouter dans les Variables Railway :
   - de préférence `OPENROUTER_API_KEY` (choix de Louis) ;
   - ou `ANTHROPIC_API_KEY` (Claude) ;
   - soit `OPENAI_API_KEY` + `BUBUS_AI_MODEL`.

   La clé doit être dédiée à BUBUS, avec une limite de dépense mensuelle réglée chez le fournisseur.

   Note : la connexion « Sign in with ChatGPT » pour un site hébergé demande l'accord d'OpenAI (formulaire partenaire, liste d'attente). Le formulaire est rempli mais **pas envoyé**. Seule la version PC peut l'utiliser aujourd'hui.
2. **Ajouter un volume Railway** (dossier `/data`). Sans lui, « Ma liste » s'efface à chaque déploiement. Le code utilise `RAILWAY_VOLUME_MOUNT_PATH` automatiquement.
3. **Vérifier que Yahoo Finance répond depuis Railway.** C'est un service non officiel et il peut bloquer les serveurs.
   - Symptôme : les actions affichent « Cours indisponible ».
   - Si c'est le cas, passer à une source officielle avec clé (par exemple Financial Modeling Prep, Alpha Vantage ou Twelve Data) dans `watch_risk.yahoo_daily` et `yahoo_search`.
4. **Tester « Continuer avec ChatGPT » avec le vrai compte de Louis**, sur le PC. Ce test n'a jamais été fait.
5. **Fusionner dans `main`** : créer la demande de fusion (PR) de `feat/appli-android-discussion-ia`, et fusionner aussi `ccr-00cafae5-fjss7k`.
6. **Partie 3 du plan : voir son portefeuille en ligne, en lecture seule.**
   - Revolut X : API signée Ed25519, en-têtes `X-Revx-API-Key`, `X-Revx-Timestamp` et `X-Revx-Signature`, base `https://revx.revolut.com`.

   Une clé Revolut X en lecture seule a été créée, mais **jamais testée** : Louis doit d'abord la ranger dans le coffre Windows avec `cmdkey /generic:BUBUS_REVOLUTX_API_KEY /user:bubus /pass`. Le script d'essai `tester_revolutx.py` est local et non versionné.
7. **Partie 5 du plan : alertes et résumé du matin**, en s'appuyant sur `store.my_list()` et `WatchRisk.describe_all()`. Exemples : variation forte, passage d'une note à 4 ou 5. Le canal reste à choisir (notification web sur l'appli, ou e-mail).

## 8. Fichiers locaux non versionnés

Ces fichiers sont exclus via `.git/info/exclude` :

- `tester_revolutx.py` ;
- `.claude/` ;
- `tools-local/` (scripts d'essai pour le PC).
