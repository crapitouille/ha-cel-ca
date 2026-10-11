# Hébergement Azure de la CEL

Les procédures pas à pas (création de la CEL, ajout et suppression de HEMS, clés d'accès) sont dans le
[README principal](../README.md#a-administrateur-de-la-cel). Ce document décrit l'hébergement lui-même.

## Ressources

Un seul **Storage Account** (Standard LRS, région Suisse Nord par défaut) dans le groupe de ressources
`rg-<communauté>` :

* **Table** `cel<communauté>` : état partagé de la CEL — registre signé des membres, présence, offres,
  réservations, bilans. Une partition par HEMS.
* **Queue** `<hems-id>` : boîte de réception de chaque HEMS (demandes de réservation, réponses, annulations).
  Les messages expirent après 1 heure.

```
azure/
  main.bicep   Storage Account : TLS 1.2 minimum, HTTPS uniquement, aucun accès public aux blobs
  deploy.sh    déploiement + configuration de l'outil d'administration + auto-test (Azure Cloud Shell)
```

Coût indicatif : quelques centimes par mois pour une dizaine de HEMS (transactions Table et Queue, quelques Mo
de données), à vérifier dans le [calculateur de prix Azure](https://azure.microsoft.com/pricing/calculator/).

## Sécurité

* Les HEMS se connectent en **HTTPS sortant** uniquement ; aucun port n'est ouvert chez les membres.
* Chaque membre reçoit une **clé d'accès** `CEL1.…` contenant des jetons SAS restreints :

  | Droit | Portée |
  |---|---|
  | lire | la table de la CEL |
  | écrire | **sa propre partition** de la table, uniquement |
  | déposer un message | dans la boîte de réception des autres HEMS |
  | lire et supprimer | **sa propre** boîte de réception, uniquement |

* La **clé du Storage Account** n'est jamais distribuée ni enregistrée : l'outil `cel` la lit à la demande avec
  `az storage account keys list` (ou depuis la variable `CEL_STORAGE_KEY`).
* Tous les messages sont **signés** par le HEMS émetteur et la liste des membres par l'administrateur : même un
  accès frauduleux au stockage ne permet pas de se faire passer pour un membre.
* `cel selftest` vérifie sur le vrai compte qu'une clé de membre ne peut ni écrire dans la partition d'un autre,
  ni modifier le registre, ni lire la boîte d'un autre.

## deploy.sh

```bash
bash ~/clouddrive/cel-ca/azure/deploy.sh --community <communauté> --account <nom-du-stockage> --admin-dir ~/clouddrive/cel-admin
```

| Option | Défaut | Rôle |
|---|---|---|
| `--community` | — | identifiant de la communauté (le même que pour `cel init`) |
| `--account` | — | nom du Storage Account, unique dans tout Azure (3–24 minuscules et chiffres) |
| `--admin-dir` | — | dossier de l'administrateur (`~/clouddrive/cel-admin`) |
| `--resource-group` | `rg-<communauté>` | groupe de ressources |
| `--location` | `switzerlandnorth` | région Azure |

Étapes : vérification de la connexion Azure et du nom, création du groupe de ressources, déploiement de
`main.bicep`, enregistrement du compte dans `cel_registry.json` (sans la clé), auto-test. Relancer le script
est sans danger.

Résultat attendu de l'auto-test :

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

## Révocation et rotation des clés

* `cel revoke` retire le membre du registre signé (tous les HEMS l'ignorent aussitôt publié) et efface sa boîte
  de réception et son état. Sa clé d'accès reste techniquement valable jusqu'à son expiration pour lire la table
  et écrire dans sa propre partition, ce qui est sans effet puisque ses messages sont ignorés.
* Pour invalider **immédiatement toutes** les clés d'accès (fuite) : *portail Azure → Storage Account → Access
  keys → Rotate key1*, puis `cel member-access --id …` pour chaque membre.

## Supprimer la CEL

```bash
az group delete -n rg-<communauté> --yes
```

Supprime le stockage et toutes ses données. Le dossier `~/clouddrive/cel-admin` n'est pas touché : la CEL peut
être recréée avec `deploy.sh`.
