# Approbation locale Risk par passkey — Plan d’implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Permettre l’approbation auditée d’une politique Risk complète avec Windows Hello/FIDO2, sans activer ni autoriser les ordres.

**Architecture:** Ajouter à SQLite des justificatifs publics, challenges atomiques à usage unique et reçus d’approbation append-only. Le registre continue d’utiliser ses révisions immuables; un service d’approbation WebAuthn vérifie le challenge et recalcule la politique, l’audit readiness et le contrôle courant avant d’écrire un reçu. L’interface locale propose l’inscription et l’approbation explicite; aucune liaison à l’adaptateur d’ordres n’est créée.

**Tech Stack:** Python 3.14, SQLite, `RiskPolicyRegistry`, `webauthn==3.0.1`, navigateur WebAuthn, JavaScript sans framework.

**Spec:** `docs/superpowers/specs/2026-10-10-local-passkey-risk-approval.md`

## Global Constraints

- Conserver l’approbation et l’exécution comme autorités séparées; `armed=false` et `orders_enabled=false` partout.
- Les révisions `risk_policy_revisions` restent immuables; ajouter des reçus append-only.
- N’accepter que l’origine locale configurée exactement et exiger la vérification utilisateur WebAuthn.
- Challenge aléatoire, lié à une finalité, valide au plus deux minutes et consommé atomiquement une seule fois.
- Le brouillon Risk à trois montants n’est jamais promu en politique complète.
- Aucun secret de clé privée, gabarit biométrique ou matériel cryptographique privé n’est persisté par BUBUS2.0.
- Aucune approbation à distance, reset HTTP, modification des limites par défaut ou connexion à l’envoi d’ordres.

## Review Focus

- Rejeu ou course entre deux demandes d’approbation : challenge atomiquement consommé une fois; test concurrent où un seul appel réussit.
- Changement de politique entre affichage et assertion : digest relu depuis le registre; assertion rejetée.
- Changement du contrôle/kill switch après challenge : `control_epoch` et `halted` relus; assertion rejetée.
- Mauvaise origine ou RP ID : vérification serveur refuse l’assertion malgré un JSON de navigateur valide.
- Base existante ou reçu altéré : migration additive et vérification d’intégrité fail-closed au démarrage.

---

### Task 1: Modèle de stockage additif et reçus vérifiables

**Files:**
- Modify: `src/invest_ai/core/orchestration.py` (migration SQLite, tables attendues/intégrité, sans modifier les tables historiques)
- Create: `src/invest_ai/core/passkey_store.py` (justificatifs, challenges, reçus)
- Test: `tests/core/test_passkey_store.py`
- Modify: `tests/core/test_orchestration.py` (compatibilité migration)

**Interfaces:**
- `PasskeyStore(store: OrchestrationStore)`
- `create_challenge(*, purpose: str, context: dict, expires_at: datetime) -> Challenge`
- `consume_challenge(*, challenge_id: str, purpose: str, now: datetime) -> Challenge`
- `save_credential(credential: PasskeyCredential) -> None`
- `append_approval(receipt: PolicyApprovalReceipt) -> None`
- `verify_integrity() -> None`

- [x] **Step 1: Écrire les tests en échec** pour migration v3→v4 conservant contrôles/révisions, immutabilité des reçus, challenge expiré, challenge consommé deux fois et concurrence de consommation.
- [x] **Step 2: Exécuter les tests ciblés** avec `python -m pytest tests/core/test_passkey_store.py tests/core/test_orchestration.py -q`; constater les échecs attendus.
- [x] **Step 3: Ajouter la migration v4** avec tables `passkey_credentials`, `webauthn_challenges`, `risk_policy_approval_receipts`, contraintes, index et triggers append-only; vérifier toutes ces tables au démarrage.
- [x] **Step 4: Implémenter `PasskeyStore`** avec transaction `BEGIN IMMEDIATE` pour consommation atomique, identifiants non secrets, expiration maximale de deux minutes, empreintes SHA-256 et vérification des reçus au chargement.
- [x] **Step 5: Relancer les tests ciblés** et vérifier que bases neuves et v3 migrées restent halted et sans politique active.

### Task 2: Cérémonies WebAuthn et approbation de politique

**Files:**
- Create: `src/invest_ai/core/passkey_approval.py` (cérémonies et vérification)
- Modify: `src/invest_ai/core/risk_policy.py` (API de candidate/approbation distincte)
- Modify: `requirements.txt` (dépendance WebAuthn version fixée)
- Test: `tests/core/test_passkey_approval.py`
- Test: `tests/core/test_risk_policy_approval.py`

**Interfaces:**
- `PasskeyApprovalService(store, registry, readiness_provider, *, rp_id: str, origin: str)`
- `begin_registration(*, now: datetime) -> RegistrationOptions`
- `finish_registration(*, response: dict, now: datetime) -> PasskeyCredential`
- `begin_policy_approval(*, revision_id: str, now: datetime) -> AuthenticationOptions`
- `finish_policy_approval(*, response: dict, now: datetime) -> PolicyApprovalReceipt`
- `RiskPolicyRegistry.register_candidate(policy: RiskPolicy, *, effective_at: datetime, reason: str, recorded_at: datetime | None = None) -> RiskPolicyRevision`
- `RiskPolicyRegistry.approval_receipt(revision_id: str) -> PolicyApprovalReceipt | None`

- [x] **Step 1: Ajouter des tests négatifs** exigeant UV et couvrant mauvaise origine/RP, challenge inattendu, expiration, finalité incorrecte, politique non complète, digest modifié, contrôle epoch changé, contrôle halted et assertion rejouée.
- [x] **Step 2: Ajouter un test positif** avec vecteurs synthétiques d’inscription et d’authentification vérifiés par la bibliothèque choisie; le reçu est lié au revision ID, digest, `control_epoch`, readiness et credential ID.
- [x] **Step 3: Vérifier la version de `webauthn`** sous Python 3.14, épingler `webauthn==3.0.1` et constater son chargement dans le venv du worktree.
- [x] **Step 4: Implémenter l’inscription** avec `userVerification=required`, clé résidente, origine/RP local exact et finalité de challenge séparée.
- [x] **Step 5: Implémenter l’approbation** en revérifiant politique, digest, readiness et contrôle; consommer le challenge, mettre à jour le compteur et écrire le reçu dans une seule transaction.
- [x] **Step 6: Relire et vérifier le reçu** par son hash et ses liens vers challenge, justificatif, epoch et révision immuable; l’approbation ne modifie pas l’état PENDING et n’active aucune politique.
- [x] **Step 7: Exécuter les tests ciblés**; vecteurs valides acceptés et altérations/rejeux refusés.

### Task 3: Routes locales fermées par origine et interface utilisateur

**Files:**
- Modify: `src/bubus/server.py` (initialiser les services locaux et routes JSON ciblées)
- Modify: `src/bubus/ui/index.html` (inscription, lecture politique complète et action approbation)
- Modify: `src/bubus/ui/desk.js` (WebAuthn browser API et affichage explicite)
- Modify: `src/bubus/ui/desk.css` (états de cérémonie)
- Test: `tests/bubus_v2/test_passkey_approval_routes.py`
- Modify: `tests/bubus_v2/test_desk.py`

**Interfaces:**
- `GET /api/desk/risk-policy-candidates` retourne uniquement politiques complètes `PENDING` et leurs champs explicites/digest.
- `POST /api/desk/passkey/registration/options`
- `POST /api/desk/passkey/registration/verify`
- `POST /api/desk/passkey/policy-approval/options` avec `revision_id` seulement.
- `POST /api/desk/passkey/policy-approval/verify` avec `revision_id`, `challenge_id` et assertion WebAuthn brute.
- Tous les handlers vérifient méthode, taille JSON, origine configurée, finalité, réponses `Cache-Control: no-store`; navigateur et requête n’envoient jamais une décision de confiance.

- [ ] **Step 1: Écrire les tests HTTP en échec** pour mauvais Host/Origin, origine distante, champs inattendus, absence de candidat complet, route d’approbation sans passkey et conservation `orders_enabled=false`.
- [ ] **Step 2: Exécuter les tests ciblés** avec `python -m pytest tests/bubus_v2/test_passkey_approval_routes.py tests/bubus_v2/test_desk.py -q`.
- [ ] **Step 3: Ajouter les routes** à méthodes et origines strictes; ne pas inclure de route générique acceptant l’état Risk du client.
- [ ] **Step 4: Ajouter l’écran** qui affiche tous les champs du `RiskPolicy`, permissions et empreinte avant `navigator.credentials.get`; afficher un résultat approuvé uniquement après réponse serveur.
- [ ] **Step 5: Afficher en permanence** le statut « Exécution réelle désactivée »; le succès d’approbation ne masque aucun blocage de readiness.
- [ ] **Step 6: Relancer les tests HTTP/UI** et vérifier en particulier que les endpoints d’ordre n’ont pas changé et restent absents/désactivés.

### Task 4: Documentation et vérification de bout en bout

**Files:**
- Modify: `README.md` (préparation locale et limitations)
- Create: `docs/passkey-local-verification.md` (étapes Windows Hello/FIDO2 manuelles)
- Test: `tests/bubus_v2/test_risk_readiness.py` (non-armement après approbation)

- [ ] **Step 1: Tester readiness après approbation** et exiger `BLOCKED`, `NOT_READY`, `armed=false`, `orders_enabled=false`.
- [ ] **Step 2: Documenter le test manuel** sur `localhost` et l’origine exacte réellement configurée, y compris inscription, confirmation Windows Hello, approbation et redémarrage/relecture du reçu.
- [ ] **Step 3: Exécuter** `python -m pytest tests/core/test_passkey_store.py tests/core/test_passkey_approval.py tests/core/test_risk_policy_approval.py tests/bubus_v2/test_passkey_approval_routes.py tests/bubus_v2/test_risk_readiness.py -q`.
- [ ] **Step 4: Exécuter** `python -m pytest -q` et `git diff --check` depuis `D:\BUBUS2.0`; rapporter les résultats réels sans prétendre que Windows Hello est validé avant l’essai manuel.

**Ordres, outbox et déploiement restent hors de cette mission.**
