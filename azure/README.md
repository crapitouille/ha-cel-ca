# CEL-CA sur Azure Storage

Depuis la v0.6.0, la CEL n'a plus besoin de VM ni de broker : un **Storage Account** (Suisse Nord)
suffit.

* **Table** `cel<communauté>` : état partagé (registre signé, présence, offres, réservations, bilans) — une
  partition par HEMS ;
* **Queue** `<hems-id>` : boîte de réception de chaque HEMS (requêtes, réponses, annulations).

Les HEMS interrogent le stockage en HTTPS sortant (aucun port à ouvrir chez eux). Chaque membre reçoit une
**clé d'accès** `CEL1.…` contenant des jetons SAS restreints : lecture de la table, écriture de **sa seule
partition**, dépôt dans la boîte des autres, lecture de **sa seule boîte**. La clé du Storage Account n'est
jamais distribuée ni stockée : `cel_admin.py` la lit à la demande avec `az` (ou `$CEL_STORAGE_KEY`).

```
azure/
  main.bicep   Storage Account Standard_LRS, TLS 1.2, HTTPS uniquement, pas d'accès public blob
  deploy.sh    déploiement + configuration + auto-test, depuis Azure Cloud Shell (Bash)
```

Coût indicatif : quelques centimes par mois pour une dizaine de HEMS (transactions Table/Queue + quelques
Mo de données), à vérifier dans le [calculateur de prix Azure](https://azure.microsoft.com/pricing/calculator/).

## Déploiement (une fois, Azure Cloud Shell en Bash)

1. Cloud Shell en **Bash** avec stockage persistant (`ls ~/clouddrive` doit fonctionner).
2. *Gérer les fichiers → Charger* `cel-ca-v0.6.2.zip`, puis :

   ```bash
   cd ~/clouddrive && rm -rf cel-ca && unzip -q ~/cel-ca-v0.6.2.zip -d ~/clouddrive && cd ~/clouddrive/cel-ca
   python3 -m pip install --user cryptography        # seule dépendance de l'outil admin
   ```

3. Clé de l'administrateur et registre vide (sauter si `~/clouddrive/cel-admin` existe déjà) :

   ```bash
   python3 tools/cel_admin.py --dir ~/clouddrive/cel-admin init --community cel-ca
   ```

4. Storage Account (le nom doit être **unique dans tout Azure** : 3–24 minuscules/chiffres) :

   ```bash
   bash azure/deploy.sh --community cel-ca --account celcastore<suffixe> --admin-dir ~/clouddrive/cel-admin
   ```

   Options : `--resource-group` (défaut `rg-cel-ca`), `--location` (défaut `switzerlandnorth`).

Le script vérifie la connexion Azure et le nom, crée le groupe de ressources, déploie `main.bicep`,
enregistre le compte dans `cel_registry.json` (sans la clé) puis lance l'auto-test, qui doit se terminer par
`All checks passed.` :

```
OK  table celcelca
OK  member inboxes
OK  member reads the table
OK  member writes its own partition
OK  member CANNOT write another partition
OK  member CANNOT write the registry
OK  member sends to another inbox
OK  member reads its own inbox
OK  member CANNOT read another inbox
All checks passed.
```

Relancer `deploy.sh` est sans danger (déploiement Bicep idempotent).

## Ajouter un HEMS

Le membre ajoute l'intégration WattKeeper (CEL-CA) dans Home Assistant ; le formulaire affiche son **identifiant HEMS**
et sa **clé publique**, qu'il t'envoie (en gardant le formulaire ouvert).

```bash
cd ~/clouddrive/cel-ca
python3 tools/cel_admin.py --dir ~/clouddrive/cel-admin enroll --pubkey <clé> --id <hems-id> --name "Maison Dupont"
python3 tools/cel_admin.py --dir ~/clouddrive/cel-admin member-access --id <hems-id>     # affiche CEL1.…
python3 tools/cel_admin.py --dir ~/clouddrive/cel-admin publish
```

Envoyer au membre la clé `CEL1.…` (canal privé : elle donne accès à la CEL au nom de son HEMS, jusqu'à son
expiration — 365 jours par défaut, `--days` pour changer). Il la colle dans *CEL access key*.

## Exploitation

```bash
A="python3 tools/cel_admin.py --dir $HOME/clouddrive/cel-admin"
$A list                                   # membres, paramètres, compte de stockage
$A revoke --id <hems-id>; $A publish      # exclut le membre, supprime sa boîte et son état
$A member-access --id <hems-id>           # nouvelle clé (expiration) -> HA : WattKeeper > ⋮ > Reconfigure
$A set-param --slot-minutes 15; $A publish  # créneaux de marché (0 = continu)
$A selftest                               # revérifie les droits d'accès des membres
```

Une clé révoquée reste techniquement valable jusqu'à son expiration (lecture de la table, écriture de sa propre partition), mais le membre
n'est plus dans le registre signé : ses messages sont ignorés par tous les HEMS et sa boîte est supprimée.
Pour couper immédiatement **toutes** les clés (fuite, p. ex.) : *Storage Account → Clés d'accès → Faire
pivoter key1*, puis réémettre une clé `member-access` par membre.

Sauvegarde : seul le dossier administrateur (`cel_admin_key.json`, `cel_registry.json`) est critique. Le
stockage peut être recréé à tout moment : `deploy.sh`, `member-access` pour chaque membre, `publish` (les
HEMS republient leur état seuls).

## Migration depuis la v0.5 (VM + Mosquitto)

L'identité des HEMS, le dossier administrateur et les membres enrôlés sont **conservés**.

1. **Déployer le stockage** (Cloud Shell, nouveau zip) — même dossier admin :

   ```bash
   bash azure/deploy.sh --community cel-ca --account celcastore<suffixe> --admin-dir ~/clouddrive/cel-admin
   ```

2. **Émettre une clé d'accès pour chaque HEMS existant** (`list` donne les identifiants) puis publier :

   ```bash
   python3 tools/cel_admin.py --dir ~/clouddrive/cel-admin list
   python3 tools/cel_admin.py --dir ~/clouddrive/cel-admin member-access --id <hems-id>
   python3 tools/cel_admin.py --dir ~/clouddrive/cel-admin publish
   ```

3. **Mettre à jour l'intégration dans Home Assistant** : remplacer `/config/custom_components/cel_ca` par la
   v0.6.0 et redémarrer. L'entrée est migrée automatiquement (paramètres MQTT supprimés, identité
   conservée) et HA demande une réauthentification : *Settings → Devices & services*, carte *WattKeeper* en haut de la
   page → *Reconfigure*, coller la clé `CEL1.…`. Les entités gardent leurs identifiants.
4. Vérifier : `binary_sensor.<hems>_cel_broker` (nom affiché « CEL connection ») = *on*, `…_enrolled` = *on*.
5. **Supprimer l'ancienne VM** une fois tous les HEMS migrés :

   ```bash
   az group delete -n rg-cel-ca-broker --yes --no-wait
   ```

   Les anciennes commandes `broker-user`, `acl` et `azure-target` n'existent plus ; le champ `broker` du
   registre est supprimé automatiquement par `deploy.sh`.
