# Plan multicast

Registre du **plan d'adressage multicast** d'un fabric **ST 2110**. On y déclare les **plages**
d'adresses réservées à chaque usage (vidéo, audio, données auxiliaires), on **attribue** les
adresses aux équipements/flux, et on **rapproche** le plan de ce qui tourne réellement sur le
réseau (via l'outil *Monitoring 2110*).

Tout est enregistré **côté serveur** (rien n'est perdu au rechargement).

## Plages

Une **plage** est un bloc d'adresses réservé à un usage. Bouton **+ Plage** (volet de gauche).

- **Adresses** : au choix un **CIDR** (ex. `239.1.0.0/24`) ou un couple **début–fin**
  (ex. `239.1.0.0` → `239.1.0.255`). Seules les adresses du bloc multicast **224.0.0.0/4**
  (224.0.0.0 – 239.255.255.255) sont acceptées.
- **Essence** : `Vidéo (2110-20)`, `Audio (2110-30)`, `Données auxiliaires (2110-40)` ou `Autre`.
- **Réseau** : `Rouge`, `Bleu` ou `Les deux` (fabric redondant 2110-7).
- **VLAN** et **Notes** : optionnels.

⚠ Si deux plages **se chevauchent**, un avertissement s'affiche (sur la plage, dans le volet,
et à l'enregistrement). Ce n'est **pas bloquant** : c'est à vous de corriger le plan.

Chaque plage affiche une **jauge d'occupation** (adresses attribuées / taille totale de la
plage). La jauge passe en **orange** à ≥ 70 % puis en **rouge** à ≥ 90 %.

## Attributions

À l'intérieur d'une plage sélectionnée, bouton **+ Attribution** :

- **Adresse ou sous-bloc** : une adresse simple (`239.1.0.10`) **ou** un sous-bloc CIDR
  (`239.1.0.16/28`). Elle doit tomber **dans** la plage de rattachement.
- **Équipement / flux**, **Libellé**, **Port UDP** : optionnels.
- Bouton **Libre ?** : propose la **prochaine adresse libre** de la plage (la première non
  encore attribuée).

## Recherche « où est une adresse ? »

Le champ en haut du volet répond à *« où est 239.1.2.3 ? »* : il indique la **plage** qui la
contient (cliquable) et l'**attribution** correspondante, ou signale une adresse **libre** /
hors bloc multicast.

## Rapprocher avec le fabric

Bouton **↔ Rapprocher** : l'outil interroge **Monitoring 2110** pour connaître les **groupes
multicast réellement actifs**, puis compare au plan et classe en quatre catégories :

- **✓ Conformes** — actifs **et** documentés (une attribution correspond).
- **＋ Actifs non documentés** — actifs, sans attribution, mais **dans une plage** connue.
- **⚠ Actifs hors de toute plage** — actifs et **hors** de tout bloc déclaré.
- **○ Documentés mais inactifs** — attribués dans le plan mais **absents** du réel.

Si *Monitoring 2110* est **arrêté** ou **inaccessible** (droits insuffisants, outil non
installé), le rapprochement affiche un message clair — **le plan reste consultable et
modifiable**.

> L'appariement se fait sur l'**adresse de groupe** ; les sources SSM (S,G) ne sont pas
> distinguées.

## Export

Bouton **Exporter** → **JSON** (plan complet, ré-importable à la main) ou **CSV** (une ligne
par attribution, tableur). Le téléchargement se fait dans le navigateur.
