(() => {
  const card = document.querySelector("[data-hardening-card]");
  if (!card) return;
  const { readJsonScript } = window.CNCUI;
  const page = readJsonScript("output-detail-config", {});
  const backendId = Number(page.backendId);
  let csrfToken = String(page.csrfToken || "");
  const find = (name) => card.querySelector(`[data-hardening-${name}]`);
  const dialog = find("confirm");
  const confirmButton = find("confirm-apply");
  const reviewed = find("reviewed");
  const status = find("configuration-status");
  const controls = find("controls");
  const customButton = find("review-custom");
  const previousButton = find("restore-previous");
  const recommendedButton = find("apply-recommended");
  let configuration = null;
  let plan = null;
  let busy = false;
  let runActive = false;
  let watching = null;

  const node = (tag, text, className) => {
    const result = document.createElement(tag);
    if (text !== undefined) result.textContent = text;
    if (className) result.className = className;
    return result;
  };
  const displayValue = (value, key = "") => {
    if (key === "ipc" && value === "none") return "None (disable IPC)";
    if (value === null || value === "" || value === false || value === "default" || value === "none") return "Runtime default";
    if (value === true) return "Enabled";
    if (Array.isArray(value)) return value.length ? value.join(", ") : "None";
    return String(value).replace("tmp_run", "/tmp + /run");
  };
  const setBusy = (value) => {
    busy = value;
    card.dataset.hardeningApplying = String(value);
    controls.disabled = value || runActive;
    recommendedButton.disabled = value || runActive;
    confirmButton.disabled = value || !plan || !reviewed.checked;
    document.dispatchEvent(new Event("cnc:hardening-refresh"));
  };

  const request = async (path, values = null) => {
    const send = () => {
      const options = { credentials: "same-origin", headers: { Accept: "application/json" } };
      if (values) {
        const body = new FormData();
        body.set("csrf_token", csrfToken);
        Object.entries(values).forEach(([key, value]) => body.set(key, String(value)));
        Object.assign(options, { method: "POST", body });
      }
      return fetch(path, options);
    };
    let response = await send();
    if (values && response.status === 403) {
      const refresh = await fetch("/api/csrf", { credentials: "same-origin", headers: { Accept: "application/json" } });
      const payload = await refresh.json();
      if (refresh.ok && payload.csrf_token) {
        csrfToken = payload.csrf_token;
        response = await send();
      }
    }
    const payload = await response.json();
    if (!response.ok) throw new Error(String(payload.detail || payload.error || "Security configuration request failed."));
    return payload;
  };

  const renderEditor = () => {
    const rows = [];
    for (const control of configuration.controls) {
      const row = node("tr");
      const id = `hardening-control-${control.key}`;
      const label = node("label", control.label);
      label.htmlFor = id;
      const nameCell = node("td");
      nameCell.append(label);
      const flagCell = node("td");
      flagCell.append(node("code", control.flag));
      const valueCell = node("td");
      const value = configuration.current[control.key];
      let input;
      if (["boolean", "select"].includes(control.type)) {
        input = node("select");
        const options = control.type === "boolean" ? [false, true] : control.choices;
        for (const optionValue of options) {
          const text = control.key === "ipc" && optionValue === "none" ? "None (disable IPC)" : displayValue(optionValue);
          const option = node("option", text);
          option.value = String(optionValue);
          input.append(option);
        }
        input.value = String(value);
      } else {
        input = node("input");
        input.type = control.type === "number" ? "number" : "text";
        input.value = Array.isArray(value) ? value.join(", ") : value ?? "";
        input.placeholder = control.type === "capabilities" ? "NET_BIND_SERVICE, …" : "Runtime default";
        if (control.type === "number") { input.min = "1"; input.step = "1"; }
        else input.maxLength = control.type === "capabilities" ? 1024 : 128;
      }
      input.id = id;
      input.dataset.hardeningField = control.key;
      valueCell.append(input);
      row.append(nameCell, flagCell, valueCell);
      rows.push(row);
    }
    for (const control of configuration.other_controls) {
      const row = node("tr", undefined, "hardening-managed-row");
      const label = node("td", control.setting.replaceAll("_", " "));
      const state = node("td", control.state);
      const reason = node("td", control.reason);
      row.append(label, state, reason);
      rows.push(row);
    }
    find("control-rows").replaceChildren(...rows);
    customButton.disabled = false;
    previousButton.hidden = !configuration.has_previous;
  };
  const loadConfiguration = async () => {
    configuration = await request(`/api/backends/${backendId}/hardening/configuration`);
    renderEditor();
  };
  const editorValues = () => Object.fromEntries(configuration.controls.map((control) => {
    const value = document.getElementById(`hardening-control-${control.key}`).value.trim();
    if (control.type === "boolean") return [control.key, value === "true"];
    if (control.type === "number") return [control.key, value === "" ? null : Number(value)];
    if (control.type === "capabilities") return [control.key, value.split(/[\s,]+/).filter(Boolean)];
    return [control.key, value];
  }));

  const preview = async (mode) => {
    if (busy || runActive) return;
    plan = null;
    reviewed.checked = false;
    setBusy(true);
    status.textContent = "Preparing security review…";
    try {
      plan = await request(`/ui/backends/${backendId}/hardening/preview`, {
        mode, configuration: JSON.stringify(mode === "manual" ? editorValues() : {}),
      });
      const changes = plan.changes.map((change) => {
        const row = node("div");
        row.append(node("strong", change.label), node("span", `${displayValue(change.before, change.setting)} → ${displayValue(change.after, change.setting)}`));
        return row;
      });
      find("changes").replaceChildren(...(changes.length ? changes : [node("p", "No saved flags change. Apply will verify the current configuration.")]));
      find("skipped").hidden = plan.skipped.length === 0;
      find("skipped-label").textContent = `Not changed (${plan.skipped.length})`;
      find("skipped-rows").replaceChildren(...plan.skipped.map((item) => node("li", `${item.setting.replaceAll("_", " ")}: ${item.reason}`)));
      find("exact-config").textContent = [...plan.flags, ...plan.mounts.map((mount) => `--volume=${mount}`)].join("\n") || "CNC runtime defaults";
      confirmButton.textContent = plan.will_restart ? "Apply and restart app" : "Apply configuration";
      find("confirm-message").textContent = "";
      status.textContent = "";
      dialog.showModal();
    } catch (error) {
      status.textContent = error.message;
    } finally {
      setBusy(false);
    }
  };

  const watch = async (operationId) => {
    if (watching === operationId) return;
    watching = operationId;
    setBusy(true);
    status.textContent = "Applying security configuration…";
    try {
      await window.CNCOperations.watchOperation(operationId, {
        onProgress: (operation) => { status.textContent = operation.details?.message || operation.phase || "Applying security configuration…"; },
        onSuccess: () => { status.textContent = "Security configuration applied. Check your app's normal workflows."; },
        onFailure: (operation) => { status.textContent = operation.details?.flash_error || operation.error || "Apply failed. Review the operation and app state before retrying."; },
      });
      await loadConfiguration();
    } catch (error) {
      status.textContent = `Could not confirm the apply result. Check operation #${operationId} in History. ${error.message}`;
    } finally {
      watching = null;
      setBusy(false);
    }
  };

  confirmButton.addEventListener("click", async () => {
    if (!plan || !reviewed.checked || busy) return;
    setBusy(true);
    try {
      const result = await request(`/ui/backends/${backendId}/hardening/apply`, {
        mode: plan.mode, configuration: JSON.stringify(plan.configuration), revision: plan.revision, reviewed: "true",
      });
      if (!result.operation_id) throw new Error("Apply result did not contain an operation ID. Check History before retrying.");
      dialog.close();
      await watch(result.operation_id);
    } catch (error) {
      find("confirm-message").textContent = error.message;
      status.textContent = error.message;
      plan = null;
      reviewed.checked = false;
    } finally {
      setBusy(false);
    }
  });
  reviewed.addEventListener("change", () => { confirmButton.disabled = busy || !plan || !reviewed.checked; });
  card.querySelectorAll("[data-hardening-confirm-close]").forEach((button) => button.addEventListener("click", () => dialog.close()));
  dialog.addEventListener("close", () => { plan = null; reviewed.checked = false; });
  recommendedButton.addEventListener("click", () => preview("recommended"));
  customButton.addEventListener("click", () => preview("manual"));
  previousButton.addEventListener("click", () => preview("previous"));
  document.addEventListener("cnc:hardening-status", (event) => {
    runActive = [event.detail.phase1, event.detail.phase2].some((run) => ["queued", "running"].includes(run?.status));
    controls.disabled = busy || runActive;
  });
  const initialize = async () => {
    try {
      await loadConfiguration();
      const payload = await request(`/api/active-operations?surface=output_detail&backend_id=${backendId}`);
      const active = payload.operations?.find((operation) => operation.kind === "ui.backend.hardening");
      if (active) await watch(active.id);
    } catch (error) {
      status.textContent = error.message;
    }
  };
  initialize();
})();
