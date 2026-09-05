(() => {
  if (window.cncMetricEventMarkers) return;

  const TOOLTIP_ATTRIBUTE = "data-metric-event-tooltip";
  const DEFAULT_HOVER_RADIUS = 16;
  const DEFAULT_VERTICAL_PADDING = 18;
  const normalizedEventsCache = new WeakMap();

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  const normalizeEvents = (events) => {
    if (!Array.isArray(events)) return [];
    const cached = normalizedEventsCache.get(events);
    if (cached) return cached;
    const normalized = events
      .map((event) => {
        const timestampMs = Number(event?.timestampMs);
        const timestamp = Number.isFinite(timestampMs) ? timestampMs : Date.parse(event?.timestamp);
        if (!Number.isFinite(timestamp)) return null;
        return { ...event, timestamp: new Date(timestamp).toISOString(), timestampMs: timestamp };
      })
      .filter(Boolean);
    normalizedEventsCache.set(events, normalized);
    return normalized;
  };

  const markerPalette = (event) => {
    const kind = String(event?.kind || "").toLowerCase();
    const severity = String(event?.severity || "").toLowerCase();
    if (severity === "error") {
      return {
        band: "rgba(255, 115, 136, 0.12)",
        line: "rgba(255, 115, 136, 0.88)",
        glow: "rgba(255, 115, 136, 0.28)",
      };
    }
    if (severity === "warn" || severity === "warning" || kind === "auto_size_resized") {
      return {
        band: "rgba(244, 203, 123, 0.13)",
        line: "rgba(244, 203, 123, 0.9)",
        glow: "rgba(244, 203, 123, 0.28)",
      };
    }
    return {
      band: "rgba(155, 240, 199, 0.11)",
      line: "rgba(155, 240, 199, 0.82)",
      glow: "rgba(155, 240, 199, 0.24)",
    };
  };

  const eventTitle = (event) => (
    String(event?.label || event?.summary || event?.kind || "Event").trim()
  );

  const defaultTimestamp = (timestamp) => {
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) return "";
    const weekday = date.toLocaleDateString([], { weekday: "short" });
    const monthDay = date.toLocaleDateString([], { month: "short", day: "numeric" });
    const time = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
    return `${weekday}, ${monthDay} ${time}`;
  };

  const eventRows = (event) => {
    const rows = Array.isArray(event?.rows) ? event.rows : [];
    return rows
      .map((row) => ({
        label: String(row?.label || "").trim(),
        value: String(row?.value || "").trim(),
      }))
      .filter((row) => row.label && row.value)
      .slice(0, 4);
  };

  const tooltipHtml = (event, formatTimestamp) => {
    const timestamp = event?.timestampMs ?? Date.parse(event?.timestamp);
    const timeLabel = Number.isFinite(timestamp)
      ? (typeof formatTimestamp === "function" ? formatTimestamp(timestamp) : defaultTimestamp(timestamp))
      : "";
    const rows = eventRows(event);
    const summary = String(event?.summary || "").trim();
    return `
      <div class="metrics-event-tooltip-time">${escapeHtml(timeLabel)}</div>
      <div class="metrics-event-tooltip-title">${escapeHtml(eventTitle(event))}</div>
      ${summary && summary !== eventTitle(event)
        ? `<div class="metrics-event-tooltip-summary">${escapeHtml(summary)}</div>`
        : ""}
      ${rows.length ? `
        <div class="metrics-event-tooltip-rows">
          ${rows.map((row) => `
            <div class="metrics-event-tooltip-row">
              <span>${escapeHtml(row.label)}</span>
              <strong>${escapeHtml(row.value)}</strong>
            </div>
          `).join("")}
        </div>
      ` : ""}
    `;
  };

  const frameForChart = (chart) => (
    chart.canvas.closest(".metrics-chart-frame") || chart.canvas.parentElement
  );

  const existingTooltip = (chart) => {
    const frame = frameForChart(chart);
    return frame?.querySelector(`[${TOOLTIP_ATTRIBUTE}]`) || null;
  };

  const ensureTooltip = (chart) => {
    const frame = frameForChart(chart);
    if (!frame) return null;
    let tooltip = existingTooltip(chart);
    if (!tooltip) {
      tooltip = document.createElement("div");
      tooltip.className = "metrics-event-tooltip";
      tooltip.setAttribute(TOOLTIP_ATTRIBUTE, "");
      tooltip.hidden = true;
      frame.appendChild(tooltip);
    }
    return tooltip;
  };

  const hideTooltip = (chart) => {
    const tooltip = existingTooltip(chart);
    if (tooltip) tooltip.hidden = true;
    chart.canvas.classList.remove("has-metric-event-hover");
    chart.$cncMetricActiveEvent = null;
  };

  const visibleMarkers = (chart, options) => {
    const events = normalizeEvents(options?.events);
    const xScale = chart.scales?.x;
    const area = chart.chartArea;
    if (!xScale || !area) return [];
    return events
      .map((event) => {
        const x = xScale.getPixelForValue(event.timestampMs);
        if (!Number.isFinite(x) || x < area.left || x > area.right) return null;
        return { event, x };
      })
      .filter(Boolean);
  };

  const nearestMarker = (chart, options, x) => {
    const markers = visibleMarkers(chart, options);
    const radius = Number(options?.hoverRadius) || DEFAULT_HOVER_RADIUS;
    let nearest = null;
    let nearestDistance = Infinity;
    markers.forEach((marker) => {
      const distance = Math.abs(marker.x - x);
      if (distance < nearestDistance) {
        nearest = marker;
        nearestDistance = distance;
      }
    });
    return nearest && nearestDistance <= radius ? nearest : null;
  };

  const isInsideMarkerHoverArea = (chart, x, y, options) => {
    const area = chart.chartArea;
    if (!area) return false;
    const padding = Number(options?.verticalPadding) || DEFAULT_VERTICAL_PADDING;
    return Number.isFinite(x)
      && Number.isFinite(y)
      && x >= area.left
      && x <= area.right
      && y >= area.top - padding
      && y <= area.bottom + padding;
  };

  const markerOptionsForChart = (chart) => (
    chart.config?._config?.options?.plugins?.cncMetricEventMarkers
    || chart.config?.options?.plugins?.cncMetricEventMarkers
    || {}
  );

  const placeTooltip = (chart, tooltip, marker, pointerY) => {
    const frame = frameForChart(chart);
    if (!frame) return;
    const canvasRect = chart.canvas.getBoundingClientRect();
    const frameRect = frame.getBoundingClientRect();
    const canvasLeft = canvasRect.left - frameRect.left;
    const canvasTop = canvasRect.top - frameRect.top;
    const y = Number.isFinite(pointerY)
      ? pointerY
      : (chart.chartArea.top + chart.chartArea.bottom) / 2;

    let left = canvasLeft + marker.x + 12;
    let top = canvasTop + y;
    tooltip.hidden = false;
    const width = tooltip.offsetWidth || 280;
    const height = tooltip.offsetHeight || 140;
    if (left + width + 8 > frame.clientWidth) {
      left = canvasLeft + marker.x - width - 12;
    }
    top = Math.max(8, Math.min(frame.clientHeight - height - 8, top - height / 2));
    tooltip.style.left = `${Math.max(8, left)}px`;
    tooltip.style.top = `${top}px`;
  };

  const handlePointerMove = (chart, nativeEvent) => {
    const rect = chart.canvas.getBoundingClientRect();
    const x = nativeEvent.clientX - rect.left;
    const y = nativeEvent.clientY - rect.top;
    const options = markerOptionsForChart(chart);
    if (!isInsideMarkerHoverArea(chart, x, y, options)) {
      const hadActiveEvent = Boolean(chart.$cncMetricActiveEvent);
      hideTooltip(chart);
      if (hadActiveEvent) chart.draw();
      return;
    }
    const marker = nearestMarker(chart, options, x);
    if (!marker) {
      const hadActiveEvent = Boolean(chart.$cncMetricActiveEvent);
      hideTooltip(chart);
      if (hadActiveEvent) chart.draw();
      return;
    }
    const tooltip = ensureTooltip(chart);
    if (!tooltip) return;
    const previousEvent = chart.$cncMetricActiveEvent;
    tooltip.innerHTML = tooltipHtml(marker.event, options?.formatTimestamp);
    chart.canvas.classList.add("has-metric-event-hover");
    chart.$cncMetricActiveEvent = marker.event;
    placeTooltip(chart, tooltip, marker, y);
    if (previousEvent !== marker.event) chart.draw();
  };

  const handlePointerLeave = (chart) => {
    const hadActiveEvent = Boolean(chart.$cncMetricActiveEvent);
    hideTooltip(chart);
    if (hadActiveEvent) chart.draw();
  };

  const plugin = {
    id: "cncMetricEventMarkers",
    afterInit(chart) {
      chart.$cncMetricActiveEvent = null;
    },
    afterDatasetsDraw(chart) {
      const markers = visibleMarkers(chart, markerOptionsForChart(chart));
      if (!markers.length) return;
      const { ctx, chartArea } = chart;
      ctx.save();
      markers.forEach((marker) => {
        const palette = markerPalette(marker.event);
        const active = chart.$cncMetricActiveEvent === marker.event;
        const x = Math.round(marker.x) + 0.5;
        ctx.fillStyle = palette.band;
        ctx.fillRect(x - (active ? 3 : 2), chartArea.top, active ? 6 : 4, chartArea.bottom - chartArea.top);
        ctx.save();
        ctx.shadowColor = palette.glow;
        ctx.shadowBlur = active ? 14 : 8;
        ctx.strokeStyle = palette.line;
        ctx.lineWidth = active ? 2.5 : 1.5;
        ctx.beginPath();
        ctx.moveTo(x, chartArea.top + 2);
        ctx.lineTo(x, chartArea.bottom);
        ctx.stroke();
        ctx.restore();
        ctx.fillStyle = palette.line;
        ctx.fillRect(x - 3, chartArea.top + 8, 6, 14);
      });
      ctx.restore();
    },
    afterEvent(chart, args) {
      const event = args?.event;
      if (!event) return;
      if (event.type === "mouseout") {
        const hadActiveEvent = Boolean(chart.$cncMetricActiveEvent);
        hideTooltip(chart);
        args.changed = hadActiveEvent;
        return;
      }
      if (!["mousemove", "touchmove", "click"].includes(event.type)) return;
      const { x, y } = event;
      const options = markerOptionsForChart(chart);
      if (!isInsideMarkerHoverArea(chart, x, y, options)) {
        const hadActiveEvent = Boolean(chart.$cncMetricActiveEvent);
        hideTooltip(chart);
        args.changed = hadActiveEvent;
        return;
      }
      const marker = nearestMarker(chart, options, x);
      if (!marker) {
        const hadActiveEvent = Boolean(chart.$cncMetricActiveEvent);
        hideTooltip(chart);
        args.changed = hadActiveEvent;
        return;
      }
      const tooltip = ensureTooltip(chart);
      if (!tooltip) return;
      const previousEvent = chart.$cncMetricActiveEvent;
      if (chart.$cncMetricActiveEvent !== marker.event) {
        tooltip.innerHTML = tooltipHtml(marker.event, options?.formatTimestamp);
      }
      chart.canvas.classList.add("has-metric-event-hover");
      chart.$cncMetricActiveEvent = marker.event;
      placeTooltip(chart, tooltip, marker, y);
      args.changed = previousEvent !== marker.event;
    },
    beforeDestroy(chart) {
      const tooltip = existingTooltip(chart);
      tooltip?.remove();
    },
  };

  window.cncMetricEventMarkers = { plugin, normalizeEvents };
})();
