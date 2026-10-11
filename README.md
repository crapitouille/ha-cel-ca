<img src="https://raw.githubusercontent.com/crapitouille/ha-cel-ca/main/custom_components/cel_ca/brand/logo@2x.png" alt="WattKeeper" height="96">

# WattKeeper - Communauté Electrique Locale (CEL-CA)

HEMS coopératifs pour Communauté Électrique Locale.

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories/)
[![Release](https://img.shields.io/github/v/release/crapitouille/ha-cel-ca)](https://github.com/crapitouille/ha-cel-ca/releases)
[![Validate](https://github.com/crapitouille/ha-cel-ca/actions/workflows/validate.yml/badge.svg)](https://github.com/crapitouille/ha-cel-ca/actions/workflows/validate.yml)
[![Tests](https://github.com/crapitouille/ha-cel-ca/actions/workflows/tests.yml/badge.svg)](https://github.com/crapitouille/ha-cel-ca/actions/workflows/tests.yml)

[![Ouvrir dans HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=crapitouille&repository=ha-cel-ca&category=integration)
[![Ajouter l'intégration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=cel_ca)

Intégration Home Assistant (2026.x) permettant aux HEMS d'un quartier membre d'une même
Communauté Électrique Locale de :

* **s'identifier de manière unique et fiable** (clé Ed25519, identifiant dérivé de la clé, registre signé par l'administrateur de la CEL) ;
* **publier leur surplus** (automatiquement depuis le compteur réseau, ou manuellement pour un créneau futur) ;
* **réserver** ce surplus chez un ou plusieurs voisins, avec réponse du producteur, réduction/annulation notifiée si le soleil disparaît, et mesure a posteriori du respect des engagements.

Depuis la v0.6.0, la CEL est hébergée sur un simple **Azure Storage Account** (Table + Queue) :
plus de VM, de broker, de certificat ni de mot de passe à gérer — quelques centimes par mois.

Architecture et protocole détaillés : [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

```
custom_components/cel_ca/   intégration HA (core/ = protocole pur Python, sans HA)
tools/cel_admin.py          outil de l'administrateur CEL (clé admin, enrôlement, clés d'accès, registre)
azure/                      Storage Account (Bicep + deploy.sh pour Azure Cloud Shell) - voir azure/README.md
examples/automations.yaml   exemples : recharge VE sur surplus, offre planifiée, notifications
tests/                      tests de non-régression (pytest, 199 tests, HA 2026.2 et 2026.10)
```

## Installation (HACS)

1. Bouton **Ouvrir dans HACS** ci-dessus (ou *HACS → ⋮ → Custom repositories* : `https://github.com/crapitouille/ha-cel-ca`, type *Integration*).
2. **Download**, choisir la dernière version, puis redémarrer Home Assistant.
3. Bouton **Ajouter l'intégration** ci-dessus (ou *Settings → Devices & services → Add integration → WattKeeper*) et suivre le § 2.

Les mises à jour apparaissent ensuite dans *Settings → Updates*. Installation manuelle : copier
`custom_components/cel_ca` dans `/config/custom_components/` et redémarrer.

## 1. Mise en place de la CEL (administrateur, une fois, dans Azure Cloud Shell Bash)

```bash
python3 tools/cel_admin.py --dir ~/clouddrive/cel-admin init --community cel-ca     # clé admin + registre vide
bash azure/deploy.sh --community cel-ca --account <nomunique> --admin-dir ~/clouddrive/cel-admin
```

`deploy.sh` crée le Storage Account, le relie au dossier administrateur et lance un **auto-test** des
droits d'accès des membres. Détails et exploitation : [`azure/README.md`](azure/README.md).
Sauvegarder `cel_admin_key.json` hors ligne (clé de l'administrateur).

## 2. Ajout d'un HEMS (chaque membre)

1. Installer l'intégration (voir *Installation (HACS)* ci-dessus) et redémarrer HA.
2. *Paramètres → Appareils et services → Ajouter → WattKeeper* : le formulaire affiche **l'identifiant HEMS et la clé publique** → les transmettre à l'administrateur.
3. L'administrateur enrôle le HEMS et renvoie une **clé d'accès CEL** (`CEL1.…`) :

   ```bash
   python3 tools/cel_admin.py --dir ~/clouddrive/cel-admin enroll --pubkey <clé> --id <hems-id> --name "Maison Dupont" [--meter CH10...]
   python3 tools/cel_admin.py --dir ~/clouddrive/cel-admin member-access --id <hems-id>     # affiche la clé CEL1.…
   python3 tools/cel_admin.py --dir ~/clouddrive/cel-admin publish
   ```

4. Coller la clé dans le champ *CEL access key* (le formulaire est resté ouvert avec la même identité ; s'il a été fermé, recommencer et renvoyer le nouvel identifiant) : la
   communauté, l'adresse du stockage et la clé publique de l'admin sont **dans la clé**, rien d'autre à saisir.
5. Étape « Rôle énergétique » : rôles, capteur de puissance réseau (p. ex. `sensor.p1_meter_power`) et convention de signe.

La clé d'accès est valable 365 jours (`--days`). **30 jours avant l'échéance**, une alerte apparaît dans
*Réparations* (date aussi visible dans l'attribut `access_key_expires` de `sensor.<hems>_identifiant_hems`) :
l'administrateur émet une nouvelle clé (`member-access`), à coller via *WattKeeper → ⋮ → Reconfigurer*.
Si la clé est refusée (expirée, révoquée), HA ouvre automatiquement une demande de **réauthentification**.
Dans les deux cas l'identité du HEMS est conservée.

Révocation : `revoke --id hems-…` (supprime aussi sa boîte de réception et son état) puis `publish`.

### Créneaux de marché (paramètre de la communauté)

```bash
python3 tools/cel_admin.py --dir ~/clouddrive/cel-admin set-param --slot-minutes 15   # 0 = marché continu
python3 tools/cel_admin.py --dir ~/clouddrive/cel-admin publish
```

Le paramètre voyage dans le **registre signé**. En mode créneaux :

* chaque producteur propose son surplus **pour le prochain créneau uniquement** (une offre = un créneau) ;
* une réservation porte **toujours sur le prochain créneau complet** (la durée demandée est ignorée) ;
* pour continuer, le HEMS **refait simplement une demande** pour le créneau suivant ;
* le producteur peut réduire l'offre du créneau en cours (surplus en baisse), jamais l'augmenter ;
* une offre manuelle (`publish_offer`) est découpée en une offre par créneau futur.

## 3. Entités

| Entité | Description |
|---|---|
| `sensor.<hems>_surplus_disponible_dans_la_communaute` | W encore réservables chez les pairs en ligne (attribut `offers`) |
| `sensor.<hems>_puissance_reservee_consommation` | W réservés par moi maintenant (attribut `reservations`) |
| `sensor.<hems>_puissance_reservee_production` | W de mon surplus réservés par d'autres |
| `sensor.<hems>_surplus_propose` | W que je propose maintenant |
| `sensor.<hems>_hems_en_ligne` | nombre de pairs en ligne (attribut `peers` avec réputation) |
| `sensor.<hems>_respect_des_engagements` | % moyen de respect de mes réservations |
| `sensor.<hems>_identifiant_hems` | diagnostic : id, clé publique, serial du registre |
| `binary_sensor.<hems>_connexion_cel` / `_enrole` | accès au stockage de la CEL / présence dans le registre |
| `switch.<hems>_publier_le_surplus_automatiquement` | producteurs uniquement |
| `sensor.<hems>_participants_en_ligne` | participants en ligne (soi compris) ; attributs `participants`, `slot_minutes`, `next_slot_start` |
| `sensor.<hems>_surplus_total_propose` / `_puissance_reservee_dans_la_communaute` | totaux de la communauté |

(Une installation existante garde ses identifiants d'entités : p. ex. `binary_sensor.<hems>_cel_broker`
reste valable, seul le nom affiché devient « CEL connection ».)

**Un appareil par participant** (créé ou supprimé automatiquement selon le registre) :
`binary_sensor.<participant>_en_ligne`, `sensor.<participant>_surplus_propose`, `_surplus_disponible`,
`_puissance_reservee_production`, `_puissance_reservee_consommation`, `_respect_des_engagements`, `_dernier_contact`.

### Tableau de bord de la communauté

*Outils de développement → Actions → `cel_ca.generate_dashboard`* → *Exécuter l'action* : la réponse **est**
le tableau de bord. La copier, puis *Paramètres → Tableaux de bord → Ajouter → Nouveau tableau de bord* →
ouvrir → crayon → ⋮ → *Éditeur de configuration brute* → tout remplacer par le contenu copié.

Événement `cel_ca_event` (`type` : `request_received`, `granted`, `rejected`, `curtailed`, `cancelled`, `completed`, `settled`, `timeout`, `registry_updated`, `group_granted`, `group_rejected`, `group_updated`).

### Fréquences de rafraîchissement (v0.6)

| Information | Délai typique |
|---|---|
| Offres, réservations, présence des voisins | ≤ 15 s (lecture de la table) |
| Requête de réservation reçue par le producteur | ≤ 5 s |
| Réponse au demandeur | 1–2 s (lecture rapide pendant 30 s après une requête) |
| Registre publié par l'admin | ≤ 15 s |

## 4. Actions

* `cel_ca.reserve` — `power_w`, `min_power_w`, `duration_min`, `producer_id`, `offer_id`, `start` → `status`, `reservation_id`, `power_w`, `start`, `end`, `producer_name`, `reason`.
* `cel_ca.reserve_global` — `power_w` (total), `min_power_w` (tout ou rien en dessous), `duration_min`, `start` → réserve chez **plusieurs voisins** au prorata de leur surplus. Renvoie `status`, `group_id`, `power_w`, `reservations`.
* `cel_ca.cancel_reservation` — `reservation_id` (réservation **ou groupe**).
* `cel_ca.publish_offer` — `power_w`, `duration_min`, `start` → `offer_id`.
* `cel_ca.withdraw_offer` — `offer_id`.
* `cel_ca.generate_dashboard` — configuration du tableau de bord de la communauté.

`config_entry_id` n'est nécessaire que si plusieurs HEMS sont configurés sur la même instance.

## 5. Migration depuis la v0.5 (broker MQTT sur VM)

Voir [`azure/README.md` § Migration](azure/README.md#migration-depuis-la-v05-vm--mosquitto) : déployer le
Storage Account, émettre une clé d'accès pour chaque HEMS existant, mettre à jour l'intégration (HA demande
la clé, **l'identité est conservée**), `publish`, puis supprimer la VM.

## 6. Tests de non-régression

```bash
python -m venv .venv && . .venv/bin/activate      # Python 3.13+
pip install -r requirements_test.txt
npm install -g azurite                             # émulateur Azure Storage (tests de bout en bout)
pytest --cov
```

* `tests/core` : identité, canonisation, validation, arbitrage, créneaux, réservation globale, réductions, métrologie, scénarios multi-HEMS, sécurité (usurpation, rejeu, ancien état, faux registre, révocation, limitation de débit), outil admin.
* `tests/storage` : signatures SAS **identiques à celles des SDK Azure officiels**, transport et administration de bout en bout sur **Azurite** (deux/trois HEMS, réservation globale, révocation, clé invalide, reconnexion), outil `cel_admin` complet.
* `tests/ha` : config flow (clé d'accès, erreurs, restauration, réauthentification, reconfiguration, alerte d'expiration, **migration depuis la v0.5**), options, entités (y compris noms français), actions, événements, persistance, diagnostics (secrets masqués), et HA de bout en bout sur Azurite.
* `tests/test_packaging.py` : distribution HACS (structure, `hacs.json`, manifeste, icône, cohérence des versions, boutons du README, workflows).
* `tests/azure` : compilation et lint Bicep, shellcheck, `deploy.sh` exécuté de bout en bout contre un faux `az` et Azurite.

Toute alerte de dépréciation de Home Assistant concernant `cel_ca` fait échouer les tests HA : les API
retirées dans une future version sont détectées avant qu'elles ne cassent l'intégration. Pour tester une
version précise de HA : `pip install "homeassistant==2026.10.0" pytest-homeassistant-custom-component` (Python 3.14).

Les tests qui dépendent d'un outil absent (`azurite`, `bicep`, `shellcheck`) sont ignorés automatiquement ; la CI GitHub les installe.

> Azurite ne contrôle pas les plages de partitions des SAS : la restriction « un membre n'écrit que sa
> propre partition » se vérifie sur le vrai compte avec `cel_admin.py selftest` (lancé par `deploy.sh`).
