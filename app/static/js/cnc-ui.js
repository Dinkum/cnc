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
      script.addEventListener("error", () => reject(new Error(`failed to load ${src}`)), { once: true });
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
    ensureMetricChartAssets,
    escapeHtml,
    formatLocalTimestamp,
    loadDeferredScript,
    normalizeTimestampValue,
    readJsonResponse,
    readJsonScript,
    renderLocalTimes,
  });
})();
