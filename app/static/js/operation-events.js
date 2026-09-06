(() => {
  const RUNNING_STATUSES = new Set(["queued", "running"]);
  const TERMINAL_STATUSES = new Set(["success", "failed", "partial", "cancelled"]);

  const operationUrl = (operationId) => `/api/operations/${encodeURIComponent(operationId)}`;
  const operationEventsUrl = (operationId) => `/api/operations/${encodeURIComponent(operationId)}/events`;
  const activeOperationsUrl = (params = {}) => {
    const url = new URL("/api/active-operations", window.location.origin);
    Object.entries(params).forEach(([key, value]) => {
      if (value === null || value === undefined || value === "") return;
      url.searchParams.set(key, value);
    });
    return `${url.pathname}${url.search}`;
  };

  const normalizeStatus = (payload) => String(payload?.status || "").toLowerCase();

  const responseError = async (response) => {
    const payload = await response.json().catch(() => ({}));
    return new Error(String(payload.detail || `operation request failed: ${response.status}`));
  };

  const dispatchPayload = async (payload, handlers, settle) => {
    if (settle.isTerminal()) return true;
    const status = normalizeStatus(payload);
    // EOF can arrive while a terminal callback awaits its page refresh.
    if (TERMINAL_STATUSES.has(status)) settle.beginTerminal();
    if (RUNNING_STATUSES.has(status)) {
      await handlers.onProgress?.(payload);
      return false;
    }
    if (status === "success") {
      try {
        await handlers.onSuccess?.(payload);
      } catch (error) {
        settle.reject(error);
        return true;
      }
      settle.resolve(payload);
      return true;
    }
    if (TERMINAL_STATUSES.has(status)) {
      let failureResult;
      try {
        failureResult = await handlers.onFailure?.(payload);
      } catch (error) {
        failureResult = error;
      }
      if (handlers.rejectOnFailure) {
        const failureError = failureResult instanceof Error
          ? failureResult
          : new Error(String(payload.error || "operation failed"));
        settle.reject(failureError);
      } else {
        settle.resolve(payload);
      }
      return true;
    }
    await handlers.onProgress?.(payload);
    return false;
  };

  const watchOperation = (operationId, handlers = {}) => new Promise((resolve, reject) => {
    const pollIntervalMs = Number(handlers.pollIntervalMs || handlers.intervalMs || 1000);
    const retryIntervalMs = Number(handlers.retryIntervalMs || 1800);
    const maxRetryIntervalMs = Number(handlers.maxRetryIntervalMs || 15000);
    const maxPollFailures = Number(handlers.maxPollFailures ?? 40);
    let eventSource = null;
    let pollTimer = null;
    let pollFailures = 0;
    let usingPoll = false;
    let settled = false;
    let terminalHandling = false;

    const cleanup = () => {
      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }
      if (pollTimer !== null) {
        window.clearTimeout(pollTimer);
        pollTimer = null;
      }
    };

    const settle = {
      isTerminal: () => settled || terminalHandling,
      beginTerminal: () => {
        terminalHandling = true;
        cleanup();
      },
      resolve: (payload) => {
        if (settled) return;
        settled = true;
        cleanup();
        resolve(payload);
      },
      reject: (error) => {
        if (settled) return;
        settled = true;
        cleanup();
        reject(error);
      },
    };

    const schedulePoll = (delay) => {
      if (settle.isTerminal()) return;
      pollTimer = window.setTimeout(poll, delay);
    };

    const retryDelay = () => {
      const exponent = Math.min(Math.max(pollFailures - 1, 0), 4);
      const delay = retryIntervalMs * (2 ** exponent);
      return Math.max(retryIntervalMs, Math.min(maxRetryIntervalMs, delay));
    };

    const handleRetry = async (error, meta = {}) => {
      if (settle.isTerminal()) return;
      pollFailures += 1;
      const nextRetryDelayMs = retryDelay();
      await handlers.onRetry?.({
        error,
        attempt: pollFailures,
        maxAttempts: maxPollFailures,
        retryDelayMs: nextRetryDelayMs,
        exhausted: Number.isFinite(maxPollFailures) && pollFailures > maxPollFailures,
        ...meta,
      });
      schedulePoll(nextRetryDelayMs);
    };

    const poll = async () => {
      if (settle.isTerminal()) return;
      try {
        const response = await fetch(operationUrl(operationId), {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        if (settle.isTerminal()) return;
        if (!response.ok) {
          const error = await responseError(response);
          if (settle.isTerminal()) return;
          if (response.status === 404) {
            await handlers.onError?.(error);
            settle.reject(error);
            return;
          }
          await handleRetry(error, { status: response.status, transport: "poll" });
          return;
        }
        pollFailures = 0;
        const payload = await response.json();
        const done = await dispatchPayload(payload, handlers, settle);
        if (!done) schedulePoll(pollIntervalMs);
      } catch (error) {
        await handleRetry(error instanceof Error ? error : new Error("operation poll failed"), { transport: "poll" });
      }
    };

    const startPolling = async (reason = "") => {
      if (settle.isTerminal()) return;
      if (usingPoll) return;
      usingPoll = true;
      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }
      await handlers.onFallback?.({ reason, transport: "poll" });
      await poll();
    };

    if (handlers.preferPolling || typeof window.EventSource !== "function") {
      void startPolling("event stream unavailable");
      return;
    }

    try {
      eventSource = new EventSource(operationEventsUrl(operationId));
      eventSource.addEventListener("operation", async (event) => {
        try {
          const payload = JSON.parse(event.data || "{}");
          pollFailures = 0;
          await dispatchPayload(payload, handlers, settle);
        } catch (error) {
          await startPolling(error instanceof Error ? error.message : "event stream parse failed");
        }
      });
      eventSource.addEventListener("error", () => {
        if (!settled) void startPolling("event stream interrupted");
      });
    } catch (error) {
      void startPolling(error instanceof Error ? error.message : "event stream failed");
    }
  });

  const activeOperations = async (params = {}) => {
    const response = await fetch(activeOperationsUrl(params), {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw await responseError(response);
    const payload = await response.json();
    return Array.isArray(payload.operations) ? payload.operations : [];
  };

  window.CNCOperations = {
    activeOperations,
    watchOperation,
  };
})();
