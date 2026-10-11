# WattKeeper (CEL-CA) — Architecture et protocole v1

Coordination de HEMS Home Assistant au sein d'une Communauté Électrique Locale (CEL).

## 1. Principes

| Décision | Choix | Raison |
|---|---|---|
| Topologie | Un Azure Storage Account par CEL (Table = état partagé, Queue = boîtes de réception) | Aucun serveur à exploiter, quelques centimes par mois, HTTPS sortant uniquement |
| Identité | Paire de clés Ed25519 par HEMS, identifiant dérivé de la clé | Impossible d'usurper un identifiant sans la clé privée |
| Confiance | Registre des membres signé par l'administrateur CEL | Une seule clé publique à configurer (ancre de confiance) |
| Arbitrage | Chaque producteur arbitre **sa propre** offre | Pas de serveur central de marché, pas de point unique de décision |
| Valeur d'une réservation | Engagement de coordination | Le décompte officiel reste celui du GRD ; le respect est mesuré a posteriori |

**Hébergement** : un Storage Account Standard_LRS en Suisse Nord (TLS 1.2, HTTPS uniquement). Chaque
HEMS reçoit de l'administrateur une **clé d'accès** contenant des jetons SAS limités (voir § 5 bis) ;
la clé du compte de stockage n'est jamais distribuée ni stockée. Détails : `azure/README.md`.

Le stockage n'est **pas** de confiance : tout message est signé de bout en bout. Les droits SAS ne sont
qu'une seconde barrière (anti-spam, cloisonnement des partitions).

## 2. Identité

```
clé privée Ed25519 (32 o)  ──►  clé publique (32 o)
hems_id = "hems-" + base32( sha256(clé publique)[0:15] ).lower()     # 24 caractères
```

* La clé est générée par l'intégration lors de l'ajout (config flow) et stockée dans l'entrée de configuration HA (sauvegardée avec HA).
* Restauration : le champ « Clé privée existante » du config flow restaure la même identité.
* La clé d'accès émise par l'administrateur est liée au `hems_id` (le config flow refuse une clé émise pour une autre identité).

## 3. Registre des membres

Écrit dans la ligne `registry` de la table par l'administrateur (outil `tools/cel_admin.py`).

```json
{ "serial": 1790001234,
  "members": [ { "id": "hems-…", "pk": "<b64url>", "name": "Maison Dupont",
                 "roles": ["consumer","producer"], "valid_until": 0, "meter": "CH…" } ],
  "revoked": [ "hems-…" ] }
```

Règles de vérification côté HEMS :
1. signature de l'administrateur (clé configurée) et `src` = identifiant de l'admin ;
2. `serial` strictement croissant (anti-rejeu / anti-retour arrière) ;
3. pour chaque membre, `id` **doit** être dérivé de `pk` ;
4. membre actif = présent, non révoqué, `valid_until` non dépassé ;
5. le registre est persisté (et re-vérifié) au redémarrage de HA.

Les messages d'un émetteur inconnu reçus avant le registre sont mis en attente puis re-vérifiés (l'ordre de lecture des lignes n'est pas garanti).

## 4. Enveloppe signée

```json
{"v":1,"typ":"offer_book","cid":"cel-ca","src":"hems-…","ts":1790000000000,
 "nonce":"<hex>","body":{…},"sig":"<b64url Ed25519>"}
```

* `sig` = signature du JSON canonique de l'enveloppe sans `sig` (clés triées, sans espaces, UTF-8).
* **Aucun flottant** : W, Wh et ms entiers → forme canonique non ambiguë.
* Taille max 16 KiB, champs stricts (aucun champ inconnu accepté).

Contrôles à la réception : structure → communauté → émetteur actif → signature → pas dans le futur (> 2 min) → cohérence topic/émetteur (et rôle) → fraîcheur :
* messages d'**état** (lignes de la table) : `ts` strictement croissant par (émetteur, type) ;
* **commandes** : |maintenant − ts| ≤ 2 min et nonce jamais vu (anti-rejeu) ;
* messages de boîte de réception : champ `to` = moi, limite de 30 msg/min par émetteur.

## 5. Topics logiques

| Topic | Retained | Écrit par | Contenu |
|---|---|---|---|
| `cel/<cid>/registry` | oui | admin | registre signé |
| `cel/<cid>/node/<id>/status` | oui | le HEMS | présence (heartbeat 60 s) ; *last will* = message vide |
| `cel/<cid>/node/<id>/offers` | oui | le HEMS | carnet d'offres |
| `cel/<cid>/node/<id>/reservations` | oui | le HEMS | réservations accordées (transparence) |
| `cel/<cid>/node/<id>/settlement` | non | le HEMS | bilans de respect des engagements |
| `cel/<cid>/node/<id>/inbox` | non | tous → lu par `<id>` | requêtes / réponses / annulations / réductions |

Un pair est « en ligne » si son dernier heartbeat date de moins de 3 min et qu'il n'a pas publié un statut
vide (arrêt propre). Sans *last will*, un HEMS coupé brutalement disparaît au bout de 3 min.

## 5 bis. Correspondance avec Azure Storage

Le protocole garde ses topics logiques ; `StorageTransport` les projette sur le Storage Account :

| Topic logique | Stockage | Lecture |
|---|---|---|
| `registry` | table `cel<cid>`, ligne (`registry`, `registry`) | balayage de la table toutes les 15 s |
| `node/<id>/status`, `offers`, `reservations` | table, ligne (`<id>`, `status`/`offers`/`reservations`) | idem, chaque changement livré une fois (empreinte SHA-256) |
| `node/<id>/settlement` | table, anneau de 8 lignes (`<id>`, `settlement-0..7`) | idem |
| `node/<id>/inbox` | queue `<id>` (TTL 1 h), message supprimé après lecture | toutes les 5 s, chaque seconde pendant 30 s après l'envoi d'une requête |

Jetons SAS d'un membre (clé d'accès `CEL1.<base64url JSON>`, 365 jours par défaut) :

| Jeton | Portée | Droits |
|---|---|---|
| `tr` | table de la CEL | lecture |
| `tw` | table, **partition = son `hems_id`** uniquement | ajout / mise à jour |
| `qs` | toutes les queues | ajout de message (écrire dans la boîte d'un pair) |
| `qp` | **sa** queue | lecture / suppression |

`cel_admin.py selftest` vérifie sur le vrai compte qu'un membre ne peut ni écrire la partition d'un autre,
ni le registre, ni lire la boîte d'un autre. Une erreur 401/403 en cours de fonctionnement (clé expirée ou
révoquée) déclenche la réauthentification dans HA.

**Latences** : interrogation périodique, ≤ 15 s pour l'état, 1–5 s pour les requêtes. Un HEMS coupé
brutalement est vu hors ligne au bout de 3 min (absence de heartbeat).

## 6. Cycle de vie

```
Producteur (A)                   Storage (Table/Queue)            Consommateur (B)
  │ mesure réseau → surplus lissé 60 s                                  │
  │── offer_book {oid, power_w, available_w, rolling} (table) ─────────►│ capteur « surplus communauté »
  │                                                                     │ service cel_ca.reserve
  │◄──────────── reserve_request {rid, oid, start, end, power_w, min} ──│
  │ arbitrage FCFS, sans sur-réservation                                │
  │── reserve_response {accepted|partial|rejected, power_w, window} ───►│ événement « granted »
  │── offer_book / reservation_book mis à jour ─────────────────────────►│ (tous les HEMS)
  │ surplus ↓  → reservation_update {curtailed|cancelled} ─────────────►│ événement « curtailed »
  │ fin de fenêtre → settlement {ratio_pct} ◄──────────────────────────►│ settlement {ratio_pct}
```

### Offres
* **Offre vivante** (automatique) : `surplus lissé − marge`, arrondie à 10 W, publiée si ≥ minimum (500 W par défaut). Horizon glissant (15 min) prolongé tant que le surplus dure ; republiée si la puissance varie de plus de 10 % ; retirée après 3 mesures consécutives sous le minimum (hystérésis ~30 s). Flag `rolling=true` : les réservations ne sont pas limitées à l'horizon affiché.
* **Offre manuelle** (service `publish_offer`) : fenêtre fixe, p. ex. issue d'une prévision solaire ; les réservations sont limitées à sa fenêtre.

### Arbitrage chez le producteur
* premier arrivé, premier servi ; la puissance réservée simultanée ne dépasse jamais l'offre (calcul par balayage des intervalles) ;
* `min_power_w` permet une attribution partielle ; sinon tout ou rien ;
* durée min 1 min, max 4 h, 64 réservations actives max ;
* requête idempotente (`rid` rejoué → même réponse) ;
* réduction d'offre : les réservations sont réduites de la plus récente à la plus ancienne (LIFO) ; une réservation qui passerait sous son `min_power_w` est annulée.

### Créneaux de marché (optionnel)
Le registre signé peut porter `"params": {"slot_minutes": 15}` (5, 10, 15, 20, 30 ou 60 : diviseurs de
l'heure, donc créneaux alignés sur les heures pleines, en temps UNIX). Quand il est présent :
* les offres automatiques couvrent **exactement le prochain créneau** ; celle du créneau en cours peut
  seulement être réduite (curtailment) ;
* une requête doit porter sur **un créneau entier non commencé** (`start` aligné, `end − start` = 1
  créneau, tolérance de 30 s) — sinon `not_slot_aligned` / `slot_started` ;
* il n'y a pas de renouvellement automatique : le consommateur envoie une nouvelle requête pour chaque
  créneau ;
* les bilans de respect sont donc calculés par créneau, comme le décompte du GRD.
Sans `params`, le marché reste continu.

### Réservation globale (plusieurs producteurs)
Le consommateur répartit la puissance demandée **au prorata du surplus disponible** de chaque offre en
ligne (parts < 50 W redistribuées), envoie les requêtes **en parallèle** (une par producteur, chacun
restant seul arbitre) et, si un producteur répond moins que prévu, fait un **second tour** auprès des
autres. **Tout ou rien** : si le total obtenu est inférieur au minimum demandé, toutes les réservations
accordées sont annulées. Les réservations partagent un identifiant de groupe (côté consommateur
uniquement, aucun changement de protocole) permettant de tout annuler d'un coup ; une réduction par un
producteur émet un événement `group_updated` avec la nouvelle puissance totale.

### Robustesse
* réponse tardive après timeout (30 s) : le consommateur annule automatiquement pour libérer la capacité ;
* reconnexion au stockage avec back-off exponentiel (5 → 60 s) ; republication de l'état à la reconnexion ;
* état (registre, offres manuelles, réservations, scores) persisté dans `.storage/cel_ca.<entry_id>` ;
* horodatages d'émission strictement croissants (évite qu'un pair rejette deux états émis dans la même milliseconde).

## 7. Mesure du respect des engagements (settlement)

À la fin de chaque réservation, chaque partie publie un bilan signé à partir de **son propre** compteur (capteur de puissance au point de raccordement, intégré par échantillonnage bloqué, trous > 5 min ignorés) :

* producteur : `ratio = énergie injectée / énergie totale réservée sur la fenêtre` (plafonné à 100 %) ;
* consommateur : `ratio = énergie soutirée / énergie totale réservée par lui sur la fenêtre` (plafonné à 100 %), l'énergie soutirée étant répartie au prorata entre ses réservations simultanées.

Pas de bilan si moins de 50 % de la fenêtre est couverte par des mesures. Le capteur « Respect des engagements » donne la moyenne glissante (50 derniers bilans) ; la réputation des pairs est visible en attribut et sert à départager les offres de puissance égale.

> Ces bilans sont **auto-déclarés mais signés** : ils sont non répudiables et pourront être rapprochés des données de comptage du GRD (champ `meter` du registre).

## 7 bis. Vue de la communauté

Chaque HEMS reconstruit l'état de tous les membres actifs du registre à partir des seuls messages déjà
échangés (présence, carnets d'offres et de réservations, bilans) : en ligne, surplus proposé/disponible,
puissance réservée chez lui et par lui, taux de respect. Aucune mesure de consommation n'est partagée.
Home Assistant expose un appareil par participant et l'action `cel_ca.generate_dashboard` produit un
tableau de bord n'utilisant que des cartes standard.

## 8. Menaces couvertes

| Menace | Contre-mesure |
|---|---|
| Usurpation d'un HEMS | identifiant dérivé de la clé + signature de chaque message |
| Faux membre / faux registre | registre signé par l'admin, dérivation id↔clé vérifiée |
| Retour à un ancien registre | serial strictement croissant, persisté |
| Rejeu de requêtes | fenêtre de 2 min + cache de nonces + `to` lié au destinataire |
| Ancien état ré-injecté | `ts` strictement croissant par émetteur et type |
| Un membre écrit dans l'état d'un autre | contrôle topic = émetteur + SAS limité à sa partition |
| Inondation d'une boîte de réception | 30 msg/min/émetteur, payload ≤ 16 KiB, carnets bornés |
| Membre exclu | révocation dans le registre → pairs retirés, réservations annulées |

Non couvert en v1 : confidentialité vis-à-vis des autres membres (les offres et réservations sont publiques dans la CEL, par transparence) ; compromission de la clé admin (à conserver hors ligne).

## 9. Évolutions envisagées

* offres par tranches issues de Forecast.Solar ;
* prix / tarif interne CEL dans les offres ;
* rapprochement des bilans avec les courbes de charge du GRD ;
* chiffrement des boîtes de réception (X25519) si la confidentialité devient nécessaire.
