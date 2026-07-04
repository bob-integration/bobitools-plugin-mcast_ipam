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

    const esc = (window.BT && BT.esc) || ((s) => String(s == null ? "" : s)
        .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])));
    const $ = (sel) => EL.querySelector(sel);
    const $$ = (sel) => Array.from(EL.querySelectorAll(sel));

    const ESSENCE_LABEL = {
        "video": "Vidéo", "audio-2110-30": "Audio 2110-30",
        "anc-2110-40": "ANC 2110-40", "autre": "Autre",
    };
    const NET_LABEL = { "rouge": "Rouge", "bleu": "Bleu", "les-deux": "Rouge + Bleu" };

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
        const r = ranges.find((x) => x.id === selected);
        if (!r) { main.innerHTML = '<div class="meta">Sélectionnez une plage à gauche, ou créez-en une.</div>'; return; }
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
        for (const s of switches) {
            let res;
            try {
                res = await BT.fetchJSON("/api/tools/monitoring_2110/switches/" +
                    encodeURIComponent(s.id) + "/groups");
            } catch (e) {
                // switch hors périmètre (MikroTik : 400) ou injoignable : on l'ignore.
                continue;
            }
            for (const g of (res.groups || [])) {
                if (!g.group || byGroup.has(g.group)) continue;
                byGroup.set(g.group, { group: g.group, vlan: g.vlan, source: g.source || "" });
            }
        }
        return Array.from(byGroup.values());
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
        let active;
        try { active = await collectActiveGroups(); }
        catch (e) {
            body.innerHTML = '<div class="mi-recon-fail">' + esc(reconMessage(e)) +
                '<br><span class="meta">Le plan reste consultable et modifiable.</span></div>';
            return;
        }
        try {
            const r = await CTX.api("reconcile", { body: { active } });
            renderRecon(r);
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
    function renderRecon(r) {
        const s = r.summary || {};
        const g = (x) => '<span class="mono">' + esc(x.group) + '</span>' +
            (x.source ? ' <span class="meta">(' + esc(x.source) + ')</span>' : "") +
            (x.vlan != null ? ' <span class="meta">VLAN ' + esc(x.vlan) + '</span>' : "");
        $("#mi-recon-body").innerHTML =
            '<div class="mi-recon-sum">' + esc(s.active_total) + ' groupes actifs · ' +
            esc(s.documented_total) + ' attributions documentées</div>' +
            reconSection("✓ Conformes", "ok", r.conformes, (x) =>
                '<li>' + g(x) + ' → ' + esc(x.assignment.label || x.assignment.device || x.assignment.address) + '</li>') +
            reconSection("＋ Actifs non documentés (dans une plage)", "warn", r.undocumented, (x) =>
                '<li>' + g(x) + ' — plage ' + esc(x.range.name) + '</li>') +
            reconSection("⚠ Actifs hors de toute plage", "bad", r.out_of_range, (x) =>
                '<li>' + g(x) + '</li>') +
            reconSection("○ Documentés mais inactifs", "idle", r.inactive, (x) =>
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
        // Fermeture des modales par clic sur le fond.
        $$(".mi-modal").forEach((m) => m.onclick = (e) => { if (e.target === m) m.hidden = true; });
        loadRanges().catch((e) => CTX.toast(e.message, "error"));
    }
    function unmount() { EL = null; CTX = null; ranges = []; assignsByRange = {}; selected = null; }

    return { mount, unmount };
})();
