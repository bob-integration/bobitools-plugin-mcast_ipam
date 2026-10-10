# Plan multicast — plugin Bobi.Tools

Registre du **plan d'adressage multicast** d'un fabric **ST 2110** pour
[Bobi.Tools](https://github.com/bob-integration/bobitools) : plages réservées à chaque usage,
attributions aux équipements et aux flux, et rapprochement avec ce qui tourne réellement.

## Ce que fait l'outil

- **Plages** en CIDR ou en début–fin, limitées au bloc 224.0.0.0/4, avec essence (vidéo 2110-20,
  audio 2110-30, données 2110-40), réseau rouge, bleu ou les deux, VLAN et notes ; un
  chevauchement est signalé sans être bloquant.
- **Attributions** d'une adresse ou d'un sous-bloc, avec le bouton « Libre ? » qui propose la
  prochaine adresse libre, et une jauge d'occupation par plage.
- **Recherche** « où est cette adresse ? » : la plage qui la contient et son attribution.
- **Rapprochement** du plan avec les groupes actifs vus par un autre outil : conformes, actifs
  non documentés, actifs hors plage, documentés mais inactifs.
- **Export** JSON (plan complet) ou CSV (une ligne par attribution).
- **Dictionnaire de noms** pour les autres outils (`GET /api/tools/mcast_ipam/names?groups=…`) :
  [NAT multicast](https://github.com/bob-integration/bobitools-plugin-mcast_nat) s'en sert pour
  afficher des noms plutôt que des adresses.

## À savoir

- Le rapprochement apparie sur l'**adresse de groupe** : les sources SSM (S,G) ne sont pas
  distinguées.
- Si l'outil qui fournit les groupes actifs est arrêté ou absent, le rapprochement le dit ; le
  plan reste consultable et modifiable.

## Prérequis

- Aucun : l'outil tourne dans Bobi.Tools (`runtime: inprocess`), sans dépendance Python
  supplémentaire.

## Installation

Dans Bobi.Tools : **Réglages → Outils → Catalogue**, bouton « Installer ». Ou, sur une machine
neuve, en une ligne :

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/bob-integration/bobitools/main/get.sh) --outils mcast_ipam
```

L'aide complète est dans [`help.md`](help.md), affichée dans Bobi.Tools (menu « ? » → Aide).

## In English

Multicast address plan registry for an ST 2110 fabric, as a Bobi.Tools plugin: address ranges
(CIDR or start–end) per essence and red/blue network, assignments with a "next free address"
helper, address lookup, occupancy gauges, JSON/CSV export, and reconciliation of the plan with
the groups actually active on the network. Other tools can query it to display stream names
instead of raw addresses. Runs in-process, no Docker required.

## Licence

GPL-3.0-or-later — © 2026 BOBI SAS. Voir [LICENSE](LICENSE).
