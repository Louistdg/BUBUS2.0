# Mettre BUBUS en ligne et l'installer sur Android

Ce guide met la version en ligne de BUBUS sur Railway, puis l'installe sur un
téléphone Android comme une appli. Cette version affiche les marchés publics et
la discussion avec l'IA. Elle ne voit pas le portefeuille et ne passe aucun ordre.

## 1. Réglages du service sur Railway

Dans le service BUBUS sur Railway :

- **Commande de démarrage** (Settings, Deploy, Start Command) :
  `PYTHONPATH=src python -m bubus.cloud_preview`
- **Domaine public** (Settings, Networking) : génère un domaine en `https://…`.
  Railway fournit le HTTPS, que BUBUS exige.

## 2. Variables d'environnement

À saisir dans l'onglet **Variables** du service. Ne les mets jamais dans le code
ni dans un message.

| Variable | Obligatoire | Rôle |
|---|---|---|
| `BUBUS_PREVIEW_USERNAME` | oui | Ton identifiant de connexion. |
| `BUBUS_PREVIEW_PASSWORD` | oui | Ton mot de passe, **24 caractères minimum**. Le changer déconnecte tous les appareils. |
| `ANTHROPIC_API_KEY` | pour Claude | Clé API Claude (console.anthropic.com), dédiée à BUBUS. |
| `OPENAI_API_KEY` | pour OpenAI | Clé API OpenAI (platform.openai.com), dédiée à BUBUS. |
| `BUBUS_AI_PROVIDER` | non | `claude` ou `openai`. Sans elle, BUBUS prend Claude si sa clé existe, sinon OpenAI. |
| `BUBUS_AI_MODEL` | pour OpenAI | Le modèle à utiliser. Pour Claude, la valeur par défaut est `claude-opus-5-5`. |
| `BUBUS_AI_DAILY_MESSAGE_LIMIT` | non | Plafond de messages par jour, 60 par défaut. Il protège ta facture. |
| `BUBUS_SESSION_SECRET` | non | Secret de signature des sessions (32 caractères minimum). Sans lui, il est dérivé du mot de passe. |

**Limite de dépense** : règle aussi une limite mensuelle directement chez le
fournisseur (console Anthropic ou OpenAI). Le plafond de BUBUS compte les
messages, pas les euros.

## 3. Installer l'appli sur Android

1. Sur le téléphone, ouvre l'adresse `https://…` de Railway dans **Chrome**.
2. Connecte-toi avec ton identifiant et ton mot de passe.
3. Menu ⋮ de Chrome, puis **Installer l'application** (ou « Ajouter à l'écran
   d'accueil »).
4. L'icône BUBUS apparaît sur l'écran d'accueil. Elle s'ouvre en plein écran,
   directement sur la discussion. La connexion reste valable 30 jours.

## 4. Ce qui est protégé

- Connexion par mot de passe, cookie de session signé, inaccessible au JavaScript
  et refusé depuis un autre site.
- Après 8 essais ratés depuis une même adresse, la connexion s'y bloque
  15 minutes. Au-delà de 50 échecs au total, elle se bloque pour tout le monde
  pendant 15 minutes.
- La discussion n'a aucun outil : elle ne lit aucune clé d'échange et ne peut
  rien exécuter.
- Aucune route de portefeuille ou d'ordre n'existe dans la version en ligne.
