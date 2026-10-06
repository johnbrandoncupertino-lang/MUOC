document.addEventListener("DOMContentLoaded", async () => {
    const balance = document.getElementById("dashBalance");
    const currency = document.getElementById("dashCurrency");
    const status = document.getElementById("dashStatus");
    const requests = document.getElementById("dashRequests");
    const pool = document.getElementById("dashPool");
    const logs = document.getElementById("dashLogs");
    const requestList = document.getElementById("dashRequestList");

    const formatNumber = (value) => Number(value || 0).toLocaleString("it-IT", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
    });

    const escapeHtml = (value) => String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");

    try {
        const wallet = await fetchData("/api/get/wallet?wallet_name=" + encodeURIComponent(
            document.body.dataset.walletName || window.location.pathname
        ));
        balance.textContent = formatNumber(wallet.balance);
        currency.textContent = wallet.currency;
        status.innerHTML = wallet.is_frozen
            ? '<span class="muoc-status bad">BLOCCATO</span>'
            : '<span class="muoc-status ok">ATTIVO</span>';
    } catch (e) {
        balance.textContent = "N/D";
        status.innerHTML = '<span class="muoc-status warn">NON DISPONIBILE</span>';
    }

    try {
        const [logData, requestData, poolData] = await Promise.all([
            fetchData("/api/get/wallet/logs?limit=8"),
            fetchData("/api/get/user/requests?limit=8"),
            fetchData("/api/get/currencyPool")
        ]);

        requests.textContent = requestData.filter(r => r.status === "Pending").length;
        pool.textContent = formatNumber(poolData.available_currency) + " " + poolData.currency_name;

        if (!logData.length) {
            logs.innerHTML = '<tr><td colspan="3" class="text-center text-muted py-3">Nessun movimento disponibile.</td></tr>';
        } else {
            logs.innerHTML = logData.map(item => {
                const date = new Date(item.timestamp).toLocaleString("it-IT");
                return '<tr><td class="text-nowrap">' + escapeHtml(date) + '</td>' +
                    '<td><strong>' + escapeHtml(item.action) + '</strong></td>' +
                    '<td>' + escapeHtml(item.details) + '</td></tr>';
            }).join("");
        }

        if (!requestData.length) {
            requestList.innerHTML = '<div class="p-3 text-muted small">Nessuna richiesta registrata.</div>';
        } else {
            requestList.innerHTML = requestData.slice(0, 5).map(item => {
                const cls = item.status === "Complete" ? "ok" : (item.status === "Pending" ? "warn" : "bad");
                return '<div class="list-group-item">' +
                    '<div class="d-flex justify-content-between align-items-center gap-2">' +
                    '<strong>' + escapeHtml(item.request_type) + '</strong>' +
                    '<span class="muoc-status ' + cls + '">' + escapeHtml(item.status) + '</span>' +
                    '</div><div class="muoc-section-note mt-1">' +
                    escapeHtml(item.ticket_uuid) + '</div></div>';
            }).join("");
        }
    } catch (e) {
        logs.innerHTML = '<tr><td colspan="3" class="text-center text-danger py-3">Impossibile caricare i dati.</td></tr>';
        requestList.innerHTML = '<div class="p-3 text-danger small">Impossibile caricare le richieste.</div>';
    }
});
