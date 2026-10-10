# Approbation locale d’une politique Risk par passkey

## Objectif

Permettre au propriétaire de vérifier puis d’approuver explicitement une politique Risk complète avec Windows Hello/FIDO2 dans l’application locale BUBUS2.0. L’approbation prouve l’usage local d’un justificatif avec vérification utilisateur; elle ne prouve pas l’identité civile et n’active jamais le trading.

## État de départ

- `RiskPolicyRegistry` enregistre des révisions immuables `PENDING`, sans mécanisme d’approbation.
- La page actuelle ne conserve qu’un brouillon de trois montants. Ce brouillon n’est pas une `RiskPolicy` et ne peut pas être promu implicitement.
- Le serveur local n’expose pas l’autorité du registre dans son parcours utilisateur.
- Le système doit continuer à répondre `BLOCKED` / `NOT_READY`, `armed=false` et `orders_enabled=false` après cette fonctionnalité.

## Comportement

1. L’application locale permet d’enregistrer une passkey avec une cérémonie WebAuthn exigeant `userVerification=required`. Elle conserve uniquement les données publiques de justificatif nécessaires à la vérification. La biométrie et la clé privée restent sous le contrôle de Windows/navigateur.
2. Une politique Risk complète est présentée à l’utilisateur avant approbation. Toutes ses limites et permissions sont explicites; aucun champ absent n’est remplacé par un défaut, le solde du compte ou une estimation IA. Le brouillon à trois montants reste non éligible.
3. Une approbation exige un challenge WebAuthn récent, aléatoire, à usage unique, attaché à la finalité `approve-risk-policy`, au revision ID et à son SHA-256 canonique, à l’audit Risk courant et à l’état `control_epoch` courant. L’utilisateur voit clairement les montants/permissions exacts concernés.
4. La validation échoue si le challenge est expiré, déjà consommé, d’une autre finalité/origine, si la politique ou son empreinte a changé, si l’audit courant ou l’époque de contrôle a changé, si le système est halted, ou si la vérification utilisateur WebAuthn manque.
5. L’approbation produit un reçu append-only, vérifiable au redémarrage, lié au justificatif et aux empreintes approuvées. Les révisions Risk existantes demeurent immuables.
6. L’interface indique « Politique approuvée localement; exécution toujours désactivée ». Aucun endpoint d’ordre, outbox active, activation du kill switch ou permission de trading n’est ajouté.

## Périmètre technique

- Application locale sur origine HTTPS sécurisée ou `localhost`; configuration unique et stricte de RP ID et d’origine, sans accepter arbitrairement `Host`, `Origin` ou `Forwarded` reçus.
- Bibliothèque serveur WebAuthn maintenue, version fixée dans les dépendances; vérification de la signature, challenge, origine, RP ID et user verification côté serveur.
- Stockage SQLite additif des justificatifs, challenges et reçus. Challenges expirant au plus tard après deux minutes, consommés atomiquement une fois. Enregistrement du premier justificatif réservé à l’initialisation locale; aucune route HTTP de réinitialisation ou suppression du justificatif.
- Réutiliser le `RiskPolicy` et le registre existants; ajouter les reçus d’approbation dans une table séparée et append-only plutôt que mettre à jour `risk_policy_revisions`.
- La route n’accepte jamais d’état d’approbation, digest ou contrôle faisant autorité depuis le navigateur : elle les relit et les recalcule depuis les magasins locaux de confiance.

## Hors périmètre

- Envoi ou préparation d’un ordre réel, armement ou autorisation de l’outbox.
- Déploiement Railway/cloud et usage depuis une origine distante.
- Validation des valeurs financières par l’IA ou conversion automatique du brouillon courant en politique complète.
- Synchronisation cloud, compte multi-utilisateur, récupération à distance ou identité civile.

## Critères d’acceptation

- Un test de migration ouvre une base existante sans perdre ses révisions, son kill switch ou son époque de contrôle.
- Des tests positifs et négatifs couvrent inscription et assertion WebAuthn, user verification obligatoire, challenge unique/expiré, mauvais RP/origine, rejeu, changement de digest, changement de `control_epoch`, système halted, et altération d’un reçu.
- Une politique complète peut être enregistrée comme candidate puis approuvée uniquement avec assertion passkey valide; le reçu survit au redémarrage.
- Un brouillon à trois champs, une politique incomplète ou une entrée contenant des limites implicites ne peut pas être approuvée.
- Après tout parcours, l’audit de readiness reste bloqué et l’état d’exécution reste désarmé.
- Vérification manuelle locale documentée avec Windows Hello/FIDO2 dans le navigateur réellement utilisé; les tests automatisés seuls ne prétendent pas valider ce matériel.
