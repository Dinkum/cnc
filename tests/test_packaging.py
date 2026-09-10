import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from app.services import runtime_assets


def _dashboard_source_text() -> str:
    return "\n".join(
        (
            Path("app/templates/index.html").read_text(encoding="utf-8"),
            Path("app/static/js/cnc-ui.js").read_text(encoding="utf-8"),
            Path("app/static/js/dashboard.js").read_text(encoding="utf-8"),
        )
    )


def _output_detail_source_text() -> str:
    return "\n".join(
        (
            Path("app/templates/output_detail.html").read_text(encoding="utf-8"),
            Path("app/static/js/cnc-ui.js").read_text(encoding="utf-8"),
            Path("app/static/js/output-detail.js").read_text(encoding="utf-8"),
        )
    )


def _template_behavior_text(template_path: str) -> str:
    if template_path == "app/templates/index.html":
        return _dashboard_source_text()
    return _output_detail_source_text()


def test_managed_systemd_assets_are_packaged_and_synced() -> None:
    install_text = Path("scripts/install_ubuntu.sh").read_text(encoding="utf-8")
    update_text = Path("scripts/update_from_github.sh").read_text(encoding="utf-8")

    for source_name, destination_name in runtime_assets.MANAGED_SYSTEMD_ASSETS:
        source_path = Path("packaging") / source_name
        assert source_path.exists()
        assert source_name in install_text
        assert source_name in update_text
        assert destination_name in install_text
        assert destination_name in update_text

    for slice_name in (
        "cnc-edge-public-ingress.slice",
        "cnc-edge-tailnet.slice",
        "cnc-admin-control.slice",
        "cnc-apps.slice",
    ):
        content = (Path("packaging/systemd") / slice_name).read_text(encoding="utf-8")
        assert "MemoryAccounting=yes" in content
        assert "CPUAccounting=yes" in content


def test_shield_image_installs_pinned_requirements_before_local_package() -> None:
    containerfile = Path("packaging/shield/Containerfile").read_text(encoding="utf-8")

    assert "COPY pyproject.toml README.md requirements.txt /opt/cnc/" in containerfile
    assert (
        "/opt/cnc/.venv/bin/pip install --no-cache-dir --require-hashes --only-binary=:all: -r /opt/cnc/requirements.txt"
        in containerfile
    )
    assert "/opt/cnc/.venv/bin/pip install --no-cache-dir --no-deps ." in containerfile
    assert "/opt/cnc/.venv/bin/pip install --no-cache-dir ." not in containerfile


def test_updater_disables_rollback_after_migration_boundary() -> None:
    script = Path("scripts/update_from_github.sh").read_text(encoding="utf-8")

    assert "MIGRATION_BOUNDARY_CROSSED=0" in script
    assert '[[ "${MIGRATION_BOUNDARY_CROSSED}" == "1" ]]' in script
    assert "automatic rollback to previous release is disabled" in script
    assert "MIGRATION_BOUNDARY_CROSSED=1" in script
    assert "old-release rollback is disabled after service restart begins" in script
    boundary_pos = script.index("MIGRATION_BOUNDARY_CROSSED=1")
    wrapper_sync_pos = script.index("if ! sync_cli_wrappers; then")
    restart_pos = script.index(
        'if ! systemctl restart "${CNC_SERVICE_NAME}"',
        boundary_pos,
    )
    assert wrapper_sync_pos < boundary_pos
    assert boundary_pos < restart_pos


def test_package_discovery_includes_app_subpackages() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    find_config = pyproject["tool"]["setuptools"]["packages"]["find"]

    assert find_config["include"] == ["app*", "migrations*"]
    assert Path("migrations/__init__.py").exists()
    assert Path("migrations/versions/__init__.py").exists()


def test_main_templates_use_device_width_viewport() -> None:
    for template_path in (
        "app/templates/index.html",
        "app/templates/output_detail.html",
    ):
        template_text = _template_behavior_text(template_path)
        assert (
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            in template_text
        )
        assert "width=980" not in template_text


def test_output_detail_behavior_is_external_versioned_and_parseable() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the JavaScript parse smoke test")

    template_text = Path("app/templates/output_detail.html").read_text(encoding="utf-8")
    script_path = Path("app/static/js/output-detail.js")
    assert '<script id="output-detail-config" type="application/json">' in template_text
    assert (
        '<script src="/static/js/cnc-ui.js?v={{ asset_version }}"></script>'
        in template_text
    )
    assert (
        '<script src="/static/js/output-detail.js?v={{ asset_version }}"></script>'
        in template_text
    )
    assert not re.findall(r"<script>(.*?)</script>", template_text, flags=re.DOTALL)
    script_text = script_path.read_text(encoding="utf-8")
    shared_text = Path("app/static/js/cnc-ui.js").read_text(encoding="utf-8")
    assert "{{" not in script_text
    assert "normalizeTimestampValue," in script_text
    assert "normalizeTimestampValue," in shared_text

    result = subprocess.run(
        [node, "--check", str(script_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_dashboard_behavior_is_external_versioned_and_parseable() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the dashboard JavaScript parse smoke test")

    template_path = Path("app/templates/index.html")
    template_text = template_path.read_text(encoding="utf-8")
    script_paths = (
        Path("app/static/js/cnc-ui.js"),
        Path("app/static/js/dashboard.js"),
    )

    assert template_path.stat().st_size < 100_000
    assert '<script type="application/json" id="dashboard-config">' in template_text
    assert "/static/js/cnc-ui.js?v={{ asset_version }}" in template_text
    assert "/static/js/dashboard.js?v={{ asset_version }}" in template_text
    assert "const currentTabs" not in template_text
    assert all(path.exists() for path in script_paths)
    for script_path in script_paths:
        result = subprocess.run(
            [node, "--check", str(script_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


def test_beta_routing_uses_pinned_local_cytoscape_asset() -> None:
    template_text = _dashboard_source_text()
    asset_path = Path("app/static/vendor/cytoscape-3.33.2.min.js")

    assert asset_path.exists()
    assert asset_path.stat().st_size > 100_000
    assert (
        '<script src="/static/vendor/cytoscape-3.33.2.min.js"></script>'
        not in template_text
    )
    assert (
        'const ensureRouteGraphAsset = () => loadDeferredScript("/static/vendor/cytoscape-3.33.2.min.js");'
        in template_text
    )
    assert "ensureRouteGraphAsset()" in template_text
    assert "window.cytoscape" in template_text
    assert "data-route-graph-canvas" in template_text
    assert "data-route-graph-details" not in template_text
    assert "estimateRouteNodeHeight" in template_text
    assert '"text-overflow-wrap": "anywhere"' in template_text
    assert "fit: true" in template_text
    assert (
        "shell._routeGraph.fit(shell._routeGraph.elements(), graphPadding)"
        in template_text
    )
    assert "const routeGraphRenderKey = (rows, canvas) =>" in template_text
    assert (
        "shell._routeGraph && shell._routeGraphRenderKey === renderKey" in template_text
    )
    assert "sizeRouteGraphCanvas(canvas, rows)" in template_text
    assert "Math.min(1380, Math.max(430, 104 + (rowCount * 116)))" in template_text
    assert "const minGroupGap = 260;" in template_text
    assert "const outputStats = new Map();" in template_text
    assert "positionOutputsByInputCenter" in template_text
    assert 'const routeGraphTaxiTurn = () => "50%";' in template_text
    assert "autoungrabify: true" in template_text
    assert "userPanningEnabled: true" in template_text
    assert "maxZoom: 0.84" in template_text
    assert '"curve-style": "taxi"' in template_text
    assert '"taxi-direction": "horizontal"' in template_text
    assert '"taxi-turn": "data(taxiTurn)"' in template_text
    assert '"taxi-turn-min-distance": 18' in template_text
    assert '"curve-style": "segments"' not in template_text
    assert '"segment-distances": "data(laneDistance)"' not in template_text
    assert '"curve-style": "unbundled-bezier"' not in template_text
    assert "line-dash-offset" in template_text


def test_templates_use_pinned_local_chart_asset() -> None:
    asset_path = Path("app/static/vendor/chart-4.4.7.umd.min.js")
    marker_path = Path("app/static/js/metric-event-markers.js")

    assert asset_path.exists()
    assert asset_path.stat().st_size > 100_000
    assert marker_path.exists()
    assert "static/js/*.js" in Path("pyproject.toml").read_text(encoding="utf-8")
    for template_path in (
        "app/templates/index.html",
        "app/templates/output_detail.html",
    ):
        template_text = _template_behavior_text(template_path)
        assert (
            '<script src="/static/vendor/chart-4.4.7.umd.min.js"></script>'
            not in template_text
        )
        assert (
            '<script src="/static/js/metric-event-markers.js"></script>'
            not in template_text
        )
        assert (
            'await loadDeferredScript("/static/vendor/chart-4.4.7.umd.min.js");'
            in template_text
        )
        assert (
            'await loadDeferredScript("/static/js/metric-event-markers.js");'
            in template_text
        )
        assert "const ensureMetricChartAssets = async () =>" in template_text
        assert "cdn.jsdelivr.net/npm/chart.js" not in template_text


def test_metric_event_markers_use_forgiving_hit_area() -> None:
    marker_text = Path("app/static/js/metric-event-markers.js").read_text(
        encoding="utf-8"
    )

    assert "const DEFAULT_HOVER_RADIUS = 16;" in marker_text
    assert "const DEFAULT_VERTICAL_PADDING = 18;" in marker_text
    assert "const isInsideMarkerHoverArea = (chart, x, y, options) =>" in marker_text
    assert "y >= area.top - padding" in marker_text
    assert "y <= area.bottom + padding" in marker_text


def test_operation_event_watcher_centralizes_sse_and_poll_fallback() -> None:
    helper_text = Path("app/static/js/operation-events.js").read_text(encoding="utf-8")
    pyproject_text = Path("pyproject.toml").read_text(encoding="utf-8")

    assert "static/js/*.js" in pyproject_text
    assert (
        "const operationUrl = (operationId) => `/api/operations/${encodeURIComponent(operationId)}`;"
        in helper_text
    )
    assert (
        "const operationEventsUrl = (operationId) => "
        "`/api/operations/${encodeURIComponent(operationId)}/events`;"
    ) in helper_text
    assert "new EventSource(operationEventsUrl(operationId))" in helper_text
    assert "fetch(operationUrl(operationId)" in helper_text
    assert "onFallback" in helper_text
    assert "maxPollFailures" in helper_text
    assert 'new URL("/api/active-operations", window.location.origin)' in helper_text
    assert (
        'new URL("/api/operations/active", window.location.origin)' not in helper_text
    )
    assert "retryDelayMs" in helper_text
    assert "exhausted: Number.isFinite(maxPollFailures)" in helper_text
    assert (
        "settle.reject(error);"
        not in helper_text.split("const handleRetry", 1)[1].split("const poll", 1)[0]
    )

    for template_path in (
        "app/templates/index.html",
        "app/templates/output_detail.html",
    ):
        template_text = Path(template_path).read_text(encoding="utf-8")
        expected_src = (
            '<script src="/static/js/operation-events.js?v={{ asset_version }}"></script>'
            if template_path == "app/templates/index.html"
            else '<script src="/static/js/operation-events.js?v={{ asset_version }}"></script>'
        )
        assert expected_src in template_text
        assert "/api/operations/" not in template_text

    dashboard_text = _dashboard_source_text()
    output_detail_text = _output_detail_source_text()
    assert "resumeDashboardOperations();" in dashboard_text
    assert (
        'window.CNCOperations.activeOperations({ surface: "dashboard" })'
        in dashboard_text
    )
    assert "resumeOutputOperations();" in output_detail_text
    assert 'surface: "output_detail"' in output_detail_text
    assert "resumeOutputSaveOperation(operation)" in output_detail_text
    assert "resumeDeleteOperation(operation)" in output_detail_text
    assert "resumeCloneOperation(operation)" in output_detail_text
    assert "resumeTransferOperation(operation)" in output_detail_text
    assert "resumeReplicaSetupOperation(operation)" in output_detail_text
    assert "resumeBackupOperation(operation, config)" in output_detail_text


def test_dashboard_tabs_query_live_panels_after_async_refresh() -> None:
    template_text = _dashboard_source_text()

    assert (
        'const currentTabs = () => Array.from(document.querySelectorAll("[data-tab]"));'
        in template_text
    )
    assert (
        'const currentPanels = () => Array.from(document.querySelectorAll("[data-panel]"));'
        in template_text
    )
    assert "currentPanels().forEach((panel) => {" in template_text
    assert (
        'const panels = Array.from(document.querySelectorAll("[data-panel]"));'
        not in template_text
    )
    assert "const dashboardTabUrl = (name) => {" in template_text
    assert 'url.searchParams.set("tab", name);' in template_text
    assert 'history.replaceState(null, "", nextUrl);' in template_text
    assert 'history.replaceState(null, "", "#" + targetName);' not in template_text
    assert 'window.addEventListener("hashchange", () => {' in template_text
    assert 'const nextTab = location.hash.replace("#", "");' in template_text
    assert template_text.index(
        'const initialHashTab = location.hash.replace("#", "");'
    ) < template_text.index("selectTab(tabIsAvailable(initialTab)")
    assert (
        'const queryTab = new URLSearchParams(window.location.search).get("tab") || "";'
        in template_text
    )
    assert "const initialTab = tabIsAvailable(queryTab)" in template_text
    assert (
        ": (tabIsAvailable(initialHashTab) ? initialHashTab : requestedTab);"
        in template_text
    )
    assert (
        "if (initialTab !== requestedTab && tabIsAvailable(initialTab)) {"
        in template_text
    )
    assert (
        "const tabIsAvailable = (name) => currentTabs().some((tab) => tab.dataset.tab === name);"
        in template_text
    )
    assert "if (tabIsAvailable(nextTab)) handleTabRequest(nextTab);" in template_text
    assert "const loadDashboardTab = async (name) => {" in template_text
    assert "fetch(`/?tab=${encodeURIComponent(targetName)}`" in template_text
    assert 'nextPanel.dataset.loaded = "1";' in template_text


def test_output_detail_metrics_allow_shield_outputs() -> None:
    template_text = _output_detail_source_text()

    assert 'selected_backend.kind not in ["app", "shield"]' in template_text
    assert 'selected_backend.kind in ["app", "shield"]' in template_text
    assert "data-output-live-metrics" in template_text
    assert "selected_output_live_metrics" in template_text
    assert "const outputLiveMetrics = (() =>" in template_text
    assert "const outputMetricsCacheKey = () =>" in template_text
    assert "readCachedMetricPayload" in template_text
    assert "writeCachedMetricPayload(payload)" in template_text
    assert "renderMetricsFromCachedStatus(outputLiveMetrics)" in template_text
    assert (
        "Resource history is only available for app and Shield outputs."
        in template_text
    )


def test_init_wrapper_collects_preferences_and_runs_ubuntu_installer() -> None:
    script_path = Path("init.sh")
    script_text = script_path.read_text(encoding="utf-8")

    assert script_path.exists()
    assert script_path.stat().st_mode & 0o111
    assert 'INSTALLER="${ROOT_DIR}/scripts/install_ubuntu.sh"' in script_text
    assert 'prompt_optional_secret "GITHUB_READONLY_PAT"' in script_text
    assert "GitHub token for private repos or rate limits" in script_text
    assert "Private repo updates" not in script_text
    assert 'prompt_optional_secret "TS_AUTHKEY"' in script_text
    assert 'prompt_yes_no "NGINX_CLOUDFLARE_ONLY"' in script_text
    assert 'export CNC_APP_DIR="${ROOT_DIR}"' in script_text
    assert "export CNC_SUPPRESS_INSTALLER_SUMMARY=1" in script_text
    assert "box_line_wrap" in script_text
    assert "Installer log: /var/log/cnc/installer.log" in script_text
    assert '"${INSTALLER}" "$@"' in script_text
    assert "CNC INIT SUMMARY" in script_text
    assert "NEXT STEPS" in script_text
    assert "USEFUL COMMANDS" in script_text


def test_readme_leads_public_clone_through_init_wrapper() -> None:
    readme_text = Path("README.md").read_text(encoding="utf-8")

    assert "> [!CAUTION]" in readme_text
    assert "CNC is provided as-is and intended for a fresh Ubuntu VPS." in readme_text
    assert "### Recommended Setup" in readme_text
    assert "## Security Baselining" in readme_text
    assert "assets/cnc-dashboard.webp" in readme_text
    assert "assets/cnc-routing.webp" in readme_text
    assert "assets/cnc-security-hardening.webp" in readme_text
    assert "a GitHub read-only PAT for this private repo" not in readme_text
    assert "Fine-grained tokens" not in readme_text
    assert "restrict public HTTP/S to Cloudflare CIDRs" in readme_text
    assert "sudo git clone https://github.com/Dinkum/cnc.git /opt/cnc" in readme_text
    assert "sudo ./init.sh" in readme_text
    assert "sudo GITHUB_READONLY_PAT=github_pat_... ./init.sh" in readme_text
    assert "x-access-token" not in readme_text


def test_output_create_form_uses_async_save_handler() -> None:
    template_text = _dashboard_source_text()
    output_detail_text = _output_detail_source_text()
    output_mutations_text = Path("app/ui/routes/output_mutations.py").read_text(
        encoding="utf-8"
    )
    http_text = Path("app/ui/http.py").read_text(encoding="utf-8")

    assert "data-backend-create-form" in template_text
    assert "const bindBackendCreateForms" in template_text
    assert "watchDashboardOperation" in template_text
    assert 'document.addEventListener("submit", async (event) => {' in template_text
    assert 'submittedForm.matches("[data-update-form]")' in template_text
    assert 'headline: "Starting update"' in template_text
    assert "...operationProgressPipelines" in template_text
    assert "const firstStep = steps[0] || {};" in template_text
    assert 'firstStep.substep || firstStep.note || ""' in template_text
    assert "stopFloatingSaveProgress();" not in template_text
    assert "window.CNCOperations.watchOperation(operationId" in template_text
    assert "transientPollFailures" not in template_text
    assert "retrying without stopping the operation" in template_text
    assert 'refreshTab = "outputs"' in template_text
    assert "fetch(`/?tab=${encodeURIComponent(refreshTab)}`" in template_text
    assert "const revealCreatedOutputRow = " in template_text
    assert "focusOutputId: details.backend_id" in template_text
    assert "focusOutputName: details.backend_name" in template_text
    assert 'row.scrollIntoView({ block: "nearest"' in template_text
    assert "row.focus({ preventScroll: true });" in template_text
    assert (
        "fetch(`${window.location.pathname}${window.location.search}`"
        not in template_text
    )
    assert "const refreshCsrfTokens = async () =>" in template_text
    assert "response.status === 403 && await refreshCsrfTokens()" in template_text
    assert "X-CNC-Dashboard-Refresh" in template_text
    assert '"createOutputProgressSteps": create_output_progress_steps' in template_text
    assert '"inputProgressPipelines": input_progress_pipelines' in template_text
    assert (
        "const createOutputProgressSteps = dashboardConfig.createOutputProgressSteps || [];"
        in template_text
    )
    assert "...(dashboardConfig.inputProgressPipelines || {})" in template_text
    assert "createOutput: createOutputProgressSteps" in template_text
    assert (
        '"outputSaveProgressSteps": output_save_progress_steps | default([], true)'
        in output_detail_text
    )
    assert (
        "const outputSaveProgressSteps = outputDetailConfig.outputSaveProgressSteps || [];"
        in output_detail_text
    )
    assert "data-output-save-inline-progress" in output_detail_text
    assert "renderInlineOutputSaveProgress" in output_detail_text
    assert "startOutputSaveProgress(runningLabel, form)" in output_detail_text
    assert "window.setInterval(renderStep, 1200)" not in output_detail_text
    assert (
        'operationProgressTrackers.set("outputSave", firstProgress)'
        in output_detail_text
    )
    assert (
        'operationProgress(operation, fallbackProgress, "outputSave")'
        in output_detail_text
    )
    assert "_run_update_backend_operation," in output_mutations_text
    assert "_run_update_backend_inputs_operation," in output_mutations_text
    assert "_run_update_backend_state_operation," in output_mutations_text
    assert '{ progress: 6, label: "Reading output form"' not in output_detail_text
    assert "output_save_progress_steps()" in http_text
    assert "renderDisabledRuntimeState" in output_detail_text
    assert "return Math.max(4, Math.min(96, numericProgress));" in template_text
    assert "const progressStepIndex = (steps, progress) =>" in template_text
    assert "const knownProgressStep = (steps, phase, substate) =>" in template_text
    assert "Creating output" in template_text
    assert "Output created" in template_text
    assert 'kind="create_backend"' in output_mutations_text
    assert (
        "_run_create_backend_operation, settings, operation.id, payload"
        in output_mutations_text
    )
    assert '<option value="none" selected>none</option>' in template_text
    assert '<option value="auto">auto</option>' not in template_text
    assert (
        'selected_output_detail.get("healthcheck_mode") == "auto"'
        not in output_detail_text
    )
    assert 'healthcheck_mode: str = Form("none")' in output_mutations_text


def test_outputs_load_header_names_live_average_window() -> None:
    template_text = _dashboard_source_text()

    assert "const SERVER_LOAD_POLL_INTERVAL_MS = 15000;" in template_text
    assert "const OUTPUT_LOAD_AVG_WINDOW_MS = 60 * 1000;" in template_text
    assert "Load (1m avg)" in template_text
    assert "averageOutputLoad" in template_text
    assert "STATUS_LIVE_REFRESH_TABS" not in template_text
    assert (
        'const STATUS_POLL_TABS = new Set(["home", "outputs", "settings"]);'
        in template_text
    )
    assert (
        'const statusPath = forceRefresh ? "/api/status?force_refresh=true" : "/api/status";'
        in template_text
    )
    assert 'readJsonResponse(response, "Status refresh failed")' in template_text
    assert "data-output-cpu-label" in template_text
    assert "data-output-memory-label" in template_text
    assert 'enabled && (kind === "app" || kind === "shield")' in template_text
    assert 'kind !== "app" && kind !== "shield"' in template_text
    assert 'kind === "shield"' in template_text
    assert "data-output-load-sync" not in template_text
    assert "load stale" not in template_text


def test_settings_renders_live_host_metrics_card() -> None:
    template_text = _dashboard_source_text()
    css_text = Path("app/static/css/app.css").read_text(encoding="utf-8")

    assert 'id="host-metrics-shell"' in template_text
    assert (
        "data-host-metrics='{{ status.get(\"host_metrics\", {}) | tojson | forceescape }}'"
        in template_text
    )
    assert 'id="host-metric-select"' in template_text
    assert 'id="host-timeframe-select"' in template_text
    assert 'id="host-metrics-chart"' in template_text
    assert "renderHostMetricsFromStatus(payload.host_metrics)" in template_text
    assert "fetch(`/api/host/metrics-history?" in template_text
    assert "loadHostMetricHistory" in template_text
    assert "let hostMetricsAbortController = null;" in template_text
    assert "hostMetricsAbortController?.abort();" in template_text
    assert 'if (targetName !== "settings")' in template_text
    assert "hostMetricsRequestId += 1;" in template_text
    assert "const settingsMetricsPanelActive = () => (" in template_text
    assert "if (!settingsMetricsPanelActive()) return;" in template_text
    assert template_text.index("let hostMetricsRequestId = 0;") < template_text.index(
        'selectTab(tabIsAvailable(initialTab) ? initialTab : "home");'
    )
    assert template_text.index(
        "let hostMetricsAbortController = null;"
    ) < template_text.index(
        'selectTab(tabIsAvailable(initialTab) ? initialTab : "home");'
    )
    assert template_text.index(
        "await ensureMetricChartAssets();"
    ) < template_text.index('hostMetricsShell.dataset.hostMetricsInitialized = "1";')
    assert "signal: controller.signal" in template_text
    assert "await ensureMetricChartAssets();" in template_text
    assert "scopedTimeframe.addEventListener" in template_text
    assert "let lastHostMetricPayload = null;" in template_text
    assert "let lastHostMetrics = {};" in template_text
    assert "const hostPayloadMatchesCurrentSelection = (payload) =>" in template_text
    assert (
        'const renderLiveHostMetricsFallback = (note = "Waiting for live host metrics.") =>'
        in template_text
    )
    assert (
        'renderLiveHostMetricsFallback("Loading host metric history.")' in template_text
    )
    assert (
        "Host metric history could not be loaded. Showing live samples."
        in template_text
    )
    assert "const reloadSelectedHostMetricHistory = () =>" in template_text
    assert "peak_value: priorSummary.peak_value ?? latestValue" in template_text
    assert "average_value: priorSummary.average_value ?? latestValue" in template_text
    assert "const hostDiskSummaryValue = (value, label) =>" in template_text
    assert (
        "`${formatPercent(percent)} · ${formatBytes(usedBytes)} used`" in template_text
    )
    assert 'label: "range-min"' in template_text
    assert 'label: "range-max"' in template_text
    assert 'label: "down"' in template_text
    assert 'label: "up"' in template_text
    assert 'borderColor: "#68d8ff"' in template_text
    assert 'borderColor: "#9bf0c7"' in template_text
    assert "const payloadYAxisMin = Number(payload?.y_axis_min);" in template_text
    assert "const payloadYAxisMax = Number(payload?.y_axis_max);" in template_text
    assert "min: yAxisMin" in template_text
    assert "max: yAxisMax" in template_text
    assert ".host-metrics-summary-grid" in css_text


def test_settings_places_metrics_below_update_and_renders_shield_toggle() -> None:
    template_text = _dashboard_source_text()

    assert template_text.index("<h2>Update CNC</h2>") < template_text.index(
        'id="host-metrics-shell"'
    )
    assert 'action="/ui/settings/beta"' in template_text
    assert 'class="beta-settings-form"' in template_text
    assert 'class="editor-actions beta-settings-actions"' in template_text
    beta_block = template_text[
        template_text.index("<h2>Beta features</h2>") : template_text.index(
            "<h2>Update CNC</h2>"
        )
    ]
    assert beta_block.index("Routing page") < beta_block.index("Enable Shield")
    assert beta_block.count('<button type="submit">Save</button>') == 1
    assert 'name="shield_enabled"' in template_text
    assert 'class="field-span-2 input-shield-card"' in template_text
    assert 'data-input-update-url="/ui/inputs/{{ item.id }}"' in template_text
    assert 'name="shield_access_code"' in template_text
    assert "value=\"{{ detail.get('shield_access_code', '') }}\"" in template_text
    assert 'data-shield-code-configured="{{' in template_text
    assert (
        'form.dataset.inputUpdateUrl || form.getAttribute("action") || form.action'
        in template_text
    )
    assert "please enter an access code to enable shield" in template_text
    assert "shield_status.output_state" in template_text
    assert (
        "{% if shield_status.server_enabled and not shield_status.backend_exists %}"
        in template_text
    )
    assert '<option value="shield">shield</option>' in template_text
    assert "shield_status.input_exists" in template_text


def test_update_card_suppresses_success_inline_status() -> None:
    template_text = _dashboard_source_text()

    assert (
        '{% if not status.last_update or status.last_update.status == "success" %}hidden{% endif %}'
        in template_text
    )
    assert 'status.last_update.status != "success"' in template_text
    assert (
        'updateInlinePill.hidden = !hasLastUpdate || status === "success";'
        in template_text
    )
    assert (
        'updateInlinePill.textContent = hasLastUpdate && status !== "success" ? status : "";'
        in template_text
    )


def test_access_key_reset_fields_live_in_modal() -> None:
    template_text = _dashboard_source_text()
    css_text = Path("app/static/css/app.css").read_text(encoding="utf-8")
    settings_mutations_text = Path("app/ui/routes/settings_mutations.py").read_text(
        encoding="utf-8"
    )

    card_block = template_text[
        template_text.index("<h2>Access key</h2>") : template_text.index(
            "<h2>Pushover</h2>"
        )
    ]
    modal_block = template_text[
        template_text.index('id="access-key-reset-modal"') : template_text.index(
            '<dialog class="confirm-modal" id="confirm-modal">'
        )
    ]

    assert "data-access-key-reset-open" in card_block
    assert 'name="access_key"' not in card_block
    assert 'name="access_key_confirm"' not in card_block
    assert 'class="access-key-disable-form"' in card_block
    assert "data-access-key-reset-modal" in modal_block
    assert 'name="access_key"' in modal_block
    assert 'name="access_key_confirm"' in modal_block
    assert "data-access-key-first-input" in modal_block
    assert "const bindAccessKeyResetModal = (root = document) =>" in template_text
    assert "bindAccessKeyResetModal(root);" in template_text
    assert ".access-key-modal-card" in css_text
    assert 'context["access_key_modal_open"] = True' in settings_mutations_text


def test_update_check_refreshes_status_without_starting_update_progress() -> None:
    template_text = _dashboard_source_text()

    assert "const refreshUpdateStatusCard = async () =>" in template_text
    check_handler = template_text[
        template_text.index(
            'submittedForm.matches("[data-update-check-form]")'
        ) : template_text.index("renderUpdateVersion(initialUpdateVersion);")
    ]
    assert "refreshUpdateStatusCard()" in check_handler
    assert "renderUpdateVersion(payload);" in check_handler
    assert "setUpdateCheckPending(false);" in check_handler
    assert "refreshUpdateStatusCard()" in check_handler
    assert ".then((statusPayload) => {" in check_handler
    assert "await refreshUpdateStatusCard()" not in check_handler
    assert "await pollUpdateStatus()" not in check_handler
    assert "startFloatingSaveProgress" not in check_handler
    assert "showFloatingSave(" not in check_handler


def test_dashboard_removes_redundant_server_tab() -> None:
    template_text = _dashboard_source_text()
    css_text = Path("app/static/css/app.css").read_text(encoding="utf-8")
    from app.ui.read_models import VALID_DASHBOARD_TABS

    dashboard_view_text = Path("app/ui/dashboard/presentation.py").read_text(
        encoding="utf-8"
    )

    assert 'data-tab="status"' not in template_text
    assert 'data-panel="status"' not in template_text
    assert "data-server-load-card" not in template_text
    assert "status" not in VALID_DASHBOARD_TABS
    assert "renderServerLoad" not in template_text
    assert "serverLoadElements" not in template_text
    assert "status-load" not in css_text
    assert (
        "Current backend limits match the last successful save."
        not in dashboard_view_text
    )
    assert "_build_status_load_panel" not in dashboard_view_text
    assert "STATUS_LIVE_REFRESH_TABS" not in template_text
    assert "if (!STATUS_POLL_TABS.has(activeTab))" in template_text
    refresh_block = template_text.split("const refreshVisibleTabStatus = () => {", 1)[
        1
    ].split("const routeNodeId", 1)[0]
    assert "pollServerLoadStatus({ reschedule: false });" in refresh_block
    assert "forceRefresh: true" not in refresh_block


def test_home_overview_only_renders_most_recent_save_status() -> None:
    template_text = _dashboard_source_text()

    assert "Most recent save" in template_text
    assert "Isolation" in template_text
    assert "isolation-info-tooltip" not in template_text
    assert "What isolation OK means" not in template_text
    assert "Private output lanes are separated" not in template_text
    assert "Host out of sync" not in template_text
    assert "sync pending" not in template_text
    assert "Host drift" not in template_text
    assert "Isolation drift" not in template_text


def test_floating_save_card_uses_single_progress_surface() -> None:
    template_text = _dashboard_source_text()
    css_text = Path("app/static/css/app.css").read_text(encoding="utf-8")

    assert 'id="input-save-pill"' not in template_text
    assert 'id="input-save-progress-fill"' in template_text
    assert 'id="input-save-action"' in template_text
    assert 'id="input-save-stage"' in template_text
    assert 'id="input-save-title"' not in template_text
    assert 'id="input-save-headline"' not in template_text
    assert 'id="input-save-step-badge"' not in template_text
    assert 'id="input-save-substeps"' not in template_text
    assert "save-card-close-floating" in template_text
    assert 'id="input-save-card" data-save-card' in template_text
    assert "save-card-top" in template_text
    assert "saveProgressPipelines" in template_text
    assert "showInitialFloatingSaveProgress" in template_text
    assert "floatingSaveCard.hidden = false;" in template_text
    assert 'shell.querySelectorAll("[data-save-card]")' in template_text
    assert ".save-progress" in css_text
    assert ".save-card-close-floating" in css_text
    assert ".save-card-top" in css_text
    assert "flex-direction: column-reverse;" in css_text
    assert ".floating-save-shell > [data-save-card]:nth-of-type(n+4)" in css_text
    assert "position: absolute;" in css_text
    assert ".save-narrative" in css_text
    assert ".save-action-line" in css_text
    assert ".save-stage-line" in css_text
    assert "-webkit-line-clamp: 2" in css_text
    assert ".save-narrative p[hidden]" in css_text
    assert ".floating-save-card.running .save-progress span::after" in css_text
    assert "animation: save-progress-pulse 1500ms ease-in-out infinite;" in css_text
    assert ".click-row.is-new td" in css_text
    assert "grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));" in css_text
    assert "grid-template-columns: repeat(7, minmax(0, 1fr));" not in css_text
    assert ".save-substeps" not in css_text
    assert ".save-step-badge" not in css_text


def test_home_overview_keeps_desktop_grid_on_tablet_widths() -> None:
    css_text = Path("app/static/css/app.css").read_text(encoding="utf-8")
    tablet_media = css_text.split("@media (max-width: 1080px)", 1)[1].split(
        "@media (max-width: 820px)", 1
    )[0]
    phone_media = css_text.split("@media (max-width: 820px)", 1)[1].split(
        "@media (max-width: 560px)", 1
    )[0]

    assert "grid-template-columns: repeat(4, minmax(0, 1fr));" in css_text
    assert ".home-overview-grid" not in tablet_media
    assert ".home-overview-grid" in phone_media


def test_mobile_layout_prevents_ios_form_zoom() -> None:
    css_text = Path("app/static/css/app.css").read_text(encoding="utf-8")
    phone_media = css_text.split("@media (max-width: 820px)", 1)[1].split(
        "@media (max-width: 560px)", 1
    )[0]

    assert 'input:not([type="checkbox"]),' in phone_media
    assert "font-size: 16px;" in phone_media
    assert "table {\n    min-width: 620px;" in phone_media
    assert "td .mono,\n  .table-primary .mono" in phone_media
    assert "overflow-wrap: anywhere;" in phone_media


def test_floating_save_card_reports_terminal_time_and_measured_duration() -> None:
    template_text = _dashboard_source_text()

    assert "const elapsedSecondsSince = (startedAt) =>" in template_text
    assert "const terminalMutationMeta = (startedAt) =>" in template_text
    assert (
        "const lastApplyMeta = (lastApply, durationOverrideSeconds = null)"
        in template_text
    )
    assert 'const timestampLabel = status === "error" ? "failed"' in template_text
    assert (
        "meta: lastApplyMeta(lastApply, elapsedSecondsSince(startedAt))"
        in template_text
    )
    assert "meta: terminalMutationMeta(saveStartedAt)" in template_text


def test_dashboard_async_errors_explain_gateway_failures() -> None:
    template_text = _dashboard_source_text()

    assert "bodyText" in template_text
    assert "statusCode >= 500" in template_text
    assert "The request was interrupted before CNC returned details." in template_text


def test_floating_save_card_tracks_action_specific_steps() -> None:
    template_text = _dashboard_source_text()

    assert 'pipeline: "createOutput"' in template_text
    assert 'pipeline: isDelete ? "deleteInput" : "saveInput"' in template_text
    assert 'pipeline: "createInput"' in template_text
    assert "const floatingSaveAction" in template_text
    assert "const actionLabel = title || headline" in template_text
    assert "floatingSaveAction.textContent = actionLabel" in template_text
    assert "floatingSaveStage.textContent = mainStage" in template_text
    assert 'tone === "success"' in template_text
    assert '? (note || substep || "")' in template_text
    assert 'headline: "Complete"' in template_text
    assert 'note: "Host changes are live."' in template_text
    assert 'meta.push({ label: "elapsed", value: duration });' in template_text
    assert "let elapsedTimer = null;" in template_text
    assert "window.setInterval(() =>" in template_text
    assert "clearTransientDashboardParams" in template_text
    assert 'url.searchParams.delete("defer_status")' in template_text
    assert 'substep = ""' in template_text
    assert "const progressCandidate = Number.isFinite(progress)" in template_text
    assert (
        "const progressRegressed = progressCandidate < lastProgress;" in template_text
    )
    assert (
        "const stageRegressed = Number.isFinite(stageProgress) && stageProgress < lastProgress;"
        in template_text
    )
    assert "substep: displayStage.note" in template_text
    assert (
        'if (isFinished && duration) meta.push({ label: "took", value: duration });'
        in template_text
    )
    assert '{ label: "took", value: duration }' in template_text
    assert '"time elapsed"' not in template_text
    assert "`${main} - ${detail}`" not in template_text
    assert "SAVING... ${label}" not in template_text
    assert "floatingSaveNote.hidden = !statusText;" in template_text
    assert "STAGE ${stepNumber} / ${totalSteps}" not in template_text
    assert "SUBSTAGE:" not in template_text
    assert "createOutput: createOutputProgressSteps" in template_text
    assert "...(dashboardConfig.inputProgressPipelines || {})" in template_text


def test_attach_dropdowns_hide_generic_item_subtitles() -> None:
    dashboard_text = _dashboard_source_text()
    output_detail_text = Path("app/templates/output_detail.html").read_text(
        encoding="utf-8"
    )
    css_text = Path("app/static/css/app.css").read_text(encoding="utf-8")

    assert (
        '{{ backend.kind }} · {{ "enabled" if backend.enabled else "disabled" }}'
        not in dashboard_text
    )
    assert "{{ item.kind_label }} · input {{ item.id }}" not in output_detail_text
    assert (
        '<span class="output-tag-meta">{{ item.kind_label }}</span>'
        not in output_detail_text
    )
    assert 'metaSpan.className = "output-tag-meta"' not in output_detail_text
    assert ".output-tag-meta" not in css_text
    assert "{{ item.unavailable_reason }}" in dashboard_text
    assert "{{ item.unavailable_reason }}" in output_detail_text


def test_async_dashboard_errors_include_http_context() -> None:
    template_text = _dashboard_source_text()

    assert (
        "const responseErrorMessage = (html, fallback, response = null) =>"
        in template_text
    )
    assert "`${response.status} ${response.statusText}`" in template_text
    assert "const dashboardErrorMessage = (html) =>" in template_text
    assert "const actionErrorText = (prefix, error) =>" in template_text
    assert (
        'dashboardMutationErrorMessage(response, html, "Input create failed")'
        in template_text
    )
    assert (
        'responseErrorMessage(html, "Output create failed", response)' in template_text
    )
    assert 'isDelete ? "Input delete failed" : "Input save failed"' in template_text
    assert 'setBanner("error", message);' in template_text


def test_update_card_renders_polled_systemd_state() -> None:
    template_text = _dashboard_source_text()
    css_text = Path("app/static/css/app.css").read_text(encoding="utf-8")

    assert "data-update-state" in template_text
    assert "data-update-state-line" in template_text
    assert "data-update-active-state" in template_text
    assert "data-update-sub-state" in template_text
    assert "data-update-result-state" in template_text
    assert "data-update-exit-state" in template_text
    assert "const unitState = details.unit_state || {};" in template_text
    assert "`${stateMain} - ${stateDetail}`" in template_text
    assert "const pollUpdateStatus = async () =>" in template_text
    assert "const payload = await refreshUpdateStatusCard();" in template_text
    assert "scheduleUpdatePoll();" in template_text
    assert ".update-run-state" in css_text
    assert ".update-state-grid" in css_text


def test_output_detail_mutations_use_async_operation_flows() -> None:
    template_text = _output_detail_source_text()

    assert "data-output-save-form" in template_text
    assert 'id="sf-kind"' not in template_text
    assert (
        '<input type="hidden" name="kind" value="{{ selected_backend.kind }}">'
        in template_text
    )
    assert "data-restore-form" in template_text
    assert "data-import-backup-form" in template_text
    assert "const watchOperation = " in template_text
    assert 'href="/#status">server</a>' not in template_text
    assert "const refreshCsrfTokens = async () =>" in template_text
    assert "response.status === 403 && await refreshCsrfTokens()" in template_text
    assert (
        'const formSubmitMethod = (form) => String(form?.getAttribute("method") || "POST").toUpperCase();'
        in template_text
    )
    assert "const requestMethod = formSubmitMethod(form);" in template_text
    assert "method: requestMethod" in template_text
    assert "const formActionPath = (form) =>" in template_text
    assert (
        "const responseErrorMessage = (response, payload, text, fallback, request = {}) =>"
        in template_text
    )
    assert (
        'const outputEnabledChip = document.querySelector(".output-page-badges .stat-chip");'
        in template_text
    )
    assert (
        'outputEnabledChip.textContent = enabled ? "enabled" : "disabled";'
        in template_text
    )
    assert "renderOutputSaveOperationProgress" in template_text
    assert "renderOutputSaveOperationResult" in template_text
    assert "window.setInterval(renderStep, 1200)" not in template_text
    assert 'credentials: "same-origin"' in template_text
    assert "window.CNCOperations.watchOperation(operationId, handlers)" in template_text
    assert (
        'deleteProgressFill.classList.toggle("is-complete", normalized >= 100)'
        in template_text
    )
    assert "finishDeleteWithoutReload" in template_text
    assert "startDeleteNarrative" not in template_text
    assert "completeDeleteProgress" not in template_text
    assert "intervalMs: 1000" in template_text
    assert "startBackupLikeOperation" in template_text


def test_output_detail_runtime_signal_polling_is_single_flight() -> None:
    template_text = _output_detail_source_text()

    assert "runtimeRefreshInFlight" in template_text
    assert "runtimeRefreshAbortController" in template_text
    assert (
        "if (runtimeRefreshInFlight && !force) return runtimeRefreshInFlight;"
        in template_text
    )
    assert "runtimeRefreshAbortController.abort();" in template_text
    assert (
        "if (runtimeRefreshInFlight === requestPromise) runtimeRefreshInFlight = null;"
        in template_text
    )
    assert "runtimeRefreshTimer = window.setInterval" not in template_text
    assert "if (document.hidden) {" in template_text
    assert "scheduleRuntimeRefresh();" in template_text
    assert "runtimeRefreshFailures += 1" in template_text
    assert (
        'const outputHasRuntimeSignals = () => outputKind === "app" && outputEnabled;'
        in template_text
    )
    assert "if (!outputHasRuntimeSignals()) return;" in template_text
    assert template_text.count("if (!outputHasRuntimeSignals()) return;") >= 2
    assert (
        "const refreshRuntimeSignals = async ({ force = false } = {}) => {\n    if (!outputHasRuntimeSignals()) return null;"
        in template_text
    )
    assert "if (!document.hidden && outputHasRuntimeSignals())" in template_text
    assert (
        'const outputHasMetrics = () => outputKind === "app" || outputKind === "shield";'
        in template_text
    )
    assert (
        "if (outputHasMetrics() && metricSelect && timeframeSelect && metricsChart)"
        in template_text
    )
    assert "let metricsAbortController = null;" in template_text
    assert "metricsAbortController?.abort();" in template_text
    assert "await ensureMetricChartAssets();" in template_text
    assert 'if (outputKind === "app" && outputEnabled)' in template_text
    assert "refreshRuntimeSignals({ force: true }).catch(() => {});" in template_text
    assert "runtimeRefreshTimer = null;" in template_text


def test_high_signal_request_and_output_action_logging_is_present() -> None:
    main_text = Path("app/main.py").read_text(encoding="utf-8")
    output_mutations_text = Path("app/ui/routes/output_mutations.py").read_text(
        encoding="utf-8"
    )
    output_lifecycle_text = Path("app/ui/operations/output_lifecycle.py").read_text(
        encoding="utf-8"
    )

    assert "def _request_log_context" in main_text
    assert "def _response_log_context" in main_text
    assert '"http.request.rejected"' in main_text
    assert "status_family" in main_text
    assert "request_kind" in main_text
    assert '"ui.backend.state.requested"' in output_mutations_text
    assert '"ui.backend.state.succeeded"' in output_mutations_text
    assert '"ui.backend.delete.operation_created"' in output_mutations_text
    assert '"ui.backend.delete.worker_applied"' in output_lifecycle_text


def test_cnc_admin_systemd_unit_declares_expected_runtime_contract() -> None:
    unit_path = Path("packaging/cnc-admin.service")
    unit_text = unit_path.read_text(encoding="utf-8")

    assert unit_path.exists()
    assert "Description=cnc admin portal" in unit_text
    assert "Type=notify" in unit_text
    assert "NotifyAccess=main" in unit_text
    assert "NotifyAccess=all" not in unit_text
    assert "WorkingDirectory=/var/lib/cnc/current" in unit_text
    assert "EnvironmentFile=-/etc/cnc.env" in unit_text
    assert (
        "ExecStart=/var/lib/cnc/current/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 9090"
        in unit_text
    )
    assert "WatchdogSec=45s" in unit_text
    assert "KillMode=mixed" in unit_text
    assert "RuntimeDirectory=cnc" in unit_text
    assert "RuntimeDirectoryMode=0700" in unit_text
    assert "Slice=cnc-apps.slice" not in unit_text
    assert "PrivateTmp=false" in unit_text
    assert (
        "ReadWritePaths=/etc /etc/containers/systemd /usr/local/bin /var/lib/cnc /var/log/cnc"
        in unit_text
    )
    assert "ProtectSystem=no" in unit_text


def test_cnc_auto_size_units_exist_and_use_managed_runtime() -> None:
    service_path = Path("packaging/cnc-auto-size.service")
    timer_path = Path("packaging/cnc-auto-size.timer")
    service_text = service_path.read_text(encoding="utf-8")
    timer_text = timer_path.read_text(encoding="utf-8")

    assert service_path.exists()
    assert timer_path.exists()
    assert (
        "ExecStart=/var/lib/cnc/current/.venv/bin/cnc-admin auto-size tick"
        in service_text
    )
    assert "EnvironmentFile=-/etc/cnc.env" in service_text
    assert (
        "ReadWritePaths=/etc /etc/containers/systemd /usr/local/bin /var/lib/cnc /var/log/cnc"
        in service_text
    )
    assert "OnCalendar=*:0/1" in timer_text
    assert "Unit=cnc-auto-size.service" in timer_text


def test_cnc_backend_alert_units_exist_and_use_managed_runtime() -> None:
    service_path = Path("packaging/cnc-backend-alerts.service")
    timer_path = Path("packaging/cnc-backend-alerts.timer")
    service_text = service_path.read_text(encoding="utf-8")
    timer_text = timer_path.read_text(encoding="utf-8")

    assert service_path.exists()
    assert timer_path.exists()
    assert (
        "ExecStart=/var/lib/cnc/current/.venv/bin/cnc-admin backend-alerts check"
        in service_text
    )
    assert "EnvironmentFile=-/etc/cnc.env" in service_text
    assert (
        "ReadWritePaths=/etc /etc/containers/systemd /usr/local/bin /var/lib/cnc /var/log/cnc"
        in service_text
    )
    assert "OnBootSec=2min" in timer_text
    assert "OnUnitActiveSec=2min" in timer_text
    assert "Unit=cnc-backend-alerts.service" in timer_text


def test_cnc_cloudflare_sync_units_exist_and_use_managed_runtime() -> None:
    service_path = Path("packaging/cnc-cloudflare-sync.service")
    timer_path = Path("packaging/cnc-cloudflare-sync.timer")
    service_text = service_path.read_text(encoding="utf-8")
    timer_text = timer_path.read_text(encoding="utf-8")

    assert service_path.exists()
    assert timer_path.exists()
    assert (
        "ExecStart=/var/lib/cnc/current/.venv/bin/cnc-admin cloudflare sync"
        in service_text
    )
    assert "EnvironmentFile=-/etc/cnc.env" in service_text
    assert (
        "ReadWritePaths=/etc/cnc.env /etc/nginx/generated /etc/ufw /etc/containers/systemd /etc/systemd/system /usr/local/bin /var/lib/cnc /var/log/cnc"
        in service_text
    )
    assert "OnCalendar=hourly" in timer_text
    assert "Unit=cnc-cloudflare-sync.service" in timer_text


def test_cnc_update_check_units_exist_and_use_managed_runtime() -> None:
    service_path = Path("packaging/cnc-update-check.service")
    timer_path = Path("packaging/cnc-update-check.timer")
    service_text = service_path.read_text(encoding="utf-8")
    timer_text = timer_path.read_text(encoding="utf-8")

    assert service_path.exists()
    assert timer_path.exists()
    assert (
        "ExecStart=/var/lib/cnc/current/.venv/bin/cnc-admin update check"
        in service_text
    )
    assert "EnvironmentFile=-/etc/cnc.env" in service_text
    assert "ReadWritePaths=/etc/cnc.env /var/lib/cnc /var/log/cnc" in service_text
    assert "OnCalendar=hourly" in timer_text
    assert "Unit=cnc-update-check.service" in timer_text


def test_installer_writes_llm_help_wrapper() -> None:
    script_path = Path("scripts/install_ubuntu.sh")
    script_text = script_path.read_text(encoding="utf-8")

    assert 'LLM_HELP_WRAPPER_PATH="/usr/local/bin/llm-help"' in script_text
    assert 'exec ${CURRENT_LINK}/.venv/bin/cnc-admin llm-help "\\$@"' in script_text
    wrapper_text = Path("packaging/cnc-ssh-backend-root").read_text(encoding="utf-8")
    assert (
        'PACKAGED_SSH_BACKEND_ROOT_WRAPPER_PATH="packaging/cnc-ssh-backend-root"'
        in script_text
    )
    assert "sed 's/__CNC_ADMIN_PORT__/9090/g'" in script_text
    assert 'if [[ "${original_args[0]:-}" == "llm-help" ]]; then' in wrapper_text
    assert 'if [[ -n "${SSH_ORIGINAL_COMMAND:-}" || -t 0 ]]; then' in wrapper_text
    assert "CNCSSH1" in wrapper_text
    assert "backend-ssh-audit" not in wrapper_text
    assert 'podman container exists "${container}"' not in wrapper_text
    assert "--max-time 2" in wrapper_text
    assert "--header @/run/cnc/backend-ssh.header" in wrapper_text
    assert "/usr/bin/podman exec" in wrapper_text
    assert "MaxAuthTries 10" in script_text
    assert (
        'Defaults!/usr/local/bin/cnc-ssh-backend-root env_keep += "SSH_ORIGINAL_COMMAND SSH_CONNECTION"'
        in script_text
    )


def test_updater_syncs_backend_ssh_wrapper_after_service_restart() -> None:
    script_text = Path("scripts/update_from_github.sh").read_text(encoding="utf-8")

    assert "sync_backend_ssh_wrapper()" in script_text
    assert "reconcile_backend_ssh_root_wrapper(get_settings())" in script_text
    assert script_text.index(
        'systemctl restart "${CNC_SERVICE_NAME}"'
    ) < script_text.rindex("if ! sync_backend_ssh_wrapper")


def test_installer_uses_known_log_path_and_quiet_command_runner() -> None:
    script_text = Path("scripts/install_ubuntu.sh").read_text(encoding="utf-8")

    assert 'INSTALL_LOG="${CNC_INSTALL_LOG:-/var/log/cnc/installer.log}"' in script_text
    assert "rotate_install_log()" in script_text
    assert "CNC_INSTALL_LOG_KEEP:-5" in script_text
    assert 'mv -f "${path}" "${path}.1"' in script_text
    assert "printf '[cmd]'" in script_text
    assert "printf ' %q' \"$@\"" in script_text
    assert "apt-get -qq update" in script_text
    assert "apt-get -qq install -y" in script_text
    assert "CNC_SUPPRESS_INSTALLER_SUMMARY:-0" in script_text


def test_installer_configures_system_log_retention() -> None:
    script_text = Path("scripts/install_ubuntu.sh").read_text(encoding="utf-8")

    assert (
        'JOURNALD_CNC_RETENTION_PATH="/etc/systemd/journald.conf.d/99-cnc-retention.conf"'
        in script_text
    )
    assert 'RSYSLOG_LOGROTATE_PATH="/etc/logrotate.d/rsyslog"' in script_text
    assert "configure_system_log_retention()" in script_text
    assert "SystemMaxUse=1G" in script_text
    assert "su root syslog" in script_text
    assert "rotate 15" in script_text
    assert "daily" in script_text
    assert "maxsize 200M" in script_text
    assert "delaycompress" not in script_text
    assert 'run_cmd logrotate -d "${RSYSLOG_LOGROTATE_PATH}"' in script_text
    assert "run_cmd systemctl restart systemd-journald" in script_text
    assert (
        'run_step "logs.retention" "Configure system log retention" configure_system_log_retention'
        in script_text
    )


def test_installer_seeds_admin_allowed_hosts_env_default() -> None:
    script_path = Path("scripts/install_ubuntu.sh")
    script_text = script_path.read_text(encoding="utf-8")

    assert 'append_env_if_missing "ADMIN_ALLOWED_HOSTS" ""' in script_text
    assert 'append_env_if_missing "TAILSCALE_TAILNET_DNS_NAME" ""' in script_text
    assert 'append_env_if_missing "MULTI_NODE_ENABLED" "0"' in script_text
    assert 'append_env_if_missing "CLUSTER_JOIN_TAILNET_BASE_URL" ""' in script_text
    assert 'append_env_if_missing "NETDATA_ENABLED" "0"' in script_text
    assert "NETDATA_CONTAINER_IMAGE" not in script_text
    assert "wireguard" not in script_text
    assert "ADMIN_ALLOWED_HOSTS=" in script_text
    assert "MULTI_NODE_ENABLED=0" in script_text
    assert "NETDATA_ENABLED=0" in script_text
    assert "install -d -m 0755 /etc/containers/systemd" in script_text
    assert (
        "for required_path in /etc /etc/containers/systemd /var/lib/cnc /var/log/cnc"
        in script_text
    )


def test_installer_does_not_enable_tailscale_ssh() -> None:
    script_text = Path("scripts/install_ubuntu.sh").read_text(encoding="utf-8")
    template_text = _dashboard_source_text()

    assert "tailscale up --ssh" not in script_text
    assert "tailscale up --ssh" not in template_text
    assert (
        'run_cmd tailscale up --authkey "${TS_AUTHKEY}" --hostname "${ts_hostname}"'
        in script_text
    )
    assert (
        'up_output="$(tailscale up --hostname "${ts_hostname}" --timeout 5s 2>&1)"'
        in script_text
    )


def test_installer_restricts_public_http_ingress_to_cloudflare_when_enabled() -> None:
    script_path = Path("scripts/install_ubuntu.sh")
    script_text = script_path.read_text(encoding="utf-8")

    assert "persist_installer_preferences_from_env()" in script_text
    assert (
        'set_env_key_value "NGINX_CLOUDFLARE_ONLY" "${NGINX_CLOUDFLARE_ONLY}"'
        in script_text
    )
    assert "cloudflare_only_http_ingress_enabled()" in script_text
    assert "configure_cloudflare_http_firewall_rules()" in script_text
    assert "run_cloudflare_ingress_tool() {" in script_text
    assert (
        'run_cmd_in_app_dir python3 -m app.services.cloudflare_ingress "$@"'
        in script_text
    )
    assert "local -a args=(rewrite-ufw)" in script_text
    assert 'args+=(--cidr "${cidr}")' in script_text
    assert 'run_cloudflare_ingress_tool "${args[@]}"' in script_text
    assert "run_cmd ufw reload" in script_text
    assert "verify_ufw_cloudflare_http_rules_loaded()" in script_text
    assert "local -a args=(verify-live)" in script_text


def test_installer_seeds_cloudflare_sync_env_defaults() -> None:
    script_path = Path("scripts/install_ubuntu.sh")
    script_text = script_path.read_text(encoding="utf-8")

    assert (
        'append_env_if_missing "CLOUDFLARE_IPS_V4_URL" "https://www.cloudflare.com/ips-v4"'
        in script_text
    )
    assert (
        'append_env_if_missing "CLOUDFLARE_IPS_V6_URL" "https://www.cloudflare.com/ips-v6"'
        in script_text
    )
    assert 'append_env_if_missing "CLOUDFLARE_SYNC_TIMEOUT_SEC" "15"' in script_text
    assert (
        'append_env_if_missing "HOST_STATE_PATH" "/var/lib/cnc/host-state.json"'
        in script_text
    )
    assert "CLOUDFLARE_IPS_V4_URL=https://www.cloudflare.com/ips-v4" in script_text
    assert "CLOUDFLARE_IPS_V6_URL=https://www.cloudflare.com/ips-v6" in script_text
    assert "HOST_STATE_PATH=/var/lib/cnc/host-state.json" in script_text


def test_installer_uses_release_layout_for_initial_cutover() -> None:
    script_path = Path("scripts/install_ubuntu.sh")
    script_text = script_path.read_text(encoding="utf-8")

    assert 'INSTALL_RELEASE_DIR=""' in script_text
    assert (
        'INSTALL_RELEASE_DIR="${RUNTIME_ROOT}/releases/${release_name}"' in script_text
    )
    assert 'run_cmd cp -a "${APP_DIR}/." "${INSTALL_RELEASE_DIR}/"' in script_text
    assert (
        'run_cmd "${INSTALL_RELEASE_DIR}/.venv/bin/pip" --disable-pip-version-check -q install --require-hashes --only-binary=:all: -r "${INSTALL_RELEASE_DIR}/requirements.txt"'
        in script_text
    )
    assert (
        'run_cmd "${INSTALL_RELEASE_DIR}/.venv/bin/pip" --disable-pip-version-check -q install --no-deps -e "${INSTALL_RELEASE_DIR}"'
        in script_text
    )
    assert 'local tmp_link="${CURRENT_LINK}.new"' in script_text
    assert 'run_cmd ln -sfn "${INSTALL_RELEASE_DIR}" "${tmp_link}"' in script_text
    assert 'run_cmd mv -Tf "${tmp_link}" "${CURRENT_LINK}"' in script_text
    assert (
        'PACKAGED_AUTO_SIZE_SERVICE_PATH="packaging/cnc-auto-size.service"'
        in script_text
    )
    assert (
        'PACKAGED_AUTO_SIZE_TIMER_PATH="packaging/cnc-auto-size.timer"' in script_text
    )
    assert (
        'PACKAGED_BACKEND_ALERTS_SERVICE_PATH="packaging/cnc-backend-alerts.service"'
        in script_text
    )
    assert (
        'PACKAGED_BACKEND_ALERTS_TIMER_PATH="packaging/cnc-backend-alerts.timer"'
        in script_text
    )
    assert (
        'PACKAGED_CLOUDFLARE_SYNC_SERVICE_PATH="packaging/cnc-cloudflare-sync.service"'
        in script_text
    )
    assert (
        'PACKAGED_CLOUDFLARE_SYNC_TIMER_PATH="packaging/cnc-cloudflare-sync.timer"'
        in script_text
    )
    assert (
        'PACKAGED_UPDATE_CHECK_SERVICE_PATH="packaging/cnc-update-check.service"'
        in script_text
    )
    assert (
        'PACKAGED_UPDATE_CHECK_TIMER_PATH="packaging/cnc-update-check.timer"'
        in script_text
    )
    assert "install_packaged_systemd_asset()" in script_text
    assert (
        'run_cmd install -D -m 0644 "${packaged_path}" "/etc/systemd/system/${destination_name}"'
        in script_text
    )
    assert (
        'install_packaged_systemd_asset "packaging/systemd/cnc-edge-public-ingress.slice" "cnc-edge-public-ingress.slice"'
        in script_text
    )
    assert (
        'install_packaged_systemd_asset "packaging/systemd/cnc-edge-tailnet.slice" "cnc-edge-tailnet.slice"'
        in script_text
    )
    assert (
        'install_packaged_systemd_asset "packaging/systemd/cnc-admin-control.slice" "cnc-admin-control.slice"'
        in script_text
    )
    assert (
        'install_packaged_systemd_asset "packaging/systemd/cnc-apps.slice" "cnc-apps.slice"'
        in script_text
    )
    assert (
        'run_cmd systemctl enable --now "${AUTO_SIZE_SERVICE_NAME}.timer"'
        in script_text
    )
    assert (
        'run_cmd systemctl enable --now "${BACKEND_ALERTS_SERVICE_NAME}.timer"'
        in script_text
    )
    assert (
        'run_cmd systemctl enable --now "${CLOUDFLARE_SYNC_SERVICE_NAME}.timer"'
        in script_text
    )
    assert (
        'run_cmd systemctl enable --now "${UPDATE_CHECK_SERVICE_NAME}.timer"'
        in script_text
    )
    assert 'run_cmd ln -sfn "${APP_DIR}" "${CURRENT_LINK}"' not in script_text


def test_installer_validates_supported_python_and_infers_update_repo() -> None:
    script_text = Path("scripts/install_ubuntu.sh").read_text(encoding="utf-8")

    assert 'PYTHON_BIN="${CNC_PYTHON_BIN:-python3}"' in script_text
    assert "require_supported_python()" in script_text
    assert "CNC requires Python 3.11 or newer" in script_text
    assert (
        'run_cmd "${PYTHON_BIN}" -m venv "${INSTALL_RELEASE_DIR}/.venv"' in script_text
    )
    assert "infer_install_github_repo()" in script_text
    assert 'append_env_if_missing "GITHUB_REPO" ""' in script_text
    assert "ensure_updater_repo_default" in script_text
    assert 'inferred_repo="Dinkum/cnc"' not in script_text
    assert 'if [[ -n "${current_repo}" ]]; then' in script_text


def test_deploy_runtime_dependencies_are_pinned() -> None:
    requirements_path = Path("requirements.txt")
    import re
    import tomllib

    manifest = requirements_path.read_text(encoding="utf-8")
    entries = manifest.replace("\\\n", " ").splitlines()
    packages = {
        item["name"]: item
        for item in tomllib.loads(Path("uv.lock").read_text())["package"]
    }
    assert entries
    for entry in entries:
        match = re.match(r"([a-z0-9-]+)==([^ ;]+)", entry)
        assert match, entry
        name, version = match.groups()
        assert packages[name]["version"] == version
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", entry)
        assert hashes, name
        locked_hashes = {
            item["hash"].removeprefix("sha256:")
            for item in packages[name].get("wheels", [])
        }
        if packages[name].get("sdist"):
            locked_hashes.add(packages[name]["sdist"]["hash"].removeprefix("sha256:"))
        assert set(hashes) <= locked_hashes
    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    for dependency in project["dependencies"]:
        name, version = dependency.split("==")
        assert packages[name]["version"] == version
        assert f"{name}=={version}" in manifest


def test_updater_syncs_managed_cli_wrappers() -> None:
    script_path = Path("scripts/update_from_github.sh")
    script_text = script_path.read_text(encoding="utf-8")
    cnc_admin_wrapper = 'write_wrapper "${CLI_WRAPPER_PATH}" "exec ${CNC_CURRENT_LINK}/.venv/bin/cnc-admin \\"\\$@\\""'
    llm_help_wrapper = 'write_wrapper "${LLM_HELP_WRAPPER_PATH}" "exec ${CNC_CURRENT_LINK}/.venv/bin/cnc-admin llm-help \\"\\$@\\""'

    assert (
        'CLI_WRAPPER_PATH="${CLI_WRAPPER_PATH:-/usr/local/bin/cnc-admin}"'
        in script_text
    )
    assert (
        'LLM_HELP_WRAPPER_PATH="${LLM_HELP_WRAPPER_PATH:-/usr/local/bin/llm-help}"'
        in script_text
    )
    assert 'log "syncing managed CLI wrappers"' in script_text
    assert (
        '"${RELEASE_DIR}/.venv/bin/pip" install --require-hashes --only-binary=:all: -r "${RELEASE_DIR}/requirements.txt"'
        in script_text
    )
    assert (
        '"${RELEASE_DIR}/.venv/bin/pip" install --no-deps -e "${RELEASE_DIR}"'
        in script_text
    )
    assert cnc_admin_wrapper in script_text
    assert llm_help_wrapper in script_text
    assert f"{cnc_admin_wrapper} || return" in script_text
    assert f"{llm_help_wrapper} || return" in script_text
    assert 'log "syncing managed systemd units"' in script_text
    assert "sync_systemd_asset_from_dir()" in script_text
    assert (
        'install -D -m 0644 "${source_dir}/${source_path}" "${SYSTEMD_UNIT_DIR}/${destination_name}" || return'
        in script_text
    )
    assert (
        'sync_systemd_asset_from_dir "${source_dir}" "packaging/systemd/cnc-edge-public-ingress.slice" "cnc-edge-public-ingress.slice"'
        in script_text
    )
    assert (
        'sync_systemd_asset_from_dir "${source_dir}" "packaging/systemd/cnc-edge-tailnet.slice" "cnc-edge-tailnet.slice"'
        in script_text
    )
    assert (
        'sync_systemd_asset_from_dir "${source_dir}" "packaging/systemd/cnc-admin-control.slice" "cnc-admin-control.slice"'
        in script_text
    )
    assert (
        'sync_systemd_asset_from_dir "${source_dir}" "packaging/systemd/cnc-apps.slice" "cnc-apps.slice"'
        in script_text
    )
    assert "systemctl enable --now cnc-auto-size.timer" in script_text
    assert "systemctl enable --now cnc-backend-alerts.timer" in script_text
    assert "systemctl enable --now cnc-cloudflare-sync.timer" in script_text
    assert "systemctl enable --now cnc-update-check.timer" in script_text
    assert "verify_admin_service_write_paths()" in script_text
    assert (
        "for required_path in /etc /etc/containers/systemd /var/lib/cnc /var/log/cnc; do"
        in script_text
    )
    assert "ProtectSystem must be no" in script_text
    assert 'nsenter -t "${main_pid}" -m -- sh -c' in script_text
    assert "if ! verify_admin_service_write_paths; then" in script_text
    assert (
        'fail "${CNC_SERVICE_NAME} cannot write required host runtime paths"'
        in script_text
    )


def test_updater_allows_public_repo_without_pat_and_rolls_back_unit_sync_failure() -> (
    None
):
    script_text = Path("scripts/update_from_github.sh").read_text(encoding="utf-8")

    assert "GITHUB_READONLY_PAT:?set GITHUB_READONLY_PAT" not in script_text
    assert 'if [[ -z "${GITHUB_READONLY_PAT:-}" ]]; then' in script_text
    assert 'DEFAULT_GITHUB_REPO="${DEFAULT_GITHUB_REPO:-Dinkum/cnc}"' not in script_text
    assert (
        'fail "GITHUB_REPO is not set and could not be inferred from git origin"'
        in script_text
    )
    assert (
        'GIT_TERMINAL_PROMPT=0 git clone --depth 1 --branch "${GITHUB_REF}"'
        in script_text
    )
    assert "if ! sync_systemd_units; then" in script_text
    assert 'log "systemd unit sync failed, rolling back"' in script_text
    assert 'restore_previous_systemd_units "${previous_target}"' in script_text


def test_installer_generates_and_hashes_initial_admin_access_key() -> None:
    script_text = Path("scripts/install_ubuntu.sh").read_text(encoding="utf-8")

    assert "ensure_initial_access_key()" in script_text
    assert "secrets.token_urlsafe(24)" in script_text
    assert "hash_access_key(key)" in script_text
    assert 'set_env_key_value "ACCESS_KEY_HASH" "${generated[1]}"' in script_text
    assert "only its Argon2 hash is saved" in script_text
    assert script_text.count("ensure_initial_access_key\n") == 2
