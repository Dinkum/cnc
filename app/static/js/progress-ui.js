(() => {
  // Shared progress rendering for every operation pipeline. The server already
  // speaks one language (OPERATION_PROGRESS_TREES), so this module is the one
  // client-side translation from operation payloads to a rendered surface.
  //
  // Two surfaces exist on purpose:
  //   card   - operations the user can walk away from (saves, backups, updates)
  //   dialog - operations that block, because leaving mid-flight is confusing
  //            (delete, clone, transfer, replica setup)

  const RUNNING_MIN = 4;
  const RUNNING_MAX = 96;
  const SUCCESS_DISMISS_MS = 4000;
  const TERMINAL_STATUSES = new Set(["success", "failed", "partial", "cancelled"]);

  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));

  // Matching strips dots and underscores so "app_runtime" and "App runtime."
  // reach the same pipeline step. Display text keeps its own normalization.
  const matchText = (value) => String(value ?? "")
    .replaceAll(".", " ")
    .replaceAll("_", " ")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();

  const displayPhase = (value) => String(value ?? "").replace(/_/g, " ").trim();

  const formatDuration = (value) => {
    const seconds = Number(value);
    if (!Number.isFinite(seconds)) return "";
    if (seconds < 1) return "<1s";
    if (seconds < 10) return `${seconds.toFixed(1)}s`;
    return `${Math.round(seconds)}s`;
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

  const operationDurationSeconds = (payload, startedAt) => {
    const reported = Number(payload?.duration_seconds);
    if (Number.isFinite(reported)) return reported;
    return elapsedSecondsSince(startedAt);
  };

  const localTimestamp = (value) => (
    window.CNCUI?.formatLocalTimestamp ? window.CNCUI.formatLocalTimestamp(value) : ""
  );

  const isTerminalStatus = (status) => TERMINAL_STATUSES.has(String(status || "").toLowerCase());

  // ---------------------------------------------------------------------------
  // Step resolution
  //
  // Two strategies existed independently and neither had the other's coverage.
  // The dashboard matched a step exactly by (phase, substate); output detail
  // matched progress fuzzily by message against substep/note. Both run here, in
  // order, so a pipeline resolves through whichever path knows the answer.
  // ---------------------------------------------------------------------------

  const exactStep = (steps, phase, substate) => {
    const wantPhase = matchText(phase);
    const wantSubstate = matchText(substate);
    if (!wantPhase || !wantSubstate || !Array.isArray(steps)) return null;
    return steps.find((step) => (
      matchText(step.headline) === wantPhase && matchText(step.substep) === wantSubstate
    )) || null;
  };

  const phaseStep = (steps, phase) => {
    const wantPhase = matchText(phase);
    if (!wantPhase || !Array.isArray(steps)) return null;
    return steps.find((step) => matchText(step.headline) === wantPhase) || null;
  };

  // Highest step whose substep or note equals the message. Highest wins because
  // a pipeline can repeat wording across phases and the later one is the truth.
  const messageProgress = (steps, message) => {
    const wantMessage = matchText(message);
    if (!wantMessage || !Array.isArray(steps)) return 0;
    return steps.reduce((highest, step) => {
      const candidates = [matchText(step.substep), matchText(step.note)];
      return candidates.includes(wantMessage)
        ? Math.max(highest, Number(step.progress) || 0)
        : highest;
    }, 0);
  };

  const phaseProgress = (steps, phase, fallback) => {
    const wantPhase = matchText(phase);
    if (!wantPhase || !Array.isArray(steps)) return fallback;
    return steps.reduce((highest, step) => (
      matchText(step.headline) === wantPhase
        ? Math.max(highest, Number(step.progress) || 0)
        : highest
    ), fallback);
  };

  const resolveProgress = (steps, { phase, substate, message, fallback }) => {
    const matched = exactStep(steps, phase, substate) || exactStep(steps, phase, message);
    if (matched && Number.isFinite(Number(matched.progress))) return Number(matched.progress);
    const byMessage = messageProgress(steps, substate || message);
    if (byMessage > 0) return Math.max(fallback, byMessage);
    return phaseProgress(steps, phase, fallback);
  };

  const resolveStage = (steps, { phase, substate, message, fallbackTitle }) => {
    const matched = exactStep(steps, phase, substate)
      || exactStep(steps, phase, message)
      || phaseStep(steps, phase);
    return {
      headline: displayPhase(phase) || matched?.headline || fallbackTitle,
      note: substate || matched?.substep || message || matched?.note || "",
    };
  };

  const stepIndexForProgress = (steps, progress) => {
    const value = Number(progress);
    if (!Number.isFinite(value) || !Array.isArray(steps) || !steps.length) return 0;
    let index = 0;
    steps.forEach((step, stepIndex) => {
      if (Number(step.progress) <= value) index = stepIndex;
    });
    return index;
  };

  const firstStep = (steps, fallbackLabel, fallbackNote, fallbackProgress = RUNNING_MIN) => {
    const step = (Array.isArray(steps) ? steps : [])[0] || {};
    return {
      progress: clamp(Number(step.progress) || fallbackProgress, 0, RUNNING_MAX),
      headline: String(step.headline || step.substep || fallbackLabel || ""),
      note: String(step.substep || step.note || fallbackNote || ""),
    };
  };

  // ---------------------------------------------------------------------------
  // Meta pills
  // ---------------------------------------------------------------------------

  const operationMeta = (payload, startedAt) => {
    const meta = [];
    const status = String(payload?.status || "").toLowerCase();
    const statusLabel = String(
      payload?.presentation?.status_label
      || (status === "success" ? "saved" : (isTerminalStatus(status) ? "failed" : "")),
    ).toLowerCase();
    const finished = isTerminalStatus(status);
    const duration = formatDuration(operationDurationSeconds(payload, startedAt));
    if (duration) meta.push({ label: finished ? "took" : "elapsed", value: duration });
    if (statusLabel === "failed" && payload?.id) {
      meta.push({ label: "inst", value: shortAlphanumericCode(payload.id) });
    }
    const timestamp = localTimestamp(payload?.finished_at || "");
    if (timestamp) meta.push({ label: statusLabel === "failed" ? "failed" : "saved", value: timestamp });
    return meta;
  };

  const durationMeta = (startedAt) => {
    const duration = formatDuration(elapsedSecondsSince(startedAt));
    return duration ? [{ label: "took", value: duration }] : [];
  };

  // ---------------------------------------------------------------------------
  // Tracker: raw operation payload -> render frame
  // ---------------------------------------------------------------------------

  const createTracker = ({
    steps = [],
    fallbackTitle = "Working",
    fallbackNote = "",
    fallbackProgress = RUNNING_MIN,
    startedAt = null,
  } = {}) => {
    const seed = firstStep(steps, fallbackTitle, fallbackNote, fallbackProgress);
    // Progress is monotonic. A pipeline that reports a phase out of order, or a
    // retry that re-reads an earlier event, must never walk the bar backwards.
    let lastProgress = seed.progress;
    let lastStage = { headline: seed.headline, note: seed.note };

    const frameFor = (payload, { tone, title } = {}) => {
      const details = payload?.details || {};
      const status = String(payload?.status || "").toLowerCase();
      const terminal = isTerminalStatus(status);
      const phase = String(payload?.phase || "");
      const substate = String(details.substate || "");
      const message = String(details.message || payload?.error || payload?.phase || status || "");

      const reported = Number(details.progress);
      const candidate = Number.isFinite(reported)
        ? reported
        : resolveProgress(steps, { phase, substate, message, fallback: lastProgress });
      const bounded = clamp(candidate, 0, 100);

      const stage = resolveStage(steps, { phase, substate, message, fallbackTitle });
      const stageProgress = Number(
        (exactStep(steps, phase, substate) || exactStep(steps, phase, message) || {}).progress,
      );

      const progressRegressed = bounded < lastProgress;
      const stageRegressed = Number.isFinite(stageProgress) && stageProgress < lastProgress;

      const displayProgress = terminal
        ? Math.max(bounded, lastProgress)
        : (progressRegressed ? lastProgress : bounded);
      const displayStage = (progressRegressed || stageRegressed) ? lastStage : stage;

      if (!progressRegressed) lastProgress = displayProgress;
      if (!progressRegressed && !stageRegressed) lastStage = stage;

      const presentedTone = terminal ? String(payload?.presentation?.tone || "") : "";
      const resolvedTone = tone || presentedTone || (terminal ? (status === "success" ? "success" : "error") : "running");
      // A running bar never reads 0% or 100%: both look finished or stuck.
      const visible = resolvedTone === "running"
        ? clamp(displayProgress, RUNNING_MIN, RUNNING_MAX)
        : displayProgress;

      const flashSuccess = details.flash_success || payload?.flash_success || "";
      const flashError = details.flash_error || payload?.flash_error || payload?.error || "";
      const recovery = String(payload?.presentation?.recovery_detail || "");
      // A failure reports its cause, never the step it happened to die on.
      // Carried on the frame so no caller has to remember to pass it.
      const resolvedNote = resolvedTone === "error"
        ? (flashError || recovery || displayStage.note)
        : displayStage.note;

      return {
        tone: resolvedTone,
        title: title || fallbackTitle,
        headline: displayStage.headline,
        note: resolvedNote,
        progress: visible,
        rawProgress: displayProgress,
        activeIndex: stepIndexForProgress(steps, displayProgress),
        meta: operationMeta(payload, startedAt),
        phase,
        substate,
        message,
        flashSuccess,
        flashError,
        details,
        status,
        terminal,
      };
    };

    return {
      get progress() { return lastProgress; },
      get stage() { return { ...lastStage }; },
      seed: () => ({
        tone: "running",
        title: fallbackTitle,
        headline: seed.headline,
        note: seed.note,
        progress: clamp(seed.progress, RUNNING_MIN, RUNNING_MAX),
        rawProgress: seed.progress,
        activeIndex: 0,
        meta: [],
        terminal: false,
      }),
      frame: frameFor,
      // Transport hiccups keep the last known stage and say so, rather than
      // resetting the bar or implying the operation itself failed.
      retryFrame: ({ status, title } = {}) => ({
        tone: "running",
        title: title || fallbackTitle,
        headline: "Waiting for CNC",
        note: status
          ? `Status poll returned ${status}; retrying without stopping the operation.`
          : "Status stream was interrupted; retrying without stopping the operation.",
        progress: clamp(lastProgress, RUNNING_MIN, RUNNING_MAX),
        rawProgress: lastProgress,
        activeIndex: stepIndexForProgress(steps, lastProgress),
        meta: [],
        terminal: false,
      }),
      // Terminal state for flows that finish without an operation payload.
      localFrame: ({ tone = "success", title, headline, note } = {}) => ({
        tone,
        title: title || fallbackTitle,
        headline: headline || (tone === "error" ? "Review error" : "Complete"),
        note: note || "",
        progress: 100,
        rawProgress: 100,
        activeIndex: Math.max(steps.length - 1, 0),
        meta: durationMeta(startedAt),
        terminal: true,
      }),
    };
  };

  // ---------------------------------------------------------------------------
  // Shared bar rendering
  // ---------------------------------------------------------------------------

  const paintTrack = (root, frame) => {
    if (!root) return;
    const fill = root.querySelector("[data-progress-fill]");
    const track = root.querySelector("[data-progress-track]");
    const value = root.querySelector("[data-progress-value]");
    const rounded = Math.round(Number(frame.progress) || 0);
    if (fill) {
      fill.style.width = `${frame.progress}%`;
      fill.dataset.tone = frame.tone;
    }
    if (track) track.setAttribute("aria-valuenow", String(rounded));
    if (value) value.textContent = `${rounded}%`;
  };

  const setText = (root, selector, text) => {
    const element = root?.querySelector(selector);
    if (!element) return;
    const value = String(text || "");
    element.textContent = value;
    element.hidden = !value;
  };

  const renderMeta = (container, meta) => {
    if (!container) return;
    container.replaceChildren();
    (meta || []).forEach((item) => {
      if (!item || !item.label || !item.value) return;
      const pill = document.createElement("span");
      pill.className = "summary-pill";
      const label = document.createElement("span");
      label.className = "metric-label";
      label.textContent = item.label;
      pill.appendChild(label);
      pill.append(` ${item.value}`);
      container.appendChild(pill);
    });
  };

  const renderActions = (container, actions) => {
    if (!container) return false;
    container.replaceChildren();
    const list = (actions || []).filter((action) => action && action.label);
    list.forEach((action, index) => {
      const isLink = Boolean(action.href);
      const element = document.createElement(isLink ? "a" : "button");
      if (isLink) {
        element.className = "button-link";
        element.setAttribute("href", action.href);
      } else {
        element.type = "button";
        element.className = index === list.length - 1 && !action.ghost ? "" : "ghost";
      }
      element.textContent = action.label;
      if (typeof action.onClick === "function") {
        element.addEventListener("click", action.onClick);
      }
      container.appendChild(element);
    });
    container.hidden = !list.length;
    return Boolean(list.length);
  };

  // ---------------------------------------------------------------------------
  // Card surface: operations the user can walk away from
  //
  // Dismissal lives here and nowhere else. Running persists, success clears
  // itself, error waits for the reader, and anything carrying an action waits
  // because the action is the point.
  // ---------------------------------------------------------------------------

  const cardSurface = (() => {
    const cards = new Map();
    let shell = null;

    const ensureShell = () => {
      if (shell && shell.isConnected) return shell;
      shell = document.querySelector("[data-progress-shell]");
      if (!shell) {
        shell = document.createElement("aside");
        shell.className = "progress-card-shell";
        shell.setAttribute("data-progress-shell", "");
        shell.setAttribute("aria-live", "polite");
        document.body.appendChild(shell);
      }
      return shell;
    };

    const el = (tag, { className = "", attrs = {}, text = "" } = {}, children = []) => {
      const node = document.createElement(tag);
      if (className) node.className = className;
      Object.entries(attrs).forEach(([name, value]) => node.setAttribute(name, value));
      if (text) node.textContent = text;
      children.forEach((child) => node.appendChild(child));
      return node;
    };

    const buildCard = (key) => {
      const close = el("button", {
        className: "progress-card-close",
        attrs: { type: "button", "data-progress-dismiss": "", "aria-label": "Dismiss status" },
        text: "×",
      });
      close.addEventListener("click", () => dismiss(key));

      const body = el("div", { className: "progress-card-body" }, [
        el("div", { className: "progress-card-head" }, [
          el("strong", { attrs: { "data-progress-title": "" } }),
          el("div", { className: "progress-card-meta", attrs: { "data-progress-meta": "" } }),
        ]),
        el("span", { className: "progress-card-stage", attrs: { "data-progress-headline": "" } }),
        el("p", { className: "progress-card-note", attrs: { "data-progress-note": "" } }),
      ]);

      const track = el("div", {
        className: "progress-track progress-track--sm",
        attrs: {
          "data-progress-track": "",
          role: "progressbar",
          "aria-valuemin": "0",
          "aria-valuemax": "100",
        },
      }, [el("span", { className: "progress-fill", attrs: { "data-progress-fill": "" } })]);

      const actions = el("div", { className: "progress-card-actions", attrs: { "data-progress-actions": "" } });
      actions.hidden = true;

      const card = el("article", { className: "progress-card" }, [close, body, track, actions]);
      card.dataset.progressCard = key;
      return card;
    };

    const trim = () => {
      const visible = Array.from(ensureShell().children);
      visible.slice(0, Math.max(0, visible.length - 3)).forEach((node) => {
        const key = node.dataset.progressCard;
        if (key) dismiss(key);
      });
    };

    const dismiss = (key) => {
      const entry = cards.get(key);
      if (!entry) return;
      if (entry.timer) window.clearTimeout(entry.timer);
      entry.node.remove();
      cards.delete(key);
      if (shell && !shell.children.length) shell.hidden = true;
    };

    const show = (key, frame, { actions = [], onDismiss = null } = {}) => {
      if (!frame) return;
      const host = ensureShell();
      host.hidden = false;
      let entry = cards.get(key);
      if (!entry) {
        entry = { node: buildCard(key), timer: null, onDismiss };
        cards.set(key, entry);
        host.appendChild(entry.node);
        trim();
      }
      if (onDismiss) entry.onDismiss = onDismiss;
      const node = entry.node;
      node.dataset.tone = frame.tone;
      setText(node, "[data-progress-title]", frame.title);
      setText(node, "[data-progress-headline]", frame.headline);
      setText(node, "[data-progress-note]", frame.note);
      renderMeta(node.querySelector("[data-progress-meta]"), frame.meta);
      paintTrack(node, frame);
      const hasActions = renderActions(node.querySelector("[data-progress-actions]"), actions);

      if (entry.timer) {
        window.clearTimeout(entry.timer);
        entry.timer = null;
      }
      // Success clears itself so the screen returns to the work. An error, or
      // anything offering a next step, stays until the reader deals with it.
      if (frame.tone === "success" && !hasActions) {
        entry.timer = window.setTimeout(() => dismiss(key), SUCCESS_DISMISS_MS);
      }
      return node;
    };

    return {
      show,
      dismiss,
      dismissAll: () => Array.from(cards.keys()).forEach(dismiss),
      has: (key) => cards.has(key),
      get size() { return cards.size; },
    };
  })();

  // ---------------------------------------------------------------------------
  // Dialog surface: operations that block
  //
  // One state machine for every progress dialog. Sections are tagged
  // [data-progress-state] and action groups [data-progress-actions-for], so a
  // dialog declares its states in markup and needs no bespoke JS.
  // ---------------------------------------------------------------------------

  const createDialogSurface = (dialog) => {
    if (!dialog) return null;
    const states = Array.from(dialog.querySelectorAll("[data-progress-state]"));
    const actionGroups = Array.from(dialog.querySelectorAll("[data-progress-actions-for]"));
    const log = dialog.querySelector("[data-progress-log]");

    const setState = (mode) => {
      states.forEach((section) => {
        section.hidden = section.dataset.progressState !== mode;
      });
      actionGroups.forEach((group) => {
        // A group may serve several states: data-progress-actions-for="success error"
        const serves = String(group.dataset.progressActionsFor || "").split(/\s+/).filter(Boolean);
        group.hidden = !serves.includes(mode);
      });
    };

    const open = () => {
      if (dialog.open || typeof dialog.showModal !== "function") return;
      try {
        dialog.showModal();
      } catch (error) {
        console.warn("progress dialog open failed", error);
      }
    };

    return {
      dialog,
      setState,
      open,
      close: (reason = "close") => {
        if (dialog.open) dialog.close(reason);
      },
      clearLog: () => {
        if (log) log.replaceChildren();
      },
      log: (message) => appendNarrative(log, message),
      render: (frame) => {
        if (!frame) return;
        paintTrack(dialog, frame);
        setText(dialog, "[data-progress-label]", frame.headline || frame.title);
        setText(dialog, "[data-progress-note]", frame.note);
      },
      actions: (list) => renderActions(dialog.querySelector("[data-progress-actions]"), list),
    };
  };

  // ---------------------------------------------------------------------------
  // Narrative log
  // ---------------------------------------------------------------------------

  const appendNarrative = (target, message) => {
    if (!target || !message) return;
    const normalized = String(message).trim();
    if (!normalized) return;
    const lastLine = target.lastElementChild;
    if (lastLine && lastLine.textContent.trim() === normalized) return;
    const line = document.createElement("div");
    line.className = "progress-log-line";
    line.textContent = normalized;
    target.appendChild(line);
    target.scrollTop = target.scrollHeight;
  };

  window.CNCProgress = Object.freeze({
    RUNNING_MIN,
    RUNNING_MAX,
    SUCCESS_DISMISS_MS,
    appendNarrative,
    card: cardSurface,
    createTracker,
    dialog: createDialogSurface,
    durationMeta,
    formatDuration,
    isTerminalStatus,
    operationMeta,
    shortAlphanumericCode,
    stepIndexForProgress,
    // Exported for tests and for surfaces that resolve steps directly.
    _internals: { exactStep, phaseStep, messageProgress, phaseProgress, resolveProgress, resolveStage, firstStep, matchText },
  });
})();
