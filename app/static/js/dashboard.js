(() => {
  const {
    ensureMetricChartAssets,
    escapeHtml,
    formatLocalTimestamp,
    loadDeferredScript,
    readJsonResponse,
    readJsonScript,
    renderLocalTimes,
  } = window.CNCUI;
  const dashboardConfig = readJsonScript("dashboard-config", {});
  const currentTabs = () => Array.from(document.querySelectorAll("[data-tab]"));
  const currentPanels = () => Array.from(document.querySelectorAll("[data-panel]"));
  if (!currentTabs().length || !currentPanels().length) return;
  let renderRouteGraphs = () => {};
  let stopRouteGraphFlow = () => {};
  let bindDashboardPanel = () => {};
  let handleTabRequest = (name) => selectTab(name);
  let afterTabSelected = () => {};
  const tabLoadRequests = new Map();
  let latestTabRequestId = 0;
  let hostMetricsRequestId = 0;
  let hostMetricsAbortController = null;
  const ensureRouteGraphAsset = () => loadDeferredScript("/static/vendor/cytoscape-3.33.2.min.js");

  const dashboardTabUrl = (name) => {
    const url = new URL(window.location.href);
    url.searchParams.set("tab", name);
    url.hash = "";
    return `${url.pathname}${url.search}`;
  };

  const selectTab = (name) => {
    const targetName = currentTabs().some((tab) => tab.dataset.tab === name) ? name : "home";
    currentTabs().forEach((tab) => {
      const active = tab.dataset.tab === targetName;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    currentPanels().forEach((panel) => {
      panel.hidden = panel.dataset.panel !== targetName;
    });
    const nextUrl = dashboardTabUrl(targetName);
    if (`${location.pathname}${location.search}` !== nextUrl || location.hash) {
      history.replaceState(null, "", nextUrl);
    }
    if (targetName === "routing") {
      window.requestAnimationFrame(() => renderRouteGraphs(document));
    } else {
      document.querySelectorAll("[data-route-graph]").forEach((shell) => stopRouteGraphFlow(shell));
    }
    if (targetName !== "settings") {
      hostMetricsRequestId += 1;
      hostMetricsAbortController?.abort();
    }
    window.requestAnimationFrame(() => afterTabSelected(targetName));
  };

  const tabIsAvailable = (name) => currentTabs().some((tab) => tab.dataset.tab === name);
  const initialHashTab = location.hash.replace("#", "");
  const queryTab = new URLSearchParams(window.location.search).get("tab") || "";
  const requestedTab = document.body.dataset.initialTab || "home";

  const clearTransientDashboardParams = () => {
    const url = new URL(window.location.href);
    if (!url.searchParams.has("defer_status")) return;
    url.searchParams.delete("defer_status");
    history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
  };
  clearTransientDashboardParams();
  const initialTab = tabIsAvailable(queryTab)
    ? requestedTab
    : (tabIsAvailable(initialHashTab) ? initialHashTab : requestedTab);
  selectTab(tabIsAvailable(initialTab) ? initialTab : "home");
  window.addEventListener("hashchange", () => {
    const nextTab = location.hash.replace("#", "");
    if (tabIsAvailable(nextTab)) handleTabRequest(nextTab);
  });

  const floatingSaveShell = document.getElementById("input-save-shell");
  const floatingSaveCard = document.getElementById("input-save-card");
  const floatingSaveAction = document.getElementById("input-save-action");
  const floatingSaveStage = document.getElementById("input-save-stage");
  const floatingSaveNote = document.getElementById("input-save-note");
  const floatingSaveMeta = document.getElementById("input-save-meta");
  const floatingSaveProgressFill = document.getElementById("input-save-progress-fill");
  const confirmModal = document.getElementById("confirm-modal");
  const confirmModalTitle = document.getElementById("confirm-modal-title");
  const confirmModalMessage = document.getElementById("confirm-modal-message");
  const confirmModalAccept = document.getElementById("confirm-modal-accept");
  const confirmModalCancel = document.getElementById("confirm-modal-cancel");
  const SERVER_LOAD_POLL_INTERVAL_MS = 15000;
  const TAB_STATUS_REFRESH_MIN_INTERVAL_MS = 10000;
  const OUTPUT_LOAD_AVG_WINDOW_MS = 60 * 1000;
  const HOST_METRIC_LIVE_WINDOW_MS = 30 * 60 * 1000;
  const HOST_METRIC_MAX_SAMPLES = 180;
  const STATUS_POLL_TABS = new Set(["home", "outputs", "settings"]);
  let serverLoadPollTimer = null;
  let serverLoadPollInFlight = false;
  let hostMutationUiBusy = 0;
  const tabStatusRefreshTimes = {};
  const outputLoadSamples = {};
  let hostMetricsShell = null;
  let hostMetricsChart = null;
  let hostMetricsSummary = null;
  let hostMetricsEmpty = null;
  let hostMetricSelect = null;
  let hostTimeframeSelect = null;
  let hostMetricsChartInstance = null;
  let lastHostMetricPayload = null;
  let lastHostMetrics = {};
  const hostMetricSamples = [];
  const refreshHostMetricRefs = () => {
    hostMetricsShell = document.getElementById("host-metrics-shell");
    hostMetricsChart = document.getElementById("host-metrics-chart");
    hostMetricsSummary = document.getElementById("host-metrics-summary");
    hostMetricsEmpty = document.getElementById("host-metrics-empty");
    hostMetricSelect = document.getElementById("host-metric-select");
    hostTimeframeSelect = document.getElementById("host-timeframe-select");
  };
  refreshHostMetricRefs();
  const HOST_METRIC_DATA_KEYS = {
    cpu: "cpu_percent",
    memory: "memory_percent",
    disk: "disk_percent",
    network: "network_total_bps",
  };
  const HOST_METRIC_FORMATTERS = {
    cpu: "percent",
    memory: "percent",
    disk: "percent",
    network: "rate",
  };

  const formatPercent = (value) => (
    Number.isFinite(value) ? `${Number(value).toFixed(1)}%` : "unavailable"
  );

  const formatBytes = (value) => {
    const bytes = Number(value);
    if (!Number.isFinite(bytes)) return "unavailable";
    const abs = Math.abs(bytes);
    if (abs >= 1024 ** 3) return `${(bytes / (1024 ** 3)).toFixed(1)} GB`;
    if (abs >= 1024 ** 2) return `${Math.round(bytes / (1024 ** 2))} MB`;
    if (abs >= 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${Math.round(bytes)} B`;
  };

  const formatRate = (value) => {
    const bytesPerSecond = Number(value);
    if (!Number.isFinite(bytesPerSecond)) return "unavailable";
    const bitsPerSecond = bytesPerSecond * 8;
    if (bitsPerSecond < 1000) return `${bitsPerSecond.toFixed(0)} bps`;
    const units = ["Kbps", "Mbps", "Gbps"];
    let scaled = bitsPerSecond / 1000;
    let unit = units[0];
    for (let index = 0; index < units.length; index += 1) {
      unit = units[index];
      if (scaled < 1000 || index === units.length - 1) break;
      scaled /= 1000;
    }
    return `${scaled.toFixed(1)} ${unit}`;
  };

  const outputLoadMeter = (percent, { enabled, kind, serviceState }) => {
    if (!enabled) return { label: "OFF", percent: 0, tone: "inactive", compact: false };
    if (kind !== "app" && kind !== "shield") return { label: "", percent: 0, tone: "inactive", compact: true };
    if (Number.isFinite(percent)) {
      const normalized = Math.max(0, Math.min(100, Number(percent)));
      return {
        label: `${normalized.toFixed(1)}%`,
        percent: normalized,
        tone: normalized >= 90 ? "critical" : (normalized >= 75 ? "warn" : "active"),
        compact: false,
      };
    }
    const state = String(serviceState || "").trim().toLowerCase();
    if (state === "active" || state === "activating" || state === "reloading") {
      return {
        label: "collecting",
        percent: 0,
        tone: "queued",
        compact: false,
      };
    }
    return { label: "", percent: 0, tone: "inactive", compact: true };
  };

  const averageOutputLoad = (key, nextValue) => {
    const now = Date.now();
    const samples = outputLoadSamples[key] || [];
    const recentSamples = samples.filter((sample) => now - sample.at <= OUTPUT_LOAD_AVG_WINDOW_MS);
    if (Number.isFinite(nextValue)) {
      recentSamples.push({ at: now, value: Number(nextValue) });
    }
    outputLoadSamples[key] = recentSamples;
    if (!recentSamples.length) return nextValue;
    const total = recentSamples.reduce((sum, sample) => sum + sample.value, 0);
    return total / recentSamples.length;
  };

  const setMeterFill = (fill, tone, percent) => {
    if (!fill) return;
    const boundedPercent = Number.isFinite(percent) ? Math.max(0, Math.min(100, Number(percent))) : 0;
    fill.style.width = `${boundedPercent}%`;
    fill.classList.toggle("warn", tone === "warn");
    fill.classList.toggle("critical", tone === "critical");
  };

  const normalizeHostMetricKey = (metricKey) => {
    const key = String(metricKey || "cpu").toLowerCase();
    return HOST_METRIC_DATA_KEYS[key] ? key : "cpu";
  };

  const hostMetricValue = (metrics, metricKey) => {
    const key = normalizeHostMetricKey(metricKey);
    const value = Number(metrics?.[HOST_METRIC_DATA_KEYS[key]]);
    return Number.isFinite(value) ? value : null;
  };

  const hostMetricSummaryValue = (value, metricKey) => {
    const key = normalizeHostMetricKey(metricKey);
    if (HOST_METRIC_FORMATTERS[key] === "rate") return formatRate(value);
    return formatPercent(value);
  };

  const hostDiskSummaryValue = (value, label) => {
    if (value === null || value === undefined) return "unavailable";
    const percent = Number(value);
    if (!Number.isFinite(percent)) return "unavailable";
    const totalBytes = Number(lastHostMetrics?.disk_total_bytes);
    const liveUsedBytes = Number(lastHostMetrics?.disk_used_bytes);
    const usedBytes = label === "last" && Number.isFinite(liveUsedBytes)
      ? liveUsedBytes
      : (Number.isFinite(totalBytes) && totalBytes > 0 ? (totalBytes * percent) / 100 : NaN);
    if (!Number.isFinite(usedBytes) || !Number.isFinite(totalBytes) || totalBytes <= 0) {
      return formatPercent(percent);
    }
    return `${formatPercent(percent)} · ${formatBytes(usedBytes)} used`;
  };

  const renderHostMetricsSummary = (payload) => {
    if (!hostMetricsSummary) return;
    const metricKey = normalizeHostMetricKey(payload?.metric?.key || payload?.metricKey || hostMetricSelect?.value);
    const summary = payload?.summary && typeof payload.summary === "object" ? payload.summary : {};
    const summaryValue = (label) => (
      summary?.[label === "avg" ? "average_value" : label === "peak" ? "peak_value" : "latest_value"]
    );
    const rows = [
      { label: "peak", value: hostMetricSummaryValue(summary?.peak_value, metricKey) },
      { label: "avg", value: hostMetricSummaryValue(summary?.average_value, metricKey) },
      { label: "last", value: hostMetricSummaryValue(summary?.latest_value, metricKey) },
    ].map((item) => ({
      ...item,
      value: metricKey === "disk" ? hostDiskSummaryValue(summaryValue(item.label), item.label) : item.value,
    }));
    hostMetricsSummary.innerHTML = rows.map((item) => `
      <div class="metrics-summary-chip">
        <span class="metrics-summary-label">${escapeHtml(item.label)}</span>
        <strong class="metrics-summary-value">${escapeHtml(item.value)}</strong>
      </div>
    `).join("");
  };

  const formatHostMetricForPayload = (value, unitKind) => {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "n/a";
    return unitKind === "rate" ? formatRate(value) : formatPercent(value);
  };

  const selectedHostMetricKey = () => normalizeHostMetricKey(hostMetricSelect?.value);
  const selectedHostTimeframeKey = () => String(hostTimeframeSelect?.value || "day");

  const hostMetricHasAnyValue = (metrics) => (
    ["cpu", "memory", "disk", "network"].some((metricKey) => hostMetricValue(metrics, metricKey) !== null)
  );

  const pushHostMetricSample = (metrics) => {
    if (!metrics || typeof metrics !== "object" || !hostMetricHasAnyValue(metrics)) return;
    const sampledAt = Date.now();
    const sample = { timestamp: sampledAt, metrics: { ...metrics } };
    const lastSample = hostMetricSamples[hostMetricSamples.length - 1];
    if (lastSample && sampledAt - lastSample.timestamp < 1000) {
      hostMetricSamples[hostMetricSamples.length - 1] = sample;
    } else {
      hostMetricSamples.push(sample);
    }
    const cutoff = sampledAt - HOST_METRIC_LIVE_WINDOW_MS;
    while (hostMetricSamples.length > HOST_METRIC_MAX_SAMPLES || hostMetricSamples[0]?.timestamp < cutoff) {
      hostMetricSamples.shift();
    }
  };

  const hostPayloadMatchesCurrentSelection = (payload) => (
    Boolean(payload?.metric && payload?.timeframe)
    && normalizeHostMetricKey(payload?.metric?.key) === selectedHostMetricKey()
    && String(payload?.timeframe?.key || "day") === selectedHostTimeframeKey()
  );

  const hostMetricSummaryFromLiveSamples = (metricKey) => {
    const values = hostMetricSamples
      .map((sample) => hostMetricValue(sample.metrics, metricKey))
      .filter((value) => value !== null);
    const liveValue = hostMetricValue(lastHostMetrics, metricKey);
    const latestValue = liveValue ?? values[values.length - 1] ?? null;
    if (!values.length && latestValue === null) {
      return { latest_value: null, peak_value: null, average_value: null, point_count: 0 };
    }
    const summaryValues = values.length ? values : [latestValue];
    const total = summaryValues.reduce((sum, value) => sum + value, 0);
    return {
      latest_value: latestValue,
      peak_value: Math.max(...summaryValues),
      average_value: total / summaryValues.length,
      point_count: summaryValues.length,
    };
  };

  const hostTickLabelForTime = (ts, timeframeKey) => {
    const date = new Date(ts);
    if (timeframeKey === "live" || timeframeKey === "hour" || timeframeKey === "day") {
      return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    }
    if (timeframeKey === "week") {
      return date.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
    }
    return date.toLocaleDateString([], { month: "short", day: "numeric" });
  };

  const hostTooltipTitleForTime = (ts) => {
    const date = new Date(ts);
    const weekday = date.toLocaleDateString([], { weekday: "short" });
    const monthDay = date.toLocaleDateString([], { month: "short", day: "numeric" });
    const time = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
    return `${weekday}, ${monthDay} ${time}`;
  };

  const buildLiveHostMetricsPayload = (note) => {
    const metricKey = selectedHostMetricKey();
    const now = Date.now();
    const series = hostMetricSamples
      .map((sample) => {
        const value = hostMetricValue(sample.metrics, metricKey);
        if (value === null) return null;
        const point = {
          timestamp: new Date(sample.timestamp).toISOString(),
          value,
          avg: value,
          min: value,
          max: value,
          visual_min: value,
          visual_max: value,
          last: value,
          count: 1,
          is_live: true,
        };
        if (metricKey === "network") {
          const rx = Number(sample.metrics?.network_rx_bps);
          const tx = Number(sample.metrics?.network_tx_bps);
          if (Number.isFinite(rx)) point.network_rx_bps = rx;
          if (Number.isFinite(tx)) point.network_tx_bps = tx;
        }
        return point;
      })
      .filter(Boolean);
    return {
      available: true,
      metric: {
        key: metricKey,
        unit_kind: HOST_METRIC_FORMATTERS[metricKey] || "percent",
      },
      timeframe: { key: "live", label: "live 30m" },
      range_start_at: new Date(Math.max(0, now - HOST_METRIC_LIVE_WINDOW_MS)).toISOString(),
      range_end_at: new Date(now + 1000).toISOString(),
      series,
      summary: hostMetricSummaryFromLiveSamples(metricKey),
      note,
      fallback: true,
    };
  };

  const renderLiveHostMetricsFallback = (note = "Waiting for live host metrics.") => {
    const payload = buildLiveHostMetricsPayload(note);
    if (!payload.series.length) {
      if (hostMetricsChartInstance) {
        hostMetricsChartInstance.destroy();
        hostMetricsChartInstance = null;
      }
      renderHostMetricsSummary(payload);
      if (hostMetricsEmpty) {
        hostMetricsEmpty.hidden = false;
        hostMetricsEmpty.textContent = note;
      }
      return false;
    }
    renderHostMetricsPayload(payload, { isFallback: true, allowFallback: false });
    return true;
  };

  const renderHostMetricsPayload = (payload, options = {}) => {
    if (!hostMetricsChart || !hostMetricsEmpty) return;
    if (!window.Chart) {
      lastHostMetricPayload = null;
      hostMetricsEmpty.hidden = false;
      hostMetricsEmpty.textContent = "Chart library unavailable.";
      return;
    }

    const allowFallback = options.allowFallback !== false;
    const isFallback = Boolean(options.isFallback);
    const preservePayload = Boolean(options.preservePayload);
    const available = Boolean(payload?.available);
    const series = Array.isArray(payload?.series) ? payload.series : [];
    if (!available) {
      if (allowFallback && renderLiveHostMetricsFallback(String(payload?.note || "Host metric history is unavailable. Showing live samples."))) return;
      lastHostMetricPayload = null;
      hostMetricsEmpty.hidden = false;
      hostMetricsEmpty.textContent = String(payload?.note || "No host metric history is available.");
      return;
    }
    if (!series.length) {
      if (allowFallback && renderLiveHostMetricsFallback("No host metric history yet. Showing live samples.")) return;
      lastHostMetricPayload = null;
      hostMetricsEmpty.hidden = false;
      hostMetricsEmpty.textContent = "No host metric samples yet.";
      renderHostMetricsSummary(payload);
      return;
    }
    if (hostMetricsChartInstance) {
      hostMetricsChartInstance.destroy();
      hostMetricsChartInstance = null;
    }
    if (!isFallback && !preservePayload) {
      lastHostMetricPayload = payload;
    }
    renderHostMetricsSummary(payload);
    hostMetricsEmpty.hidden = !(isFallback && payload?.note);
    if (!hostMetricsEmpty.hidden) {
      hostMetricsEmpty.textContent = String(payload.note || "");
    }

    const timeframeKey = String(payload?.timeframe?.key || "day");
    const unitKind = String(payload?.metric?.unit_kind || "percent");
    const metricKey = String(payload?.metric?.key || "cpu");
    const chartEvents = window.cncMetricEventMarkers?.normalizeEvents(payload?.chart_events) || [];
    const avgPoints = series.map((item) => ({ x: Date.parse(item.timestamp), y: Number(item.visual_value ?? item.avg ?? item.value) }));
    const minPoints = series.map((item) => ({ x: Date.parse(item.timestamp), y: Number(item.visual_min ?? item.min ?? item.avg ?? item.value) }));
    const maxPoints = series.map((item) => ({ x: Date.parse(item.timestamp), y: Number(item.visual_max ?? item.max ?? item.avg ?? item.value) }));
    const downPoints = series
      .map((item) => ({ x: Date.parse(item.timestamp), y: Number(item.network_rx_bps) }))
      .filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y));
    const upPoints = series
      .map((item) => ({ x: Date.parse(item.timestamp), y: Number(item.network_tx_bps) }))
      .filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y));
    const selectedRangeStart = Date.parse(payload?.range_start_at);
    const selectedRangeEnd = Date.parse(payload?.range_end_at);
    const xMin = Number.isFinite(selectedRangeStart) ? selectedRangeStart : avgPoints[0].x;
    const xMax = Number.isFinite(selectedRangeEnd) ? selectedRangeEnd : avgPoints[avgPoints.length - 1].x;
    const hasDirectionalNetwork = downPoints.length || upPoints.length;
    const chartValuePoints = metricKey === "network" && hasDirectionalNetwork
      ? [...downPoints, ...upPoints]
      : [...avgPoints, ...minPoints, ...maxPoints];
    const dataValues = chartValuePoints.map((point) => point.y).filter(Number.isFinite);
    const dataMax = dataValues.length ? Math.max(...dataValues) : 1;
    const dataMin = dataValues.length ? Math.min(...dataValues) : 0;
    const dataRange = Math.max(dataMax - dataMin, dataMax * 0.1, 0.01);
    const fallbackYAxisMin = Math.max(0, dataMin - dataRange * 0.15);
    const fallbackPercentYAxisMax = () => {
      if (metricKey === "memory" || metricKey === "disk") return 100;
      const padded = Math.max(dataMax * 1.2, dataMax + 0.1);
      return [1, 2, 5, 10, 20, 50, 100].find((stop) => padded <= stop) || Math.max(100, dataMax + dataRange * 0.4);
    };
    const fallbackYAxisMax = metricKey === "network"
      ? Math.max(1, dataMax + dataRange * 0.4)
      : fallbackPercentYAxisMax();
    const payloadYAxisMin = Number(payload?.y_axis_min);
    const payloadYAxisMax = Number(payload?.y_axis_max);
    const yAxisMin = Number.isFinite(payloadYAxisMin) ? payloadYAxisMin : fallbackYAxisMin;
    const yAxisMax = Number.isFinite(payloadYAxisMax) && payloadYAxisMax > yAxisMin
      ? payloadYAxisMax
      : fallbackYAxisMax;
    const datasets = metricKey === "network"
      ? (hasDirectionalNetwork ? [
          {
            label: "down",
            data: downPoints,
            borderColor: "#68d8ff",
            backgroundColor: "transparent",
            borderWidth: 2,
            pointRadius: downPoints.length === 1 ? 3 : 0,
            pointHoverRadius: 5,
            pointHoverBackgroundColor: "#68d8ff",
            pointHoverBorderColor: "rgba(9,14,24,0.9)",
            pointHoverBorderWidth: 2.5,
            tension: 0.42,
            cubicInterpolationMode: "monotone",
          },
          {
            label: "up",
            data: upPoints,
            borderColor: "#9bf0c7",
            backgroundColor: "transparent",
            borderWidth: 2,
            pointRadius: upPoints.length === 1 ? 3 : 0,
            pointHoverRadius: 5,
            pointHoverBackgroundColor: "#9bf0c7",
            pointHoverBorderColor: "rgba(9,14,24,0.9)",
            pointHoverBorderWidth: 2.5,
            tension: 0.42,
            cubicInterpolationMode: "monotone",
          },
        ] : [
          {
            label: "total",
            data: avgPoints.filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y)),
            borderColor: "#86dcff",
            backgroundColor: "transparent",
            borderWidth: 2,
            pointRadius: avgPoints.length === 1 ? 3 : 0,
            pointHoverRadius: 5,
            pointHoverBackgroundColor: "#86dcff",
            pointHoverBorderColor: "rgba(9,14,24,0.9)",
            pointHoverBorderWidth: 2.5,
            tension: 0.42,
            cubicInterpolationMode: "monotone",
          },
        ])
      : [
          {
            label: "range-min",
            data: minPoints,
            borderColor: "rgba(70,190,255,0)",
            borderWidth: 0,
            backgroundColor: "rgba(70,190,255,0)",
            pointRadius: 0,
            pointHoverRadius: 0,
            tension: 0.35,
            cubicInterpolationMode: "monotone",
          },
          {
            label: "range-max",
            data: maxPoints,
            borderColor: "rgba(70,190,255,0)",
            borderWidth: 0,
            backgroundColor: "rgba(70,190,255,0.14)",
            fill: "-1",
            pointRadius: 0,
            pointHoverRadius: 0,
            tension: 0.35,
            cubicInterpolationMode: "monotone",
          },
          {
            label: "avg",
            data: avgPoints.filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y)),
            borderColor: "#86dcff",
            backgroundColor: "transparent",
            borderWidth: 2,
            fill: false,
            pointRadius: avgPoints.length === 1 ? 3 : 0,
            pointHoverRadius: 5,
            pointHoverBackgroundColor: "#86dcff",
            pointHoverBorderColor: "rgba(9,14,24,0.9)",
            pointHoverBorderWidth: 2.5,
            tension: 0.42,
            cubicInterpolationMode: "monotone",
          },
        ];

    hostMetricsChartInstance = new Chart(hostMetricsChart.getContext("2d"), {
      type: "line",
      data: { datasets },
      plugins: [window.cncMetricEventMarkers?.plugin].filter(Boolean),
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 250, easing: "easeOutQuart" },
        interaction: { mode: "index", intersect: false, axis: "x" },
        plugins: {
          cncMetricEventMarkers: {
            events: chartEvents,
            formatTimestamp: hostTooltipTitleForTime,
          },
          legend: {
            display: metricKey === "network",
            labels: {
              color: "rgba(220,231,253,0.68)",
              boxWidth: 10,
              boxHeight: 10,
              usePointStyle: true,
              pointStyle: "line",
              font: { family: "monospace", size: 11, weight: "600" },
            },
          },
          tooltip: {
            mode: "index",
            intersect: false,
            axis: "x",
            backgroundColor: "rgba(9,14,24,0.96)",
            borderColor: "rgba(146,174,226,0.22)",
            borderWidth: 1,
            titleColor: "rgba(220,231,253,0.55)",
            bodyColor: "#f4f8ff",
            titleFont: { family: "monospace", size: 11 },
            bodyFont: { family: "monospace", size: 13, weight: "600" },
            padding: { x: 12, y: 8 },
            cornerRadius: 10,
            displayColors: metricKey === "network" && hasDirectionalNetwork,
            filter: (item) => {
              const label = String(item.dataset?.label || "");
              return metricKey === "network" && hasDirectionalNetwork
                ? (label === "down" || label === "up")
                : (label === "avg" || label === "total");
            },
            callbacks: {
              title(items) {
                const timestamp = Number(items?.[0]?.parsed?.x);
                if (!Number.isFinite(timestamp)) return "";
                return hostTooltipTitleForTime(timestamp);
              },
              label(item) {
                if (metricKey === "network") {
                  const label = item.dataset?.label || "";
                  return `${label} ${formatHostMetricForPayload(item.parsed.y, unitKind)}`;
                }
                return formatHostMetricForPayload(item.parsed.y, unitKind);
              },
            },
          },
        },
        scales: {
          x: {
            type: "linear",
            min: xMin,
            max: xMax,
            grid: { color: "rgba(146,174,226,0.08)" },
            ticks: {
              color: "rgba(220,231,253,0.56)",
              font: { family: "monospace", size: 11 },
              maxTicksLimit: 5,
              callback(value) {
                return hostTickLabelForTime(Number(value), timeframeKey);
              },
            },
            border: { display: false },
          },
          y: {
            min: yAxisMin,
            max: yAxisMax,
            grid: { color: "rgba(146,174,226,0.08)" },
            ticks: {
              maxTicksLimit: 5,
              color: "rgba(220,231,253,0.56)",
              font: { family: "monospace", size: 11 },
              callback(value) {
                return formatHostMetricForPayload(Number(value), unitKind);
              },
            },
            border: { display: false },
          },
        },
      },
    });
  };

  const showHostMetricHistoryError = () => {
    lastHostMetricPayload = null;
    if (renderLiveHostMetricsFallback("Host metric history could not be loaded. Showing live samples.")) return;
    if (!hostMetricsEmpty) return;
    hostMetricsEmpty.hidden = false;
    hostMetricsEmpty.textContent = "Host metric history could not be loaded.";
  };

  const loadHostMetricHistory = async () => {
    refreshHostMetricRefs();
    if (!hostMetricSelect || !hostTimeframeSelect || !hostMetricsChart || !hostMetricsEmpty) return;
    const requestId = ++hostMetricsRequestId;
    hostMetricsAbortController?.abort();
    const controller = new AbortController();
    hostMetricsAbortController = controller;
    try {
      await ensureMetricChartAssets();
      if (controller.signal.aborted || requestId !== hostMetricsRequestId) return;
      renderLiveHostMetricsFallback("Loading host metric history.");
      const chartWidth = Math.round(
        hostMetricsChart.parentElement?.clientWidth || hostMetricsChart.clientWidth || 720
      );
      const query = new URLSearchParams({
        metric: hostMetricSelect.value,
        timeframe: hostTimeframeSelect.value,
        width: String(chartWidth),
      });
      const response = await fetch(`/api/host/metrics-history?${query.toString()}`, {
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`host metric history request failed: ${response.status}`);
      const payload = await response.json();
      if (requestId !== hostMetricsRequestId) return;
      renderHostMetricsPayload(payload);
    } catch (error) {
      if (error?.name === "AbortError") return;
      if (requestId === hostMetricsRequestId) showHostMetricHistoryError();
    } finally {
      if (hostMetricsAbortController === controller) hostMetricsAbortController = null;
    }
  };

  const settingsMetricsPanelLoaded = () => {
    refreshHostMetricRefs();
    const panel = hostMetricsShell?.closest('[data-panel="settings"]');
    return Boolean(hostMetricsShell && panel?.dataset.loaded === "1");
  };
  const settingsMetricsPanelActive = () => (
    (document.querySelector("[data-tab].is-active")?.dataset.tab || "") === "settings"
    && settingsMetricsPanelLoaded()
  );

  const reloadSelectedHostMetricHistory = () => {
    refreshHostMetricRefs();
    lastHostMetricPayload = null;
    loadHostMetricHistory();
  };

  const bindHostMetricControls = (root = document) => {
    refreshHostMetricRefs();
    if (!settingsMetricsPanelLoaded()) return;
    const scopedSelect = root.querySelector?.("#host-metric-select") || hostMetricSelect;
    const scopedTimeframe = root.querySelector?.("#host-timeframe-select") || hostTimeframeSelect;
    if (scopedSelect && scopedSelect.dataset.boundHostMetrics !== "1") {
      scopedSelect.dataset.boundHostMetrics = "1";
      scopedSelect.addEventListener("change", reloadSelectedHostMetricHistory);
    }
    if (scopedTimeframe && scopedTimeframe.dataset.boundHostMetrics !== "1") {
      scopedTimeframe.dataset.boundHostMetrics = "1";
      scopedTimeframe.addEventListener("change", reloadSelectedHostMetricHistory);
    }
  };

  const initializeHostMetricsPanel = async () => {
    refreshHostMetricRefs();
    if (!settingsMetricsPanelActive()) return;
    bindHostMetricControls(document);
    if (hostMetricsShell.dataset.hostMetricsInitialized === "1") return;
    try {
      await ensureMetricChartAssets();
      if (!settingsMetricsPanelActive()) return;
      hostMetricsShell.dataset.hostMetricsInitialized = "1";
      renderHostMetricsFromStatus(JSON.parse(hostMetricsShell.dataset.hostMetrics || "{}"));
    } catch (error) {
      if (!settingsMetricsPanelActive()) return;
      hostMetricsShell.dataset.hostMetricsInitialized = "1";
      console.error("host metric bootstrap failed", error);
      renderHostMetricsFromStatus({});
    }
    if (!settingsMetricsPanelActive()) return;
    loadHostMetricHistory();
  };

  const renderHostMetricsFromStatus = (metrics) => {
    refreshHostMetricRefs();
    if (!hostMetricsShell) return;
    const nextMetrics = metrics && typeof metrics === "object" ? metrics : {};
    lastHostMetrics = nextMetrics;
    pushHostMetricSample(nextMetrics);
    const metricKey = selectedHostMetricKey();
    const priorSummary = hostPayloadMatchesCurrentSelection(lastHostMetricPayload) && lastHostMetricPayload?.summary
      ? lastHostMetricPayload.summary
      : {};
    const latestValue = hostMetricValue(nextMetrics, metricKey) ?? priorSummary.latest_value ?? null;
    if (hostPayloadMatchesCurrentSelection(lastHostMetricPayload)) {
      const liveTimestamp = Date.now();
      const series = Array.isArray(lastHostMetricPayload.series)
        ? lastHostMetricPayload.series.map((point) => ({ ...point }))
        : [];
      if (latestValue !== null) {
        const livePoint = {
          timestamp: new Date(liveTimestamp).toISOString(),
          value: latestValue,
          avg: latestValue,
          min: latestValue,
          max: latestValue,
          visual_min: latestValue,
          visual_max: latestValue,
          last: latestValue,
          count: 1,
          is_live: true,
        };
        if (metricKey === "network") {
          const rx = Number(nextMetrics.network_rx_bps);
          const tx = Number(nextMetrics.network_tx_bps);
          if (Number.isFinite(rx)) livePoint.network_rx_bps = rx;
          if (Number.isFinite(tx)) livePoint.network_tx_bps = tx;
        }
        const lastPointTime = Date.parse(series[series.length - 1]?.timestamp || "");
        if (Number.isFinite(lastPointTime) && Math.abs(liveTimestamp - lastPointTime) < 1000) {
          series[series.length - 1] = livePoint;
        } else {
          series.push(livePoint);
        }
      }
      renderHostMetricsPayload({
        ...lastHostMetricPayload,
        range_end_at: new Date(Math.max(liveTimestamp, Date.parse(lastHostMetricPayload.range_end_at || "") || 0)).toISOString(),
        series,
        summary: {
          latest_value: latestValue,
          peak_value: priorSummary.peak_value ?? latestValue,
          average_value: priorSummary.average_value ?? latestValue,
          point_count: series.length,
        },
      }, { allowFallback: false, preservePayload: true });
      return;
    }
    renderLiveHostMetricsFallback();
  };

  const backendNameFromService = (service) => {
    const explicit = String(service?.backend || "").trim();
    if (explicit) return explicit;
    const serviceName = String(service?.service || "").trim();
    return serviceName.startsWith("cnc-app-") ? serviceName.slice("cnc-app-".length) : "";
  };

  const renderOutputsFromStatus = (payload) => {
    const rows = Array.from(document.querySelectorAll("[data-output-row]"));
    if (!rows.length) return;
    const services = Array.isArray(payload?.services) ? payload.services : [];
    const serviceMap = new Map();
    services.forEach((service) => {
      const backendName = backendNameFromService(service);
      if (backendName) serviceMap.set(backendName, service);
    });

    let unhealthyCount = 0;
    let enabledCount = 0;
    rows.forEach((row) => {
      const backendName = row.dataset.outputName || "";
      const kind = row.dataset.outputKind || "app";
      const enabled = row.dataset.outputEnabled === "1";
      if (enabled) enabledCount += 1;
      const service = serviceMap.get(backendName);
      const activeState = enabled && (kind === "app" || kind === "shield")
        ? String(service?.data?.ActiveState || "unknown").toLowerCase()
        : "";
      const runtimeValue = !enabled
        ? "not enabled"
        : (kind === "static"
          ? "healthy"
          : (kind === "shield"
            ? (activeState && activeState !== "active" ? "unhealthy" : "healthy")
            : (activeState === "active"
              ? "healthy"
              : "unhealthy")));
      const runtimeTone = !enabled
        ? "inactive"
        : (kind === "static"
          ? "success"
          : (runtimeValue === "unhealthy"
            ? "error"
            : "success"));
      if (runtimeValue === "unhealthy") {
        unhealthyCount += 1;
      }

      const runtimePill = row.querySelector("[data-output-runtime-pill]");
      if (runtimePill) {
        runtimePill.className = `pill ${runtimeTone}`;
        runtimePill.textContent = runtimeValue || "unknown";
      }

      const cpuPercent = averageOutputLoad(`${backendName}:cpu`, Number(service?.metrics?.cpu_percent));
      const memoryPercent = averageOutputLoad(`${backendName}:memory`, Number(service?.metrics?.memory_percent));
      const cpuMeter = outputLoadMeter(cpuPercent, {
        enabled,
        kind,
        serviceState: activeState,
      });
      const memoryMeter = outputLoadMeter(memoryPercent, {
        enabled,
        kind,
        serviceState: activeState,
      });
      const cpuLabel = row.querySelector("[data-output-cpu-label]");
      const cpuFill = row.querySelector("[data-output-cpu-fill]");
      const memoryLabel = row.querySelector("[data-output-memory-label]");
      const memoryFill = row.querySelector("[data-output-memory-fill]");
      if (cpuLabel) cpuLabel.textContent = cpuMeter.label;
      if (memoryLabel) memoryLabel.textContent = memoryMeter.label;
      row.querySelector("[data-output-cpu]")?.classList.toggle("is-compact", Boolean(cpuMeter.compact));
      row.querySelector("[data-output-memory]")?.classList.toggle("is-compact", Boolean(memoryMeter.compact));
      setMeterFill(cpuFill, cpuMeter.tone, cpuMeter.percent);
      setMeterFill(memoryFill, memoryMeter.tone, memoryMeter.percent);
    });

    const enabledSummary = document.querySelector("[data-outputs-enabled-summary]");
    if (enabledSummary) {
      enabledSummary.textContent = `${enabledCount}/${rows.length} enabled`;
    }
    const unhealthySummary = document.querySelector("[data-outputs-unhealthy-summary]");
    if (unhealthySummary) {
      unhealthySummary.textContent = `${unhealthyCount} unhealthy`;
    }
  };

  const renderHomeFromStatus = (payload) => {
    const overview = payload?.dashboard_overview;
    if (!overview || typeof overview !== "object") return;

    const inputsEnabled = document.querySelector("[data-home-inputs-enabled]");
    const inputsTotal = document.querySelector("[data-home-inputs-total]");
    const routes = document.querySelector("[data-home-routes]");
    const backendsEnabled = document.querySelector("[data-home-backends-enabled]");
    const backendsDetail = document.querySelector("[data-home-backends-detail]");
    if (inputsEnabled) inputsEnabled.textContent = String(overview.inputs_enabled ?? 0);
    if (inputsTotal) inputsTotal.textContent = `${overview.inputs_total ?? 0} configured`;
    if (routes) routes.textContent = `${overview.routes_active ?? 0}/${overview.routes_total ?? 0}`;
    if (backendsEnabled) backendsEnabled.textContent = String(overview.backends_enabled ?? 0);

    const enabledApps = Number(overview.app_backends_enabled ?? 0);
    const appStatuses = Array.isArray(payload?.services)
      ? payload.services.filter((item) => typeof item?.backend === "string")
      : [];
    if (backendsDetail) {
      backendsDetail.textContent = enabledApps > 0
        ? `${appStatuses.filter((item) => item.ok === true).length}/${enabledApps} apps healthy`
        : `${overview.backends_total ?? 0} configured`;
    }

    const isolation = document.querySelector("[data-home-isolation]");
    if (isolation) {
      const isolationStatus = payload?.app_network_isolation;
      const leaks = Array.isArray(isolationStatus?.leaks) ? isolationStatus.leaks : [];
      const known = isolationStatus?.checked === true;
      const healthy = known && isolationStatus?.ok === true;
      isolation.textContent = healthy ? "OK" : known && leaks.length ? "warning" : "unknown";
      isolation.classList.remove("success", "warn");
      isolation.classList.add(healthy ? "success" : "warn");
    }
  };

  const scheduleServerLoadPoll = () => {
    if (serverLoadPollTimer) window.clearTimeout(serverLoadPollTimer);
    serverLoadPollTimer = window.setTimeout(pollServerLoadStatus, SERVER_LOAD_POLL_INTERVAL_MS);
  };

  const pollServerLoadStatus = async ({ forceRefresh = false, reschedule = true } = {}) => {
    const activeTab = document.querySelector("[data-tab].is-active")?.dataset.tab || "";
    if (!STATUS_POLL_TABS.has(activeTab)) {
      if (reschedule) scheduleServerLoadPoll();
      return;
    }
    if (activeTab === "settings" && updateFlowActive()) {
      if (reschedule) scheduleServerLoadPoll();
      return;
    }
    if (serverLoadPollInFlight || document.hidden || hostMutationUiBusy > 0) {
      if (reschedule) scheduleServerLoadPoll();
      return;
    }
    serverLoadPollInFlight = true;
    try {
      const statusPath = forceRefresh ? "/api/status?force_refresh=true" : "/api/status";
      const response = await fetch(statusPath, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      const payload = await readJsonResponse(response, "Status refresh failed");
      if (!response.ok) {
        throw new Error(payload.detail || `${response.status} ${response.statusText}`.trim());
      }
      renderHostMetricsFromStatus(payload.host_metrics);
      renderOutputsFromStatus(payload);
      if (activeTab === "home") {
        renderHomeFromStatus(payload);
      }
      if (activeTab === "settings") {
        renderUpdateVersion(payload.update_version);
        renderUpdate(payload.last_update);
      }
    } catch (error) {
      console.error("server load poll failed", error);
    } finally {
      serverLoadPollInFlight = false;
      if (reschedule) scheduleServerLoadPoll();
    }
  };

  const refreshVisibleTabStatus = () => {
    const activeTab = document.querySelector("[data-tab].is-active")?.dataset.tab || "";
    if (!STATUS_POLL_TABS.has(activeTab)) return;
    const now = Date.now();
    if (now - (tabStatusRefreshTimes[activeTab] || 0) < TAB_STATUS_REFRESH_MIN_INTERVAL_MS) return;
    tabStatusRefreshTimes[activeTab] = now;
    pollServerLoadStatus({ reschedule: false });
    scheduleServerLoadPoll();
  };

  const routeNodeId = (kind, value) => `${kind}:${encodeURIComponent(String(value || ""))}`;

  const routeStateLabel = (state) => {
    if (state === "active") return "active";
    if (state === "error") return "conflict";
    return "inactive";
  };

  const clippedRouteLabel = (value, maxLength) => {
    const text = String(value || "").trim();
    if (!text) return "";
    return text.length > maxLength ? `${text.slice(0, maxLength - 3)}...` : text;
  };

  const routeInputLabel = (row) => clippedRouteLabel(row?.input_value, 42) || "input";

  const routeOutputLabel = (row) => clippedRouteLabel(row?.backend_name, 28) || "none";

  const estimateRouteNodeHeight = (label, width) => {
    const text = String(label || "").trim();
    if (!text) return 58;
    const usableChars = Math.max(10, Math.floor((width - 32) / 7.2));
    let lines = 1;
    let currentLine = 0;
    text.split(/\s+/).filter(Boolean).forEach((word) => {
      const length = word.length;
      if (length > usableChars) {
        if (currentLine > 0) {
          lines += 1;
          currentLine = 0;
        }
        lines += Math.ceil(length / usableChars) - 1;
        currentLine = length % usableChars || usableChars;
        return;
      }
      if (currentLine === 0) {
        currentLine = length;
      } else if (currentLine + 1 + length <= usableChars) {
        currentLine += 1 + length;
      } else {
        lines += 1;
        currentLine = length;
      }
    });
    return Math.max(58, Math.min(128, 30 + (lines * 18)));
  };

  const routeGraphNodeCounts = (rows) => {
    const inputs = new Set();
    const outputs = new Set();
    rows.forEach((row) => {
      inputs.add(routeNodeId("input", row?.input_value));
      const outputName = String(row?.backend_name || "none").trim() || "none";
      outputs.add(routeNodeId("output", outputName));
    });
    return { inputs: inputs.size, outputs: outputs.size };
  };

  const sizeRouteGraphCanvas = (canvas, rows) => {
    if (!canvas) return;
    const { inputs, outputs } = routeGraphNodeCounts(rows);
    const rowCount = Math.max(inputs, outputs, 1);
    const desiredHeight = Math.min(1380, Math.max(430, 104 + (rowCount * 116)));
    canvas.style.minHeight = `${desiredHeight}px`;
  };

  const routeGraphRenderKey = (rows, canvas) => {
    const width = Math.round(canvas?.clientWidth || 0);
    return `${width}:${JSON.stringify(rows)}`;
  };

  stopRouteGraphFlow = (shell) => {
    if (shell?._routeGraphFlowFrame) {
      window.cancelAnimationFrame(shell._routeGraphFlowFrame);
      shell._routeGraphFlowFrame = null;
    }
    if (shell?._routeGraphSettleTimer) {
      window.clearTimeout(shell._routeGraphSettleTimer);
      shell._routeGraphSettleTimer = null;
    }
  };

  const startRouteGraphFlow = (shell) => {
    if (!shell?._routeGraph || shell._routeGraphFlowFrame) return;
    const flowEdges = shell._routeGraph.edges(".route-flow");
    if (!flowEdges.length) return;
    let offset = shell._routeGraphFlowOffset || 0;
    const animateFlow = () => {
      offset = (offset - 0.32) % 26;
      shell._routeGraphFlowOffset = offset;
      flowEdges.style("line-dash-offset", offset);
      shell._routeGraphFlowFrame = window.requestAnimationFrame(animateFlow);
    };
    animateFlow();
  };

  const routeGraphTaxiTurn = () => "50%";

  const buildRouteGraphElements = (rows, canvas) => {
    const nodes = new Map();
    const edges = [];
    const width = Math.max(canvas?.clientWidth || 720, 360);
    const height = Math.max(canvas?.clientHeight || 360, 320);
    const minGroupGap = 260;
    const minSideClearance = 52;
    const nodeWidthLimit = (width - minGroupGap - (minSideClearance * 2)) / 2;
    const longestLabelLength = rows.reduce((longest, row) => {
      return Math.max(longest, routeInputLabel(row).length, routeOutputLabel(row).length);
    }, 0);
    const desiredNodeWidth = Math.max(156, Math.min(280, 46 + (Math.min(longestLabelLength, 34) * 6.4)));
    const maxNodeWidth = Math.max(136, Math.min(width * 0.3, 280, Math.max(136, nodeWidthLimit)));
    const nodeWidth = Math.max(136, Math.min(maxNodeWidth, desiredNodeWidth));
    const labelMaxWidth = Math.max(104, nodeWidth - 34);

    rows.forEach((row, index) => {
      const inputId = routeNodeId("input", row.input_value);
      const outputName = String(row.backend_name || "none").trim() || "none";
      const outputId = routeNodeId("output", outputName);
      const state = String(row.route_state || "inactive");
      const inputLabel = routeInputLabel(row);
      const outputLabel = routeOutputLabel(row);
      const inputHeight = estimateRouteNodeHeight(inputLabel, labelMaxWidth);
      const outputHeight = estimateRouteNodeHeight(outputLabel, labelMaxWidth);
      const taxiTurn = routeGraphTaxiTurn();

      if (!nodes.has(inputId)) {
        nodes.set(inputId, {
          data: {
            id: inputId,
            label: inputLabel,
            type: "input",
            enabled: Boolean(row.input_enabled),
            kind: String(row.input_kind || "input"),
            height: inputHeight,
            width: nodeWidth,
            labelMaxWidth,
          },
          classes: `input-node ${row.input_enabled ? "enabled" : "disabled"}`,
        });
      }
      if (!nodes.has(outputId)) {
        nodes.set(outputId, {
          data: {
            id: outputId,
            label: outputLabel,
            type: "output",
            enabled: Boolean(row.backend_enabled),
            kind: String(row.kind || "output"),
            height: outputHeight,
            width: nodeWidth,
            labelMaxWidth,
          },
          classes: `output-node ${row.backend_enabled ? "enabled" : "disabled"}`,
        });
      }

      edges.push({
        data: {
          id: `route-edge-${index}`,
          source: inputId,
          target: outputId,
          label: routeStateLabel(state),
          state,
          taxiTurn,
        },
        classes: state,
      });
      if (state === "active") {
        edges.push({
          data: {
            id: `route-flow-edge-${index}`,
            source: inputId,
            target: outputId,
            state,
            taxiTurn,
          },
          classes: "route-flow",
        });
      }
    });
    const nodeElements = [...nodes.values()];
    const inputNodes = nodeElements.filter((node) => node.data.type === "input");
    const outputNodes = nodeElements.filter((node) => node.data.type === "output");
    const maxNodeHeight = Math.max(58, ...nodeElements.map((node) => Number(node.data.height) || 58));
    const outputStats = new Map();
    const inputStats = new Map();
    rows.forEach((row, index) => {
      const inputId = routeNodeId("input", row.input_value);
      const outputName = String(row.backend_name || "none").trim() || "none";
      const outputId = routeNodeId("output", outputName);
      const state = String(row.route_state || "inactive");
      const stateRank = state === "active" ? 0 : (state === "error" ? 1 : 2);
      const outputRank = outputStats.get(outputId) || {
        firstIndex: index,
        stateRank,
        enabled: Boolean(row.backend_enabled),
        routeCount: 0,
        isNone: outputName === "none",
      };
      outputRank.firstIndex = Math.min(outputRank.firstIndex, index);
      outputRank.stateRank = Math.min(outputRank.stateRank, stateRank);
      outputRank.enabled = outputRank.enabled || Boolean(row.backend_enabled);
      outputRank.routeCount += 1;
      outputStats.set(outputId, outputRank);

      const inputRank = inputStats.get(inputId) || { firstIndex: index, stateRank, outputId };
      if (stateRank < inputRank.stateRank) {
        inputRank.stateRank = stateRank;
        inputRank.outputId = outputId;
      }
      inputStats.set(inputId, inputRank);
    });
    outputNodes.sort((a, b) => {
      const aStats = outputStats.get(a.data.id) || {};
      const bStats = outputStats.get(b.data.id) || {};
      const aDisabledRank = aStats.isNone || !aStats.enabled ? 1 : 0;
      const bDisabledRank = bStats.isNone || !bStats.enabled ? 1 : 0;
      return (
        aDisabledRank - bDisabledRank ||
        (aStats.stateRank ?? 2) - (bStats.stateRank ?? 2) ||
        (aStats.firstIndex ?? 0) - (bStats.firstIndex ?? 0)
      );
    });
    const outputOrder = new Map(outputNodes.map((node, index) => [node.data.id, index]));
    inputNodes.sort((a, b) => {
      const aStats = inputStats.get(a.data.id) || {};
      const bStats = inputStats.get(b.data.id) || {};
      return (
        (outputOrder.get(aStats.outputId) ?? 999) - (outputOrder.get(bStats.outputId) ?? 999) ||
        (aStats.stateRank ?? 2) - (bStats.stateRank ?? 2) ||
        (aStats.firstIndex ?? 0) - (bStats.firstIndex ?? 0)
      );
    });
    const laneInset = Math.max((nodeWidth / 2) + minSideClearance, Math.min(width * 0.2, 280));
    const leftX = laneInset;
    const rightX = Math.max(width - laneInset, leftX + nodeWidth + minGroupGap);
    const columnBounds = () => {
      const top = Math.max(86, maxNodeHeight + 34);
      const bottom = Math.max(top, height - top);
      return { top, bottom };
    };
    const positionColumn = (items, x) => {
      const { top, bottom } = columnBounds();
      const available = Math.max(0, bottom - top);
      const rowGap = items.length > 1 ? Math.max(maxNodeHeight + 48, available / (items.length - 1)) : 0;
      const startY = items.length > 1 ? (height - (rowGap * (items.length - 1))) / 2 : height / 2;
      items.forEach((node, index) => {
        node.position = {
          x,
          y: startY + (index * rowGap),
        };
      });
    };
    const positionOutputsByInputCenter = () => {
      const { top, bottom } = columnBounds();
      const minGap = maxNodeHeight + 28;
      const preferredY = new Map(outputNodes.map((node) => [node.data.id, []]));
      rows.forEach((row) => {
        const inputNode = nodes.get(routeNodeId("input", row.input_value));
        const outputName = String(row.backend_name || "none").trim() || "none";
        const outputId = routeNodeId("output", outputName);
        if (inputNode?.position && preferredY.has(outputId)) {
          preferredY.get(outputId).push(inputNode.position.y);
        }
      });
      outputNodes.forEach((node) => {
        const values = preferredY.get(node.data.id) || [];
        const average = values.length
          ? values.reduce((total, value) => total + value, 0) / values.length
          : height / 2;
        node.position = {
          x: rightX,
          y: Math.max(top, Math.min(bottom, average)),
        };
      });
      outputNodes.sort((a, b) => a.position.y - b.position.y);
      for (let index = 1; index < outputNodes.length; index += 1) {
        const previous = outputNodes[index - 1];
        const current = outputNodes[index];
        current.position.y = Math.max(current.position.y, previous.position.y + minGap);
      }
      if (outputNodes.length) {
        const overflow = outputNodes[outputNodes.length - 1].position.y - bottom;
        if (overflow > 0) {
          outputNodes.forEach((node) => {
            node.position.y -= overflow;
          });
        }
        outputNodes[0].position.y = Math.max(top, outputNodes[0].position.y);
      }
    };
    positionColumn(inputNodes, leftX);
    positionOutputsByInputCenter();
    return [...nodeElements, ...edges];
  };

  renderRouteGraphs = (root = document) => {
    root.querySelectorAll("[data-route-graph]").forEach((shell) => {
      const canvas = shell.querySelector("[data-route-graph-canvas]");
      const empty = shell.querySelector("[data-route-graph-empty]");
      if (!canvas) return;

      let rows = [];
      try {
        rows = JSON.parse(shell.dataset.routes || "[]");
      } catch (error) {
        rows = [];
      }
      if (!Array.isArray(rows) || rows.length === 0) {
        if (shell._routeGraph) {
          stopRouteGraphFlow(shell);
          shell._routeGraph.destroy();
          shell._routeGraph = null;
          shell._routeGraphRenderKey = null;
        }
        canvas.hidden = true;
        if (empty) {
          empty.hidden = false;
          empty.textContent = "No routes yet.";
        }
        return;
      }

      if (empty) empty.hidden = true;
      canvas.hidden = false;
      sizeRouteGraphCanvas(canvas, rows);

      if (typeof window.cytoscape !== "function") {
        if (empty) {
          empty.hidden = false;
          empty.textContent = "Loading route graph...";
        }
        ensureRouteGraphAsset()
          .then(() => renderRouteGraphs(root))
          .catch(() => {
            if (empty) {
              empty.hidden = false;
              empty.textContent = "Route graph unavailable.";
            }
          });
        return;
      }
      if (canvas.clientWidth === 0 || canvas.clientHeight === 0) return;
      const graphPadding = Math.max(28, Math.min(canvas.clientWidth * 0.04, 64));
      const renderKey = routeGraphRenderKey(rows, canvas);
      if (shell._routeGraph && shell._routeGraphRenderKey === renderKey) {
        shell._routeGraph.resize();
        shell._routeGraph.fit(shell._routeGraph.elements(), graphPadding);
        startRouteGraphFlow(shell);
        return;
      }
      if (shell._routeGraph) {
        stopRouteGraphFlow(shell);
        shell._routeGraph.destroy();
        shell._routeGraph = null;
        shell._routeGraphRenderKey = null;
      }

      shell._routeGraph = window.cytoscape({
        container: canvas,
        elements: buildRouteGraphElements(rows, canvas),
        boxSelectionEnabled: false,
        autoungrabify: true,
        autounselectify: true,
        style: [
          {
            selector: "node",
            style: {
              "background-color": "#121d2d",
              "background-opacity": 0.92,
              "border-color": "rgba(156, 193, 255, 0.28)",
              "border-width": 1.2,
              color: "#f7fbff",
              "font-family": "Avenir Next, Segoe UI, Helvetica Neue, sans-serif",
              "font-size": 13,
              "font-weight": 700,
              height: "data(height)",
              label: "data(label)",
              padding: "14px",
              shape: "round-rectangle",
              "text-halign": "center",
              "text-justification": "center",
              "text-max-width": "data(labelMaxWidth)",
              "text-overflow-wrap": "anywhere",
              "text-wrap": "wrap",
              "text-valign": "center",
              width: "data(width)",
            },
          },
          {
            selector: ".input-node",
            style: {
              "background-color": "#10233b",
              "border-color": "#74d6ff",
            },
          },
          {
            selector: ".output-node",
            style: {
              "background-color": "#0f2d26",
              "border-color": "#90f3c7",
            },
          },
          {
            selector: "node.disabled",
            style: {
              "background-color": "#101723",
              "border-color": "rgba(158, 175, 205, 0.36)",
              color: "#9eafcd",
            },
          },
          {
            selector: "edge",
            style: {
              "curve-style": "taxi",
              "line-color": "rgba(158, 175, 205, 0.48)",
              "target-arrow-color": "rgba(158, 175, 205, 0.48)",
              "target-arrow-shape": "triangle",
              "taxi-direction": "horizontal",
              "taxi-turn": "data(taxiTurn)",
              "taxi-turn-min-distance": 18,
              width: 2,
            },
          },
          {
            selector: "edge.route-flow",
            style: {
              "curve-style": "taxi",
              "line-color": "#d6fff0",
              "line-dash-pattern": [10, 16],
              "line-dash-offset": 0,
              "line-style": "dashed",
              "target-arrow-shape": "none",
              "taxi-direction": "horizontal",
              "taxi-turn": "data(taxiTurn)",
              "taxi-turn-min-distance": 18,
              opacity: 0.72,
              width: 4,
              "z-index": 8,
            },
          },
          {
            selector: "edge.active",
            style: {
              "line-color": "#9bf0c7",
              "target-arrow-color": "#9bf0c7",
              width: 3,
            },
          },
          {
            selector: "edge.error",
            style: {
              "line-color": "#ff969b",
              "target-arrow-color": "#ff969b",
              width: 3,
            },
          },
        ],
        layout: {
          name: "preset",
          fit: true,
          padding: graphPadding,
        },
        maxZoom: 0.84,
        minZoom: 0.25,
        panningEnabled: true,
        userPanningEnabled: true,
        userZoomingEnabled: false,
      });
      shell._routeGraph.resize();
      shell._routeGraph.fit(shell._routeGraph.elements(), graphPadding);
      shell._routeGraphRenderKey = renderKey;
      shell._routeGraph.nodes().ungrabify();
      const homePan = { ...shell._routeGraph.pan() };
      shell._routeGraph.on("pan", () => {
        if (shell._routeGraphSettling) return;
        if (shell._routeGraphSettleTimer) window.clearTimeout(shell._routeGraphSettleTimer);
        shell._routeGraphSettleTimer = window.setTimeout(() => {
          if (!shell._routeGraph) return;
          shell._routeGraphSettling = true;
          shell._routeGraph.animate(
            { pan: homePan },
            {
              duration: 520,
              easing: "ease-out",
              complete: () => {
                shell._routeGraphSettling = false;
              },
            },
          );
        }, 700);
      });

      startRouteGraphFlow(shell);
    });
  };

  const createOutputProgressSteps = dashboardConfig.createOutputProgressSteps || [];
  const operationProgressPipelines = dashboardConfig.operationProgressPipelines || {};

  const saveProgressPipelines = {
    ...(dashboardConfig.inputProgressPipelines || {}),
    ...operationProgressPipelines,
    createOutput: createOutputProgressSteps,
  };

  const saveVisualProgress = (progress, steps, activeIndex, tone) => {
    const numericProgress = Math.max(0, Math.min(100, Number(progress) || 0));
    if (tone !== "running") return numericProgress;
    return Math.max(4, Math.min(96, numericProgress));
  };

  const showFloatingSave = ({ tone = "queued", title, headline, substep = "", note, meta = [], progress = 8, steps = [], activeIndex = 0 }) => {
    if (!floatingSaveShell || !floatingSaveCard) return;
    if (tone === "success") clearBanners({ successOnly: true });
    floatingSaveShell.hidden = false;
    floatingSaveCard.hidden = false;
    floatingSaveCard.className = `save-job-card floating-save-card ${tone}`;
    const activeStep = steps[Math.min(activeIndex, Math.max(steps.length - 1, 0))] || {};
    const actionLabel = title || headline || activeStep.headline || "Saving";
    const mainStage = headline || activeStep.headline || actionLabel;
    const subStage = tone === "success"
      ? (note || substep || "")
      : (substep || activeStep.substep || note || "");
    if (floatingSaveAction) {
      floatingSaveAction.textContent = actionLabel;
      floatingSaveAction.hidden = !actionLabel;
    }
    if (floatingSaveStage) {
      floatingSaveStage.textContent = mainStage;
      floatingSaveStage.hidden = !mainStage;
    }
    if (floatingSaveNote) {
      const statusText = subStage || "";
      floatingSaveNote.textContent = statusText;
      floatingSaveNote.hidden = !statusText;
    }
    if (floatingSaveProgressFill) {
      const boundedProgress = saveVisualProgress(progress, steps, activeIndex, tone);
      floatingSaveProgressFill.style.setProperty("--save-progress", `${boundedProgress}%`);
    }
    if (floatingSaveMeta) {
      floatingSaveMeta.replaceChildren();
      meta.forEach((item) => {
        if (!item || !item.label || !item.value) return;
        const pill = document.createElement("span");
        pill.className = "summary-pill";
        const label = document.createElement("span");
        label.className = "metric-label";
        label.textContent = item.label;
        pill.appendChild(label);
        pill.append(` ${item.value}`);
        floatingSaveMeta.appendChild(pill);
      });
    }
  };

  const showInitialFloatingSaveProgress = ({ title, pipeline = "saveInput" }) => {
    const steps = saveProgressPipelines[pipeline] || saveProgressPipelines.saveInput;
    const step = steps[0] || {};
    showFloatingSave({
      tone: "running",
      title,
      headline: step.headline,
      substep: step.substep,
      note: step.note,
      progress: Number(step.progress) || 4,
      steps,
      activeIndex: 0,
    });
  };

  const hideFloatingSave = () => {
    if (!floatingSaveShell) return;
    floatingSaveShell.hidden = true;
  };

  const beginHostMutationUi = () => {
    hostMutationUiBusy += 1;
  };

  const endHostMutationUi = () => {
    hostMutationUiBusy = Math.max(0, hostMutationUiBusy - 1);
  };

  const refreshCsrfTokens = async () => {
    const response = await fetch("/api/csrf", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    const payload = await response.json().catch(() => ({}));
    const token = String(payload.csrf_token || "").trim();
    if (!response.ok || !token) return "";
    document.querySelectorAll("input[name='csrf_token']").forEach((input) => {
      input.value = token;
    });
    return token;
  };

  const formatDuration = (value) => {
    const seconds = Number(value);
    if (!Number.isFinite(seconds)) return "";
    if (seconds < 1) return "<1s";
    if (seconds < 10) return `${seconds.toFixed(1)}s`;
    return `${Math.round(seconds)}s`;
  };

  const lastApplySummary = (lastApply) => {
    const details = lastApply?.details || {};
    if (lastApply?.status === "error" && details.phase === "app_healthcheck") {
      const parts = [];
      if (details.backend) parts.push(`${details.backend} healthcheck`);
      else parts.push("healthcheck");
      if (details.http_status) parts.push(`returned HTTP ${details.http_status}`);
      else if (details.error) parts.push(details.error);
      if (details.url) parts.push(`at ${details.url}`);
      if (details.host_header) parts.push(`with Host ${details.host_header}`);
      return parts.join(" ");
    }
    if (details.phase && details.error) return `${details.phase}: ${details.error}`;
    const counts = [];
    if (Array.isArray(details.nginx_files) && details.nginx_files.length) {
      counts.push(`${details.nginx_files.length} nginx change${details.nginx_files.length === 1 ? "" : "s"}`);
    }
    if (Array.isArray(details.route_contracts) && details.route_contracts.length) {
      counts.push(`${details.route_contracts.length} route${details.route_contracts.length === 1 ? "" : "s"}`);
    }
    if (Array.isArray(details.tailscale_paths) && details.tailscale_paths.length) {
      counts.push(`${details.tailscale_paths.length} tailnet path${details.tailscale_paths.length === 1 ? "" : "s"}`);
    }
    if (Array.isArray(details.tailscale_services) && details.tailscale_services.length) {
      counts.push(`${details.tailscale_services.length} tailnet service${details.tailscale_services.length === 1 ? "" : "s"}`);
    }
    if (counts.length) return `${counts.join(", ")} saved.`;
    return lastApply?.status === "success" ? "Host config saved." : "Dashboard state refreshed.";
  };

  const shortAlphanumericCode = (value) => {
    const number = Number(value);
    if (!Number.isFinite(number)) return "PENDING0";
    const alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
    let remaining = Math.max(0, Math.trunc(number)) % 1099511627776;
    let code = "";
    for (let index = 0; index < 8; index += 1) {
      code = alphabet[remaining % 32] + code;
      remaining = Math.floor(remaining / 32);
    }
    return code;
  };

  const elapsedSecondsSince = (startedAt) => {
    if (typeof startedAt !== "number") return null;
    const elapsed = (performance.now() - startedAt) / 1000;
    return Number.isFinite(elapsed) && elapsed >= 0 ? elapsed : null;
  };

  const terminalMutationMeta = (startedAt) => {
    const duration = formatDuration(elapsedSecondsSince(startedAt));
    return duration ? [{ label: "took", value: duration }] : [];
  };

  const lastApplyMeta = (lastApply, durationOverrideSeconds = null) => {
    const meta = [];
    const status = lastApply?.status || "";
    if (status === "error" && lastApply?.id) {
      meta.push({ label: "inst", value: shortAlphanumericCode(lastApply.id) });
    }
    const durationSource = Number.isFinite(durationOverrideSeconds) ? durationOverrideSeconds : lastApply?.duration_seconds;
    const duration = formatDuration(durationSource);
    if (duration) meta.push({ label: "took", value: duration });
    const timestamp = formatLocalTimestamp(lastApply?.finished_at || lastApply?.created_at);
    const timestampLabel = status === "error" ? "failed" : (status === "success" ? "saved" : "updated");
    if (timestamp) meta.push({ label: timestampLabel, value: timestamp });
    return meta;
  };

  const showLatestSaveStatus = async ({ successTitle, errorTitle, startedAt = null, pipeline = "saveInput" }) => {
    let statusPayload = null;
    try {
      const statusResponse = await fetch("/api/status", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!statusResponse.ok) throw new Error(`status refresh failed: ${statusResponse.status}`);
      statusPayload = await statusResponse.json();
    } catch (error) {
      console.warn("status refresh after save failed", error);
    }
    const lastApply = (statusPayload && statusPayload.last_apply) || {};
    const steps = saveProgressPipelines[pipeline] || saveProgressPipelines.saveInput;
    const tone = lastApply.status === "error" ? "error" : (lastApply.status === "success" ? "success" : (lastApply.status || "queued"));
    const successNote = statusPayload && lastApply.status === "success"
      ? "Host changes are live."
      : "Saved. Live status refresh is catching up.";
    showFloatingSave({
      tone: statusPayload ? tone : "success",
      title: lastApply.status === "error" ? errorTitle : successTitle,
      headline: lastApply.status === "error" ? "Save failed" : "Complete",
      note: lastApply.status === "error" ? lastApplySummary(lastApply) : successNote,
      meta: lastApplyMeta(lastApply, elapsedSecondsSince(startedAt)),
      progress: 100,
      steps,
      activeIndex: 0,
    });
  };

  const responseErrorMessage = (html, fallback, response = null) => {
    const nextDocument = new DOMParser().parseFromString(html, "text/html");
    const banner = nextDocument.querySelector(".banner.error");
    const pageTitle = nextDocument.querySelector("title")?.textContent || "";
    const bodyText = nextDocument.body?.textContent || "";
    const detail = (banner?.textContent || pageTitle || bodyText || "").replace(/\s+/g, " ").trim();
    const status = response && !response.ok ? `${response.status} ${response.statusText}`.replace(/\s+/g, " ").trim() : "";
    const statusCode = response?.status || 0;
    if (statusCode === 403) {
      return `${fallback}: page token expired after the admin service restarted. Refresh and try again.`;
    }
    if (!detail && statusCode >= 500) {
      return `${fallback}: server returned ${status || statusCode}. The request was interrupted before CNC returned details.`;
    }
    if (statusCode >= 500 && detail.toLowerCase() === "bad gateway") {
      return `${fallback}: server returned ${status || statusCode}. The request was interrupted before CNC returned details.`;
    }
    if (detail && status) return `${detail} (${status})`;
    if (detail) return detail;
    if (status) return `${fallback} (${status})`;
    return fallback;
  };

  const dashboardMutationErrorMessage = (response, text, fallback) => {
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      try {
        const payload = JSON.parse(text || "{}");
        const detail = String(payload.flash_error || payload.error || payload.detail || "").replace(/\s+/g, " ").trim();
        if (detail) return detail;
      } catch (_error) {}
    }
    return responseErrorMessage(text, fallback, response);
  };

  const operationDurationSeconds = (payload, fallbackStartedAt = null) => {
    const started = payload?.started_at ? new Date(payload.started_at) : null;
    const finished = payload?.finished_at ? new Date(payload.finished_at) : null;
    if (started && finished && !Number.isNaN(started.getTime()) && !Number.isNaN(finished.getTime())) {
      const seconds = (finished.getTime() - started.getTime()) / 1000;
      if (Number.isFinite(seconds) && seconds >= 0) return seconds;
    }
    return elapsedSecondsSince(fallbackStartedAt);
  };

  const operationMeta = (payload, startedAt) => {
    const meta = [];
    const duration = formatDuration(operationDurationSeconds(payload, startedAt));
    const status = String(payload?.status || "").toLowerCase();
    const isFinished = ["success", "failed", "partial", "cancelled"].includes(status);
    if (isFinished && duration) meta.push({ label: "took", value: duration });
    if (!isFinished && duration) meta.push({ label: "elapsed", value: duration });
    const finishedAt = payload?.finished_at || "";
    const timestamp = formatLocalTimestamp(finishedAt);
    if (timestamp) meta.push({ label: payload?.status === "failed" ? "failed" : "saved", value: timestamp });
    return meta;
  };

  const progressStepIndex = (steps, progress) => {
    const value = Number(progress);
    if (!Number.isFinite(value) || !Array.isArray(steps) || !steps.length) return 0;
    let index = 0;
    steps.forEach((step, stepIndex) => {
      if (Number(step.progress) <= value) index = stepIndex;
    });
    return index;
  };

  const knownProgressStep = (steps, phase, substate) => {
    const normalizedPhase = String(phase || "").trim().toLowerCase();
    const normalizedSubstate = String(substate || "").trim().toLowerCase();
    if (!normalizedPhase || !normalizedSubstate || !Array.isArray(steps)) return null;
    return steps.find((step) => (
      String(step.headline || "").trim().toLowerCase() === normalizedPhase
      && String(step.substep || "").trim().toLowerCase() === normalizedSubstate
    )) || null;
  };

  const progressForStage = (steps, phase, substate = "", fallback = 0) => {
    const exactStep = knownProgressStep(steps, phase, substate);
    const normalizedPhase = String(phase || "").trim().toLowerCase();
    const phaseStep = normalizedPhase && Array.isArray(steps)
      ? steps.find((step) => String(step.headline || "").trim().toLowerCase() === normalizedPhase)
      : null;
    const progress = Number((exactStep || phaseStep || {}).progress);
    return Number.isFinite(progress) ? progress : fallback;
  };

  const operationStage = (payload, fallbackTitle, steps) => {
    const phase = String(payload?.phase || "").replace(/_/g, " ").trim();
    const substate = String(payload?.details?.substate || "").trim();
    const message = String(payload?.details?.message || payload?.error || "").trim();
    const knownStep = knownProgressStep(steps, phase, substate) || knownProgressStep(steps, phase, message);
    return {
      headline: phase || knownStep?.headline || fallbackTitle,
      note: substate || knownStep?.substep || message || knownStep?.note || "",
      progress: knownStep ? Number(knownStep.progress) : null,
    };
  };

  const watchDashboardOperation = (operationId, { successTitle, errorTitle, startedAt, pipeline = "saveInput", refreshTab = "outputs", runningTitle = successTitle }) => new Promise((resolve, reject) => {
    const steps = saveProgressPipelines[pipeline] || saveProgressPipelines.saveInput;
    const firstStep = steps[0] || {};
    let lastProgress = Number(firstStep.progress) || 0;
    let lastStage = {
      headline: firstStep.headline || successTitle,
      note: firstStep.substep || firstStep.note || "",
    };
    let lastPayload = null;
    let elapsedTimer = null;

    const stopElapsedTimer = () => {
      if (!elapsedTimer) return;
      window.clearInterval(elapsedTimer);
      elapsedTimer = null;
    };

    const renderOperationProgress = (payload) => {
      lastPayload = payload;
      const details = payload.details || {};
      const stage = operationStage(payload, successTitle, steps);
      const progress = Number(details.progress);
      const stageProgress = Number(stage.progress);
      const progressCandidate = Number.isFinite(progress)
        ? progress
        : (Number.isFinite(stageProgress) ? stageProgress : lastProgress);
      const progressRegressed = progressCandidate < lastProgress;
      const stageRegressed = Number.isFinite(stageProgress) && stageProgress < lastProgress;
      const displayProgress = !progressRegressed ? progressCandidate : lastProgress;
      const displayStage = progressRegressed || stageRegressed ? lastStage : stage;
      if (!progressRegressed) {
        lastProgress = progressCandidate;
      }
      if (!progressRegressed && !stageRegressed) {
        lastStage = stage;
      }
      showFloatingSave({
        tone: "running",
        title: runningTitle,
        headline: displayStage.headline,
        substep: displayStage.note,
        note: displayStage.note,
        meta: operationMeta(payload, startedAt),
        progress: displayProgress,
        steps,
        activeIndex: progressStepIndex(steps, displayProgress),
      });
    };

    elapsedTimer = window.setInterval(() => {
      if (lastPayload) renderOperationProgress(lastPayload);
    }, 1000);

    window.CNCOperations.watchOperation(operationId, {
      pollIntervalMs: 1400,
      retryIntervalMs: 1800,
      maxPollFailures: 40,
      rejectOnFailure: true,
      onRetry: ({ status }) => {
        showFloatingSave({
          tone: "running",
          title: runningTitle,
          headline: "Waiting for CNC",
          substep: status ? `status poll returned ${status}; retrying` : "status stream interrupted; retrying",
          note: status
            ? `Status poll returned ${status}; retrying without stopping the operation.`
            : "Status stream was interrupted; retrying without stopping the operation.",
          meta: [],
          progress: lastProgress,
          steps,
          activeIndex: progressStepIndex(steps, lastProgress),
        });
      },
      onProgress: renderOperationProgress,
      onSuccess: async (payload) => {
        const details = payload.details || {};
        stopElapsedTimer();
        showFloatingSave({
          tone: "success",
          title: successTitle,
          headline: "Complete",
          note: "Host changes are live.",
          meta: operationMeta(payload, startedAt),
          progress: 100,
          steps,
          activeIndex: steps.length - 1,
        });
        try {
          const dashboardResponse = await fetch(`/?tab=${encodeURIComponent(refreshTab)}`, {
            credentials: "same-origin",
            headers: { Accept: "text/html" },
          });
          const html = await dashboardResponse.text();
          if (dashboardResponse.ok) {
            refreshDashboardAfterInputSave(html, refreshTab, {
              focusOutputId: details.backend_id,
              focusOutputName: details.backend_name,
            });
          }
        } catch (error) {
          console.error("dashboard refresh after operation failed", error);
        }
        resolve(payload);
      },
      onFailure: (payload) => {
        stopElapsedTimer();
        const details = payload.details || {};
        const message = details.flash_error || payload.error || details.message || `${errorTitle}.`;
        const error = new Error(actionErrorText(errorTitle, { message }));
        showFloatingSave({
          tone: "error",
          title: errorTitle,
          headline: "Review error",
          note: error.message,
          meta: operationMeta(payload, startedAt),
          progress: 100,
          steps,
          activeIndex: steps.length - 1,
        });
        setBanner("error", error.message);
        return error;
      },
      onError: (error) => {
        stopElapsedTimer();
        reject(error);
      },
    }).then((payload) => {
      stopElapsedTimer();
      resolve(payload);
    }).catch((error) => {
      stopElapsedTimer();
      if (error) {
        reject(error);
      }
    });
  });

  const dashboardOperationResumeConfig = (operation) => {
    const kind = String(operation?.kind || "");
    if (kind === "ui.input.create") {
      return {
        successTitle: "Input created",
        errorTitle: "Input create failed",
        pipeline: "createInput",
        refreshTab: "inputs",
        runningTitle: "Creating input",
      };
    }
    if (kind === "ui.input.update") {
      return {
        successTitle: "Input saved",
        errorTitle: "Input save failed",
        pipeline: "saveInput",
        refreshTab: "inputs",
        runningTitle: "Saving input",
      };
    }
    if (kind === "ui.input.delete") {
      return {
        successTitle: "Input deleted",
        errorTitle: "Input delete failed",
        pipeline: "deleteInput",
        refreshTab: "inputs",
        runningTitle: "Deleting input",
      };
    }
    if (kind === "create_backend") {
      return {
        successTitle: "Output created",
        errorTitle: "Output create failed",
        pipeline: "createOutput",
        refreshTab: "outputs",
        runningTitle: "Creating output",
      };
    }
    return null;
  };

  const resumeDashboardOperations = async () => {
    if (!window.CNCOperations?.activeOperations) return;
    let operations = [];
    try {
      operations = await window.CNCOperations.activeOperations({ surface: "dashboard" });
    } catch (error) {
      console.warn("active dashboard operation lookup failed", error);
      return;
    }
    operations.forEach((operation) => {
      const config = dashboardOperationResumeConfig(operation);
      if (!config) return;
      showInitialFloatingSaveProgress({
        title: config.runningTitle,
        pipeline: config.pipeline,
      });
      beginHostMutationUi();
      watchDashboardOperation(operation.id, {
        ...config,
        startedAt: null,
      }).catch((error) => {
        setBanner("error", error instanceof Error ? error.message : config.errorTitle);
      }).finally(() => {
        endHostMutationUi();
      });
    });
  };

  const dashboardErrorMessage = (html) => {
    const nextDocument = new DOMParser().parseFromString(html, "text/html");
    const banner = nextDocument.querySelector(".banner.error");
    return (banner?.textContent || "").replace(/\s+/g, " ").trim();
  };

  const actionErrorText = (prefix, error) => {
    const message = (error?.message || prefix).replace(/\s+/g, " ").trim();
    return message.toLowerCase().startsWith(prefix.toLowerCase()) ? message : `${prefix}: ${message}`;
  };

  const bindSaveCardCloses = (root = document) => {
    root.querySelectorAll("[data-save-card-close]").forEach((button) => {
      if (button.dataset.boundSaveCardClose === "1") return;
      button.dataset.boundSaveCardClose = "1";
      button.addEventListener("click", () => {
        const shell = button.closest(".floating-save-shell");
        const card = button.closest("[data-save-card]");
        if (card) card.hidden = true;
        if (shell) {
          const visibleCards = Array.from(shell.querySelectorAll("[data-save-card]")).filter((item) => !item.hidden);
          shell.hidden = visibleCards.length === 0;
        }
      });
    });
  };

  const clearBanners = ({ successOnly = false } = {}) => {
    document.querySelectorAll(".banner").forEach((banner) => {
      if (successOnly && !banner.classList.contains("success")) return;
      banner.remove();
    });
  };

  const floatingSaveClearance = () => {
    if (!floatingSaveShell || floatingSaveShell.hidden) return 24;
    const shellBox = floatingSaveShell.getBoundingClientRect();
    return Math.max(24, Math.ceil(shellBox.height + 48));
  };

  const revealCreatedOutputRow = ({ backendId = null, backendName = "" } = {}) => {
    const id = backendId === null || backendId === undefined ? "" : String(backendId);
    const name = String(backendName || "").trim();
    let row = id
      ? Array.from(document.querySelectorAll("[data-output-row]"))
        .find((item) => item.dataset.outputRow === id) || null
      : null;
    if (!row && name) {
      row = Array.from(document.querySelectorAll("[data-output-row]"))
        .find((item) => item.dataset.outputName === name) || null;
    }
    if (!row) return;
    row.classList.add("is-new");
    row.style.scrollMarginBottom = `${floatingSaveClearance()}px`;
    row.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "smooth" });
    window.requestAnimationFrame(() => {
      const rowBox = row.getBoundingClientRect();
      const shellTop = floatingSaveShell && !floatingSaveShell.hidden
        ? floatingSaveShell.getBoundingClientRect().top
        : window.innerHeight;
      if (rowBox.bottom > shellTop - 18) {
        window.scrollBy({
          top: rowBox.bottom - shellTop + 28,
          left: 0,
          behavior: "smooth",
        });
      }
      row.focus({ preventScroll: true });
    });
    window.setTimeout(() => {
      row.classList.remove("is-new");
    }, 5200);
  };

  document.addEventListener("click", (event) => {
    const closeButton = event.target.closest(".banner-close");
    if (!closeButton) return;
    closeButton.closest(".banner")?.remove();
  });

  document.querySelectorAll(".banner").forEach((banner) => {
    if (banner.querySelector(".banner-close")) return;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "banner-close";
    close.setAttribute("aria-label", "Dismiss message");
    close.textContent = "\u00d7";
    banner.appendChild(close);
  });

  const teardownRouteGraphs = (root) => {
    root?.querySelectorAll?.("[data-route-graph]").forEach((shell) => {
      stopRouteGraphFlow(shell);
      if (shell._routeGraph) {
        shell._routeGraph.destroy();
        shell._routeGraph = null;
      }
      shell._routeGraphRenderKey = null;
    });
  };

  const replaceFromDocument = (selector, nextDocument) => {
    const current = document.querySelector(selector);
    const next = nextDocument.querySelector(selector);
    if (!current || !next) return null;
    teardownRouteGraphs(current);
    current.replaceWith(next);
    return next;
  };

  const syncBannersFromDocument = (nextDocument) => {
    clearBanners();
    const tabbar = document.querySelector(".tabbar");
    if (!tabbar || !tabbar.parentNode) return;
    const nextBanners = Array.from(nextDocument.querySelectorAll(".banner")).filter(
      (banner) => !banner.classList.contains("success"),
    );
    if (!nextBanners.length) return;
    const fragment = document.createDocumentFragment();
    nextBanners.forEach((banner) => fragment.appendChild(banner));
    tabbar.parentNode.insertBefore(fragment, tabbar.nextSibling);
  };

  const refreshDashboardAfterInputSave = (html, activeTab = document.querySelector("[data-tab].is-active")?.dataset.tab || "home", { focusOutputId = null, focusOutputName = "" } = {}) => {
    const nextDocument = new DOMParser().parseFromString(html, "text/html");
    replaceFromDocument(".site-header", nextDocument);
    replaceFromDocument('.panel[data-panel="home"]', nextDocument);
    const nextInputsPanel = replaceFromDocument('.panel[data-panel="inputs"]', nextDocument);
    replaceFromDocument('.panel[data-panel="routing"]', nextDocument);
    const nextOutputsPanel = replaceFromDocument('.panel[data-panel="outputs"]', nextDocument);
    syncBannersFromDocument(nextDocument);
    selectTab(activeTab);
    bindSaveCardCloses(document);
    if (nextInputsPanel) {
      bindEditorToggles(nextInputsPanel);
      bindEditorCloses(nextInputsPanel);
      bindConfirmButtons(nextInputsPanel);
      bindInputKindForms(nextInputsPanel);
      bindRows(nextInputsPanel);
      bindInputCreateForms(nextInputsPanel);
      bindInputUpdateForms(nextInputsPanel);
      bindOutputAttachForms(nextInputsPanel);
      bindToggleSwitches(nextInputsPanel);
    }
    if (nextOutputsPanel) {
      bindEditorToggles(nextOutputsPanel);
      bindEditorCloses(nextOutputsPanel);
      bindConfirmButtons(nextOutputsPanel);
      bindBackendKindForms(nextOutputsPanel);
      bindBackendCreateForms(nextOutputsPanel);
      bindOutputAttachForms(nextOutputsPanel);
      bindToggleSwitches(nextOutputsPanel);
    }
    bindRows(document);
    if (activeTab === "outputs" && (focusOutputId || focusOutputName)) {
      revealCreatedOutputRow({ backendId: focusOutputId, backendName: focusOutputName });
    }
  };

  const loadDashboardTab = async (name) => {
    const targetName = tabIsAvailable(name) ? name : "home";
    const requestId = latestTabRequestId + 1;
    latestTabRequestId = requestId;
    const requestIsCurrent = () => requestId === latestTabRequestId;
    const currentPanel = document.querySelector(`.panel[data-panel="${CSS.escape(targetName)}"]`);
    if (currentPanel?.dataset.loaded === "1") {
      selectTab(targetName);
      return;
    }
    if (tabLoadRequests.has(targetName)) {
      await tabLoadRequests.get(targetName);
      if (requestIsCurrent()) {
        selectTab(targetName);
      }
      return;
    }
    const request = (async () => {
      const response = await fetch(`/?tab=${encodeURIComponent(targetName)}`, {
        credentials: "same-origin",
        headers: { Accept: "text/html" },
      });
      if (!response.ok) throw new Error(`tab request failed: ${response.status}`);
      const html = await response.text();
      if (!requestIsCurrent()) return false;
      const nextDocument = new DOMParser().parseFromString(html, "text/html");
      replaceFromDocument(".site-header", nextDocument);
      const nextPanel = replaceFromDocument(`.panel[data-panel="${CSS.escape(targetName)}"]`, nextDocument);
      syncBannersFromDocument(nextDocument);
      if (nextPanel) {
        nextPanel.dataset.loaded = "1";
        bindDashboardPanel(nextPanel);
      }
      return true;
    })();
    tabLoadRequests.set(targetName, request);
    let loaded = false;
    try {
      loaded = await request;
    } catch (error) {
      console.error("dashboard tab load failed", error);
    } finally {
      tabLoadRequests.delete(targetName);
    }
    if (loaded && requestIsCurrent()) {
      selectTab(targetName);
    }
  };

  const markInputPending = (inputId, pending) => {
    if (!inputId) return;
    const row = document.querySelector(`[data-input-row="${inputId}"]`);
    const detailRow = document.querySelector(`[data-input-detail-row="${inputId}"]`);
    const card = document.querySelector(`[data-input-card="${inputId}"]`);
    row?.classList.toggle("is-pending", pending);
    detailRow?.classList.toggle("is-pending", pending);
    card?.classList.toggle("is-pending", pending);
  };

  const openConfirmModal = ({ title = "Confirm action", message = "Are you sure?", acceptLabel = "OK" }) => {
    if (!confirmModal) {
      return Promise.resolve(window.confirm(message));
    }
    if (confirmModalTitle) confirmModalTitle.textContent = title;
    if (confirmModalMessage) confirmModalMessage.textContent = message;
    if (confirmModalAccept) confirmModalAccept.textContent = acceptLabel;
    confirmModal.returnValue = "";
    confirmModal.showModal();
    return new Promise((resolve) => {
      const handleClose = () => {
        confirmModal.removeEventListener("close", handleClose);
        resolve(confirmModal.returnValue === "confirm");
      };
      confirmModal.addEventListener("close", handleClose);
    });
  };

  if (confirmModalCancel) {
    confirmModalCancel.addEventListener("click", () => {
      confirmModal?.close("cancel");
    });
  }

  if (confirmModalAccept) {
    confirmModalAccept.addEventListener("click", () => {
      confirmModal?.close("confirm");
    });
  }

  const bindEditorToggles = (root = document) => {
    root.querySelectorAll("[data-editor-toggle]").forEach((button) => {
      if (button.dataset.boundEditorToggle === "1") return;
      button.dataset.boundEditorToggle = "1";
      button.addEventListener("click", () => {
        const id = button.dataset.editorToggle;
        const editor = id ? document.getElementById(id) : null;
        if (!editor) return;
        editor.hidden = !editor.hidden;
        if (!editor.hidden) {
          const input = editor.querySelector("input, select, textarea");
          if (input) input.focus();
        }
      });
    });
  };

  const bindEditorCloses = (root = document) => {
    root.querySelectorAll("[data-editor-close]").forEach((button) => {
      if (button.dataset.boundEditorClose === "1") return;
      button.dataset.boundEditorClose = "1";
      button.addEventListener("click", () => {
        const id = button.dataset.editorClose;
        const editor = id ? document.getElementById(id) : null;
        if (!editor) return;
        editor.hidden = true;
      });
    });
  };

  const bindConfirmButtons = (root = document) => {
    root.querySelectorAll("[data-confirm-message]").forEach((button) => {
      if (button.dataset.boundConfirm === "1") return;
      button.dataset.boundConfirm = "1";
      button.addEventListener("click", async (event) => {
        const message = button.dataset.confirmMessage || "Are you sure?";
        event.preventDefault();
        const confirmed = await openConfirmModal({
          title: button.dataset.confirmTitle || "Confirm delete",
          message,
          acceptLabel: button.dataset.confirmAccept || "OK",
        });
        if (!confirmed) return;
        if (button.form?.requestSubmit) {
          button.form.requestSubmit(button);
          return;
        }
        button.form?.submit();
      });
    });
  };

  const openAccessKeyResetModal = (modal) => {
    if (!(modal instanceof HTMLDialogElement)) return;
    if (!modal.open) modal.showModal();
    window.setTimeout(() => {
      modal.querySelector("[data-access-key-first-input]")?.focus();
    }, 0);
  };

  const bindAccessKeyResetModal = (root = document) => {
    root.querySelectorAll("[data-access-key-reset-open]").forEach((button) => {
      if (button.dataset.boundAccessKeyResetOpen === "1") return;
      button.dataset.boundAccessKeyResetOpen = "1";
      button.addEventListener("click", () => {
        const panel = button.closest("[data-panel]");
        const modal = panel?.querySelector("[data-access-key-reset-modal]");
        openAccessKeyResetModal(modal);
      });
    });
    root.querySelectorAll("[data-access-key-reset-close]").forEach((button) => {
      if (button.dataset.boundAccessKeyResetClose === "1") return;
      button.dataset.boundAccessKeyResetClose = "1";
      button.addEventListener("click", () => {
        button.closest("[data-access-key-reset-modal]")?.close();
      });
    });
    root.querySelectorAll("[data-access-key-reset-modal][data-open-on-load]").forEach((modal) => {
      delete modal.dataset.openOnLoad;
      openAccessKeyResetModal(modal);
    });
  };

  const syncInputValueField = (form) => {
    const kindSelect = form.querySelector("select[name='kind']");
    const valueField = form.querySelector("[data-input-value-field]");
    if (!kindSelect || !valueField) return;
    const isShield = kindSelect.value === "shield";
    const isTailnetPath = kindSelect.value === "tailnet_path";
    const isTailnetService = kindSelect.value === "tailnet_service";
    valueField.placeholder = isTailnetPath ? "/app1" : (isTailnetService ? "app-dev" : (isShield ? "shield.example.com" : "api.example.com"));
    valueField.title = isTailnetPath
      ? "Use a slash-prefixed tailnet path like /app1."
      : (isTailnetService ? "Use a Tailscale service name like app-dev." : (isShield ? "Use the public hostname for the Shield access gate." : ""));
  };

  const bindInputKindForms = (root = document) => {
    root.querySelectorAll("[data-input-kind-form]").forEach((form) => {
      if (form.dataset.boundInputKind === "1") return;
      form.dataset.boundInputKind = "1";
      const kindSelect = form.querySelector("select[name='kind']");
      if (!kindSelect) return;
      syncInputValueField(form);
      kindSelect.addEventListener("change", () => syncInputValueField(form));
    });
  };

  const syncBackendKindForm = (form) => {
    const kindSelect = form.querySelector("select[name='kind']");
    if (!kindSelect) return;
    const selectedKind = ["static", "shield"].includes(kindSelect.value) ? kindSelect.value : "app";
    const nameField = form.querySelector("input[name='name']");
    if (nameField && selectedKind === "shield") {
      nameField.value = "shield";
    }
    form.querySelectorAll("[data-backend-kind-section]").forEach((section) => {
      section.hidden = section.dataset.backendKindSection !== selectedKind;
    });
    syncResourceSizeControls(form);
  };

  const syncResourceSizeControls = (form) => {
    const sizeSelect = form.querySelector("[data-resource-size-control]");
    if (!sizeSelect) return;
    const kindSelect = form.querySelector("select[name='kind']");
    const selectedKind = kindSelect && ["static", "shield"].includes(kindSelect.value) ? kindSelect.value : "app";
    const isCustom = sizeSelect.value === "custom";
    form.querySelectorAll("[data-custom-resource-limit]").forEach((section) => {
      const kindHidden = section.dataset.backendKindSection && section.dataset.backendKindSection !== selectedKind;
      section.hidden = !isCustom || Boolean(kindHidden);
      section.querySelectorAll("input, select, textarea").forEach((field) => {
        field.disabled = !isCustom || section.hidden;
      });
    });
  };

  const bindBackendKindForms = (root = document) => {
    root.querySelectorAll("[data-backend-kind-form]").forEach((form) => {
      if (form.dataset.boundBackendKind === "1") return;
      form.dataset.boundBackendKind = "1";
      const kindSelect = form.querySelector("select[name='kind']");
      const resourceSizeSelect = form.querySelector("[data-resource-size-control]");
      if (!kindSelect) return;
      syncBackendKindForm(form);
      kindSelect.addEventListener("change", () => syncBackendKindForm(form));
      resourceSizeSelect && resourceSizeSelect.addEventListener("change", () => syncResourceSizeControls(form));
    });
  };

  const closeSiblings = (targetRow) => {
    const table = targetRow.closest("table");
    if (!table) return;
    table.querySelectorAll(".click-row[data-detail-target]").forEach((row) => {
      if (row === targetRow) return;
      const detailId = row.dataset.detailTarget;
      const detail = detailId ? document.getElementById(detailId) : null;
      if (detail) detail.hidden = true;
      row.setAttribute("aria-expanded", "false");
    });
  };

  const toggleDetail = (row) => {
    const detailId = row.dataset.detailTarget;
    const detail = detailId ? document.getElementById(detailId) : null;
    if (!detail) return;
    const nextHidden = !detail.hidden;
    closeSiblings(row);
    detail.hidden = nextHidden;
    row.setAttribute("aria-expanded", nextHidden ? "false" : "true");
  };

  const bindRows = (root = document) => {
    root.querySelectorAll(".click-row").forEach((row) => {
      if (row.dataset.boundClickRow === "1") return;
      row.dataset.boundClickRow = "1";
      const href = row.dataset.rowLink;
      if (href) {
        row.addEventListener("click", () => {
          window.location.href = href;
        });
        row.addEventListener("keydown", (event) => {
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          window.location.href = href;
        });
        return;
      }
      row.addEventListener("click", () => toggleDetail(row));
      row.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        toggleDetail(row);
      });
    });
  };

  const bindInputCreateForms = (root = document) => {
    root.querySelectorAll("[data-input-create-form]").forEach((form) => {
      if (form.dataset.boundInputCreate === "1") return;
      form.dataset.boundInputCreate = "1";
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (form.dataset.submitting === "1") return;
        form.dataset.submitting = "1";
        beginHostMutationUi();
        const editor = form.closest(".editor");
        if (editor) editor.hidden = true;
        const submitButton = form.querySelector("button[type='submit']");
        const originalLabel = submitButton ? submitButton.textContent : "";
        if (submitButton) {
          submitButton.disabled = true;
          submitButton.textContent = "Saving...";
        }
        setBanner("", "");
        const saveStartedAt = performance.now();
        showInitialFloatingSaveProgress({ title: "Creating input", pipeline: "createInput" });
        try {
          let response = await fetch(form.action, {
            method: form.method || "POST",
            credentials: "same-origin",
            body: new FormData(form),
            headers: {
              Accept: "application/json, text/html",
              "X-CNC-Dashboard-Refresh": "1",
            },
          });
          if (response.status === 403 && await refreshCsrfTokens()) {
            response = await fetch(form.action, {
              method: form.method || "POST",
              credentials: "same-origin",
              body: new FormData(form),
              headers: {
                Accept: "application/json, text/html",
                "X-CNC-Dashboard-Refresh": "1",
              },
            });
          }
          const contentType = response.headers.get("content-type") || "";
          if (response.status === 202 && contentType.includes("application/json")) {
            const payload = await response.json();
            if (!payload.operation_id) throw new Error("Input create failed: operation id missing.");
            await watchDashboardOperation(payload.operation_id, {
              successTitle: "Input created",
              errorTitle: "Input create failed",
              startedAt: saveStartedAt,
              pipeline: "createInput",
              refreshTab: "inputs",
              runningTitle: "Creating input",
            });
            return;
          }
          const html = await response.text();
          if (!response.ok || dashboardErrorMessage(html)) {
            throw new Error(dashboardMutationErrorMessage(response, html, "Input create failed"));
          }
          refreshDashboardAfterInputSave(html, "inputs");
          await showLatestSaveStatus({
            successTitle: "Input created",
            errorTitle: "Input create failed",
            startedAt: saveStartedAt,
            pipeline: "createInput",
          });
        } catch (error) {
          const message = actionErrorText("Input create failed", error);
          setBanner("error", message);
          showFloatingSave({
            tone: "error",
            title: "Input create failed",
            headline: "Review error",
            note: message,
            meta: terminalMutationMeta(saveStartedAt),
            progress: 100,
            steps: saveProgressPipelines.createInput,
            activeIndex: saveProgressPipelines.createInput.length - 1,
          });
          if (editor) editor.hidden = false;
        } finally {
          delete form.dataset.submitting;
          if (submitButton) {
            submitButton.disabled = false;
            submitButton.textContent = originalLabel;
          }
          endHostMutationUi();
          selectTab("inputs");
        }
      });
    });
  };

  const bindBackendCreateForms = (root = document) => {
    root.querySelectorAll("[data-backend-create-form]").forEach((form) => {
      if (form.dataset.boundBackendCreate === "1") return;
      form.dataset.boundBackendCreate = "1";
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (form.dataset.submitting === "1") return;
        form.dataset.submitting = "1";
        beginHostMutationUi();
        const editor = form.closest(".editor");
        if (editor) editor.hidden = true;
        const submitButton = form.querySelector("button[type='submit']");
        const originalLabel = submitButton ? submitButton.textContent : "";
        if (submitButton) {
          submitButton.disabled = true;
          submitButton.textContent = "Saving...";
        }
        setBanner("", "");
        const saveStartedAt = performance.now();
        showInitialFloatingSaveProgress({ title: "Creating output", pipeline: "createOutput" });
        try {
          let response = await fetch(form.action, {
            method: form.method || "POST",
            credentials: "same-origin",
            body: new FormData(form),
            headers: {
              Accept: "application/json, text/html",
              "X-CNC-Dashboard-Refresh": "1",
            },
          });
          if (response.status === 403 && await refreshCsrfTokens()) {
            response = await fetch(form.action, {
              method: form.method || "POST",
              credentials: "same-origin",
              body: new FormData(form),
              headers: {
                Accept: "application/json, text/html",
                "X-CNC-Dashboard-Refresh": "1",
              },
            });
          }
          const contentType = response.headers.get("content-type") || "";
          if (response.status === 202 && contentType.includes("application/json")) {
            const payload = await response.json();
            if (!payload.operation_id) throw new Error("Output create failed: operation id missing.");
            await watchDashboardOperation(payload.operation_id, {
              successTitle: "Output created",
              errorTitle: "Output create failed",
              startedAt: saveStartedAt,
              pipeline: "createOutput",
              runningTitle: "Creating output",
            });
            return;
          }
          const html = await response.text();
          if (!response.ok || dashboardErrorMessage(html)) {
            throw new Error(responseErrorMessage(html, "Output create failed", response));
          }
          refreshDashboardAfterInputSave(html, "outputs");
          await showLatestSaveStatus({
            successTitle: "Output created",
            errorTitle: "Output create failed",
            startedAt: saveStartedAt,
            pipeline: "createOutput",
          });
        } catch (error) {
          const message = actionErrorText("Output create failed", error);
          setBanner("error", message);
          showFloatingSave({
            tone: "error",
            title: "Output create failed",
            headline: "Review error",
            note: message,
            meta: terminalMutationMeta(saveStartedAt),
            progress: 100,
            steps: saveProgressPipelines.createOutput,
            activeIndex: saveProgressPipelines.createOutput.length - 1,
          });
          if (editor) editor.hidden = false;
        } finally {
          delete form.dataset.submitting;
          if (submitButton) {
            submitButton.disabled = false;
            submitButton.textContent = originalLabel;
          }
          endHostMutationUi();
          selectTab("outputs");
        }
      });
    });
  };

  const bindInputUpdateForms = (root = document) => {
    root.querySelectorAll("[data-input-update-form]").forEach((form) => {
      if (form.dataset.boundInputUpdate === "1") return;
      form.dataset.boundInputUpdate = "1";
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (form.dataset.submitting === "1") return;
        const submitter = event.submitter || form.querySelector("button[type='submit']");
        const inputId = form.dataset.inputId || "";
        const isDelete = (submitter?.value || "save") === "delete";
        if (!isDelete) {
          const shieldToggle = form.elements.namedItem("shield_enabled");
          const shieldCode = form.elements.namedItem("shield_access_code");
          const shieldCard = form.querySelector("[data-shield-code-configured]");
          const hasStoredShieldCode = shieldCard?.dataset.shieldCodeConfigured === "true";
          if (shieldToggle?.checked && !hasStoredShieldCode && !String(shieldCode?.value || "").trim()) {
            setBanner("error", "please enter an access code to enable shield");
            shieldCode?.focus();
            return;
          }
        }
        form.dataset.submitting = "1";
        beginHostMutationUi();
        const originalLabel = submitter ? submitter.textContent : "";
        if (submitter) {
          submitter.disabled = true;
          submitter.textContent = isDelete ? "Deleting..." : "Saving...";
        }
        setBanner("", "");
        markInputPending(inputId, true);
        const saveStartedAt = performance.now();
        showInitialFloatingSaveProgress({
          title: isDelete ? "Deleting input" : "Saving input",
          pipeline: isDelete ? "deleteInput" : "saveInput",
        });
        try {
          const formData = new FormData(form);
          if (submitter?.name) {
            formData.set(submitter.name, submitter.value);
          }
          const actionPath = submitter?.hasAttribute("formaction")
            ? submitter.getAttribute("formaction")
            : (form.dataset.inputUpdateUrl || form.getAttribute("action") || form.action);
          const submitUrl = new URL(actionPath || form.action, window.location.href).toString();
          let response = await fetch(submitUrl, {
            method: form.method || "POST",
            credentials: "same-origin",
            body: formData,
            headers: { Accept: "application/json, text/html" },
          });
          if (response.status === 403 && await refreshCsrfTokens()) {
            const retryFormData = new FormData(form);
            if (submitter?.name) {
              retryFormData.set(submitter.name, submitter.value);
            }
            response = await fetch(submitUrl, {
              method: form.method || "POST",
              credentials: "same-origin",
              body: retryFormData,
              headers: { Accept: "application/json, text/html" },
            });
          }
          const contentType = response.headers.get("content-type") || "";
          if (response.status === 202 && contentType.includes("application/json")) {
            const payload = await response.json();
            if (!payload.operation_id) throw new Error(`${isDelete ? "Input delete" : "Input save"} failed: operation id missing.`);
            await watchDashboardOperation(payload.operation_id, {
              successTitle: isDelete ? "Input deleted" : "Input saved",
              errorTitle: isDelete ? "Input delete failed" : "Input save failed",
              startedAt: saveStartedAt,
              pipeline: isDelete ? "deleteInput" : "saveInput",
              refreshTab: "inputs",
              runningTitle: isDelete ? "Deleting input" : "Saving input",
            });
            return;
          }
          const html = await response.text();
          if (!response.ok || dashboardErrorMessage(html)) {
            throw new Error(dashboardMutationErrorMessage(response, html, isDelete ? "Input delete failed" : "Input save failed"));
          }
          refreshDashboardAfterInputSave(html, "inputs");
          await showLatestSaveStatus({
            successTitle: isDelete ? "Input deleted" : "Input saved",
            errorTitle: isDelete ? "Input delete failed" : "Input save failed",
            startedAt: saveStartedAt,
            pipeline: isDelete ? "deleteInput" : "saveInput",
          });
        } catch (error) {
          const failureTitle = isDelete ? "Input delete failed" : "Input save failed";
          const message = actionErrorText(failureTitle, error);
          setBanner("error", message);
          const steps = isDelete ? saveProgressPipelines.deleteInput : saveProgressPipelines.saveInput;
          showFloatingSave({
            tone: "error",
            title: failureTitle,
            headline: "Review error",
            note: message,
            meta: terminalMutationMeta(saveStartedAt),
            progress: 100,
            steps,
            activeIndex: steps.length - 1,
          });
        } finally {
          delete form.dataset.submitting;
          markInputPending(inputId, false);
          if (submitter) {
            submitter.disabled = false;
            submitter.textContent = originalLabel;
          }
          endHostMutationUi();
          selectTab("inputs");
        }
      });
    });
  };

  const closeOutputDropdowns = (activeWrap = null) => {
    document.querySelectorAll("[data-output-add-wrap]").forEach((wrap) => {
      if (activeWrap && wrap === activeWrap) return;
      const dropdown = wrap.querySelector("[data-output-dropdown]");
      if (dropdown) dropdown.hidden = true;
    });
  };

  document.addEventListener("click", (event) => {
    const activeWrap = event.target?.closest?.("[data-output-add-wrap]") || null;
    closeOutputDropdowns(activeWrap);
  });

  const bindOutputAttachForms = (root = document) => {
    root.querySelectorAll("[data-output-attach-form]").forEach((scope) => {
      if (scope.dataset.boundOutputAttach === "1") return;
      scope.dataset.boundOutputAttach = "1";
      const tagsWrap = scope.querySelector("[data-output-tags]");
      const emptyMsg = tagsWrap ? tagsWrap.querySelector(".output-tag-empty") : null;
      const addWrap = scope.querySelector("[data-output-add-wrap]");
      const addBtn = addWrap ? addWrap.querySelector("[data-output-add-toggle]") : null;
      const dropdown = addWrap ? addWrap.querySelector("[data-output-dropdown]") : null;
      const searchInput = dropdown ? dropdown.querySelector("[data-output-search]") : null;
      const listWrap = dropdown ? dropdown.querySelector("[data-output-list]") : null;
      const hiddenName = scope.dataset.outputHiddenName || "backend_ids";

      const bindOption = (opt) => {
        if (opt.dataset.boundOutputOption === "1") return;
        opt.dataset.boundOutputOption = "1";
        opt.addEventListener("click", () => {
          addTag(opt.dataset.outputId, opt.dataset.outputName, opt.dataset.outputKind);
          if (dropdown) dropdown.hidden = true;
        });
      };

      const materializeOutputList = () => {
        if (!listWrap || listWrap.dataset.outputLazyMaterialized === "1") return;
        if (listWrap.dataset.outputLazyOptions !== "backends") return;
        const template = document.getElementById("backend-output-option-template");
        if (template) {
          listWrap.appendChild(template.content.cloneNode(true));
          const attachedIds = new Set((listWrap.dataset.attachedIds || "").split(",").filter(Boolean));
          listWrap.querySelectorAll("[data-output-option]").forEach((opt) => {
            opt.hidden = attachedIds.has(opt.dataset.outputId || "");
            bindOption(opt);
          });
        }
        listWrap.dataset.outputLazyMaterialized = "1";
      };

      const syncEmpty = () => {
        if (!emptyMsg || !tagsWrap) return;
        emptyMsg.hidden = tagsWrap.querySelectorAll(".output-tag").length > 0;
      };

      const syncAddButton = () => {
        if (!addBtn || !listWrap) return;
        if (listWrap.dataset.outputLazyOptions && listWrap.dataset.outputLazyMaterialized !== "1") return;
        const visible = Array.from(listWrap.querySelectorAll("[data-output-option]:not([hidden])"))
          .filter((option) => option.style.display !== "none");
        addBtn.disabled = visible.length === 0;
      };

      const removeTag = (tag) => {
        const id = tag.dataset.outputId;
        tag.remove();
        if (listWrap) {
          materializeOutputList();
          const option = listWrap.querySelector(`[data-output-option][data-output-id="${id}"]`);
          if (option) option.hidden = false;
        }
        syncEmpty();
        syncAddButton();
      };

      const addTag = (id, name, _kind) => {
        if (!tagsWrap) return;
        const tag = document.createElement("span");
        tag.className = "output-tag";
        tag.dataset.outputId = id;
        const hidden = document.createElement("input");
        hidden.type = "hidden";
        hidden.name = hiddenName;
        hidden.value = id;
        const nameSpan = document.createElement("span");
        nameSpan.className = "output-tag-name";
        nameSpan.textContent = name;
        const removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.className = "output-tag-remove";
        removeBtn.setAttribute("aria-label", `Remove ${name}`);
        removeBtn.innerHTML = "&times;";
        removeBtn.addEventListener("click", () => removeTag(tag));
        tag.appendChild(hidden);
        tag.appendChild(nameSpan);
        tag.appendChild(removeBtn);
        tagsWrap.insertBefore(tag, emptyMsg);
        if (listWrap) {
          const option = listWrap.querySelector(`[data-output-option][data-output-id="${id}"]`);
          if (option) option.hidden = true;
        }
        syncEmpty();
        syncAddButton();
      };

      tagsWrap && tagsWrap.querySelectorAll("[data-remove-output]").forEach((btn) => {
        btn.addEventListener("click", () => removeTag(btn.closest(".output-tag")));
      });

      if (addBtn && dropdown) {
        addBtn.addEventListener("click", () => {
          materializeOutputList();
          closeOutputDropdowns(addWrap);
          dropdown.hidden = !dropdown.hidden;
          if (!dropdown.hidden && searchInput) {
            searchInput.value = "";
            searchInput.dispatchEvent(new Event("input"));
            searchInput.focus();
          }
        });
      }

      if (searchInput && listWrap) {
        searchInput.addEventListener("input", () => {
          const q = searchInput.value.toLowerCase().trim();
          listWrap.querySelectorAll("[data-output-option]").forEach((opt) => {
            if (opt.hidden && tagsWrap && tagsWrap.querySelector(`[data-output-id="${opt.dataset.outputId}"]`)) return;
            if (!q) return;
            const match = (opt.dataset.outputName || "").toLowerCase().includes(q);
            opt.style.display = match ? "" : "none";
          });
          if (!q) listWrap.querySelectorAll("[data-output-option]").forEach((opt) => { opt.style.display = ""; });
          syncAddButton();
        });
      }

      if (listWrap) {
        listWrap.querySelectorAll("[data-output-option]").forEach((opt) => {
          bindOption(opt);
        });
      }

      syncEmpty();
      syncAddButton();
    });
  };

  const bindToggleSwitches = (root = document) => {
    root.querySelectorAll(".toggle-switch").forEach((sw) => {
      if (sw.dataset.boundSwitch === "1") return;
      sw.dataset.boundSwitch = "1";
      const cb = sw.querySelector("input[type='checkbox']");
      if (!cb) return;
      const sync = () => sw.classList.toggle("is-on", cb.checked);
      cb.addEventListener("change", sync);
      sync();
    });
  };

  const readClusterNodes = () => {
    const payload = document.getElementById("cluster-nodes-data")?.textContent || "[]";
    try {
      const parsed = JSON.parse(payload);
      return Array.isArray(parsed) ? parsed : [];
    } catch (_error) {
      return [];
    }
  };

  const bindNodeControls = (root = document) => {
    const addModal = document.getElementById("node-add-modal");
    const detailsModal = document.getElementById("node-details-modal");
    const stepOneDone = document.querySelector("[data-node-step-one-done]");
    const tailnetCommand = document.querySelector("[data-node-tailnet-command]");
    const tailnetCopy = document.querySelector("[data-node-tailnet-copy]");
    const installStep = document.querySelector("[data-node-install-step]");
    const installCommand = document.querySelector("[data-node-install-command]");
    const installCopy = document.querySelector("[data-node-install-copy]");
    const joinStatus = document.querySelector("[data-node-join-status]");
    const joinCountdown = document.querySelector("[data-node-join-countdown] strong");
    let joinCountdownTimer = null;
    let currentJoinCommand = "";
    let latestClusterNodes = null;
    const nodeFields = {
      name: document.querySelector("[data-node-details-name]"),
      summary: document.querySelector("[data-node-details-summary]"),
      ram: document.querySelector("[data-node-detail-ram]"),
      cpu: document.querySelector("[data-node-detail-cpu]"),
      disk: document.querySelector("[data-node-detail-disk]"),
      tailnet: document.querySelector("[data-node-detail-tailnet]"),
      tailnetUrl: document.querySelector("[data-node-detail-tailnet-url]"),
      latency: document.querySelector("[data-node-detail-latency]"),
      version: document.querySelector("[data-node-detail-version]"),
      lastSeen: document.querySelector("[data-node-detail-last-seen]"),
    };
    const removeButton = document.querySelector("[data-node-remove]");
    const removeNote = document.querySelector("[data-node-remove-note]");
    const stopJoinCountdown = () => {
      if (joinCountdownTimer !== null) {
        window.clearInterval(joinCountdownTimer);
        joinCountdownTimer = null;
      }
    };
    const startJoinCountdown = () => {
      stopJoinCountdown();
      let remainingSeconds = 15 * 60;
      const render = () => {
        const minutes = Math.floor(remainingSeconds / 60);
        const seconds = remainingSeconds % 60;
        if (joinCountdown) {
          joinCountdown.textContent = `${minutes}:${String(seconds).padStart(2, "0")}`;
        }
        if (remainingSeconds <= 0) {
          stopJoinCountdown();
          return;
        }
        remainingSeconds -= 1;
      };
      render();
      joinCountdownTimer = window.setInterval(render, 1000);
    };
    const fetchJoinCommand = async () => {
      const requestJoinCommand = () => {
        const csrfToken = document.querySelector("input[name='csrf_token']")?.value || "";
        return fetch("/ui/settings/nodes/join-command", {
          method: "POST",
          credentials: "same-origin",
          headers: {
            Accept: "application/json",
            "X-CSRF-Token": csrfToken,
          },
        });
      };
      let response = await requestJoinCommand();
      if (response.status === 403 && await refreshCsrfTokens()) {
        response = await requestJoinCommand();
      }
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || "failed to create join command");
      }
      return String(payload.command || "");
    };
    const refreshClusterNodes = async () => {
      const response = await fetch("/ui/settings/nodes/data", {
        method: "GET",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !Array.isArray(payload.nodes)) {
        throw new Error(payload.detail || "failed to refresh nodes");
      }
      latestClusterNodes = payload.nodes;
      return latestClusterNodes;
    };
    const currentClusterNodes = () => latestClusterNodes || readClusterNodes();

    root.querySelectorAll("[data-node-add-open]").forEach((button) => {
      if (button.dataset.boundNodeAdd === "1") return;
      button.dataset.boundNodeAdd = "1";
      button.addEventListener("click", () => {
        if (joinStatus) joinStatus.hidden = true;
        currentJoinCommand = "";
        if (tailnetCopy) tailnetCopy.textContent = "Copy";
        if (installCopy) installCopy.textContent = "Copy";
        if (installCopy) installCopy.hidden = true;
        if (installCommand) installCommand.textContent = "Click Done above to reveal the CNC install command.";
        installStep?.classList.add("is-pending");
        stepOneDone?.removeAttribute("disabled");
        startJoinCountdown();
        if (typeof addModal?.showModal === "function") {
          addModal.showModal();
        }
      });
    });

    root.querySelectorAll("[data-node-details-open]").forEach((button) => {
      if (button.dataset.boundNodeDetails === "1") return;
      button.dataset.boundNodeDetails = "1";
      button.addEventListener("click", async () => {
        let nodes = currentClusterNodes();
        try {
          nodes = await refreshClusterNodes();
        } catch (_error) {
          nodes = currentClusterNodes();
        }
        const node = nodes.find((item) => String(item.id) === String(button.dataset.nodeDetailsOpen));
        if (!node) return;
        if (nodeFields.name) nodeFields.name.textContent = String(node.name || "Node");
        if (nodeFields.summary) nodeFields.summary.textContent = `${node.role || "node"} · ${node.state || "unknown"}`;
        if (nodeFields.ram) nodeFields.ram.textContent = String(node.ram || "unknown");
        if (nodeFields.cpu) nodeFields.cpu.textContent = String(node.cpu || "unknown");
        if (nodeFields.disk) nodeFields.disk.textContent = String(node.disk || "unknown");
        if (nodeFields.tailnet) nodeFields.tailnet.textContent = String(node.tailnet_ip || "not assigned");
        if (nodeFields.tailnetUrl) {
          const tailnetUrl = String(node.tailnet_url || "");
          nodeFields.tailnetUrl.textContent = tailnetUrl || "not available";
          if (tailnetUrl) {
            nodeFields.tailnetUrl.href = tailnetUrl;
          } else {
            nodeFields.tailnetUrl.removeAttribute("href");
          }
        }
        if (nodeFields.latency) nodeFields.latency.textContent = String(node.latency || "unknown");
        if (nodeFields.version) nodeFields.version.textContent = String(node.version || "unknown");
        if (nodeFields.lastSeen) nodeFields.lastSeen.textContent = String(node.last_seen || "unknown");
        if (removeButton) {
          removeButton.hidden = String(node.removable || "false") !== "true";
          removeButton.dataset.nodeRemove = String(node.id || "");
          removeButton.removeAttribute("disabled");
        }
        if (removeNote) removeNote.hidden = true;
        if (typeof detailsModal?.showModal === "function") {
          detailsModal.showModal();
        }
      });
    });

    if (stepOneDone && stepOneDone.dataset.boundNodeStepOne !== "1") {
      stepOneDone.dataset.boundNodeStepOne = "1";
      stepOneDone.addEventListener("click", async () => {
        stepOneDone.setAttribute("disabled", "disabled");
        if (installCommand) installCommand.textContent = "Creating 15-minute join command...";
        try {
          currentJoinCommand = await fetchJoinCommand();
        } catch (error) {
          currentJoinCommand = "";
          if (installCommand) installCommand.textContent = error.message || "Failed to create join command.";
          stepOneDone.removeAttribute("disabled");
          return;
        }
        installStep?.classList.remove("is-pending");
        if (installCommand) installCommand.textContent = currentJoinCommand;
        if (installCopy) installCopy.hidden = false;
      });
    }

    if (installCopy && installCopy.dataset.boundNodeInstallCopy !== "1") {
      installCopy.dataset.boundNodeInstallCopy = "1";
      installCopy.addEventListener("click", async () => {
        const command = currentJoinCommand || installCommand?.textContent?.trim() || "";
        try {
          await navigator.clipboard?.writeText(command);
        } catch (_error) {
          // Clipboard access may be blocked in some browser contexts; still show the next UI state.
        }
        installCopy.textContent = "Copied";
        if (joinStatus) joinStatus.hidden = false;
      });
    }

    if (tailnetCopy && tailnetCopy.dataset.boundNodeTailnetCopy !== "1") {
      tailnetCopy.dataset.boundNodeTailnetCopy = "1";
      tailnetCopy.addEventListener("click", async () => {
        const command = tailnetCommand?.textContent?.trim() || "";
        try {
          await navigator.clipboard?.writeText(command);
        } catch (_error) {
          // Clipboard access may be blocked in some browser contexts; the command remains visible.
        }
        tailnetCopy.textContent = "Copied";
      });
    }

    const bindNodeButton = (selector, key, handler) => {
      const button = document.querySelector(selector);
      if (!button || button.dataset[key] === "1") return;
      button.dataset[key] = "1";
      button.addEventListener("click", handler);
    };
    bindNodeButton("[data-node-add-close]", "boundNodeAddClose", () => {
      stopJoinCountdown();
      addModal?.close("cancel");
    });
    addModal?.addEventListener("close", stopJoinCountdown);
    bindNodeButton("[data-node-details-close]", "boundNodeDetailsClose", () => detailsModal?.close("cancel"));
    bindNodeButton("[data-node-details-done]", "boundNodeDetailsDone", () => detailsModal?.close("done"));
    bindNodeButton("[data-node-remove]", "boundNodeRemove", async () => {
      const nodeId = removeButton?.dataset.nodeRemove || "";
      if (!nodeId) return;
      if (removeNote) {
        removeNote.hidden = false;
        removeNote.textContent = "Removing a node only severs CNC trust and join authentication. It does not delete or modify the remote server.";
      }
      if (!window.confirm("Remove this node from CNC? This only severs CNC trust and mesh membership. It does not delete or modify the remote server.")) {
        return;
      }
      removeButton?.setAttribute("disabled", "disabled");
      try {
        const requestRemove = () => {
          const token = document.querySelector("input[name='csrf_token']")?.value || "";
          return fetch(`/ui/settings/nodes/${encodeURIComponent(nodeId)}/remove`, {
            method: "POST",
            credentials: "same-origin",
            headers: {
              Accept: "application/json",
              "X-CSRF-Token": token,
            },
          });
        };
        let response = await requestRemove();
        if (response.status === 403 && await refreshCsrfTokens()) {
          response = await requestRemove();
        }
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(payload.detail || "failed to remove node");
        }
        window.location.href = "/?tab=settings";
      } catch (error) {
        removeButton?.removeAttribute("disabled");
        if (removeNote) {
          removeNote.hidden = false;
          removeNote.textContent = error.message || "Failed to remove node.";
        }
      }
    });
  };

  bindDashboardPanel = (root = document) => {
    bindEditorToggles(root);
    bindEditorCloses(root);
    bindSaveCardCloses(root);
    bindConfirmButtons(root);
    bindInputKindForms(root);
    bindBackendKindForms(root);
    bindRows(root);
    bindInputCreateForms(root);
    bindBackendCreateForms(root);
    bindInputUpdateForms(root);
    bindOutputAttachForms(root);
    bindToggleSwitches(root);
    bindAccessKeyResetModal(root);
    bindNodeControls(root);
    bindHostMetricControls(root);
    renderLocalTimes(root);
    renderRouteGraphs(root);
    initializeHostMetricsPanel();
  };

  afterTabSelected = (targetName) => {
    if (targetName === "settings") {
      initializeHostMetricsPanel();
    }
    if (STATUS_POLL_TABS.has(targetName)) {
      refreshVisibleTabStatus();
    }
  };

  handleTabRequest = loadDashboardTab;

  currentTabs().forEach((tab) => {
    tab.addEventListener("click", () => handleTabRequest(tab.dataset.tab));
  });

  bindDashboardPanel(document);
  if (initialTab !== requestedTab && tabIsAvailable(initialTab)) {
    handleTabRequest(initialTab);
  }
  let routeGraphResizeFrame = null;
  window.addEventListener("resize", () => {
    if (routeGraphResizeFrame !== null) return;
    routeGraphResizeFrame = window.requestAnimationFrame(() => {
      routeGraphResizeFrame = null;
      if (document.querySelector("[data-panel='routing']")?.hidden) return;
      renderRouteGraphs(document);
    });
  });

  document.querySelectorAll("[data-run-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const id = button.dataset.runToggle;
      const detail = id ? document.getElementById(id) : null;
      if (!detail) return;
      const nextHidden = !detail.hidden;
      detail.hidden = nextHidden;
      button.setAttribute("aria-expanded", nextHidden ? "false" : "true");
    });
  });

  document.querySelectorAll("[data-copy-target]").forEach((button) => {
    button.addEventListener("click", async () => {
      const id = button.dataset.copyTarget;
      const detail = id ? document.getElementById(id) : null;
      if (!detail || !navigator.clipboard) return;
      const original = button.textContent;
      try {
        await navigator.clipboard.writeText(detail.textContent || "");
        button.textContent = "Copied";
      } catch (_error) {
        button.textContent = "Failed";
      }
      window.setTimeout(() => {
        button.textContent = original;
      }, 1200);
    });
  });

  let updateCheckForm = null;
  let updateCheckSubmit = null;
  let updateForm = null;
  let updateSubmit = null;
  let updateCard = null;
  let updateCurrentVersion = null;
  let updateAvailableVersion = null;
  let updateAvailabilityNote = null;
  let updateSummaryToggle = null;
  let updatePill = null;
  let updateInlinePill = null;
  let updateTimestamp = null;
  let updateMessage = null;
  let updateMeta = null;
  let updateState = null;
  let updateStateLine = null;
  let updateActiveState = null;
  let updateSubState = null;
  let updateResultState = null;
  let updateExitState = null;
  let updateUnit = null;
  let updateScript = null;
  let updateLogBlock = null;
  let updateLog = null;
  let updateSummary = null;
  let updateCreatedAt = null;
  const refreshUpdateRefs = () => {
    updateCheckForm = document.querySelector("[data-update-check-form]");
    updateCheckSubmit = document.querySelector("[data-update-check-submit]");
    updateForm = document.querySelector("[data-update-form]");
    updateSubmit = document.querySelector("[data-update-submit]");
    updateCard = document.querySelector("[data-update-card]");
    updateCurrentVersion = document.querySelector("[data-update-current-version]");
    updateAvailableVersion = document.querySelector("[data-update-available-version]");
    updateAvailabilityNote = document.querySelector("[data-update-availability-note]");
    updateSummaryToggle = document.querySelector("[data-update-summary-toggle]");
    updatePill = document.querySelector("[data-update-pill]");
    updateInlinePill = document.querySelector("[data-update-inline-pill]");
    updateTimestamp = document.querySelector("[data-update-timestamp]");
    updateMessage = document.querySelector("[data-update-message]");
    updateMeta = document.querySelector("[data-update-meta]");
    updateState = document.querySelector("[data-update-state]");
    updateStateLine = document.querySelector("[data-update-state-line]");
    updateActiveState = document.querySelector("[data-update-active-state]");
    updateSubState = document.querySelector("[data-update-sub-state]");
    updateResultState = document.querySelector("[data-update-result-state]");
    updateExitState = document.querySelector("[data-update-exit-state]");
    updateUnit = document.querySelector("[data-update-unit]");
    updateScript = document.querySelector("[data-update-script]");
    updateLogBlock = document.querySelector("[data-update-log-block]");
    updateLog = document.querySelector("[data-update-log]");
    updateSummary = document.querySelector("[data-update-summary]");
    updateCreatedAt = document.querySelector("[data-update-created-at]");
  };
  refreshUpdateRefs();
  const initialUpdateVersion = dashboardConfig.initialUpdateVersion || {};
  let latestUpdateVersion = initialUpdateVersion;
  let updatePollTimer = null;
  let updateSubmitVersion = initialUpdateVersion?.current_version || "dev";
  let updateTargetVersion = initialUpdateVersion?.available_version || "";
  let updateStartedAt = 0;
  let updatePostSuccessPollCount = 0;
  let updateTransientFailureCount = 0;
  let updateProgressFloor = 0;
  let updateDisplayStage = { headline: "", substep: "" };
  const UPDATE_POLL_INTERVAL_MS = 4000;
  const UPDATE_POST_SUCCESS_POLLS_MAX = 12;
  const UPDATE_TRANSIENT_FAILURES_MAX = 8;
  const ERROR_INST_PATTERN = /\bError\s+CNC-\d{5}-([0-9A-HJKMNPQRSTVWXYZ]{8})\b/i;

  const debugBundleFilename = (response, fallbackName) => {
    const disposition = response.headers.get("content-disposition") || "";
    const match = disposition.match(/filename="?([^";]+)"?/i);
    return match?.[1] || fallbackName;
  };

  const downloadBundle = async (url, fallbackName, button) => {
    if (!url || !button) return;
    const previousText = button.textContent;
    button.disabled = true;
    button.classList.add("is-loading");
    button.textContent = "Collecting";
    try {
      const response = await fetch(url, {
        credentials: "same-origin",
        headers: { Accept: "application/zip" },
      });
      if (!response.ok) throw new Error(`bundle request failed: ${response.status}`);
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = debugBundleFilename(response, fallbackName);
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    } catch (error) {
      console.error("bundle download failed", error);
      button.textContent = "Failed";
      window.setTimeout(() => {
        button.textContent = previousText || "Download Debug Bundle";
      }, 1400);
    } finally {
      button.disabled = false;
      button.classList.remove("is-loading");
      if (button.textContent === "Collecting") {
        button.textContent = previousText || "Download Debug Bundle";
      }
    }
  };

  const downloadErrorDebugBundle = async (errorInst, button) => {
    if (!errorInst || !button) return;
    await downloadBundle(
      `/api/support/errors/${encodeURIComponent(errorInst)}/bundle.zip`,
      `cnc-debug-${errorInst}.zip`,
      button,
    );
  };

  const enhanceErrorBanner = (banner) => {
    if (!banner || banner.dataset.debugBundleBound === "1") return;
    const message = banner.querySelector("span")?.textContent || banner.textContent || "";
    const errorInst = message.match(ERROR_INST_PATTERN)?.[1]?.toUpperCase();
    if (!errorInst) return;
    banner.dataset.debugBundleBound = "1";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "banner-debug-button";
    button.textContent = "Download Debug Bundle";
    button.addEventListener("click", () => {
      downloadErrorDebugBundle(errorInst, button);
    });
    const close = banner.querySelector(".banner-close");
    if (close) {
      close.insertAdjacentElement("beforebegin", button);
    } else {
      banner.appendChild(button);
    }
  };

  const setBanner = (kind, message) => {
    document.querySelectorAll(".banner").forEach((banner) => banner.remove());
    if (!message) return;
    const banner = document.createElement("section");
    banner.className = `banner ${kind}`;
    const copy = document.createElement("span");
    copy.textContent = message;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "banner-close";
    close.setAttribute("aria-label", "Dismiss message");
    close.textContent = "\u00d7";
    banner.append(copy, close);
    if (kind === "error") enhanceErrorBanner(banner);
    const tabbar = document.querySelector(".tabbar");
    if (tabbar && tabbar.parentNode) {
      tabbar.insertAdjacentElement("afterend", banner);
    }
  };
  document.querySelectorAll(".banner.error").forEach(enhanceErrorBanner);
  document.querySelectorAll("[data-support-bundle-button]").forEach((button) => {
    button.addEventListener("click", () => {
      downloadBundle("/api/support/bundle.zip", "cnc-support.zip", button);
    });
  });

    const setUpdatePending = (pending) => {
      refreshUpdateRefs();
      if (!updateSubmit) return;
      const hasUpdate = Boolean(latestUpdateVersion?.has_update);
      const availableVersion = latestUpdateVersion?.available_version || "";
      const availabilityError = latestUpdateVersion?.error || "";
      updateSubmit.disabled = pending || !hasUpdate;
      if (pending) {
        updateSubmit.textContent = "Updating...";
        return;
      }
      if (hasUpdate && availableVersion) {
        if (updateForm) updateForm.hidden = false;
        updateSubmit.textContent = `Update to ${availableVersion}`;
        return;
      }
      if (updateForm) updateForm.hidden = true;
      updateSubmit.textContent = availabilityError ? "Update unavailable" : "Up to date";
    };

  const setUpdateCheckPending = (pending) => {
    refreshUpdateRefs();
    if (!updateCheckSubmit) return;
    updateCheckSubmit.disabled = pending;
    updateCheckSubmit.textContent = pending ? "Checking..." : "Check for updates";
  };

  const renderUpdateVersion = (updateVersion) => {
    refreshUpdateRefs();
    latestUpdateVersion = updateVersion || {};
    const currentVersion = latestUpdateVersion.current_version || "dev";
    const availableVersion = latestUpdateVersion.available_version || "";
    const hasUpdate = Boolean(latestUpdateVersion.has_update);
    const error = latestUpdateVersion.error || "";
    const ref = latestUpdateVersion.ref || "main";
    const checkedAt = latestUpdateVersion.checked_at || "";
    if (updateCurrentVersion) {
      updateCurrentVersion.textContent = currentVersion;
    }
    if (updateAvailableVersion) {
      updateAvailableVersion.textContent = (hasUpdate && availableVersion)
        ? availableVersion
        : (error ? "unavailable" : (checkedAt ? "up to date" : "not checked"));
    }
    if (updateAvailabilityNote) {
      if (error) {
        updateAvailabilityNote.textContent = `Update check failed: ${error}`;
      } else if (hasUpdate && availableVersion) {
        updateAvailabilityNote.textContent = `New version ${availableVersion} is available on ${ref}.`;
      } else if (checkedAt) {
        updateAvailabilityNote.textContent = `Last checked ${formatLocalTimestamp(checkedAt) || checkedAt}.`;
      } else {
        updateAvailabilityNote.textContent = "";
      }
    }
  };

  const renderUpdate = (lastUpdate) => {
    refreshUpdateRefs();
    const hasLastUpdate = Boolean(lastUpdate?.status || lastUpdate?.created_at || lastUpdate?.message);
    const status = hasLastUpdate ? (lastUpdate?.status || "idle") : "idle";
    const message = hasLastUpdate
      ? (lastUpdate?.message || "Checks out the latest configured GitHub revision in a background unit.")
      : "Checks out the latest configured GitHub revision in a background unit.";
    const createdAt = hasLastUpdate ? (lastUpdate?.created_at || "no runs yet") : "no runs yet";
    const details = hasLastUpdate ? (lastUpdate?.details || {}) : {};

    document.body.dataset.updateStatus = status;
    if (updateCard) updateCard.className = "run-group";
    if (updateSummaryToggle) updateSummaryToggle.className = `run-summary ${status}`;
    if (updatePill) {
      updatePill.className = `pill ${hasLastUpdate ? status : "inactive"}`;
      updatePill.textContent = status;
    }
    if (updateInlinePill) {
      updateInlinePill.hidden = !hasLastUpdate || status === "success";
      updateInlinePill.className = `pill ${hasLastUpdate ? status : "inactive"}`;
      updateInlinePill.textContent = hasLastUpdate && status !== "success" ? status : "";
    }
    if (updateTimestamp) {
      updateTimestamp.textContent = hasLastUpdate ? (formatLocalTimestamp(createdAt) || createdAt) : "no runs yet";
      if (hasLastUpdate) {
        updateTimestamp.dataset.localTime = createdAt;
      } else {
        delete updateTimestamp.dataset.localTime;
      }
    }
    if (updateMessage) updateMessage.textContent = message;
    if (updateSummary) {
      updateSummary.textContent = hasLastUpdate ? `${status} \u2014 ${message}` : "no runs yet";
    }
    if (updateCreatedAt) {
      const formatted = hasLastUpdate ? formatLocalTimestamp(lastUpdate?.created_at) : "";
      updateCreatedAt.textContent = formatted || "never";
      updateCreatedAt.dataset.localTime = hasLastUpdate ? (lastUpdate?.created_at || "") : "";
    }
    if (updateCard) {
      updateCard.hidden = !hasLastUpdate || !["error", "failed", "queued", "running"].includes(status);
    }
    const unitState = details.unit_state || {};
    const activeState = unitState.ActiveState || "";
    const subState = unitState.SubState || "";
    const resultState = unitState.Result || "";
    const exitState = unitState.ExecMainStatus || "";
    const hasUnitState = Boolean(activeState || subState || resultState || exitState);
    if (updateState) updateState.hidden = !hasUnitState;
    if (updateStateLine) {
      const stateMain = activeState || status;
      const stateDetail = subState || message;
      updateStateLine.textContent = hasUnitState ? `${stateMain} - ${stateDetail}` : "";
    }
    if (updateActiveState) updateActiveState.textContent = activeState || "-";
    if (updateSubState) updateSubState.textContent = subState || "-";
    if (updateResultState) updateResultState.textContent = resultState || "-";
    if (updateExitState) updateExitState.textContent = exitState || "-";
    if (updateMeta) {
      const hasMeta = Boolean(details.unit || details.script);
      updateMeta.hidden = !hasMeta;
    }
    if (updateUnit) {
      updateUnit.hidden = !details.unit;
      updateUnit.textContent = details.unit || "";
    }
    if (updateScript) {
      updateScript.hidden = !details.script;
      updateScript.textContent = details.script || "";
    }
    if (updateLogBlock) {
      updateLogBlock.hidden = !details.log_excerpt;
    }
    if (updateLog) {
      updateLog.textContent = details.log_excerpt || "";
    }
    setUpdatePending(status === "queued" || status === "running");
  };

  const scheduleUpdatePoll = () => {
    if (updatePollTimer) {
      window.clearTimeout(updatePollTimer);
    }
    updatePollTimer = window.setTimeout(pollUpdateStatus, UPDATE_POLL_INTERVAL_MS);
  };

  const updateVersionRefreshSettled = (updateVersion) => {
    const currentVersion = String(updateVersion?.current_version || "").trim();
    const hasUpdate = Boolean(updateVersion?.has_update);
    if (!currentVersion) return false;
    if (updateTargetVersion && currentVersion === updateTargetVersion) return true;
    if (updateSubmitVersion && currentVersion !== updateSubmitVersion) return true;
    return !hasUpdate && (!updateSubmitVersion || currentVersion !== updateSubmitVersion);
  };

  const updateFlowActive = () => {
    const status = document.body.dataset.updateStatus || "";
    return status === "queued" || status === "running" || updatePostSuccessPollCount > 0;
  };

  const resetUpdateProgress = () => {
    updateProgressFloor = 0;
    updateDisplayStage = { headline: "", substep: "" };
  };

  const showUpdateProgress = ({ headline, substep, meta = [] }) => {
    const steps = saveProgressPipelines.updateCnc || [];
    const nextProgress = progressForStage(steps, headline, substep, updateProgressFloor);
    const regressed = nextProgress < updateProgressFloor;
    const displayProgress = regressed ? updateProgressFloor : nextProgress;
    const displayStage = regressed && updateDisplayStage.headline
      ? updateDisplayStage
      : { headline, substep };
    if (!regressed) {
      updateProgressFloor = nextProgress;
      updateDisplayStage = displayStage;
    }
    showFloatingSave({
      tone: "running",
      title: "Updating CNC",
      headline: displayStage.headline,
      substep: displayStage.substep,
      meta,
      progress: displayProgress,
      steps,
      activeIndex: progressStepIndex(steps, displayProgress),
    });
  };

  const loadUpdateStatusPayload = async () => {
    const response = await fetch("/api/status", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    const responseText = await response.text();
    let payload = {};
    if (responseText) {
      try {
        payload = JSON.parse(responseText);
      } catch (_error) {
        payload = {};
      }
    }
    if (!response.ok) {
      throw new Error(payload.detail || responseText || `${response.status} ${response.statusText}`.trim());
    }
    return payload;
  };

  const refreshUpdateStatusCard = async () => {
    const payload = await loadUpdateStatusPayload();
    try {
      renderUpdateVersion(payload.update_version);
      renderUpdate(payload.last_update);
    } catch (error) {
      console.error("update status render failed", { error, payload });
      throw error;
    }
    return payload;
  };

  const pollUpdateStatus = async () => {
    try {
      const payload = await refreshUpdateStatusCard();
      updateTransientFailureCount = 0;
      const nextStatus = payload.last_update?.status;
      if (nextStatus === "queued" || nextStatus === "running") {
        showUpdateProgress({
          headline: nextStatus === "queued" ? "Queue update" : "Update CNC",
          substep: nextStatus === "queued"
            ? "Watching background updater"
            : (payload.last_update?.message || "Applying release"),
          meta: [],
        });
        scheduleUpdatePoll();
        return;
      }
      if (nextStatus === "success") {
        if (!updateVersionRefreshSettled(payload.update_version) && updatePostSuccessPollCount < UPDATE_POST_SUCCESS_POLLS_MAX) {
          updatePostSuccessPollCount += 1;
          setBanner("success", "Update completed. Waiting for admin service restart to refresh the version card...");
          showUpdateProgress({
            headline: "Verify update",
            substep: "Refreshing version card",
            meta: [],
          });
          scheduleUpdatePoll();
          return;
        }
        updatePostSuccessPollCount = 0;
        const currentVersion = payload.update_version?.current_version || "latest";
        setBanner("success", `Update completed. Running ${currentVersion}.`);
        showFloatingSave({
          tone: "success",
          title: "Update completed",
          headline: `Running ${currentVersion}`,
          note: `CNC is running ${currentVersion}.`,
          meta: updateStartedAt ? terminalMutationMeta(updateStartedAt) : [],
          progress: 100,
          steps: saveProgressPipelines.updateCnc,
          activeIndex: saveProgressPipelines.updateCnc.length - 1,
        });
      } else if (nextStatus === "error") {
        updatePostSuccessPollCount = 0;
        setBanner("error", `Update failed: ${JSON.stringify(payload.last_update?.details || {})}`);
        showFloatingSave({
          tone: "error",
          title: "Update failed",
          headline: "Review update log",
          note: payload.last_update?.message || "Update failed.",
          meta: updateStartedAt ? terminalMutationMeta(updateStartedAt) : [],
          progress: 100,
          steps: saveProgressPipelines.updateCnc,
          activeIndex: saveProgressPipelines.updateCnc.length - 1,
        });
      }
    } catch (error) {
      console.error("update status poll failed", error);
      if (updateFlowActive() && updateTransientFailureCount < UPDATE_TRANSIENT_FAILURES_MAX) {
        updateTransientFailureCount += 1;
        setBanner("success", "Waiting for the admin service to come back after update restart...");
        showUpdateProgress({
          headline: "Restart CNC",
          substep: "Waiting for admin service",
          meta: [],
        });
        scheduleUpdatePoll();
        return;
      }
      updatePostSuccessPollCount = 0;
      setUpdatePending(false);
      setBanner("error", `Update status poll failed: ${error.message}`);
    }
  };

  document.addEventListener("submit", async (event) => {
    const submittedForm = event.target;
    if (!(submittedForm instanceof HTMLFormElement) || !submittedForm.matches("[data-update-form]")) return;
    event.preventDefault();
      if (updatePollTimer) {
        window.clearTimeout(updatePollTimer);
        updatePollTimer = null;
      }
      refreshUpdateRefs();
      setBanner("", "");
      updateSubmitVersion = latestUpdateVersion?.current_version || "dev";
      updateTargetVersion = latestUpdateVersion?.available_version || "";
      updateStartedAt = performance.now();
      updatePostSuccessPollCount = 0;
      updateTransientFailureCount = 0;
      resetUpdateProgress();
      setUpdatePending(true);
      showUpdateProgress({
        headline: "Starting update",
        substep: "Sending update request",
        meta: [],
      });
      const formData = new FormData(submittedForm);
      try {
        let csrfHeader = String(formData.get("csrf_token") || "");
        let response = await fetch("/api/update", {
          method: "POST",
          credentials: "same-origin",
          headers: {
            Accept: "application/json",
            "X-CSRF-Token": csrfHeader,
          },
        });
        if (response.status === 403) {
          const token = await refreshCsrfTokens();
          if (token) {
            csrfHeader = token;
            response = await fetch("/api/update", {
              method: "POST",
              credentials: "same-origin",
              headers: {
                Accept: "application/json",
                "X-CSRF-Token": csrfHeader,
              },
            });
          }
        }
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.detail || "update request failed");
        }
        renderUpdate(payload);
        showUpdateProgress({
          headline: "Queue update",
          substep: "Watching background updater",
          meta: [],
        });
        setBanner("success", "Update running in background.");
        scheduleUpdatePoll();
      } catch (error) {
        setUpdatePending(false);
        setBanner("error", `Update failed: ${error.message}`);
        showFloatingSave({
          tone: "error",
          title: "Update failed",
          headline: "Request failed",
          note: `Update failed: ${error.message}`,
          meta: terminalMutationMeta(updateStartedAt),
          progress: 100,
          steps: saveProgressPipelines.updateCnc,
          activeIndex: saveProgressPipelines.updateCnc.length - 1,
        });
      }
  });

  document.addEventListener("submit", async (event) => {
    const submittedForm = event.target;
    if (!(submittedForm instanceof HTMLFormElement) || !submittedForm.matches("[data-update-check-form]")) return;
    event.preventDefault();
      refreshUpdateRefs();
      setBanner("", "");
      setUpdateCheckPending(true);
      const formData = new FormData(submittedForm);
      try {
        let csrfHeader = String(formData.get("csrf_token") || "");
        let response = await fetch("/api/update/check", {
          method: "POST",
          credentials: "same-origin",
          headers: {
            Accept: "application/json",
            "X-CSRF-Token": csrfHeader,
          },
        });
        if (response.status === 403) {
          const token = await refreshCsrfTokens();
          if (token) {
            csrfHeader = token;
            response = await fetch("/api/update/check", {
              method: "POST",
              credentials: "same-origin",
              headers: {
                Accept: "application/json",
                "X-CSRF-Token": csrfHeader,
              },
            });
          }
        }
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.detail || "update check failed");
        }
        renderUpdateVersion(payload);
        setUpdateCheckPending(false);
        refreshUpdateStatusCard()
          .then((statusPayload) => {
            const nextStatus = statusPayload.last_update?.status;
            if (nextStatus === "queued" || nextStatus === "running") {
              scheduleUpdatePoll();
            }
          })
          .catch((statusError) => {
            console.error("update status refresh after check failed", statusError);
            setUpdatePending(false);
          });
      } catch (error) {
        setBanner("error", `Update check failed: ${error.message}`);
      } finally {
        setUpdateCheckPending(false);
      }
  });

  renderUpdateVersion(initialUpdateVersion);
  renderLocalTimes(document);
  resumeDashboardOperations();
  initializeHostMetricsPanel();
  afterTabSelected(document.querySelector("[data-tab].is-active")?.dataset.tab || requestedTab);
  const updateStatus = document.body.dataset.updateStatus;
  if (updateStatus === "queued" || updateStatus === "running") {
    setUpdatePending(true);
    scheduleUpdatePoll();
  }
  scheduleServerLoadPoll();
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    scheduleServerLoadPoll();
  });
  window.addEventListener("pagehide", () => {
    hostMetricsAbortController?.abort();
  });
})();
