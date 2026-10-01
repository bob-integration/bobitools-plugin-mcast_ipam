# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
Backend in-process de l'outil « Plan multicast » (registre du plan d'adressage multicast
d'un fabric ST 2110).

Contrat d'un backend Bobi.Tools (runtime=inprocess) :

    def api(path, method, payload, ctx) -> data | (status, data)

- `path`    : segment(s) après /api/tools/mcast_ipam/
- `method`  : verbe HTTP entrant
- `payload` : corps JSON (POST/PUT) ou query (GET), déjà parsé en dict
- `ctx`     : { user, store, setting, audit, tool_dir }
    - ctx.store : stockage générique scopé à l'outil (list/get/create/update/delete),
      ici découpé en DEUX scopes :
        * scope "range"  → plages d'adresses multicast (name = libellé de la plage)
        * scope "assign" → attributions à l'intérieur d'une plage (name = libellé/adresse)

Tout le métier (validation des adresses multicast, détection de chevauchement, prochaine
adresse libre, rapprochement avec le réel, export) est fait ICI : le front reste une simple
vue. Le rapprochement reçoit du front la liste des groupes réellement actifs (collectés par
lui auprès de monitoring_2110) et n'appelle donc jamais le réseau depuis le serveur.

FORMAT DES OBJETS store
-----------------------
Plage (scope "range"), value :
    { "mode": "cidr"|"range", "cidr": "239.1.0.0/24"?, "start": "239.1.0.0",
      "end": "239.1.0.255", "essence": "video"|"audio-2110-30"|"anc-2110-40"|"autre",
      "network": "rouge"|"bleu"|"les-deux", "vlan": 100|null, "notes": "" }
Attribution (scope "assign"), value :
    { "range_id": <int|null>, "kind": "single"|"block", "address": "239.1.0.10"|"239.1.0.16/28",
      "device": "", "label": "", "udp_port": <int|null> }
"""
import csv
import io
import ipaddress
import time

RANGE_SCOPE = "range"
ASSIGN_SCOPE = "assign"
# Un fabric dont l'adressage suit une convention n'a pas besoin qu'on énumère ses plages : la
# convention SUFFIT à dire où va chaque flux. On stocke donc la RÈGLE et les FAMILLES, et la
# grille des adresses se calcule. Les plages manuelles restent disponibles pour tout ce qui
# échappe à la convention.
RULE_SCOPE = "rule"
# L'objet qu'on crée est l'ÉQUIPEMENT — c'est lui qu'on branche, qu'on nomme et qu'on
# renumérote. La « famille » n'est pas une chose qu'on déclare : c'est un attribut de
# l'équipement, et le regroupement s'en déduit à l'affichage.
EQUIP_SCOPE = "equipment"
# La famille n'est pas un objet qu'on crée — c'est un attribut de l'équipement. Mais elle
# mérite un NOM : « 239.4.x » ne dit rien, « SNP régie » si. On stocke donc une simple
# étiquette par numéro de famille, sans en faire une entité dont dépendraient les
# équipements : supprimer l'étiquette ne doit rien casser, juste retirer le nom.
FAMLABEL_SCOPE = "family_label"

# Bloc multicast IPv4 : 224.0.0.0/4 (224.0.0.0 – 239.255.255.255).
_MCAST_NET = ipaddress.ip_network("224.0.0.0/4")
_MCAST_MIN = int(_MCAST_NET.network_address)
_MCAST_MAX = int(_MCAST_NET.broadcast_address)

# Essences 2110 acceptées (video / audio 2110-30 / ancillary 2110-40 / autre).
ESSENCES = {"video", "audio-2110-30", "anc-2110-40", "autre"}
# Réseaux redondants d'un fabric 2110 (rouge / bleu / les-deux).
NETWORKS = {"rouge", "bleu", "les-deux"}

# Garde-fou : nombre d'adresses parcourues au plus pour « prochaine adresse libre » et le
# calcul d'occupation, pour ne jamais balayer une plage pathologiquement énorme (ex. /4).
_SCAN_CAP = 200_000


# ─── Helpers d'adresses ───────────────────────────────────────────────────────

def _is_v4(addr):
    try:
        return isinstance(ipaddress.ip_address(addr), ipaddress.IPv4Address)
    except ValueError:
        return False


def _v4_int(addr):
    """Adresse IPv4 pointée -> entier. Lève ValueError si invalide."""
    ip = ipaddress.ip_address(str(addr).strip())
    if not isinstance(ip, ipaddress.IPv4Address):
        raise ValueError("adresse IPv4 requise")
    return int(ip)


def _is_mcast_int(n):
    return _MCAST_MIN <= n <= _MCAST_MAX


def _dotted(n):
    return str(ipaddress.IPv4Address(n))


def _parse_range_bounds(payload):
    """Extrait (mode, cidr|None, start_int, end_int) d'un payload de plage.

    Accepte soit `cidr` (ex. "239.1.0.0/24"), soit `start`+`end` (bornes incluses).
    Valide que les bornes sont dans le bloc multicast 224.0.0.0/4. Lève ValueError avec un
    message clair sinon.
    """
    cidr = (payload.get("cidr") or "").strip()
    start = (payload.get("start") or "").strip()
    end = (payload.get("end") or "").strip()

    if cidr:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            raise ValueError("CIDR invalide")
        if not isinstance(net, ipaddress.IPv4Network):
            raise ValueError("CIDR IPv4 requis")
        lo, hi = int(net.network_address), int(net.broadcast_address)
        mode, cidr_out = "cidr", str(net)
    elif start and end:
        try:
            lo, hi = _v4_int(start), _v4_int(end)
        except ValueError:
            raise ValueError("adresses de début/fin invalides")
        if lo > hi:
            raise ValueError("l'adresse de début est supérieure à l'adresse de fin")
        mode, cidr_out = "range", None
    else:
        raise ValueError("fournir un CIDR, ou une adresse de début ET de fin")

    if not (_is_mcast_int(lo) and _is_mcast_int(hi)):
        raise ValueError("hors du bloc multicast 224.0.0.0/4")
    return mode, cidr_out, lo, hi


def _range_bounds(row):
    """(start_int, end_int) d'une ligne de plage stockée (recalculés à la lecture)."""
    v = row.get("value") or {}
    try:
        if v.get("mode") == "cidr" and v.get("cidr"):
            net = ipaddress.ip_network(v["cidr"], strict=False)
            return int(net.network_address), int(net.broadcast_address)
        return _v4_int(v.get("start")), _v4_int(v.get("end"))
    except (ValueError, TypeError):
        return None, None


def _assign_addr_bounds(row):
    """(lo, hi, kind, cidr_or_ip) d'une attribution : adresse simple -> (n, n) ; sous-bloc
    CIDR -> (réseau, broadcast). Retourne (None, None, ...) si l'adresse est illisible."""
    v = row.get("value") or {}
    addr = (v.get("address") or "").strip()
    if not addr:
        return None, None, None, None
    if "/" in addr:
        try:
            net = ipaddress.ip_network(addr, strict=False)
        except ValueError:
            return None, None, "block", addr
        return int(net.network_address), int(net.broadcast_address), "block", str(net)
    try:
        n = _v4_int(addr)
    except ValueError:
        return None, None, "single", addr
    return n, n, "single", addr


# ─── Sérialisation des lignes vers le front ───────────────────────────────────

def _range_out(row):
    v = row.get("value") or {}
    lo, hi = _range_bounds(row)
    total = (hi - lo + 1) if (lo is not None and hi is not None) else 0
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "mode": v.get("mode"),
        "cidr": v.get("cidr"),
        "start": _dotted(lo) if lo is not None else v.get("start"),
        "end": _dotted(hi) if hi is not None else v.get("end"),
        "pool": bool(v.get("pool")),
        "start_int": lo, "end_int": hi, "total": total,
        "essence": v.get("essence") or "autre",
        "network": v.get("network") or "les-deux",
        "vlan": v.get("vlan"),
        "notes": v.get("notes") or "",
    }


def _assign_out(row):
    v = row.get("value") or {}
    lo, hi, kind, addr = _assign_addr_bounds(row)
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "range_id": v.get("range_id"),
        "kind": kind,
        "address": addr,
        "lo": lo, "hi": hi,
        "size": (hi - lo + 1) if (lo is not None and hi is not None) else 0,
        "device": v.get("device") or "",
        "label": v.get("label") or "",
        "udp_port": v.get("udp_port"),
    }


def _list_ranges(ctx):
    return [_range_out(r) for r in ctx.store.list(RANGE_SCOPE)]


def _list_assigns(ctx):
    return [_assign_out(r) for r in ctx.store.list(ASSIGN_SCOPE)]


def _overlaps(ranges):
    """Paires de plages qui se chevauchent (avertissement, pas une erreur bloquante).
    Retourne [{a:id, a_name, b:id, b_name}]."""
    out = []
    valid = [r for r in ranges if r["start_int"] is not None]
    for i in range(len(valid)):
        for j in range(i + 1, len(valid)):
            a, b = valid[i], valid[j]
            if a["start_int"] <= b["end_int"] and b["start_int"] <= a["end_int"]:
                out.append({"a": a["id"], "a_name": a["name"],
                            "b": b["id"], "b_name": b["name"]})
    return out


def _used_ints(assigns, lo, hi):
    """Ensemble des entiers occupés dans [lo, hi] par les attributions données (adresses
    simples + sous-blocs), borné par _SCAN_CAP pour rester sûr."""
    used = set()
    for a in assigns:
        if a["lo"] is None:
            continue
        s, e = max(a["lo"], lo), min(a["hi"], hi)
        if s > e:
            continue
        for n in range(s, min(e, s + _SCAN_CAP) + 1):
            used.add(n)
    return used


# ─── Validation d'une attribution ─────────────────────────────────────────────

def _validate_assign(payload, ctx, ranges, self_id=None):
    """Valide/normalise une attribution. Retourne (value_dict, name) ou lève ValueError."""
    addr = (payload.get("address") or "").strip()
    if not addr:
        raise ValueError("adresse (ou sous-bloc) requise")
    if "/" in addr:
        try:
            net = ipaddress.ip_network(addr, strict=False)
        except ValueError:
            raise ValueError("sous-bloc CIDR invalide")
        if not isinstance(net, ipaddress.IPv4Network):
            raise ValueError("sous-bloc IPv4 requis")
        lo, hi, kind, norm = int(net.network_address), int(net.broadcast_address), "block", str(net)
    else:
        try:
            lo = _v4_int(addr)
        except ValueError:
            raise ValueError("adresse IPv4 invalide")
        hi, kind, norm = lo, "single", _dotted(lo)
    if not (_is_mcast_int(lo) and _is_mcast_int(hi)):
        raise ValueError("adresse hors du bloc multicast 224.0.0.0/4")

    range_id = payload.get("range_id")
    range_id = int(range_id) if range_id not in (None, "", "null") else None
    if range_id is not None:
        rg = next((r for r in ranges if r["id"] == range_id), None)
        if not rg:
            raise ValueError("plage de rattachement introuvable")
        if not (rg["start_int"] <= lo and hi <= rg["end_int"]):
            raise ValueError(f"l'adresse sort de la plage « {rg['name']} »")

    udp = payload.get("udp_port")
    if udp in (None, "", "null"):
        udp = None
    else:
        try:
            udp = int(udp)
        except (TypeError, ValueError):
            raise ValueError("port UDP invalide")
        if not (0 <= udp <= 65535):
            raise ValueError("port UDP hors plage (0–65535)")

    value = {
        "range_id": range_id, "kind": kind, "address": norm,
        "device": (payload.get("device") or "").strip()[:120],
        "label": (payload.get("label") or "").strip()[:200],
        "udp_port": udp,
    }
    name = value["label"] or value["device"] or norm
    return value, name



# ─── Règles d'adressage et familles ───────────────────────────────────────────
#
# Une RÈGLE décrit comment une adresse multicast se déduit de l'équipement qui l'émet :
# `239.FAMILLE.ÉQUIPEMENT.FLUX`, le dernier octet désignant à la fois l'essence et le rang du
# flux. Une FAMILLE instancie la règle pour un groupe d'équipements. Mesuré sur le parc le
# 2026-09-17 : 392 adresses sur 392 respectent cette forme, et 384 secours sur 384 valent bien
# famille + 100. C'est cette régularité qui rend le calcul préférable à la saisie.

RULE_DEFAULT_NAME = "Convention Bobi"

# Essences de la convention maison. `first`/`last` bornent le dernier octet, `port` est le
# port UDP attendu. L'ordre compte : il fixe l'ordre d'affichage de la grille.
RULE_DEFAULT_ESSENCES = [
    {"key": "video", "label": "Vidéo", "first": 1, "last": 50, "port": 2120},
    {"key": "anc", "label": "ANC", "first": 51, "last": 100, "port": 2140},
    {"key": "audio1", "label": "Audio 1", "first": 101, "last": 150, "port": 2130},
    {"key": "audio2", "label": "Audio 2", "first": 201, "last": 250, "port": 2130},
]
RULE_DEFAULT_OFFSET = 100        # secours 2022-7 : même adresse, famille + 100


def _rule_value(payload, base=None):
    """Valide une règle. Lève ValueError avec un message destiné à l'opérateur."""
    base = base or {}
    ess = payload.get("essences", base.get("essences") or RULE_DEFAULT_ESSENCES)
    if not isinstance(ess, list) or not ess:
        raise ValueError("au moins une essence est requise")
    propre = []
    for e in ess:
        if not isinstance(e, dict):
            raise ValueError("essence mal formée")
        cle = (e.get("key") or "").strip()
        if not cle:
            raise ValueError("chaque essence a besoin d'une clé")
        try:
            first, last = int(e.get("first")), int(e.get("last"))
        except (TypeError, ValueError):
            raise ValueError("bornes d'essence non numériques (%s)" % cle)
        if not (0 <= first <= last <= 255):
            raise ValueError("bornes hors 0–255 pour « %s »" % cle)
        port = e.get("port")
        port = int(port) if port not in (None, "", "null") else None
        if port is not None and not (0 < port < 65536):
            raise ValueError("port UDP invalide pour « %s »" % cle)
        propre.append({"key": cle, "label": (e.get("label") or cle).strip(),
                       "first": first, "last": last, "port": port})
    # Deux essences qui se recouvrent rendraient l'appartenance d'une adresse ambiguë : on
    # refuse, plutôt que de laisser la première gagner en silence.
    for i, a in enumerate(propre):
        for b in propre[i + 1:]:
            if a["first"] <= b["last"] and b["first"] <= a["last"]:
                raise ValueError("les essences « %s » et « %s » se recouvrent"
                                 % (a["label"], b["label"]))
    off = payload.get("offset", base.get("offset", RULE_DEFAULT_OFFSET))
    try:
        off = int(off)
    except (TypeError, ValueError):
        raise ValueError("décalage de secours non numérique")
    if not (0 <= off <= 255):
        raise ValueError("décalage de secours hors 0–255")
    return {"essences": propre, "offset": off,
            "notes": (payload.get("notes", base.get("notes", "")) or "").strip()}


def _rule_out(row):
    v = row.get("value") or {}
    return {"id": row.get("id"), "name": row.get("name"), "essences": v.get("essences") or [],
            "offset": v.get("offset", RULE_DEFAULT_OFFSET), "notes": v.get("notes") or ""}


def _list_rules(ctx):
    return [_rule_out(r) for r in ctx.store.list(RULE_SCOPE)]


def _ensure_default_rule(ctx):
    """Crée la convention maison au premier accès, et une seule fois.

    Un registre vide n'apprend rien à personne : la règle par défaut est celle qu'on a
    mesurée sur le fabric, donc immédiatement juste. Elle reste entièrement modifiable."""
    if ctx.store.list(RULE_SCOPE):
        return
    ctx.store.create(RULE_DEFAULT_NAME,
                     {"essences": RULE_DEFAULT_ESSENCES, "offset": RULE_DEFAULT_OFFSET,
                      "notes": "Convention mesurée sur le fabric : 392 adresses sur 392 "
                               "conformes, 384 secours sur 384."},
                     scope=RULE_SCOPE)
    ctx.audit("rule_seed", RULE_DEFAULT_NAME)


def _parse_equipments(brut):
    """« 1,2,3 » ou « 1-16 » ou « 1-3,7,10-12 » → [1,2,3,…]. Ordonné, sans doublon."""
    if isinstance(brut, list):
        sortie = []
        for x in brut:
            try:
                sortie.append(int(x))
            except (TypeError, ValueError):
                raise ValueError("numéro d'équipement non numérique : %r" % (x,))
        vus = sorted(set(sortie))
    else:
        vus = set()
        for morceau in str(brut or "").replace(";", ",").split(","):
            morceau = morceau.strip()
            if not morceau:
                continue
            if "-" in morceau:
                a, _, b = morceau.partition("-")
                try:
                    a, b = int(a), int(b)
                except ValueError:
                    raise ValueError("intervalle d'équipements illisible : « %s »" % morceau)
                if a > b:
                    raise ValueError("intervalle inversé : « %s »" % morceau)
                vus.update(range(a, b + 1))
            else:
                try:
                    vus.add(int(morceau))
                except ValueError:
                    raise ValueError("numéro d'équipement illisible : « %s »" % morceau)
        vus = sorted(vus)
    if not vus:
        raise ValueError("aucun équipement")
    hors = [x for x in vus if not (0 <= x <= 255)]
    if hors:
        raise ValueError("numéro d'équipement hors 0–255 : %s" % hors[0])
    return vus


def _equip_value(payload, rules, base=None):
    base = base or {}
    rid = payload.get("rule_id", base.get("rule_id"))
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        raise ValueError("règle non précisée")
    if not any(r["id"] == rid for r in rules):
        raise ValueError("règle introuvable")
    fam = payload.get("family", base.get("family"))
    num = payload.get("number", base.get("number"))
    try:
        fam, num = int(fam), int(num)
    except (TypeError, ValueError):
        raise ValueError("famille et numéro d'équipement doivent être des nombres")
    if not (0 <= fam <= 255):
        raise ValueError("famille hors 0–255")
    if not (0 <= num <= 255):
        raise ValueError("numéro d'équipement hors 0–255")
    network = (payload.get("network", base.get("network", "les-deux")) or "les-deux").strip()
    if network not in NETWORKS:
        raise ValueError("réseau inconnu")
    # L'unicast est DOCUMENTAIRE : mesuré sur le parc, le numéro d'équipement ne s'en déduit
    # pas (10.1.211.49 porte le multicast 239.4.1.x). Le calculer produirait des adresses
    # fausses ; on se contente donc de le retenir, pour retrouver la machine.
    uni = (payload.get("unicast", base.get("unicast", "")) or "").strip()
    if uni:
        try:
            ipaddress.ip_address(uni)
        except ValueError:
            raise ValueError("adresse unicast invalide")
    # Un équipement ne porte pas forcément toutes les essences, ni toute l'étendue qui leur
    # est ouverte : un boîtier purement audio n'a que faire de 50 adresses vidéo. On réserve
    # donc ce qu'il a — le reste de la plage demeure libre pour un autre.
    regle = next(r for r in rules if r["id"] == rid)
    dispo = [e["key"] for e in regle["essences"]]
    choisies = payload.get("essences", base.get("essences"))
    if choisies is None:
        choisies = dispo
    if not isinstance(choisies, list) or not choisies:
        raise ValueError("au moins une essence est requise")
    inconnues = [k for k in choisies if k not in dispo]
    if inconnues:
        raise ValueError("essence absente de la règle : %s" % inconnues[0])
    choisies = [k for k in dispo if k in choisies]      # ordre de la règle, sans doublon
    canaux = payload.get("channels", base.get("channels", 1))
    try:
        canaux = int(canaux)
    except (TypeError, ValueError):
        raise ValueError("nombre de canaux non numérique")
    if canaux < 1:
        raise ValueError("au moins un canal")
    # La plus étroite des essences retenues fixe le plafond : au-delà, l'adresse déborderait
    # sur l'essence suivante et changerait de sens.
    for e in regle["essences"]:
        if e["key"] in choisies:
            place = e["last"] - e["first"] + 1
            if canaux > place:
                raise ValueError("« %s » n'offre que %d canaux (%d–%d)"
                                 % (e["label"], place, e["first"], e["last"]))
    return {"rule_id": rid, "family": fam, "number": num, "network": network,
            "unicast": uni, "essences": choisies, "channels": canaux,
            "notes": (payload.get("notes", base.get("notes", "")) or "").strip()}


def _equip_out(row):
    v = row.get("value") or {}
    return {"id": row.get("id"), "name": row.get("name"), "rule_id": v.get("rule_id"),
            "family": v.get("family"), "number": v.get("number"),
            "network": v.get("network") or "les-deux", "unicast": v.get("unicast") or "",
            "essences": v.get("essences"), "channels": v.get("channels") or 1,
            "notes": v.get("notes") or ""}


def _famlabel_out(row):
    v = row.get("value") or {}
    return {"id": row.get("id"), "name": row.get("name"), "family": v.get("family"),
            "notes": v.get("notes") or ""}


def _list_famlabels(ctx):
    return sorted((_famlabel_out(r) for r in ctx.store.list(FAMLABEL_SCOPE)),
                  key=lambda f: f["family"] if isinstance(f["family"], int) else 0)


def _famlabel_value(payload, base=None):
    base = base or {}
    fam = payload.get("family", base.get("family"))
    try:
        fam = int(fam)
    except (TypeError, ValueError):
        raise ValueError("numéro de famille non numérique")
    if not (0 <= fam <= 255):
        raise ValueError("famille hors 0–255")
    return {"family": fam, "notes": (payload.get("notes", base.get("notes", "")) or "").strip()}


def _list_equipments(ctx):
    return sorted((_equip_out(r) for r in ctx.store.list(EQUIP_SCOPE)),
                  key=lambda e: (e["family"] or 0, e["number"] or 0))


def _blocks(rule, equip):
    """Adresses d'UN équipement : un bloc par essence et par réseau. Jamais stockées."""
    reseaux = []
    if equip["network"] in ("rouge", "les-deux"):
        reseaux.append(("rouge", equip["family"]))
    if equip["network"] in ("bleu", "les-deux"):
        reseaux.append(("bleu", equip["family"] + rule["offset"]))
    # Un équipement antérieur au choix des essences n'en déclare aucune : il les prend
    # toutes, comme avant, plutôt que de disparaître de la grille.
    choisies = equip.get("essences") or [e["key"] for e in rule["essences"]]
    canaux = equip.get("channels") or 1
    blocs = []
    for ess in rule["essences"]:
        if ess["key"] not in choisies:
            continue
        fin = min(ess["first"] + canaux - 1, ess["last"])
        for reseau, octet in reseaux:
            if not (0 <= octet <= 255):
                continue
            blocs.append({"essence": ess["key"], "essence_label": ess["label"],
                          "network": reseau, "port": ess["port"],
                          "start": "239.%d.%d.%d" % (octet, equip["number"], ess["first"]),
                          "end": "239.%d.%d.%d" % (octet, equip["number"], fin),
                          "size": fin - ess["first"] + 1})
    return blocs


def _blocs_calcules(ctx):
    """Tous les blocs que la convention réserve, en entiers : [(lo, hi, libellé)].

    Sert à prévenir un télescopage entre les deux mécanismes. Deux plans coexistent — la règle
    et la saisie manuelle — et rien n'empêche d'attribuer à la main une adresse que la
    convention a déjà donnée à un équipement. On AVERTIT sans bloquer, comme pour les
    chevauchements de plages : c'est à l'exploitant de trancher, pas à l'outil."""
    rules = {r["id"]: r for r in _list_rules(ctx)}
    out = []
    for e in _list_equipments(ctx):
        r = rules.get(e["rule_id"])
        if not r:
            continue
        for b in _blocks(r, e):
            try:
                out.append((_v4_int(b["start"]), _v4_int(b["end"]),
                            "%s — %s (%s)" % (e["name"], b["essence_label"], b["network"])))
            except ValueError:
                continue
    return out


def _telescopage(ctx, lo, hi):
    """Blocs de la convention qu'un intervalle [lo, hi] recouperait."""
    return [lib for a, b, lib in _blocs_calcules(ctx) if lo <= b and a <= hi]


def _plan_index(rules, equipments):
    """{(octet_famille, numéro): (équipement, règle, réseau)} pour reconnaître une adresse.

    Le réseau se déduit de l'octet : le bloc décalé est le secours, par construction."""
    par_id = {r["id"]: r for r in rules}
    idx, familles = {}, {}
    for e in equipments:
        r = par_id.get(e["rule_id"])
        if not r:
            continue
        paires = []
        if e["network"] in ("rouge", "les-deux"):
            paires.append((e["family"], "rouge"))
        if e["network"] in ("bleu", "les-deux"):
            paires.append((e["family"] + r["offset"], "bleu"))
        for octet, reseau in paires:
            idx[(octet, e["number"])] = (e, r, reseau)
            familles.setdefault(octet, (e["family"], r, reseau))
    return idx, familles


def _reconnaitre(group, idx, familles):
    """Ce que la convention dit d'une adresse : équipement, essence, port attendu.

    Trois issues distinctes, et c'est voulu : adresse d'un équipement connu, adresse d'une
    famille connue sur un équipement qu'on n'a pas recensé (du matériel présent, pas une
    faute), ou adresse étrangère au plan."""
    try:
        o = [int(x) for x in str(group).split(".")]
    except ValueError:
        return None
    if len(o) != 4 or o[0] != 239:
        return None
    trouve = idx.get((o[1], o[2]))
    if not trouve:
        fam = familles.get(o[1])
        if not fam:
            return None
        return {"equip": None, "rule": fam[1], "network": fam[2], "family": fam[0],
                "number": o[2], "essence": None, "port": None, "known": False}
    equip, regle, reseau = trouve
    choisies = equip.get("essences") or [e["key"] for e in regle["essences"]]
    canaux = equip.get("channels") or 1
    for ess in regle["essences"]:
        if ess["first"] <= o[3] <= ess["last"]:
            # Trois cas à ne pas confondre : l'essence n'est pas portée par cet équipement,
            # elle l'est mais au-delà des canaux déclarés, ou tout est en ordre. Seul le
            # premier relève de la convention ; les deux autres disent que le plan est en
            # retard sur le matériel, ce qui se corrige dans le plan, pas dans l'équipement.
            return {"equip": equip, "rule": regle, "network": reseau,
                    "family": equip["family"], "number": equip["number"],
                    "essence": ess, "port": ess["port"], "known": True,
                    "essence_declaree": ess["key"] in choisies,
                    "canal": o[3] - ess["first"] + 1,
                    "canal_declare": (o[3] - ess["first"] + 1) <= canaux,
                    "canaux_declares": canaux}
    return {"equip": equip, "rule": regle, "network": reseau, "family": equip["family"],
            "number": equip["number"], "essence": None, "port": None, "known": True}


# ─── Rapprochement avec le réel ───────────────────────────────────────────────

def _reconcile(active, ranges, assigns, failed=None):
    """Compare le PLAN (plages + attributions) aux groupes réellement ACTIFS (fournis par le
    front depuis monitoring_2110) et classe en 4 catégories.

    `failed` : switchs dont les groupes n'ont PAS pu être lus, recensés par le front. Avant,
    cette information n'existait pas : un leaf injoignable contribuait zéro groupe et toutes
    les attributions qu'il portait basculaient en « inactives » — présentées à l'opérateur
    comme un constat, alors qu'elles étaient peut-être bien vivantes. Le risque n'est pas
    théorique : on réattribue volontiers une adresse déclarée inactive, et deux émetteurs sur
    la même adresse cassent le flux en direct. Une collecte partielle doit donc voyager
    jusqu'au rapport, où elle disqualifie explicitement la liste des inactifs."""
    # Groupes actifs dédoublonnés par adresse de groupe (on garde vlan/source du 1er vu).
    seen, act = set(), []
    for g in active or []:
        grp = (g.get("group") or "").strip()
        if not grp or grp in seen:
            continue
        try:
            n = _v4_int(grp)
        except ValueError:
            continue
        seen.add(grp)
        act.append({"group": grp, "int": n, "vlan": g.get("vlan"),
                    "source": (g.get("source") or "").strip()})

    def find_assign(n):
        for a in assigns:
            if a["lo"] is not None and a["lo"] <= n <= a["hi"]:
                return a
        return None

    def find_range(n):
        for r in ranges:
            if r["start_int"] is not None and r["start_int"] <= n <= r["end_int"]:
                return r
        return None

    conformes, undocumented, out_of_range = [], [], []
    active_ints = {a["int"] for a in act}
    for a in act:
        asg = find_assign(a["int"])
        rg = find_range(a["int"])
        base = {"group": a["group"], "vlan": a["vlan"], "source": a["source"]}
        if asg:
            conformes.append({**base, "assignment": {
                "id": asg["id"], "label": asg["label"], "device": asg["device"],
                "address": asg["address"], "range_id": asg["range_id"]},
                "range": ({"id": rg["id"], "name": rg["name"]} if rg else None)})
        elif rg:
            undocumented.append({**base, "range": {"id": rg["id"], "name": rg["name"]}})
        else:
            out_of_range.append(base)

    # Attributions documentées mais inactives : aucune adresse couverte n'est active.
    inactive = []
    rg_by_id = {r["id"]: r for r in ranges}
    for a in assigns:
        if a["lo"] is None:
            continue
        alive = any(a["lo"] <= n <= a["hi"] for n in active_ints)
        if not alive:
            rg = rg_by_id.get(a["range_id"])
            inactive.append({"id": a["id"], "label": a["label"], "device": a["device"],
                             "address": a["address"], "range_id": a["range_id"],
                             "range_name": rg["name"] if rg else None})

    fails = [{"name": str(f.get("name") or f.get("id") or "?"),
              "reason": str(f.get("reason") or "")}
             for f in (failed or []) if isinstance(f, dict)]

    return {
        "conformes": conformes,
        "undocumented": undocumented,
        "out_of_range": out_of_range,
        "inactive": inactive,
        "summary": {
            "conformes": len(conformes), "undocumented": len(undocumented),
            "out_of_range": len(out_of_range), "inactive": len(inactive),
            "active_total": len(act), "documented_total": len(assigns),
            # `complete` gouverne l'affichage : à faux, « inactif » ne veut plus dire
            # « éteint » mais « pas vu », et la liste ne doit pas servir à libérer une adresse.
            "failed_switches": fails, "complete": not fails,
        },
    }


# ─── Export CSV ───────────────────────────────────────────────────────────────

def _export_csv(ranges, assigns):
    """Plan à plat en CSV (une ligne par attribution ; les plages vides apparaissent aussi)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["plage", "cidr_ou_bornes", "essence", "reseau", "vlan",
                "adresse", "type", "equipement", "libelle", "port_udp"])
    rg_by_id = {r["id"]: r for r in ranges}
    by_range = {}
    for a in assigns:
        by_range.setdefault(a["range_id"], []).append(a)
    for r in ranges:
        bornes = r["cidr"] or f"{r['start']}–{r['end']}"
        items = by_range.get(r["id"], [])
        if not items:
            w.writerow([r["name"], bornes, r["essence"], r["network"],
                        r["vlan"] if r["vlan"] is not None else "", "", "", "", "", ""])
        for a in items:
            w.writerow([r["name"], bornes, r["essence"], r["network"],
                        r["vlan"] if r["vlan"] is not None else "",
                        a["address"], a["kind"], a["device"], a["label"],
                        a["udp_port"] if a["udp_port"] is not None else ""])
    # Attributions orphelines (sans plage de rattachement).
    for a in by_range.get(None, []):
        w.writerow(["(hors plage)", "", "", "", "", a["address"], a["kind"],
                    a["device"], a["label"], a["udp_port"] if a["udp_port"] is not None else ""])
    return buf.getvalue()


# ─── Routage ──────────────────────────────────────────────────────────────────

def _reconcile_regle(active, rules, equipments, ranges=None, assigns=None, failed=None):
    """Confronte les groupes ACTIFS à la règle, plutôt qu'à des plages énumérées.

    Le SENS est déterminant, et c'est pourquoi le front l'achemine jusqu'ici. Un flux que NOS
    équipements émettent hors convention est un défaut à corriger — il porte notre nom. Un flux
    que nous RECEVONS d'un tiers n'a aucune raison de suivre notre plan : le ranger parmi les
    anomalies noierait les vrais défauts sous du bruit qui n'appelle aucune action.

    Cinq catégories, dont l'une dit explicitement ce qu'on n'a pas pu voir."""
    idx, familles = _plan_index(rules, equipments)
    ranges, assigns = ranges or [], assigns or []

    def a_la_main(n):
        """Ce que le plan MANUEL dit d'une adresse : son attribution, sinon sa plage.

        Une adresse peut être parfaitement documentée sans suivre la convention — un flux
        tiers qu'on a choisi de consigner, un bloc hérité. La ranger « hors plan » pour cette
        seule raison reviendrait à punir l'exploitant d'avoir pris la peine de l'écrire."""
        for a in assigns:
            if a.get("lo") is not None and a["lo"] <= n <= a["hi"]:
                return a, next((r for r in ranges if r["id"] == a.get("range_id")), None)
        for r in ranges:
            if r.get("start_int") is not None and r["start_int"] <= n <= r["end_int"]:
                return None, r
        return None, None
    vus, act = set(), []
    for g in active or []:
        grp = (g.get("group") or "").strip()
        if not grp or grp in vus:
            continue
        try:
            _v4_int(grp)
        except ValueError:
            continue
        vus.add(grp)
        act.append(g)

    conformes, a_corriger, exterieurs, hors_plan, inconnus = [], [], [], [], []
    non_declares, manuels = [], []
    for g in act:
        grp = (g.get("group") or "").strip()
        sens = (g.get("sens") or "").strip()
        port = g.get("port")
        port = int(port) if str(port or "").isdigit() else None
        base = {"group": grp, "vlan": g.get("vlan"), "source": (g.get("source") or "").strip(),
                "signal": g.get("signal") or "", "sens": sens, "kind": g.get("kind") or "",
                "port": port}
        # Une plage RÉSERVÉE prime sur la convention : c'est une décision humaine explicite,
        # postérieure à la règle, et qui dit précisément « ici, je ne sais pas ce qui viendra ».
        # La laisser derrière le verdict de la règle ferait ressortir chaque adresse du vivier
        # comme un équipement non recensé — le contraire de ce que la réservation exprime.
        vivier = next((r for r in ranges if r.get("pool") and r.get("start_int") is not None
                       and r["start_int"] <= _v4_int(grp) <= r["end_int"]), None)
        if vivier:
            asg, _ = a_la_main(_v4_int(grp))
            manuels.append({**base, "label": (asg or {}).get("label") or "",
                            "device": (asg or {}).get("device") or "",
                            "range": vivier.get("name") or "", "pool": True})
            continue
        vu = _reconnaitre(grp, idx, familles)
        if not vu:
            # La convention ne connaît pas cette adresse : le plan manuel, peut-être.
            asg, rg = a_la_main(_v4_int(grp))
            if asg:
                manuels.append({**base, "label": asg.get("label") or "",
                                "device": asg.get("device") or "",
                                "range": (rg or {}).get("name") or ""})
            elif rg:
                non_declares.append({**base, "motif": "dans la plage « %s », sans attribution"
                                                      % (rg.get("name") or "")})
            else:
                (exterieurs if sens == "recu" else hors_plan).append(base)
            continue
        info = {**base, "family": vu["family"], "equipment": vu["number"],
                "network": vu["network"],
                "equipment_name": (vu["equip"] or {}).get("name", ""),
                "essence": (vu["essence"] or {}).get("key"),
                "essence_label": (vu["essence"] or {}).get("label"),
                "port_attendu": vu["port"]}
        if not vu["known"]:
            # L'adresse relève d'une famille connue, mais d'un équipement qu'on n'a jamais
            # déclaré : c'est du matériel présent et non recensé, pas une violation.
            inconnus.append(info)
            continue
        if vu["essence"] is None:
            (exterieurs if sens == "recu" else hors_plan).append(info)
            continue
        info["canal"] = vu.get("canal")
        info["canaux_declares"] = vu.get("canaux_declares")
        if not vu.get("essence_declaree", True):
            (exterieurs if sens == "recu" else non_declares).append(
                {**info, "motif": "essence non déclarée sur cet équipement"})
            continue
        if not vu.get("canal_declare", True):
            (exterieurs if sens == "recu" else non_declares).append(
                {**info, "motif": "canal %d, au-delà des %d déclarés"
                                  % (vu.get("canal") or 0, vu.get("canaux_declares") or 0)})
            continue
        if vu["port"] is not None and port is not None and port != vu["port"]:
            (exterieurs if sens == "recu" else a_corriger).append(info)
            continue
        conformes.append(info)

    return {"conformes": conformes, "a_corriger": a_corriger, "exterieurs": exterieurs,
            "hors_plan": hors_plan, "equipements_inconnus": inconnus,
            "non_declares": non_declares, "manuels": manuels,
            "failed": list(failed or []),
            "resume": {"actifs": len(act), "conformes": len(conformes),
                       "a_corriger": len(a_corriger), "exterieurs": len(exterieurs),
                       "hors_plan": len(hors_plan), "inconnus": len(inconnus),
                       "non_declares": len(non_declares), "manuels": len(manuels),
                       "switchs_non_lus": len(failed or [])}}


def api(path, method, payload, ctx):
    parts = [p for p in (path or "").split("/") if p]
    payload = payload or {}

    # ── Règles d'adressage ──
    if parts == ["rules"]:
        if method == "GET":
            _ensure_default_rule(ctx)
            return {"rules": _list_rules(ctx)}
        if method == "POST":
            name = (payload.get("name") or "").strip()
            if not name:
                return 400, {"error": "nom de règle requis"}
            try:
                value = _rule_value(payload)
            except ValueError as e:
                return 400, {"error": str(e)}
            rid = ctx.store.create(name, value, scope=RULE_SCOPE)
            ctx.audit("rule_create", name)
            return {"id": rid}
        return 405, {"error": "méthode non permise"}

    if len(parts) == 2 and parts[0] == "rules":
        try:
            rid = int(parts[1])
        except ValueError:
            return 400, {"error": "identifiant invalide"}
        row = ctx.store.get(rid)
        if not row or row.get("scope") != RULE_SCOPE:
            return 404, {"error": "règle introuvable"}
        if method == "PUT":
            name = (payload.get("name") or row.get("name") or "").strip()
            if not name:
                return 400, {"error": "nom de règle requis"}
            try:
                value = _rule_value(payload, base=row.get("value") or {})
            except ValueError as e:
                return 400, {"error": str(e)}
            ctx.store.update(rid, name=name, value=value)
            ctx.audit("rule_update", name)
            return {"ok": True}
        if method == "DELETE":
            # Une famille orpheline ne saurait plus calculer ses adresses : on refuse plutôt
            # que de laisser un plan à moitié interprétable.
            liees = [e for e in _list_equipments(ctx) if e["rule_id"] == rid]
            if liees:
                return 409, {"error": "%d équipement(s) utilisent cette règle" % len(liees),
                             "equipments": [e["name"] for e in liees]}
            ctx.store.delete(rid)
            ctx.audit("rule_delete", row.get("name") or str(rid))
            return {"ok": True}
        return 405, {"error": "méthode non permise"}

    # ── Équipements ──
    if parts == ["equipments"]:
        if method == "GET":
            _ensure_default_rule(ctx)
            rules = _list_rules(ctx)
            par_id = {r["id"]: r for r in rules}
            eqs = _list_equipments(ctx)
            for e in eqs:
                r = par_id.get(e["rule_id"])
                e["blocks"] = _blocks(r, e) if r else []
                e["rule_name"] = r["name"] if r else ""
            # Les étiquettes voyagent avec les équipements : le volet les affiche ensemble,
            # et un second aller-retour pour quelques noms serait du gaspillage.
            return {"equipments": eqs, "rules": rules,
                    "family_labels": _list_famlabels(ctx)}
        if method == "POST":
            name = (payload.get("name") or "").strip()
            if not name:
                return 400, {"error": "nom d'équipement requis"}
            try:
                value = _equip_value(payload, _list_rules(ctx))
            except ValueError as e:
                return 400, {"error": str(e)}
            for e in _list_equipments(ctx):
                if (e["family"], e["number"]) == (value["family"], value["number"]):
                    return 409, {"error": "239.%d.%d.x est déjà pris par « %s »"
                                          % (value["family"], value["number"], e["name"])}
            eid = ctx.store.create(name, value, scope=EQUIP_SCOPE)
            ctx.audit("equipment_create", "%s (239.%d.%d.x)"
                      % (name, value["family"], value["number"]))
            return {"id": eid}
        return 405, {"error": "méthode non permise"}

    if len(parts) == 2 and parts[0] == "equipments":
        try:
            eid = int(parts[1])
        except ValueError:
            return 400, {"error": "identifiant invalide"}
        row = ctx.store.get(eid)
        if not row or row.get("scope") != EQUIP_SCOPE:
            return 404, {"error": "équipement introuvable"}
        if method == "PUT":
            name = (payload.get("name") or row.get("name") or "").strip()
            if not name:
                return 400, {"error": "nom d'équipement requis"}
            try:
                value = _equip_value(payload, _list_rules(ctx), base=row.get("value") or {})
            except ValueError as e:
                return 400, {"error": str(e)}
            for e in _list_equipments(ctx):
                if e["id"] != eid and (e["family"], e["number"]) == (value["family"], value["number"]):
                    return 409, {"error": "239.%d.%d.x est déjà pris par « %s »"
                                          % (value["family"], value["number"], e["name"])}
            ctx.store.update(eid, name=name, value=value)
            ctx.audit("equipment_update", name)
            return {"ok": True}
        if method == "DELETE":
            ctx.store.delete(eid)
            ctx.audit("equipment_delete", row.get("name") or str(eid))
            return {"ok": True}
        return 405, {"error": "méthode non permise"}

    if parts == ["reconcile-rule"] and method == "POST":
        _ensure_default_rule(ctx)
        return _reconcile_regle(payload.get("active") or [], _list_rules(ctx),
                                _list_equipments(ctx), _list_ranges(ctx), _list_assigns(ctx),
                                payload.get("failed") or [])

    # ── Étiquettes de famille ──
    if parts == ["family-labels"]:
        if method == "GET":
            return {"family_labels": _list_famlabels(ctx)}
        if method == "POST":
            name = (payload.get("name") or "").strip()
            if not name:
                return 400, {"error": "nom de famille requis"}
            try:
                value = _famlabel_value(payload)
            except ValueError as e:
                return 400, {"error": str(e)}
            for f in _list_famlabels(ctx):
                if f["family"] == value["family"]:
                    return 409, {"error": "la famille %d est déjà nommée « %s »"
                                          % (value["family"], f["name"])}
            fid = ctx.store.create(name, value, scope=FAMLABEL_SCOPE)
            ctx.audit("family_label", "%d = %s" % (value["family"], name))
            return {"id": fid}
        return 405, {"error": "méthode non permise"}

    if len(parts) == 2 and parts[0] == "family-labels":
        try:
            fid = int(parts[1])
        except ValueError:
            return 400, {"error": "identifiant invalide"}
        row = ctx.store.get(fid)
        if not row or row.get("scope") != FAMLABEL_SCOPE:
            return 404, {"error": "étiquette introuvable"}
        if method == "PUT":
            name = (payload.get("name") or row.get("name") or "").strip()
            if not name:
                return 400, {"error": "nom de famille requis"}
            try:
                value = _famlabel_value(payload, base=row.get("value") or {})
            except ValueError as e:
                return 400, {"error": str(e)}
            ctx.store.update(fid, name=name, value=value)
            ctx.audit("family_label", "%d = %s" % (value["family"], name))
            return {"ok": True}
        if method == "DELETE":
            ctx.store.delete(fid)
            ctx.audit("family_label_delete", row.get("name") or str(fid))
            return {"ok": True}
        return 405, {"error": "méthode non permise"}

    # ── Plages ──
    if parts == ["ranges"]:
        if method == "GET":
            ranges = _list_ranges(ctx)
            return {"ranges": ranges, "overlaps": _overlaps(ranges)}
        if method == "POST":
            name = (payload.get("name") or "").strip()
            if not name:
                return 400, {"error": "nom de plage requis"}
            try:
                mode, cidr, lo, hi = _parse_range_bounds(payload)
            except ValueError as e:
                return 400, {"error": str(e)}
            essence = (payload.get("essence") or "autre").strip()
            if essence not in ESSENCES:
                return 400, {"error": "essence inconnue"}
            network = (payload.get("network") or "les-deux").strip()
            if network not in NETWORKS:
                return 400, {"error": "réseau inconnu"}
            vlan = payload.get("vlan")
            vlan = int(vlan) if vlan not in (None, "", "null") else None
            value = {"mode": mode, "cidr": cidr, "start": _dotted(lo), "end": _dotted(hi),
                     "essence": essence, "network": network, "vlan": vlan,
                     # Une plage RÉSERVÉE est un vivier : une machine y pioche ses adresses
                     # sans qu'on sache lesquelles à l'avance. Elle est donc vide par
                     # construction, et le rester n'est pas un oubli. Sans ce drapeau, l'outil
                     # réclamait une attribution pour chaque adresse qui s'y allumait, et sa
                     # jauge affichait 0 % à vie.
                     "pool": bool(payload.get("pool")),
                     "notes": (payload.get("notes") or "").strip()}
            sid = ctx.store.create(name, value, scope=RANGE_SCOPE)
            ctx.audit("range_create", f"{name} {cidr or _dotted(lo) + '–' + _dotted(hi)}")
            # Chevauchements après ajout (avertissement renvoyé au front).
            ranges = _list_ranges(ctx)
            warn = [o for o in _overlaps(ranges) if sid in (o["a"], o["b"])]
            return {"id": sid, "overlaps": warn, "convention": _telescopage(ctx, lo, hi)}
        return 405, {"error": "méthode non permise"}

    if len(parts) == 2 and parts[0] == "ranges":
        try:
            sid = int(parts[1])
        except ValueError:
            return 404, {"error": "introuvable"}
        row = ctx.store.get(sid)
        if not row or row.get("scope") != RANGE_SCOPE:
            return 404, {"error": "plage introuvable"}
        if method == "PUT":
            name = (payload.get("name") or row.get("name")).strip()
            try:
                mode, cidr, lo, hi = _parse_range_bounds(payload)
            except ValueError as e:
                return 400, {"error": str(e)}
            essence = (payload.get("essence") or "autre").strip()
            if essence not in ESSENCES:
                return 400, {"error": "essence inconnue"}
            network = (payload.get("network") or "les-deux").strip()
            if network not in NETWORKS:
                return 400, {"error": "réseau inconnu"}
            vlan = payload.get("vlan")
            vlan = int(vlan) if vlan not in (None, "", "null") else None
            value = {"mode": mode, "cidr": cidr, "start": _dotted(lo), "end": _dotted(hi),
                     "essence": essence, "network": network, "vlan": vlan,
                     # Une plage RÉSERVÉE est un vivier : une machine y pioche ses adresses
                     # sans qu'on sache lesquelles à l'avance. Elle est donc vide par
                     # construction, et le rester n'est pas un oubli. Sans ce drapeau, l'outil
                     # réclamait une attribution pour chaque adresse qui s'y allumait, et sa
                     # jauge affichait 0 % à vie.
                     "pool": bool(payload.get("pool")),
                     "notes": (payload.get("notes") or "").strip()}
            ctx.store.update(sid, name=name, value=value)
            ctx.audit("range_update", name)
            ranges = _list_ranges(ctx)
            warn = [o for o in _overlaps(ranges) if sid in (o["a"], o["b"])]
            return {"ok": True, "overlaps": warn}
        if method == "DELETE":
            # Cascade : retire aussi les attributions rattachées à cette plage.
            n = 0
            for a in ctx.store.list(ASSIGN_SCOPE):
                if (a.get("value") or {}).get("range_id") == sid:
                    ctx.store.delete(a["id"])
                    n += 1
            ctx.store.delete(sid)
            ctx.audit("range_delete", f"{row.get('name')} (+{n} attributions)")
            return {"ok": True, "removed_assignments": n}
        return 405, {"error": "méthode non permise"}

    # ── Attributions ──
    if parts == ["assigns"]:
        if method == "GET":
            assigns = _list_assigns(ctx)
            rid = payload.get("range_id")
            if rid not in (None, "", "null"):
                try:
                    rid = int(rid)
                    assigns = [a for a in assigns if a["range_id"] == rid]
                except ValueError:
                    pass
            return {"assigns": assigns}
        if method == "POST":
            ranges = _list_ranges(ctx)
            try:
                value, name = _validate_assign(payload, ctx, ranges)
            except ValueError as e:
                return 400, {"error": str(e)}
            # Refus des doublons stricts d'adresse simple dans la même plage.
            if value["kind"] == "single":
                for a in _list_assigns(ctx):
                    if a["address"] == value["address"] and a["range_id"] == value["range_id"]:
                        return 409, {"error": f"adresse déjà attribuée : {value['address']}"}
            sid = ctx.store.create(name, value, scope=ASSIGN_SCOPE)
            ctx.audit("assign_create", f"{name} {value['address']}")
            ligne = next((a for a in _list_assigns(ctx) if a["id"] == sid), None)
            conv = _telescopage(ctx, ligne["lo"], ligne["hi"]) if ligne and ligne.get("lo") is not None else []
            return {"id": sid, "convention": conv}
        return 405, {"error": "méthode non permise"}

    if len(parts) == 2 and parts[0] == "assigns":
        try:
            sid = int(parts[1])
        except ValueError:
            return 404, {"error": "introuvable"}
        row = ctx.store.get(sid)
        if not row or row.get("scope") != ASSIGN_SCOPE:
            return 404, {"error": "attribution introuvable"}
        if method == "PUT":
            ranges = _list_ranges(ctx)
            try:
                value, name = _validate_assign(payload, ctx, ranges, self_id=sid)
            except ValueError as e:
                return 400, {"error": str(e)}
            if value["kind"] == "single":
                for a in _list_assigns(ctx):
                    if a["id"] != sid and a["address"] == value["address"] \
                            and a["range_id"] == value["range_id"]:
                        return 409, {"error": f"adresse déjà attribuée : {value['address']}"}
            ctx.store.update(sid, name=name, value=value)
            ctx.audit("assign_update", f"{name} {value['address']}")
            return {"ok": True}
        if method == "DELETE":
            ctx.store.delete(sid)
            ctx.audit("assign_delete", row.get("name") or str(sid))
            return {"ok": True}
        return 405, {"error": "méthode non permise"}

    # ── Prochaine adresse libre dans une plage ──
    if parts == ["next-free"] and method == "GET":
        rid = payload.get("range_id")
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            return 400, {"error": "range_id requis"}
        rg = next((r for r in _list_ranges(ctx) if r["id"] == rid), None)
        if not rg or rg["start_int"] is None:
            return 404, {"error": "plage introuvable"}
        lo, hi = rg["start_int"], rg["end_int"]
        assigns = [a for a in _list_assigns(ctx) if a["lo"] is not None]
        used = _used_ints(assigns, lo, hi)
        limit = min(hi, lo + _SCAN_CAP)
        for n in range(lo, limit + 1):
            if n not in used:
                return {"address": _dotted(n), "range_id": rid}
        if hi > limit:
            return 404, {"error": "plage trop grande pour trouver une adresse libre"}
        return 404, {"error": "plage pleine : aucune adresse libre"}

    # ── Recherche « où est 239.1.2.3 ? » ──
    if parts == ["find"] and method == "GET":
        q = (payload.get("address") or "").strip()
        try:
            n = _v4_int(q)
        except ValueError:
            return 400, {"error": "adresse IPv4 invalide"}
        rg = next((r for r in _list_ranges(ctx)
                   if r["start_int"] is not None and r["start_int"] <= n <= r["end_int"]), None)
        asg = next((a for a in _list_assigns(ctx)
                    if a["lo"] is not None and a["lo"] <= n <= a["hi"]), None)
        return {"address": _dotted(n),
                "is_multicast": _is_mcast_int(n),
                "range": rg, "assignment": asg}

    # ── Rapprochement avec le fabric ──
    if parts == ["reconcile"] and method == "POST":
        active = payload.get("active")
        if not isinstance(active, list):
            return 400, {"error": "liste 'active' requise"}
        res = _reconcile(active, _list_ranges(ctx), _list_assigns(ctx),
                         payload.get("failed"))
        nko = len(res["summary"]["failed_switches"])
        ctx.audit("reconcile", f"{res['summary']['active_total']} groupes actifs"
                               + (f" — collecte INCOMPLÈTE : {nko} switch(s) non lus" if nko else ""))
        return res

    # ── Export (données brutes ; le front fabrique le téléchargement) ──
    if parts == ["export"] and method == "GET":
        ranges = _list_ranges(ctx)
        assigns = _list_assigns(ctx)
        return {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "ranges": ranges, "assigns": assigns,
            "csv": _export_csv(ranges, assigns),
        }

    return 404, {"error": "route inconnue"}
