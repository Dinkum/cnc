(() => {
  const dialog = document.getElementById("operation-history");
  if (!dialog) return;
  const { escapeHtml, formatLocalTimestamp, readJsonResponse } = window.CNCUI;
  const list = dialog.querySelector("[data-history-list]");
  const message = dialog.querySelector("[data-history-message]");
  const filter = dialog.querySelector("[data-history-status]");
  const previous = dialog.querySelector("[data-history-prev]");
  const next = dialog.querySelector("[data-history-next]");
  const refresh = dialog.querySelector("[data-history-refresh]");
  const pageLabel = dialog.querySelector("[data-history-page]");
  let cursors = [null];
  let page = 0;
  let nextCursor = null;
  let controller = null;

  const operationRow = (operation) => {
    const status = ["success", "failed", "partial", "cancelled"].includes(operation.status) ? operation.status : "unknown";
    const details = operation.details && typeof operation.details === "object" ? operation.details : {};
    const label = String(operation.kind || "operation").replace(/^ui\./, "").replaceAll(/[._]/g, " ").replaceAll(/\bbackend\b/g, "output");
    const target = details.backend_name || details.input_name || (operation.backend_id ? `Output ${operation.backend_id}` : "");
    const note = operation.error || details.message || operation.phase || "No further details.";
    return `<details class="operation-history-row">
      <summary>
        <span class="operation-history-name"><strong>${escapeHtml(label)}</strong><span class="subtle">#${escapeHtml(operation.id)}${target ? ` · ${escapeHtml(target)}` : ""}</span></span>
        <span class="operation-history-result" data-status="${status}">${escapeHtml(status === "success" ? "successful" : status)}</span>
        <time datetime="${escapeHtml(operation.finished_at || operation.started_at)}">${escapeHtml(formatLocalTimestamp(operation.finished_at || operation.started_at))}</time>
      </summary>
      <div class="operation-history-detail"><p>${escapeHtml(note)}</p>
        <a href="/api/operations/${encodeURIComponent(operation.id)}" target="_blank" rel="noopener">Full details ↗</a>
      </div>
    </details>`;
  };

  const load = async (targetPage = page, reset = false) => {
    controller?.abort();
    const request = new AbortController();
    controller = request;
    previous.disabled = next.disabled = refresh.disabled = true;
    list.setAttribute("aria-busy", "true");
    message.textContent = "Loading…";
    const query = new URLSearchParams({ limit: "8", status: filter.value });
    const cursor = reset ? null : cursors[targetPage];
    if (cursor) query.set("before", cursor);
    try {
      const response = await fetch(`/api/operations?${query}`, { signal: request.signal, credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error("History could not be loaded.");
      const payload = await readJsonResponse(response, "History");
      if (controller !== request || !dialog.open) return;
      if (reset) cursors = [null];
      page = targetPage;
      nextCursor = payload.next_cursor;
      list.innerHTML = payload.operations.map(operationRow).join("");
      message.textContent = payload.operations.length ? "" : "No completed operations.";
      pageLabel.textContent = `Page ${page + 1}`;
    } catch (error) {
      if (controller !== request || error.name === "AbortError") return;
      list.replaceChildren();
      nextCursor = null;
      message.textContent = "History could not be loaded. Try Refresh.";
    } finally {
      if (controller === request) {
        controller = null;
        list.removeAttribute("aria-busy");
        previous.disabled = page === 0;
        next.disabled = !nextCursor;
        refresh.disabled = false;
      }
    }
  };

  // Delegation survives the dashboard replacing a lazily loaded Home panel.
  document.addEventListener("click", (event) => {
    if (!event.target.closest("[data-operation-history-open]")) return;
    dialog.showModal();
    list.replaceChildren();
    load(0, true);
  });
  dialog.querySelector("[data-history-close]").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => { controller?.abort(); controller = null; });
  dialog.addEventListener("click", (event) => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
  });
  filter.addEventListener("change", () => load(0, true));
  refresh.addEventListener("click", () => load(0, true));
  previous.addEventListener("click", () => { if (page > 0) load(page - 1); });
  next.addEventListener("click", () => {
    if (!nextCursor) return;
    cursors[page + 1] = nextCursor;
    load(page + 1);
  });
})();
