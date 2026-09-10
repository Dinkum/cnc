(() => {
  const deferredScripts = new Map();

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  const readJsonResponse = async (response, fallbackLabel) => {
    const text = await response.text();
    if (!text.trim()) {
      throw new Error(`${fallbackLabel}: empty response`);
    }
    try {
      return JSON.parse(text);
    } catch {
      throw new Error(`${fallbackLabel}: response was not valid JSON`);
    }
  };

  const withRequestDeadline = async (request, { controller = new AbortController(), timeoutMs = 30000 } = {}) => {
    const timer = window.setTimeout(() => {
      controller.abort(new DOMException("Request timed out", "TimeoutError"));
    }, timeoutMs);
    try {
      // Include body consumption: receiving headers does not finish a request.
      return await request(controller.signal);
    } catch (error) {
      // Body readers may report AbortError even for a deadline. Preserve the
      // reason so pollers distinguish timeouts from deliberate cancellation.
      throw controller.signal.aborted ? controller.signal.reason || error : error;
    } finally {
      window.clearTimeout(timer);
    }
  };

  const readJsonScript = (id, fallback = {}) => {
    const element = document.getElementById(id);
    if (!element?.textContent?.trim()) return fallback;
    try {
      return JSON.parse(element.textContent);
    } catch (error) {
      console.error(`failed to parse #${id}`, error);
      return fallback;
    }
  };

  const loadDeferredScript = (src) => {
    if ((src.includes("chart-") && window.Chart) || (src.includes("metric-event-markers") && window.cncMetricEventMarkers)) {
      return Promise.resolve();
    }
    if (deferredScripts.has(src)) return deferredScripts.get(src);
    const existingScript = Array.from(document.scripts).find((script) => script.getAttribute("src") === src);
    if (existingScript?.dataset.loaded === "1") return Promise.resolve();
    const promise = new Promise((resolve, reject) => {
      const script = existingScript || document.createElement("script");
      script.addEventListener("load", () => {
        script.dataset.loaded = "1";
        resolve();
      }, { once: true });
      script.addEventListener("error", () => {
        deferredScripts.delete(src);
        script.remove();
        reject(new Error(`failed to load ${src}`));
      }, { once: true });
      if (!existingScript) {
        script.src = src;
        script.async = true;
        document.head.appendChild(script);
      }
    });
    deferredScripts.set(src, promise);
    return promise;
  };

  const ensureMetricChartAssets = async () => {
    await loadDeferredScript("/static/vendor/chart-4.4.7.umd.min.js");
    await loadDeferredScript("/static/js/metric-event-markers.js");
  };

  const metricNumber = (value) => {
    if (value == null || typeof value === "boolean" || (typeof value === "string" && !value.trim())) return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  };

  const metricCsv = (payload, scope) => {
    const columns = ["scope", "metric", "unit", ...new Set(payload.series.flatMap((point) => Object.keys(point)))];
    const unit = payload.metric.unit_kind === "rate" ? "bytes_per_second" : payload.metric.unit_kind;
    const cell = (value) => {
      let text = String(value ?? "");
      // Keep text cells inert in spreadsheet applications; numeric negatives
      // remain numeric and retain their exact value.
      if (typeof value === "string" && /^[=+@\-\t\r]/.test(text)) text = `'${text}`;
      return `"${text.replaceAll('"', '""')}"`;
    };
    const rows = payload.series.map((point) => {
      const row = { scope, metric: payload.metric.key, unit, ...point };
      return columns.map((key) => row[key]);
    });
    return [columns, ...rows].map((row) => row.map(cell).join(",")).join("\r\n") + "\r\n";
  };

  const bindMetricExport = (control, getPayload, scope) => {
    if (!control) return;
    const available = () => {
      const payload = getPayload();
      return payload?.available && payload?.series?.length ? payload : null;
    };
    const toggle = control.querySelector?.("[data-export-toggle]");
    const options = control.querySelector?.(".metric-export-options");
    const close = () => {
      if (!toggle || !options) return;
      options.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
    };
    (toggle || control).disabled = !available();
    if (!available()) close();
    const download = (format) => {
      const payload = available();
      if (!payload || !["csv", "json"].includes(format)) return;
      const content = format === "csv" ? metricCsv(payload, scope) : JSON.stringify(payload, null, 2) + "\n";
      const blob = new Blob([content], { type: format === "csv" ? "text/csv;charset=utf-8" : "application/json" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${scope}-${payload.metric.key}-${payload.timeframe.key}.${format}`.replace(/[^a-zA-Z0-9_.-]/g, "-");
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    };
    if (!toggle || !options) {
      control.onchange = () => {
        const format = control.value;
        control.value = "";
        download(format);
      };
      return;
    }
    toggle.onclick = () => {
      options.hidden = !options.hidden;
      toggle.setAttribute("aria-expanded", String(!options.hidden));
    };
    control.querySelectorAll("[data-export-format]").forEach((button) => {
      button.onclick = () => {
        download(button.dataset.exportFormat);
        close();
        toggle.focus();
      };
    });
    control.onkeydown = (event) => {
      if (event.key !== "Escape") return;
      close();
      toggle.focus();
    };
    control.onfocusout = (event) => {
      if (!control.contains(event.relatedTarget)) close();
    };
    if (!control.dataset.exportBound) {
      document.addEventListener("click", (event) => {
        if (!control.contains(event.target)) close();
      });
      control.dataset.exportBound = "true";
    }
  };

  const normalizeTimestampValue = (value) => {
    const raw = String(value || "").trim();
    if (!raw) return "";
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(raw)) return `${raw}Z`;
    return raw;
  };

  const formatLocalTimestamp = (value, format = "datetime") => {
    const normalized = normalizeTimestampValue(value);
    if (!normalized) return "";
    const date = new Date(normalized);
    if (Number.isNaN(date.getTime())) return "";
    if (format === "time") {
      return new Intl.DateTimeFormat(undefined, {
        hour: "numeric",
        minute: "2-digit",
      }).format(date);
    }
    return new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    }).format(date);
  };

  const renderLocalTimes = (root = document) => {
    root.querySelectorAll("[data-local-time]").forEach((element) => {
      const formatted = formatLocalTimestamp(
        element.dataset.localTime || element.textContent,
        element.dataset.localTimeFormat || "datetime"
      );
      if (formatted) element.textContent = formatted;
    });
  };

  window.CNCUI = Object.freeze({
    bindMetricExport,
    metricCsv,
    metricNumber,
    ensureMetricChartAssets,
    escapeHtml,
    formatLocalTimestamp,
    loadDeferredScript,
    normalizeTimestampValue,
    readJsonResponse,
    readJsonScript,
    renderLocalTimes,
    withRequestDeadline,
  });
})();
