<img src="https://raw.githubusercontent.com/crapitouille/ha-cel-ca/main/custom_components/cel_ca/brand/logo@2x.png" alt="WattKeeper" height="96">

# WattKeeper - Communauté Electrique Locale (CEL-CA)

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories/)
[![Release](https://img.shields.io/github/v/release/crapitouille/ha-cel-ca)](https://github.com/crapitouille/ha-cel-ca/releases)
[![Validate](https://github.com/crapitouille/ha-cel-ca/actions/workflows/validate.yml/badge.svg)](https://github.com/crapitouille/ha-cel-ca/actions/workflows/validate.yml)
[![Tests](https://github.com/crapitouille/ha-cel-ca/actions/workflows/tests.yml/badge.svg)](https://github.com/crapitouille/ha-cel-ca/actions/workflows/tests.yml)

[![Ouvrir dans HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=crapitouille&repository=ha-cel-ca&category=integration)
[![Ajouter l'intégration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=cel_ca)

Intégration Home Assistant (2026.x) qui fait coopérer les HEMS (*Home Energy Management Systems*) des
habitations d'une même **Communauté Électrique Locale** (CEL) :

* chaque HEMS a une **identité unique et infalsifiable** (clé cryptographique), reconnue par l'administrateur de la CEL ;
* chaque HEMS **publie son surplus** solaire automatiquement ;
* les autres HEMS peuvent **réserver** ce surplus chez un ou plusieurs voisins (par exemple pour recharger une voiture),
  et le respect des réservations est mesuré.

## Comment ça marche

```
          Administrateur de la CEL                         Membres (un Home Assistant par maison)
   ┌──────────────────────────────────┐            ┌─────────────────────┐   ┌─────────────────────┐
   │ Azure Cloud Shell                │            │ HA  « Maison A »    │   │ HA  « Maison B »    │
   │  outil cel_admin :               │  clé CEL1. │  intégration        │   │  intégration        │
   │  enrôle, révoque, publie le      │ ─────────► │  WattKeeper         │   │  WattKeeper         │
   │  registre signé des membres      │            └──────────┬──────────┘   └──────────┬──────────┘
   └────────────────┬─────────────────┘                       │  HTTPS sortant          │
                    └────────────────►  Azure Storage Account (Table + Queue)  ◄───────┘
                                        quelques centimes par mois
```

* **L'administrateur** gère la CEL depuis le *Cloud Shell* du portail Azure : rien à installer sur son PC.
* **Chaque membre** installe l'intégration dans son Home Assistant et colle la **clé d'accès** (`CEL1.…`)
  que l'administrateur lui envoie. Aucun port à ouvrir chez lui.

| Je veux… | Qui | Section |
|---|---|---|
| Créer la CEL | administrateur | [A1](#a1-préparer-le-cloud-shell-une-fois) · [A2](#a2-créer-la-cel-une-fois) |
| Ajouter un HEMS | membre + administrateur | [B1](#b1-installer-lintégration) · [B2](#b2-rejoindre-la-cel) · [A3](#a3-ajouter-un-hems) |
| Voir la liste des HEMS | administrateur | [A4](#a4-voir-la-liste-des-hems) |
| Supprimer un HEMS | administrateur | [A5](#a5-supprimer-un-hems) |
| Renouveler une clé d'accès | administrateur + membre | [A6](#a6-renouveler-la-clé-dun-hems) · [B4](#b4-coller-une-nouvelle-clé-daccès) |
| Changer le nom d'un HEMS | administrateur | [A7](#a7-modifier-un-hems) |
| Régler les créneaux de marché | administrateur | [A8](#a8-créneaux-de-marché) |
| Réserver du surplus | membre | [C](#c-utilisation) |

---

## A. Administrateur de la CEL

Toutes les commandes se tapent dans le **Cloud Shell** du portail Azure, en mode **Bash**.

### A1. Préparer le Cloud Shell (une fois)

1. Ouvrir [portal.azure.com](https://portal.azure.com), cliquer sur l'icône **>_** en haut, choisir **Bash**.
2. Télécharger le zip de la dernière version depuis la page
   [Releases](https://github.com/crapitouille/ha-cel-ca/releases) (*Source code (zip)*), puis dans le Cloud Shell :
   **Manage files → Upload** et choisir ce fichier. Il arrive dans le dossier personnel (`~`).
3. Décompresser et installer la commande `cel` (remplacer `<fichier>.zip` par le nom du fichier chargé) :

   ```bash
   mkdir -p ~/clouddrive && cd ~/clouddrive && rm -rf cel-ca
   unzip -q ~/<fichier>.zip -d /tmp/cel-unzip && mv /tmp/cel-unzip/* ~/clouddrive/cel-ca && rm -rf /tmp/cel-unzip
   python3 -m pip install --user --quiet cryptography
   grep -q '^cel()' ~/.bashrc || echo 'cel() { python3 ~/clouddrive/cel-ca/tools/cel_admin.py --dir ~/clouddrive/cel-admin "$@"; }' >> ~/.bashrc
   source ~/.bashrc
   ```

   La commande **`cel`** est maintenant disponible (elle reste disponible aux prochaines sessions).

> **Mettre à jour les outils** (nouvelle version) : refaire les étapes 2 et 3. Le dossier
> `~/clouddrive/cel-admin` (clé de l'administrateur et liste des membres) n'est **pas** touché.

### A2. Créer la CEL (une fois)

1. **Créer la clé de l'administrateur** et le registre vide (remplacer `cel-ca` par le nom court de votre communauté) :

   ```bash
   cel init --community cel-ca
   ```

   ⚠️ Télécharger ensuite `~/clouddrive/cel-admin/cel_admin_key.json` (*Manage files → Download*) et le garder
   **en lieu sûr, hors ligne** : sans ce fichier, il faudrait reconfigurer tous les HEMS.

2. **Choisir un nom de stockage** unique dans tout Azure (3 à 24 minuscules/chiffres) et vérifier qu'il est libre :

   ```bash
   ACCOUNT=celca$RANDOM; echo $ACCOUNT
   az storage account check-name --name $ACCOUNT -o table      # NameAvailable doit être True
   ```

3. **Déployer** (≈ 1 minute) :

   ```bash
   bash ~/clouddrive/cel-ca/azure/deploy.sh --community cel-ca --account $ACCOUNT --admin-dir ~/clouddrive/cel-admin
   ```

   Le script crée le groupe de ressources `rg-cel-ca` et le Storage Account en Suisse (Nord), puis lance un
   auto-test. Il doit se terminer par **`All checks passed.`** Si une ligne commence par `KO`, ne pas continuer
   (voir [A9](#a9-vérifier-et-dépanner)).

### A3. Ajouter un HEMS

Le membre suit d'abord [B1](#b1-installer-lintégration) et [B2](#b2-rejoindre-la-cel) et vous envoie son
**identifiant HEMS** (`hems-…`) et sa **clé publique** (43 caractères).

```bash
cel enroll --id hems-xxxxxxxxxxxxxxxxxxxx --pubkey <clé-publique> --name "Maison Dupont"
cel member-access --id hems-xxxxxxxxxxxxxxxxxxxx | grep '^CEL1\.'
cel publish
```

* `enroll` refuse si l'identifiant et la clé publique ne correspondent pas (copie incomplète) : les recopier.
* `member-access` affiche la **clé d'accès** (une longue ligne `CEL1.…`) : la sélectionner entièrement, clic droit
  → *Copy*, et l'envoyer au membre **par un canal privé** (elle permet d'agir au nom de son HEMS).
* `publish` diffuse la liste des membres : les autres HEMS voient le nouveau venu en moins de 15 secondes.

Options de `enroll` : `--roles producer,consumer` (défaut : les deux), `--meter CH10…` (point de mesure),
`--valid-days 365` (adhésion limitée dans le temps ; défaut : illimitée).
Option de `member-access` : `--days 365` (validité de la clé d'accès, défaut 365 jours).

### A4. Voir la liste des HEMS

```bash
cel list
```

```
Community cel-ca - registry serial 1791574143
  slot: 15 min
  storage: celca12345
  hems-2w7pu63op6pg4soy3jexduzk  Maison Alberto     consumer,producer  active
  hems-h5d63xndbjzkikqcxsbtgent  Maison Dupont      consumer,producer  REVOKED
```

Pour voir qui est **en ligne** et ce que chacun propose ou réserve, utiliser le
[tableau de bord](#tableau-de-bord-de-la-communauté) dans Home Assistant.

### A5. Supprimer un HEMS

```bash
cel revoke --id hems-xxxxxxxxxxxxxxxxxxxx
cel publish
```

Le HEMS est exclu : tous les autres l'ignorent, ses réservations sont annulées, sa boîte de réception et son
état sont effacés du stockage, et son appareil disparaît des Home Assistant des autres membres. Il reste
affiché `REVOKED` dans `cel list`. Pour le réadmettre plus tard : refaire [A3](#a3-ajouter-un-hems).

Si un membre réinstalle Home Assistant **sans sauvegarde**, son HEMS reçoit une nouvelle identité : ajouter le
nouveau ([A3](#a3-ajouter-un-hems)) et supprimer l'ancien (A5).

### A6. Renouveler la clé d'un HEMS

Une clé d'accès est valable 365 jours. 30 jours avant l'échéance, le membre voit une alerte dans
*Réparations* de Home Assistant. Émettre une nouvelle clé et la lui envoyer (il suit [B4](#b4-coller-une-nouvelle-clé-daccès)) :

```bash
cel member-access --id hems-xxxxxxxxxxxxxxxxxxxx | grep '^CEL1\.'
```

### A7. Modifier un HEMS

Pour changer le nom, les rôles ou le point de mesure : refaire `enroll` avec la **même** clé publique
(visible dans `~/clouddrive/cel-admin/cel_registry.json`) et les nouvelles valeurs, puis `cel publish`.
L'identité et la clé d'accès du HEMS ne changent pas.

### A8. Créneaux de marché

Par défaut le marché est continu. Pour caler offres et réservations sur des créneaux fixes alignés sur l'heure
(comme les courbes de charge du gestionnaire de réseau) :

```bash
cel set-param --slot-minutes 15      # 5, 10, 15, 20, 30 ou 60 ; 0 = marché continu
cel publish
```

En mode créneaux, chaque offre et chaque réservation porte sur **le prochain créneau complet** ; pour continuer,
le HEMS refait simplement une demande au créneau suivant.

### A9. Vérifier et dépanner

```bash
cel selftest
```

Vérifie le stockage et que la clé d'un membre **ne permet pas** d'écrire chez un autre ni de lire sa boîte. Toutes
les lignes doivent être `OK`.

* **Clé d'un membre perdue ou divulguée** : `cel member-access --id …` émet une nouvelle clé ; l'ancienne reste
  valable jusqu'à son expiration, sauf si vous faites tourner la clé du compte de stockage (*portail Azure →
  Storage Account → Access keys → Rotate key1*). Dans ce cas, **toutes** les clés deviennent invalides :
  refaire `member-access` pour chaque membre.
* **Sauvegarde** : seul le dossier `~/clouddrive/cel-admin` est important. Le stockage peut être recréé à tout
  moment avec `deploy.sh` ; il suffit ensuite de refaire `member-access` pour chaque membre et `cel publish`.
* Détails techniques de l'hébergement : [`azure/README.md`](azure/README.md).

---

## B. Membre (Home Assistant)

### B1. Installer l'intégration

1. Cliquer sur le bouton **Ouvrir dans HACS** en haut de cette page (ou dans HACS : *⋮ → Custom repositories*,
   ajouter `https://github.com/crapitouille/ha-cel-ca`, type *Integration*).
2. **Download** → choisir la dernière version → **Download**.
3. **Settings → System → Restart**.

Les mises à jour apparaîtront ensuite dans *Settings → Updates*.

### B2. Rejoindre la CEL

1. Cliquer sur le bouton **Ajouter l'intégration** en haut de cette page (ou *Settings → Devices & services →
   Add integration → WattKeeper*).
2. La fenêtre affiche votre **HEMS identifier** et votre **Public key** : les envoyer à l'administrateur.
   **Ne fermez pas cette fenêtre** en attendant sa réponse (sinon une nouvelle identité est créée et il faut lui
   renvoyer les nouvelles valeurs).
3. Remplir :
   * **HEMS name** : le nom de votre maison (il sert aussi à nommer les entités) ;
   * **CEL access key** : la clé `CEL1.…` reçue de l'administrateur ;
   * **Existing private key (restore only)** : laisser vide.
4. **Energy role** :
   * **Roles** : *Producer* si vous avez des panneaux solaires, *Consumer* si vous voulez réserver du surplus (ou les deux) ;
   * **Grid power sensor** : la puissance mesurée à votre compteur (par ex. `sensor.p1_meter_power`) ;
   * **Sign convention** : *Positive when importing from the grid* si le capteur est positif quand vous consommez du réseau
     (cas des compteurs P1), sinon l'autre choix.
5. **Submit**. Vérifier dans l'appareil créé que **CEL connection** et **Enrolled** sont sur *On*.

Messages possibles :
* *This access key was issued for another HEMS id* : la clé a été émise pour un autre identifiant ;
* *This is not a valid CEL access key* : clé incomplète, la recopier ;
* *Cannot reach the CEL storage* : pas d'accès Internet ou stockage indisponible ;
* *Your inbox does not exist…* : demander à l'administrateur de refaire `cel member-access`.

### B3. Réglages

*Settings → Devices & services → WattKeeper → Configure* : rôles, capteur, marge de sécurité (W gardés pour
vous), puissance minimale proposée, horizon de l'offre, seuil de republication, lissage du surplus. Le
commutateur **Publish surplus automatically** suspend ou reprend la publication de votre surplus.

### B4. Coller une nouvelle clé d'accès

Quand l'administrateur vous envoie une nouvelle clé (renouvellement) : *Settings → Devices & services →
WattKeeper → ⋮ → Reconfigure*, coller la clé, **Submit**. Si la clé a déjà expiré, Home Assistant affiche
lui-même une demande de **réauthentification** au même endroit. Votre identité est conservée.

### B5. Quitter la CEL ou réinstaller

* **Quitter** : supprimer l'intégration (*WattKeeper → ⋮ → Delete*) et prévenir l'administrateur ([A5](#a5-supprimer-un-hems)).
* **Réinstaller Home Assistant** : votre identité est enregistrée dans les **sauvegardes** de Home Assistant ;
  en restaurant une sauvegarde, vous retrouvez le même HEMS. Sans sauvegarde, refaire [B2](#b2-rejoindre-la-cel)
  (nouvelle identité) et demander à l'administrateur de supprimer l'ancienne.

---

## C. Utilisation

### Entités

Sur l'appareil de votre maison (noms affichés en anglais / français selon la langue de Home Assistant) :

| Entité | Signification |
|---|---|
| CEL connection / Connexion CEL | accès au stockage de la CEL |
| Enrolled / Enrôlé | votre HEMS figure dans la liste des membres |
| Offered surplus / Surplus proposé | W que vous proposez maintenant |
| Reserved power (production) | W de votre surplus réservés par des voisins |
| Reserved power (consumption) | W que vous avez réservés chez des voisins |
| Community surplus available | W encore réservables chez les voisins en ligne |
| Community surplus offered / reserved power | totaux de la communauté |
| Participants online | membres en ligne (vous compris) ; l'attribut `participants` donne le tableau complet |
| HEMS online | nombre de voisins en ligne |
| Commitment compliance | % de respect de vos réservations |
| HEMS identifier | votre identifiant ; attributs : clé publique, date d'expiration de la clé d'accès |
| Publish surplus automatically | commutateur (producteurs) |

**Un appareil par voisin**, créé ou retiré automatiquement : en ligne, surplus proposé et disponible,
puissances réservées, respect des engagements, dernier contact.

Délais typiques : offres et présence des voisins ≤ 15 s, réponse à une réservation 1 à 5 s.

### Tableau de bord de la communauté

*Developer tools → Actions → `cel_ca.generate_dashboard` → Perform action* : la réponse **est** le tableau de bord.
La copier, puis *Settings → Dashboards → Add dashboard → New dashboard from scratch*, l'ouvrir → crayon →
⋮ → *Raw configuration editor* → tout remplacer par le texte copié → *Save*. Relancer l'action quand un nouveau
membre rejoint la CEL pour ajouter ses graphiques.

### Réserver du surplus

| Action | Paramètres principaux | Effet |
|---|---|---|
| `cel_ca.reserve_global` | `power_w`, `min_power_w`, `duration_min` | réserve chez **plusieurs voisins** au prorata de leur surplus ; tout ou rien sous `min_power_w` |
| `cel_ca.reserve` | `power_w`, `min_power_w`, `duration_min`, `producer_id` | réserve chez **un** voisin |
| `cel_ca.cancel_reservation` | `reservation_id` | annule une réservation ou un groupe |
| `cel_ca.publish_offer` | `power_w`, `duration_min`, `start` | offre manuelle (par ex. selon une prévision solaire) |
| `cel_ca.withdraw_offer` | `offer_id` | retire une offre manuelle |
| `cel_ca.generate_dashboard` | — | tableau de bord de la communauté |

Les actions renvoient un résultat (`status`, puissance obtenue, identifiants). L'événement `cel_ca_event`
signale les changements : `granted`, `rejected`, `curtailed` (le producteur réduit car le soleil baisse),
`cancelled`, `completed`, `settled`, `group_updated`… Exemples prêts à l'emploi (recharge de voiture sur
surplus, notifications) : [`examples/automations.yaml`](examples/automations.yaml).

---

## D. Développement

```
custom_components/cel_ca/   intégration Home Assistant (core/ = protocole en Python pur)
tools/cel_admin.py          outil de l'administrateur (commande « cel »)
azure/                      Storage Account : Bicep et deploy.sh
docs/ARCHITECTURE.md        architecture, protocole et sécurité
tests/                      tests de non-régression (pytest, 204 tests, HA 2026.2 et 2026.10)
```

```bash
python -m venv .venv && . .venv/bin/activate      # Python 3.13+ (3.14 pour HA 2026.10)
pip install -r requirements_test.txt
npm install -g azurite                             # émulateur Azure Storage
pytest --cov
```

* `tests/core` : identité, messages signés, arbitrage, créneaux, réservation globale, sécurité (usurpation, rejeu, faux registre, révocation, limitation de débit), outil admin.
* `tests/storage` : signatures SAS identiques aux SDK Azure officiels ; transport et administration de bout en bout sur Azurite.
* `tests/ha` : configuration, réauthentification, reconfiguration, alerte d'expiration, entités, actions, événements, persistance, diagnostics, HA de bout en bout sur Azurite. Toute alerte de dépréciation de Home Assistant concernant `cel_ca` fait échouer les tests.
* `tests/test_packaging.py` : distribution HACS (structure, `hacs.json`, manifeste, logo, versions, boutons du README, workflows).
* `tests/azure` : Bicep, shellcheck, `deploy.sh` de bout en bout (faux `az` + Azurite).

Publier une version : mettre à jour la version dans `manifest.json`, `pyproject.toml` et `core/node.py`, puis
créer la release `v<version>` sur GitHub ; HACS la propose ensuite comme mise à jour.

Licence : [MIT](LICENSE).
