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


# ─── Rapprochement avec le réel ───────────────────────────────────────────────

def _reconcile(active, ranges, assigns):
    """Compare le PLAN (plages + attributions) aux groupes réellement ACTIFS (fournis par le
    front depuis monitoring_2110) et classe en 4 catégories."""
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

    return {
        "conformes": conformes,
        "undocumented": undocumented,
        "out_of_range": out_of_range,
        "inactive": inactive,
        "summary": {
            "conformes": len(conformes), "undocumented": len(undocumented),
            "out_of_range": len(out_of_range), "inactive": len(inactive),
            "active_total": len(act), "documented_total": len(assigns),
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

def api(path, method, payload, ctx):
    parts = [p for p in (path or "").split("/") if p]
    payload = payload or {}

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
                     "notes": (payload.get("notes") or "").strip()}
            sid = ctx.store.create(name, value, scope=RANGE_SCOPE)
            ctx.audit("range_create", f"{name} {cidr or _dotted(lo) + '–' + _dotted(hi)}")
            # Chevauchements après ajout (avertissement renvoyé au front).
            ranges = _list_ranges(ctx)
            warn = [o for o in _overlaps(ranges) if sid in (o["a"], o["b"])]
            return {"id": sid, "overlaps": warn}
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
            return {"id": sid}
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
        res = _reconcile(active, _list_ranges(ctx), _list_assigns(ctx))
        ctx.audit("reconcile", f"{res['summary']['active_total']} groupes actifs")
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
