// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Auteur : Cyril Mazouer, pour le compte de BOBI SAS
// Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.
//
// Plan multicast — UI de l'outil Bobi.Tools (runtime=inprocess + backend.py). Tout le métier
// (validation multicast, chevauchements, prochaine adresse libre, rapprochement, export) est
// fait par le backend via ctx.api(...). Le rapprochement collecte les groupes RÉELLEMENT
// actifs auprès de Monitoring 2110 (mêmes session/permissions) puis les envoie au backend qui
// fait le diff — et dégrade proprement si Monitoring 2110 est arrêté ou inaccessible.
window.BTTools = window.BTTools || {};
window.BTTools.mcast_ipam = (function () {
    let EL = null, CTX = null;
    let ranges = [], overlaps = [], assignsByRange = {}, selected = null;
    // Le plan repose d'abord sur une CONVENTION : on stocke la règle et les familles, et les
    // adresses se calculent. Les plages manuelles ne servent plus qu'à ce qui y échappe.
    let rules = [], equipments = [], selEquip = null;

    const esc = (window.BT && BT.esc) || ((s) => String(s == null ? "" : s)
        .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])));
    const $ = (sel) => EL.querySelector(sel);
    const $$ = (sel) => Array.from(EL.querySelectorAll(sel));

    const ESSENCE_LABEL = {
        "video": "Vidéo", "audio-2110-30": "Audio 2110-30",
        "anc-2110-40": "ANC 2110-40", "autre": "Autre",
    };
    const NET_LABEL = { "rouge": "Rouge", "bleu": "Bleu", "les-deux": "Rouge + Bleu" };

    // ── Règle et familles ────────────────────────────────────
    async function loadPlan() {
        const r = await CTX.api("equipments");
        equipments = r.equipments || [];
        rules = r.rules || [];
    }

    function renderFamilies() {
        $("#mi-fam-count").textContent = equipments.length ? "(" + equipments.length + ")" : "";
        const ul = $("#mi-families");
        if (!equipments.length) {
            ul.innerHTML = '<li class="meta">Aucun équipement. Déclarez-en un : ses adresses s\'en déduiront.</li>';
            return;
        }
        // La famille n'est pas un objet qu'on crée, c'est un attribut : elle sert ici de
        // simple intertitre, reconstruit à chaque rendu depuis les équipements eux-mêmes.
        const parFamille = new Map();
        equipments.forEach((e) => {
            if (!parFamille.has(e.family)) parFamille.set(e.family, []);
            parFamille.get(e.family).push(e);
        });
        const familles = Array.from(parFamille.keys()).sort((a, b) => a - b);
        ul.innerHTML = familles.map((fam) => {
            const liste = parFamille.get(fam);
            return '<li class="mi-fam-head meta">Famille ' + fam +
                ' <span class="mono">239.' + fam + '.x</span></li>' +
                liste.map((e) => {
                    const r = ruleOf(e);
                    const bleu = (e.network === "les-deux" || e.network === "bleu");
                    const sec = bleu ? " + 239." + (e.family + (r.offset || 0)) + "." + e.number + ".x" : "";
                    return '<li class="mi-range' + (e.id === selEquip ? " sel" : "") + '" data-id="' + e.id + '">' +
                        '<div class="mi-range-top"><span class="mi-range-name">' + esc(e.name) + '</span>' +
                        '<span class="mi-chip e-video">n° ' + e.number + '</span></div>' +
                        '<div class="mi-range-cidr mono">239.' + e.family + "." + e.number + ".x" + esc(sec) + '</div>' +
                        (e.unicast ? '<div class="mi-fam-sub meta mono">' + esc(e.unicast) + "</div>" : "") +
                        '</li>';
                }).join("");
        }).join("");
        ul.querySelectorAll(".mi-range").forEach((li) => li.onclick = () => selectFamily(Number(li.dataset.id)));
    }

    function ruleOf(e) { return rules.find((r) => r.id === e.rule_id) || { essences: [], offset: 0 }; }

    function selectFamily(id) {
        selEquip = id; selected = null;
        renderFamilies(); renderRanges(); renderMain();
    }

    // ── Chargement des données ───────────────────────────────
    async function loadRanges() {
        const r = await CTX.api("ranges");
        ranges = r.ranges || [];
        overlaps = r.overlaps || [];
        renderRanges();
        if (selected != null && !ranges.some((x) => x.id === selected)) selected = null;
        renderMain();
    }
    async function loadAssigns(rangeId) {
        const r = await CTX.api("assigns?range_id=" + encodeURIComponent(rangeId));
        assignsByRange[rangeId] = r.assigns || [];
        return assignsByRange[rangeId];
    }

    // ── Liste des plages (volet gauche) + jauge ──────────────
    function overlapPeers(id) {
        const peers = [];
        overlaps.forEach((o) => {
            if (o.a === id) peers.push(o.b_name);
            if (o.b === id) peers.push(o.a_name);
        });
        return peers;
    }
    function gaugeHTML(used, total) {
        const pct = total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0;
        const cls = pct >= 90 ? "hot" : pct >= 70 ? "warm" : "";
        return '<div class="mi-gauge" title="' + used + ' / ' + total + ' adresses">' +
            '<div class="mi-gauge-fill ' + cls + '" style="width:' + pct + '%"></div>' +
            '<span class="mi-gauge-txt">' + used + '/' + total + '</span></div>';
    }
    function renderRanges() {
        $("#mi-count").textContent = ranges.length ? "(" + ranges.length + ")" : "";
        const ul = $("#mi-ranges");
        if (!ranges.length) { ul.innerHTML = '<li class="meta">Aucune plage. Créez-en une.</li>'; return; }
        ul.innerHTML = ranges.map((r) => {
            const peers = overlapPeers(r.id);
            const used = (assignsByRange[r.id] || []).reduce((n, a) => n + (a.size || 0), 0);
            const bounds = r.cidr || (r.start + "–" + r.end);
            return '<li class="mi-range' + (r.id === selected ? " sel" : "") + '" data-id="' + r.id + '">' +
                '<div class="mi-range-top"><span class="mi-range-name">' + esc(r.name) + '</span>' +
                '<span class="mi-chip e-' + esc(r.essence) + '">' + esc(ESSENCE_LABEL[r.essence] || r.essence) + '</span></div>' +
                '<div class="mi-range-cidr mono">' + esc(bounds) + '</div>' +
                gaugeHTML(used, r.total) +
                (peers.length ? '<div class="mi-warn" title="Chevauchement">⚠ chevauche : ' + esc(peers.join(", ")) + '</div>' : "") +
                '</li>';
        }).join("");
        ul.querySelectorAll(".mi-range").forEach((li) => li.onclick = () => selectRange(Number(li.dataset.id)));
    }

    async function selectRange(id) {
        selected = id;
        try { await loadAssigns(id); } catch (e) { CTX.toast(e.message, "error"); }
        renderRanges();
        renderMain();
    }

    // ── Panneau principal : détail d'une plage + attributions ─
    function renderMain() {
        const main = $("#mi-main");
        const e = equipments.find((x) => x.id === selEquip);
        if (e) return renderEquipBlocks(main, e);
        const r = ranges.find((x) => x.id === selected);
        if (!r) {
            main.innerHTML = '<div class="meta">Sélectionnez un équipement à gauche, ou déclarez-en un.</div>';
            return;
        }
        const assigns = assignsByRange[r.id] || [];
        const used = assigns.reduce((n, a) => n + (a.size || 0), 0);
        const bounds = r.cidr || (r.start + "–" + r.end);
        const rows = assigns.length ? assigns.map((a) =>
            '<tr data-id="' + a.id + '">' +
            '<td class="mono">' + esc(a.address) + '</td>' +
            '<td>' + esc(a.device || "—") + '</td>' +
            '<td>' + esc(a.label || "—") + '</td>' +
            '<td class="mono">' + (a.udp_port != null ? esc(a.udp_port) : "—") + '</td>' +
            '<td class="mi-actcell">' +
            '<button class="btn mi-mini mi-a-edit" data-id="' + a.id + '">Éditer</button> ' +
            '<button class="btn btn-red mi-mini mi-a-del" data-id="' + a.id + '">✕</button></td></tr>'
        ).join("") : '<tr><td colspan="5" class="meta">Aucune attribution.</td></tr>';

        main.innerHTML =
            '<div class="mi-detail-head">' +
            '<div><h2>' + esc(r.name) + '</h2>' +
            '<div class="mi-detail-meta mono">' + esc(bounds) + '</div></div>' +
            '<div class="mi-detail-actions">' +
            '<button class="btn mi-r-edit" type="button">Modifier la plage</button> ' +
            '<button class="btn btn-red mi-r-del" type="button">Supprimer</button></div></div>' +
            '<div class="mi-detail-badges">' +
            '<span class="mi-chip e-' + esc(r.essence) + '">' + esc(ESSENCE_LABEL[r.essence] || r.essence) + '</span>' +
            '<span class="mi-chip net-' + esc(r.network) + '">' + esc(NET_LABEL[r.network] || r.network) + '</span>' +
            (r.vlan != null ? '<span class="mi-chip">VLAN ' + esc(r.vlan) + '</span>' : "") +
            '</div>' +
            (r.notes ? '<div class="mi-notes">' + esc(r.notes) + '</div>' : "") +
            '<div class="mi-occ">' + gaugeHTML(used, r.total) + '</div>' +
            '<div class="mi-assign-head"><strong>Attributions</strong>' +
            '<button class="btn btn-green mi-a-new" type="button">+ Attribution</button></div>' +
            '<table class="mi-table"><thead><tr><th>Adresse</th><th>Équipement</th><th>Libellé</th>' +
            '<th>UDP</th><th></th></tr></thead><tbody>' + rows + '</tbody></table>';

        main.querySelector(".mi-r-edit").onclick = () => openRangeModal(r);
        main.querySelector(".mi-r-del").onclick = () => delRange(r);
        main.querySelector(".mi-a-new").onclick = () => openAssignModal(null, r.id);
        main.querySelectorAll(".mi-a-edit").forEach((b) => b.onclick = () =>
            openAssignModal(assigns.find((a) => a.id === Number(b.dataset.id)), r.id));
        main.querySelectorAll(".mi-a-del").forEach((b) => b.onclick = () =>
            delAssign(Number(b.dataset.id), r.id));
    }

    // ── Modale plage ─────────────────────────────────────────
    // ── Adresses calculées d'un équipement ───────────────────
    // Aucune n'est stockée : la règle les produit. Les écrire en base créerait une seconde
    // vérité, fausse dès qu'on renumérote l'équipement.
    function renderEquipBlocks(main, e) {
        const rule = ruleOf(e);
        const nets = e.network === "les-deux" ? ["rouge", "bleu"] : [e.network];
        const par = {};
        (e.blocks || []).forEach((b) => { par[b.essence + "|" + b.network] = b; });
        const rows = (rule.essences || []).map((ess) => {
            const cells = nets.map((net) => {
                const b = par[ess.key + "|" + net];
                if (!b) return "<td></td>";
                return '<td><div class="mi-cell mi-net-' + net + '"><span class="mi-net-dot"></span>' +
                    '<span class="mono">' + esc(b.start) + " – " + esc(b.end) + "</span></div></td>";
            }).join("");
            return "<tr><th>" + esc(ess.label) +
                '<span class="mi-th-sub">' + ess.first + "–" + ess.last + "</span></th>" +
                cells + '<td class="mono">' + (ess.port ? ":" + ess.port : "—") + "</td></tr>";
        }).join("");
        main.innerHTML =
            '<div class="mi-main-head"><div><h3>' + esc(e.name) + "</h3>" +
            '<div class="meta">famille ' + e.family + ", équipement " + e.number +
            " · règle « " + esc(e.rule_name || "") + " » · " + esc(NET_LABEL[e.network] || e.network) +
            (e.unicast ? ' · unicast <span class="mono">' + esc(e.unicast) + "</span>" : "") +
            "</div></div>" +
            '<div class="mi-main-actions">' +
            '<button class="btn" id="mi-fam-edit" type="button">Modifier</button>' +
            '<button class="btn btn-danger" id="mi-fam-del" type="button">Supprimer</button></div></div>' +
            (e.notes ? '<p class="meta">' + esc(e.notes) + "</p>" : "") +
            '<div class="mi-grid-wrap"><table class="mi-grid"><thead><tr><th>Essence</th>' +
            nets.map((n) => "<th>" + (n === "rouge" ? "Rouge" : "Bleu") + "</th>").join("") +
            "<th>Port</th></tr></thead><tbody>" +
            (rows || '<tr><td class="mi-empty">La règle ne définit aucune essence.</td></tr>') +
            "</tbody></table></div>";
        $("#mi-fam-edit").onclick = () => openFamilyModal(e);
        $("#mi-fam-del").onclick = () => delFamily(e);
    }

    function openFamilyModal(e) {
        const m = $("#mi-fam-modal"), form = $("#mi-fam-form");
        $("#mi-fam-title").textContent = e ? "Modifier l'équipement" : "Nouvel équipement";
        $("#mi-fam-rule").innerHTML = rules.map((r) =>
            '<option value="' + r.id + '">' + esc(r.name) + "</option>").join("");
        form.reset();
        form.dataset.id = e ? e.id : "";
        const rule = rules.find((r) => r.id === (e ? e.rule_id : Number(form.rule_id.value))) || rules[0];
        const prises = e && e.essences ? e.essences : (rule ? rule.essences.map((x) => x.key) : []);
        $("#mi-fam-ess").innerHTML = (rule ? rule.essences : []).map((x) =>
            '<label class="mi-ess-box"><input type="checkbox" value="' + esc(x.key) + '"' +
            (prises.indexOf(x.key) >= 0 ? " checked" : "") + "> " + esc(x.label) +
            '<span class="mi-hint"> ' + x.first + "–" + x.last + "</span></label>").join("");
        EL.querySelectorAll("#mi-fam-ess input").forEach((c) => c.onchange = previewEquip);
        form.channels.value = e ? e.channels : 1;
        if (e) {
            form.name.value = e.name;
            form.family.value = e.family;
            form.number.value = e.number;
            form.network.value = e.network;
            form.rule_id.value = e.rule_id;
            form.unicast.value = e.unicast || "";
            form.notes.value = e.notes || "";
        }
        previewEquip();
        m.hidden = false;
    }

    // Montrer l'adresse AVANT d'enregistrer : c'est le seul moment où une erreur de numéro
    // se voit encore, et la corriger après coup veut dire reconfigurer la machine.
    function previewEquip() {
        const form = $("#mi-fam-form"), out = $("#mi-fam-preview");
        if (!form || !out) return;
        const fam = parseInt(form.family.value, 10), num = parseInt(form.number.value, 10);
        const rule = rules.find((r) => r.id === Number(form.rule_id.value)) || rules[0];
        if (!rule || isNaN(fam) || isNaN(num)) { out.textContent = ""; return; }
        const bleu = (form.network.value === "les-deux" || form.network.value === "bleu");
        const prises = Array.from(EL.querySelectorAll("#mi-fam-ess input:checked")).map((c) => c.value);
        const n = Math.max(1, parseInt(form.channels.value, 10) || 1);
        const lignes = (rule.essences || []).filter((x) => prises.indexOf(x.key) >= 0).map((x) => {
            const fin = Math.min(x.first + n - 1, x.last);
            return '<span class="mi-prev-l">' + esc(x.label) + ' <span class="mono">239.' +
                fam + "." + num + "." + x.first + "–" + fin + "</span>" +
                (x.port ? ' <span class="meta">:' + x.port + "</span>" : "") + "</span>";
        });
        const total = lignes.length * n * (bleu && form.network.value === "les-deux" ? 2 : 1);
        out.innerHTML = lignes.length
            ? lignes.join("") + '<span class="mi-prev-tot">' + total + " adresses réservées" +
              (bleu ? ", secours en 239." + (fam + rule.offset) + "." + num + ".x" : "") + "</span>"
            : "Choisissez au moins une essence.";
    }

    async function submitFamily(ev) {
        ev.preventDefault();
        const form = ev.target, id = form.dataset.id;
        const body = {
            name: form.name.value.trim(), family: form.family.value,
            number: form.number.value, network: form.network.value,
            rule_id: form.rule_id.value, unicast: form.unicast.value.trim(),
            essences: Array.from(EL.querySelectorAll("#mi-fam-ess input:checked")).map((c) => c.value),
            channels: form.channels.value, notes: form.notes.value.trim(),
        };
        try {
            const r = await CTX.api(id ? "equipments/" + id : "equipments",
                                    { method: id ? "PUT" : "POST", body: body });
            m_hide("#mi-fam-modal");
            await loadPlan();
            selEquip = id ? Number(id) : (r && r.id) || selEquip;
            renderFamilies(); renderMain();
            CTX.toast(id ? "Équipement modifié." : "Équipement créé.", "success");
        } catch (e) { CTX.toast(e.message, "error"); }
    }

    async function delFamily(e) {
        if (!confirm("Supprimer « " + e.name + " » ? Ses adresses ne seront plus reconnues.")) return;
        try {
            await CTX.api("equipments/" + e.id, { method: "DELETE" });
            selEquip = null;
            await loadPlan();
            renderFamilies(); renderMain();
            CTX.toast("Équipement supprimé.", "success");
        } catch (err) { CTX.toast(err.message, "error"); }
    }

    const m_hide = (sel) => { const el = $(sel); if (el) el.hidden = true; };

    // ── Réglage de la convention ─────────────────────────────
    function openRuleModal() {
        const rule = rules[0];
        if (!rule) { CTX.toast("Aucune règle à modifier.", "error"); return; }
        const form = $("#mi-rule-form");
        form.dataset.id = rule.id;
        form.name.value = rule.name;
        form.offset.value = rule.offset;
        form.notes.value = rule.notes || "";
        $("#mi-rule-ess").innerHTML =
            '<table class="mi-ess"><thead><tr><th>Essence</th><th>Du</th><th>Au</th><th>Port UDP</th></tr></thead><tbody>' +
            (rule.essences || []).map((e) =>
                '<tr data-key="' + esc(e.key) + '">' +
                '<td><input class="mi-e-label" value="' + esc(e.label) + '"></td>' +
                '<td><input class="mi-e-first mono" type="number" min="0" max="255" value="' + e.first + '"></td>' +
                '<td><input class="mi-e-last mono" type="number" min="0" max="255" value="' + e.last + '"></td>' +
                '<td><input class="mi-e-port mono" type="number" min="1" max="65535" value="' +
                (e.port == null ? "" : e.port) + '"></td></tr>').join("") +
            "</tbody></table>";
        $("#mi-rule-modal").hidden = false;
    }

    async function submitRule(ev) {
        ev.preventDefault();
        const form = ev.target;
        const essences = Array.from(EL.querySelectorAll("#mi-rule-ess tbody tr")).map((tr) => ({
            key: tr.dataset.key,
            label: tr.querySelector(".mi-e-label").value.trim(),
            first: tr.querySelector(".mi-e-first").value,
            last: tr.querySelector(".mi-e-last").value,
            port: tr.querySelector(".mi-e-port").value,
        }));
        try {
            await CTX.api("rules/" + form.dataset.id, {
                method: "PUT",
                body: { name: form.name.value.trim(), offset: form.offset.value,
                        notes: form.notes.value.trim(), essences: essences },
            });
            m_hide("#mi-rule-modal");
            await loadPlan();
            renderFamilies(); renderMain();
            CTX.toast("Convention enregistrée.", "success");
        } catch (e) { CTX.toast(e.message, "error"); }
    }

    function openRangeModal(r) {
        const m = $("#mi-range-modal"), f = $("#mi-range-form");
        f.reset();
        $("#mi-range-title").textContent = r ? "Modifier la plage" : "Nouvelle plage";
        f.dataset.id = r ? r.id : "";
        if (r) {
            f.name.value = r.name || "";
            const mode = r.mode === "range" ? "range" : "cidr";
            f.querySelector('input[name="mode"][value="' + mode + '"]').checked = true;
            if (mode === "cidr") f.cidr.value = r.cidr || "";
            else { f.start.value = r.start || ""; f.end.value = r.end || ""; }
            f.essence.value = r.essence || "video";
            f.network.value = r.network || "les-deux";
            if (r.vlan != null) f.vlan.value = r.vlan;
            f.notes.value = r.notes || "";
        }
        syncRangeMode();
        m.hidden = false;
        f.name.focus();
    }
    function syncRangeMode() {
        const f = $("#mi-range-form");
        const mode = f.querySelector('input[name="mode"]:checked').value;
        f.cidr.disabled = mode !== "cidr";
        f.start.disabled = f.end.disabled = mode !== "range";
    }
    async function submitRange(ev) {
        ev.preventDefault();
        const f = $("#mi-range-form");
        const mode = f.querySelector('input[name="mode"]:checked').value;
        const body = {
            name: f.name.value.trim(),
            essence: f.essence.value, network: f.network.value,
            vlan: f.vlan.value.trim() || null,
            notes: f.notes.value.trim(),
        };
        if (mode === "cidr") body.cidr = f.cidr.value.trim();
        else { body.start = f.start.value.trim(); body.end = f.end.value.trim(); }
        const id = f.dataset.id;
        try {
            const res = id
                ? await CTX.api("ranges/" + id, { method: "PUT", body })
                : await CTX.api("ranges", { body });
            $("#mi-range-modal").hidden = true;
            CTX.toast("Plage enregistrée.", "success");
            if (res && res.overlaps && res.overlaps.length) {
                const names = res.overlaps.map((o) => o.a_name + " ↔ " + o.b_name).join(", ");
                CTX.toast("⚠ Chevauchement : " + names, "warning");
            }
            const newId = id ? Number(id) : (res && res.id);
            await loadRanges();
            if (newId) await selectRange(newId);
        } catch (e) { CTX.toast(e.message, "error"); }
    }
    async function delRange(r) {
        if (!confirm('Supprimer la plage « ' + r.name + ' » et ses attributions ?')) return;
        try {
            const res = await CTX.api("ranges/" + r.id, { method: "DELETE" });
            CTX.toast("Plage supprimée" + (res.removed_assignments ? " (+" + res.removed_assignments + " attributions)" : "") + ".", "success");
            selected = null;
            await loadRanges();
        } catch (e) { CTX.toast(e.message, "error"); }
    }

    // ── Modale attribution ───────────────────────────────────
    function openAssignModal(a, rangeId) {
        const m = $("#mi-assign-modal"), f = $("#mi-assign-form");
        f.reset();
        $("#mi-assign-title").textContent = a ? "Modifier l'attribution" : "Nouvelle attribution";
        f.dataset.id = a ? a.id : "";
        f.range_id.value = rangeId != null ? rangeId : "";
        if (a) {
            f.address.value = a.address || "";
            f.device.value = a.device || "";
            f.label.value = a.label || "";
            if (a.udp_port != null) f.udp_port.value = a.udp_port;
        }
        m.hidden = false;
        f.address.focus();
    }
    async function nextFree() {
        const f = $("#mi-assign-form");
        const rid = f.range_id.value;
        if (!rid) { CTX.toast("Aucune plage cible.", "warning"); return; }
        try {
            const r = await CTX.api("next-free?range_id=" + encodeURIComponent(rid));
            f.address.value = r.address;
        } catch (e) { CTX.toast(e.message, "warning"); }
    }
    async function submitAssign(ev) {
        ev.preventDefault();
        const f = $("#mi-assign-form");
        const rid = f.range_id.value;
        const body = {
            range_id: rid || null,
            address: f.address.value.trim(),
            device: f.device.value.trim(),
            label: f.label.value.trim(),
            udp_port: f.udp_port.value.trim() || null,
        };
        const id = f.dataset.id;
        try {
            id ? await CTX.api("assigns/" + id, { method: "PUT", body })
               : await CTX.api("assigns", { body });
            $("#mi-assign-modal").hidden = true;
            CTX.toast("Attribution enregistrée.", "success");
            await loadAssigns(Number(rid));
            renderRanges(); renderMain();
        } catch (e) { CTX.toast(e.message, "error"); }
    }
    async function delAssign(id, rangeId) {
        if (!confirm("Supprimer cette attribution ?")) return;
        try {
            await CTX.api("assigns/" + id, { method: "DELETE" });
            await loadAssigns(rangeId);
            renderRanges(); renderMain();
        } catch (e) { CTX.toast(e.message, "error"); }
    }

    // ── Recherche « où est 239.1.2.3 ? » ─────────────────────
    let findTimer = null;
    function onFind() {
        clearTimeout(findTimer);
        const q = $("#mi-find").value.trim();
        const box = $("#mi-find-res");
        if (!q) { box.hidden = true; box.innerHTML = ""; return; }
        findTimer = setTimeout(async () => {
            try {
                const r = await CTX.api("find?address=" + encodeURIComponent(q));
                let html = "";
                if (!r.is_multicast) html += '<div class="mi-warn">Hors bloc multicast (224.0.0.0/4).</div>';
                html += '<div>Plage : ' + (r.range
                    ? '<a href="#" data-goto="' + r.range.id + '">' + esc(r.range.name) + '</a>'
                    : '<em>aucune</em>') + '</div>';
                html += '<div>Attribution : ' + (r.assignment
                    ? esc(r.assignment.label || r.assignment.device || r.assignment.address)
                    : '<em>libre / non documentée</em>') + '</div>';
                box.innerHTML = html;
                box.hidden = false;
                const link = box.querySelector("[data-goto]");
                if (link) link.onclick = (e) => { e.preventDefault(); selectRange(Number(link.dataset.goto)); };
            } catch (e) {
                box.innerHTML = '<div class="mi-warn">' + esc(e.message) + '</div>';
                box.hidden = false;
            }
        }, 250);
    }

    // ── Rapprochement avec le fabric (via Monitoring 2110) ───
    async function collectActiveGroups() {
        // 1) liste des switchs. Une erreur ici (arrêté / accès refusé) fait échouer tout le
        //    rapprochement avec un message clair — le plan, lui, reste utilisable.
        const sw = await BT.fetchJSON("/api/tools/monitoring_2110/switches");
        const switches = (sw && sw.switches) || [];
        const byGroup = new Map();
        const failed = [];
        for (const s of switches) {
            const name = s.name || s.id;
            let res;
            try {
                res = await BT.fetchJSON("/api/tools/monitoring_2110/switches/" +
                    encodeURIComponent(s.id) + "/groups");
            } catch (e) {
                // Un 400 = switch hors périmètre (MikroTik) : exclusion légitime et définitive,
                // il n'a rien à dire du multicast. Tout autre échec est une LACUNE de collecte.
                // Avant, les deux étaient confondus dans un `continue` muet : les flux vivants
                // d'un switch non lu ressortaient en « documentés mais inactifs ».
                if (e.status !== 400) failed.push({ id: s.id, name: name, reason: e.message || ("HTTP " + e.status) });
                continue;
            }
            // Piège : un Nexus injoignable ne lève PAS d'erreur ici. monitoring_2110 rattrape
            // le DriverError et répond 200 avec une liste vide — indiscernable d'un switch
            // sans aucun groupe. Seul indice restant dans le contrat de cet endpoint : le
            // texte brut de la commande, vide lui aussi quand rien n'a été lu, alors qu'un
            // switch réellement interrogé en renvoie toujours l'en-tête, même sans groupe.
            // (`unreachable` est testé d'abord au cas où monitoring_2110 se mette à le poser.)
            if (res && res.unreachable) {
                failed.push({ id: s.id, name: name, reason: res.error || "injoignable" });
                continue;
            }
            if (!(res.groups || []).length && !((res.raw_text || "") + "").trim()) {
                failed.push({ id: s.id, name: name, reason: "aucune réponse du switch" });
                continue;
            }
            for (const g of (res.groups || [])) {
                if (!g.group || byGroup.has(g.group)) continue;
                byGroup.set(g.group, { group: g.group, vlan: g.vlan, source: g.source || "" });
            }
        }
        // Le relevé des switchs donne l'adresse et la source, jamais le SENS ni le port UDP.
        // Or c'est le sens qui sépare un défaut de configuration chez nous d'un flux tiers
        // qui n'a pas à suivre notre plan. Monitoring 2110 le sait, par les SDP des IPG :
        // on enrichit donc chaque groupe quand il répond, et on s'en passe sinon.
        try {
            const ipg = await BT.fetchJSON("/api/tools/monitoring_2110/ipg");
            for (const x of (ipg && ipg.apparies) || []) {
                const g = byGroup.get(x.group);
                if (!g) continue;
                g.sens = x.sens || "";
                g.port = x.port == null ? null : x.port;
                g.signal = x.signal || "";
                g.kind = x.kind || "";
            }
        } catch (e) { /* enrichissement facultatif : le rapprochement reste possible sans */ }
        return { groups: Array.from(byGroup.values()), failed: failed };
    }
    function reconMessage(e) {
        if (e.status === 503) return "Monitoring 2110 est arrêté. Démarrez-le pour lancer le rapprochement.";
        if (e.status === 403) return "Accès à Monitoring 2110 refusé (droits insuffisants).";
        if (e.status === 404) return "Outil Monitoring 2110 introuvable (non installé ?).";
        return "Monitoring 2110 inaccessible : " + e.message;
    }
    async function doReconcile() {
        const m = $("#mi-recon-modal"), body = $("#mi-recon-body");
        body.innerHTML = '<div class="meta">Collecte des groupes actifs…</div>';
        m.hidden = false;
        let active, failed;
        try { const c = await collectActiveGroups(); active = c.groups; failed = c.failed; }
        catch (e) {
            body.innerHTML = '<div class="mi-recon-fail">' + esc(reconMessage(e)) +
                '<br><span class="meta">Le plan reste consultable et modifiable.</span></div>';
            return;
        }
        try {
            const r = await CTX.api("reconcile-rule", { body: { active, failed } });
            renderReconRule(r);
        } catch (e) {
            body.innerHTML = '<div class="mi-recon-fail">' + esc(e.message) + '</div>';
        }
    }
    function reconSection(title, cls, items, render) {
        return '<details class="mi-recon-sec ' + cls + '"' + (items.length ? " open" : "") + '>' +
            '<summary>' + esc(title) + ' <span class="mi-recon-n">' + items.length + '</span></summary>' +
            (items.length ? '<ul>' + items.map(render).join("") + '</ul>' : '<div class="meta">—</div>') +
            '</details>';
    }
    // Cinq catégories, dont deux qui n'appellent AUCUNE action : les sources extérieures et
    // les équipements non recensés. Les mêler aux défauts ferait passer 11 lignes de bruit
    // devant les 3 qui comptent.
    function renderReconRule(r) {
        const s = r.resume || {};
        const ligne = (x) =>
            '<li><span class="mono">' + esc(x.group) + "</span>" +
            (x.signal ? ' <span class="mi-recon-lbl">' + esc(x.signal) + "</span>" : "") +
            (x.equipment_name ? ' <span class="meta">' + esc(x.equipment_name) + "</span>" : "") +
            "</li>";
        const ligneEcart = (x) =>
            '<li><span class="mono">' + esc(x.group) + "</span>" +
            (x.signal ? ' <span class="mi-recon-lbl">' + esc(x.signal) + "</span>" : "") +
            ' <span class="mi-recon-why">port ' + esc(String(x.port)) +
            ", attendu " + esc(String(x.port_attendu)) + "</span></li>";
        const ligneExt = (x) =>
            '<li><span class="mono">' + esc(x.group) + "</span>" +
            (x.signal ? ' <span class="mi-recon-lbl">' + esc(x.signal) + "</span>" : "") +
            (x.port ? ' <span class="meta">port ' + esc(String(x.port)) + "</span>" : "") + "</li>";
        const ligneEquip = (x) =>
            '<li><span class="mono">' + esc(x.group) + "</span>" +
            ' <span class="mi-recon-why">équipement ' + esc(String(x.equipment)) +
            " absent de « " + esc(x.family_name || "") + " »</span></li>";

        $("#mi-recon-body").innerHTML =
            '<div class="mi-recon-sum">' + (s.actifs || 0) + " groupes actifs · " +
            (s.conformes || 0) + " conformes à la convention</div>" +
            (r.failed && r.failed.length
                ? '<div class="mi-recon-fail">⚠ ' + r.failed.length +
                  " switch(s) non lus : la liste est incomplète.<br><span class=\"meta\">" +
                  esc(r.failed.map((f) => f.name || f.id).join(", ")) + "</span></div>"
                : "") +
            reconSection("⚠ Non conformes — à corriger", "sec-warn", r.a_corriger || [], ligneEcart) +
            reconSection("⚑ Actifs hors de toute famille", "sec-out", r.hors_plan || [], ligneExt) +
            reconSection("＋ Équipements présents non recensés", "sec-undoc", r.equipements_inconnus || [], ligneEquip) +
            reconSection("＋ Actifs au-delà du déclaré", "sec-undoc", r.non_declares || [],
                (x) => '<li><span class="mono">' + esc(x.group) + "</span>" +
                    (x.signal ? ' <span class="mi-recon-lbl">' + esc(x.signal) + "</span>" : "") +
                    ' <span class="mi-recon-why">' + esc(x.motif || "") + "</span></li>") +
            reconSection("○ Sources extérieures", "sec-inactive", r.exterieurs || [], ligneExt) +
            reconSection("✓ Conformes", "sec-ok", r.conformes || [], ligne);
    }

    function renderRecon(r) {
        const s = r.summary || {};
        const g = (x) => '<span class="mono">' + esc(x.group) + '</span>' +
            (x.source ? ' <span class="meta">(' + esc(x.source) + ')</span>' : "") +
            (x.vlan != null ? ' <span class="meta">VLAN ' + esc(x.vlan) + '</span>' : "");
        // Collecte partielle : on le dit AVANT les listes. Sans ce bandeau, la section des
        // inactifs se lisait comme un constat, alors qu'elle ne dit rien des switchs non lus.
        const fails = s.failed_switches || [];
        const complete = s.complete !== false;
        const warn = complete ? "" : '<div class="mi-recon-fail">Collecte incomplète : ' +
            esc(fails.length) + ' switch(s) non lus (' +
            fails.map((f) => esc(f.name) + (f.reason ? ' — ' + esc(f.reason) : "")).join(", ") +
            '). Les groupes qu\'ils portent sont absents du relevé : la liste des inactifs ' +
            'n\'est PAS fiable, ne libérez aucune adresse sur cette base.</div>';
        $("#mi-recon-body").innerHTML = warn +
            '<div class="mi-recon-sum">' + esc(s.active_total) + ' groupes actifs · ' +
            esc(s.documented_total) + ' attributions documentées</div>' +
            reconSection("✓ Conformes", "ok", r.conformes, (x) =>
                '<li>' + g(x) + ' → ' + esc(x.assignment.label || x.assignment.device || x.assignment.address) + '</li>') +
            reconSection("＋ Actifs non documentés (dans une plage)", "warn", r.undocumented, (x) =>
                '<li>' + g(x) + ' — plage ' + esc(x.range.name) + '</li>') +
            reconSection("⚠ Actifs hors de toute plage", "bad", r.out_of_range, (x) =>
                '<li>' + g(x) + '</li>') +
            reconSection(complete ? "○ Documentés mais inactifs"
                                  : "○ Documentés, activité INDÉTERMINÉE", "idle", r.inactive, (x) =>
                '<li><span class="mono">' + esc(x.address) + '</span> ' +
                esc(x.label || x.device || "") + (x.range_name ? ' <span class="meta">(' + esc(x.range_name) + ')</span>' : "") + '</li>');
    }

    // ── Export JSON / CSV ────────────────────────────────────
    function download(name, mime, text) {
        const blob = new Blob([text], { type: mime });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url; a.download = name;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
    async function exportPlan(fmt) {
        $("#mi-export-d").open = false;
        try {
            const r = await CTX.api("export");
            const stamp = (r.generated_at || "").replace(/[:T]/g, "-");
            if (fmt === "csv") download("plan-multicast-" + stamp + ".csv", "text/csv", r.csv || "");
            else download("plan-multicast-" + stamp + ".json", "application/json",
                JSON.stringify({ generated_at: r.generated_at, ranges: r.ranges, assigns: r.assigns }, null, 2));
        } catch (e) { CTX.toast(e.message, "error"); }
    }

    // ── Montage ──────────────────────────────────────────────
    function mount(el, ctx) {
        EL = el; CTX = ctx;
        $("#mi-range-new").onclick = () => openRangeModal(null);
        $("#mi-range-cancel").onclick = () => { $("#mi-range-modal").hidden = true; };
        $("#mi-range-form").onsubmit = submitRange;
        $$('#mi-range-form input[name="mode"]').forEach((r) => r.onchange = syncRangeMode);
        $("#mi-assign-cancel").onclick = () => { $("#mi-assign-modal").hidden = true; };
        $("#mi-assign-form").onsubmit = submitAssign;
        $("#mi-nextfree").onclick = nextFree;
        $("#mi-find").oninput = onFind;
        $("#mi-reconcile").onclick = doReconcile;
        $("#mi-recon-close").onclick = () => { $("#mi-recon-modal").hidden = true; };
        $("#mi-exp-json").onclick = () => exportPlan("json");
        $("#mi-exp-csv").onclick = () => exportPlan("csv");
        $("#mi-fam-new").onclick = () => openFamilyModal(null);
        $("#mi-fam-cancel").onclick = () => m_hide("#mi-fam-modal");
        $("#mi-fam-form").onsubmit = submitFamily;
        ["family", "number", "network", "rule_id", "channels"].forEach((n) => {
            const el = $('#mi-fam-form [name="' + n + '"]');
            if (el) el.oninput = el.onchange = previewEquip;
        });
        $("#mi-rule-edit").onclick = openRuleModal;
        $("#mi-rule-cancel").onclick = () => m_hide("#mi-rule-modal");
        $("#mi-rule-form").onsubmit = submitRule;
        // Fermeture des modales par clic sur le fond.
        $$(".mi-modal").forEach((m) => m.onclick = (e) => { if (e.target === m) m.hidden = true; });
        // La règle et les familles portent le plan : elles se chargent d'abord. Un échec sur
        // les plages manuelles ne doit pas empêcher la grille de s'afficher, ni l'inverse.
        loadPlan().then(() => { renderFamilies(); renderMain(); })
            .catch((e) => CTX.toast(e.message, "error"));
        loadRanges().catch((e) => CTX.toast(e.message, "error"));
    }
    function unmount() {
        EL = null; CTX = null; ranges = []; assignsByRange = {}; selected = null;
        rules = []; equipments = []; selEquip = null;
    }

    return { mount, unmount };
})();
