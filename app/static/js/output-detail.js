(() => {
  const {
    ensureMetricChartAssets,
    escapeHtml,
    formatLocalTimestamp,
    loadDeferredScript,
    normalizeTimestampValue,
    renderLocalTimes,
  } = window.CNCUI;
  const outputDetailConfig = window.CNCUI.readJsonScript("output-detail-config", {});
  const operationProgressPipelines = outputDetailConfig.operationProgressPipelines || {};
  const outputSaveProgressSteps = outputDetailConfig.outputSaveProgressSteps || [];
  const backendId = Number(outputDetailConfig.backendId);
  const backendName = String(outputDetailConfig.backendName || "");
  let csrfToken = String(outputDetailConfig.csrfToken || "");

  renderLocalTimes(document);

  const syncResourceSizeControls = (root = document) => {
    root.querySelectorAll("[data-resource-size-control]").forEach((select) => {
      const form = select.closest("form") || document;
      const isCustom = select.value === "custom";
      form.querySelectorAll("[data-custom-resource-limit]").forEach((section) => {
        section.hidden = !isCustom;
        section.querySelectorAll("input, select, textarea").forEach((field) => {
          field.disabled = !isCustom;
        });
      });
    });
  };

  document.querySelectorAll("[data-resource-size-control]").forEach((select) => {
    select.addEventListener("change", () => syncResourceSizeControls(select.closest("form") || document));
  });
  syncResourceSizeControls(document);

  const outputAttachFormIds = (form) => (
    Array.from(form?.querySelectorAll("input[name='input_ids']") || [])
      .map((input) => String(input.value || "").trim())
      .filter(Boolean)
      .sort()
      .join(",")
  );

  const markOutputAttachFormClean = (form) => {
    const scope = form?.querySelector("[data-output-attach-form]");
    if (!scope) return;
    form.dataset.outputAttachInitialIds = outputAttachFormIds(form);
    form.dispatchEvent(new Event("output-attach-dirtycheck"));
  };

  // --- output/input attach/detach ---
  document.querySelectorAll("[data-output-attach-form]").forEach((scope) => {
    const form = scope.closest("form");
    const tagsWrap = scope.querySelector("[data-output-tags]");
    const emptyMsg = tagsWrap ? tagsWrap.querySelector(".output-tag-empty") : null;
    const addWrap = scope.querySelector("[data-output-add-wrap]");
    const addBtn = addWrap ? addWrap.querySelector("[data-output-add-toggle]") : null;
    const dropdown = addWrap ? addWrap.querySelector("[data-output-dropdown]") : null;
    const searchInput = dropdown ? dropdown.querySelector("[data-output-search]") : null;
    const listWrap = dropdown ? dropdown.querySelector("[data-output-list]") : null;
    const saveActions = form ? form.querySelector("[data-output-attach-save]") : null;
    let attachOptionsLoading = false;

    if (form) {
      form.dataset.outputAttachInitialIds = outputAttachFormIds(form);
    }

    const syncEmpty = () => {
      if (emptyMsg && tagsWrap) emptyMsg.hidden = tagsWrap.querySelectorAll(".output-tag").length > 0;
    };
    const hasLazyAttachOptions = () => (
      Boolean(addWrap?.dataset.outputAttachOptionsUrl) && listWrap?.dataset.loaded !== "1"
    );
    const syncAddButton = () => {
      if (addBtn && listWrap) {
        addBtn.disabled = attachOptionsLoading || (
          !hasLazyAttachOptions()
          && listWrap.querySelectorAll("[data-output-option]:not([hidden])").length === 0
        );
      }
    };
    const syncDirty = () => {
      if (!form || !saveActions) return;
      const isDirty = outputAttachFormIds(form) !== (form.dataset.outputAttachInitialIds || "");
      saveActions.hidden = !isDirty;
      saveActions.querySelectorAll("button, input").forEach((control) => {
        control.disabled = !isDirty;
      });
    };
    const removeTag = (tag) => {
      const id = tag.dataset.outputId;
      tag.remove();
      if (listWrap) {
        const opt = listWrap.querySelector(`[data-output-option][data-output-id="${id}"]`);
        if (opt) opt.hidden = false;
      }
      syncEmpty();
      syncAddButton();
      syncDirty();
    };
    const addTag = (id, name) => {
      if (!tagsWrap) return;
      const tag = document.createElement("span");
      tag.className = "output-tag";
      tag.dataset.outputId = id;
      const hidden = document.createElement("input");
      hidden.type = "hidden";
      hidden.name = "input_ids";
      hidden.value = id;
      const nameSpan = document.createElement("span");
      nameSpan.className = "output-tag-name mono";
      nameSpan.textContent = name;
      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "output-tag-remove";
      removeBtn.innerHTML = "&times;";
      removeBtn.addEventListener("click", () => removeTag(tag));
      tag.appendChild(hidden);
      tag.appendChild(nameSpan);
      tag.appendChild(removeBtn);
      tagsWrap.insertBefore(tag, emptyMsg);
      if (listWrap) {
        const opt = listWrap.querySelector(`[data-output-option][data-output-id="${id}"]`);
        if (opt) opt.hidden = true;
      }
      syncEmpty();
      syncAddButton();
      syncDirty();
    };
    const bindOutputOption = (opt) => {
      opt.addEventListener("click", () => {
        addTag(opt.dataset.outputId, opt.dataset.outputName);
        if (dropdown) dropdown.hidden = true;
      });
    };
    const renderOutputOption = (item) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "output-add-item";
      button.dataset.outputOption = "";
      button.dataset.outputId = String(item.id || "");
      button.dataset.outputName = String(item.value || "");
      button.dataset.outputKind = String(item.kind_label || "");
      button.dataset.outputEnabled = "";
      if (item.attached) button.hidden = true;
      if (!item.attachable) {
        button.disabled = true;
        if (item.unavailable_reason) button.title = String(item.unavailable_reason);
      }
      const name = document.createElement("span");
      name.className = "output-add-item-name mono";
      name.textContent = String(item.value || "");
      button.appendChild(name);
      if (item.unavailable_reason) {
        const meta = document.createElement("span");
        meta.className = "output-add-item-meta";
        meta.textContent = String(item.unavailable_reason || "");
        button.appendChild(meta);
      }
      bindOutputOption(button);
      return button;
    };
    const loadAttachOptions = async () => {
      if (!listWrap || listWrap.dataset.loaded === "1" || attachOptionsLoading) return;
      const url = addWrap?.dataset.outputAttachOptionsUrl || "";
      if (!url) return;
      attachOptionsLoading = true;
      if (addBtn) addBtn.disabled = true;
      try {
        const response = await fetch(url, {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        if (!response.ok) throw new Error(`input options failed: ${response.status}`);
        const payload = await response.json();
        listWrap.replaceChildren();
        (payload.options || []).forEach((item) => {
          listWrap.appendChild(renderOutputOption(item));
        });
        listWrap.dataset.loaded = "1";
      } catch {
        listWrap.dataset.loaded = "error";
      } finally {
        attachOptionsLoading = false;
        syncAddButton();
      }
    };
    tagsWrap && tagsWrap.querySelectorAll("[data-remove-output]").forEach((btn) => {
      btn.addEventListener("click", () => removeTag(btn.closest(".output-tag")));
    });
    if (addBtn && dropdown) {
      addBtn.addEventListener("click", async (event) => {
        event.stopPropagation();
        await loadAttachOptions();
        dropdown.hidden = !dropdown.hidden;
        if (!dropdown.hidden && searchInput) { searchInput.value = ""; searchInput.dispatchEvent(new Event("input")); searchInput.focus(); }
      });
      document.addEventListener("click", (e) => {
        if (e.target instanceof Node && !addWrap.contains(e.target)) dropdown.hidden = true;
      });
    }
    if (searchInput && listWrap) {
      searchInput.addEventListener("input", () => {
        const q = searchInput.value.toLowerCase().trim();
        listWrap.querySelectorAll("[data-output-option]").forEach((opt) => {
          if (opt.hidden && tagsWrap && tagsWrap.querySelector(`[data-output-id="${opt.dataset.outputId}"]`)) return;
          opt.style.display = (!q || (opt.dataset.outputName || "").toLowerCase().includes(q)) ? "" : "none";
        });
      });
    }
    if (listWrap) {
      listWrap.querySelectorAll("[data-output-option]").forEach((opt) => {
        bindOutputOption(opt);
      });
    }
    syncEmpty();
    syncAddButton();
    syncDirty();
    form?.addEventListener("output-attach-dirtycheck", syncDirty);
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
        window.setTimeout(() => {
          button.textContent = original || "Copy";
        }, 1400);
      } catch {
        button.textContent = "Failed";
        window.setTimeout(() => {
          button.textContent = original || "Copy";
        }, 1400);
      }
    });
  });

  const deleteModal = document.getElementById("delete-output-modal");
  const shieldCodeErrorModal = document.getElementById("shield-code-error-modal");
  const deleteForm = document.querySelector("[data-delete-output-form]");
  const deleteTrigger = document.querySelector("[data-delete-output-trigger]");
  const deleteAccept = deleteModal?.querySelector("[data-confirm-accept]");
  const deleteCancel = deleteModal?.querySelector("[data-confirm-cancel]");
  const deleteCloseButtons = deleteModal ? Array.from(deleteModal.querySelectorAll("[data-delete-close]")) : [];
  const deleteStates = deleteModal ? Array.from(deleteModal.querySelectorAll("[data-delete-state]")) : [];
  const deleteActionGroups = deleteModal ? Array.from(deleteModal.querySelectorAll("[data-delete-actions]")) : [];
  const deleteProgressLabel = deleteModal?.querySelector("[data-delete-progress-label]");
  const deleteProgressFill = deleteModal?.querySelector("[data-delete-progress-fill]");
  const deleteProgressNote = deleteModal?.querySelector("[data-delete-progress-note]");
  const cloneModal = document.getElementById("clone-output-modal");
  const cloneTrigger = document.querySelector("[data-clone-output-trigger]");
  const cloneForm = cloneModal?.querySelector("[data-clone-output-form]");
  const cloneNameInput = cloneModal?.querySelector("[data-clone-name]");
  const clonePortInput = cloneModal?.querySelector("[data-clone-port]");
  const cloneCancelButtons = cloneModal ? Array.from(cloneModal.querySelectorAll("[data-clone-cancel], [data-clone-close]")) : [];
  const cloneSubmit = cloneModal?.querySelector("[data-clone-submit]");
  const cloneProgressLabel = cloneModal?.querySelector("[data-clone-progress-label]");
  const cloneProgressValue = cloneModal?.querySelector("[data-clone-progress-value]");
  const cloneProgressFill = cloneModal?.querySelector("[data-clone-progress-fill]");
  const cloneProgressLog = cloneModal?.querySelector("[data-clone-progress-log]");
  const cloneProgressNote = cloneModal?.querySelector("[data-clone-progress-note]");
  const cloneSuccessNote = cloneModal?.querySelector("[data-clone-success-note]");
  const cloneOpenLink = cloneModal?.querySelector("[data-clone-open-link]");
  const cloneErrorText = cloneModal?.querySelector("[data-clone-error-text]");
  const transferModal = document.getElementById("transfer-output-modal");
  const transferTrigger = document.querySelector("[data-transfer-output-trigger]");
  const transferForm = transferModal?.querySelector("[data-transfer-output-form]");
  const transferClose = transferModal?.querySelector("[data-transfer-close]");
  const transferCancel = transferModal?.querySelector("[data-transfer-cancel]");
  const transferNext = transferModal?.querySelector("[data-transfer-next]");
  const transferBack = transferModal?.querySelector("[data-transfer-back]");
  const transferTargetPicker = transferModal?.querySelector("[data-transfer-target-picker]");
  const transferTargetInput = transferModal?.querySelector("[data-transfer-target-value]");
  const transferTargetToggle = transferModal?.querySelector("[data-transfer-target-toggle]");
  const transferTargetList = transferModal?.querySelector("[data-transfer-target-list]");
  const transferTargetLabel = transferModal?.querySelector("[data-transfer-target-label]");
  const transferTargetRole = transferModal?.querySelector("[data-transfer-target-role]");
  const transferTargetOptions = transferTargetList
    ? Array.from(transferTargetList.querySelectorAll("[data-transfer-target-option]"))
    : [];
  const transferProgressLabel = transferModal?.querySelector("[data-transfer-progress-label]");
  const transferProgressValue = transferModal?.querySelector("[data-transfer-progress-value]");
  const transferProgressFill = transferModal?.querySelector("[data-transfer-progress-fill]");
  const transferProgressNote = transferModal?.querySelector("[data-transfer-progress-note]");
  const transferMini = document.querySelector("[data-transfer-mini]");
  const transferMiniLabel = document.querySelector("[data-transfer-mini-label]");
  const transferMiniNote = document.querySelector("[data-transfer-mini-note]");
  const transferMiniFill = document.querySelector("[data-transfer-mini-fill]");
  const transferReopen = document.querySelector("[data-transfer-reopen]");
  const replicaSetupModal = document.getElementById("replica-setup-modal");
  const replicaSetupForm = replicaSetupModal?.querySelector("[data-replica-setup-form]");
  const replicaSetupTarget = replicaSetupModal?.querySelector("[data-replica-setup-target]");
  const replicaSetupNodeName = replicaSetupModal?.querySelector("[data-replica-setup-node-name]");
  const replicaSetupStates = replicaSetupModal ? Array.from(replicaSetupModal.querySelectorAll("[data-replica-setup-state]")) : [];
  const replicaSetupActionGroups = replicaSetupModal ? Array.from(replicaSetupModal.querySelectorAll("[data-replica-setup-actions]")) : [];
  const replicaSetupCancelButtons = replicaSetupModal ? Array.from(replicaSetupModal.querySelectorAll("[data-replica-setup-cancel]")) : [];
  const replicaSetupDone = replicaSetupModal?.querySelector("[data-replica-setup-done]");
  const replicaSetupRetry = replicaSetupModal?.querySelector("[data-replica-setup-retry]");
  const replicaSetupProgressLabel = replicaSetupModal?.querySelector("[data-replica-setup-progress-label]");
  const replicaSetupProgressValue = replicaSetupModal?.querySelector("[data-replica-setup-progress-value]");
  const replicaSetupProgressFill = replicaSetupModal?.querySelector("[data-replica-setup-progress-fill]");
  const replicaSetupProgressNote = replicaSetupModal?.querySelector("[data-replica-setup-progress-note]");
  const replicaSetupSuccessNote = replicaSetupModal?.querySelector("[data-replica-setup-success-note]");
  const replicaSetupErrorText = replicaSetupModal?.querySelector("[data-replica-setup-error-text]");
  const flashBlock = document.getElementById("output-flash-block");
  const backupForm = document.querySelector("[data-backup-form]");
  const backupSubmit = backupForm?.querySelector("[data-backup-submit]");
  const backupProgressShell = document.getElementById("backup-progress-shell");
  const backupProgressFill = backupProgressShell?.querySelector("[data-backup-progress-fill]");
  const backupProgressLog = backupProgressShell?.querySelector("[data-backup-progress-log]");
  const deleteBackupModal = document.getElementById("delete-backup-modal");
  const deleteBackupCopy = deleteBackupModal?.querySelector("[data-delete-backup-copy]");
  const deleteBackupAccept = deleteBackupModal?.querySelector("[data-delete-backup-accept]");
  const deleteBackupCancel = deleteBackupModal?.querySelector("[data-delete-backup-cancel]");
  const outputEnabledChip = document.querySelector(".output-page-badges .stat-chip");
  const operationProgressTrackers = new Map();
  let clonePending = false;
  let deletePending = false;
  let transferPending = false;
  let replicaSetupPending = false;
  let pendingReplicaSetupCard = null;
  let resumeReplicaSetupOperation = () => {};
  let applyInboundInterfaceOperationResult = () => {};
  let deleteFinishedRedirect = "";
  let activeDeleteBackupForm = null;
  let backupPending = false;
  let backupSignalsRequestId = 0;

  const setDeleteMode = (mode) => {
    deleteStates.forEach((section) => {
      section.hidden = section.dataset.deleteState !== mode;
    });
    deleteActionGroups.forEach((group) => {
      group.hidden = group.dataset.deleteActions !== mode;
    });
  };

  const setDeleteProgress = (percent, label, note, statusText) => {
    const normalized = Math.max(0, Math.min(100, Number(percent) || 0));
    if (deleteProgressFill) {
      deleteProgressFill.style.width = `${normalized}%`;
      deleteProgressFill.classList.toggle("is-complete", normalized >= 100);
    }
    if (deleteProgressLabel) deleteProgressLabel.textContent = label || "Deleting output";
    if (deleteProgressNote) deleteProgressNote.textContent = statusText || note || "Removing the output.";
  };

  const firstOperationStep = (pipelineName, fallbackLabel, fallbackNote, fallbackProgress = 4) => {
    const step = (operationProgressPipelines[pipelineName] || [])[0] || {};
    const progress = Math.max(0, Math.min(96, Number(step.progress) || fallbackProgress));
    return {
      progress,
      label: String(step.headline || step.substep || fallbackLabel),
      note: String(step.substep || step.note || fallbackNote),
    };
  };

  const markFirstOperationStep = (pipelineName, fallbackLabel, fallbackNote, fallbackProgress = 4) => {
    const step = firstOperationStep(pipelineName, fallbackLabel, fallbackNote, fallbackProgress);
    operationProgressTrackers.set(pipelineName, step.progress);
    return step;
  };

  const openProgressDialog = (dialog) => {
    if (!dialog || dialog.open || typeof dialog.showModal !== "function") return;
    try {
      dialog.showModal();
    } catch (error) {
      console.warn("operation progress dialog open failed", error);
    }
  };

  const finishDeleteWithoutReload = (redirectUrl) => {
    deletePending = false;
    deleteFinishedRedirect = String(redirectUrl || "/?tab=outputs");
  };

  if (deleteModal && deleteForm && deleteTrigger && deleteAccept && deleteCancel) {
    deleteTrigger.addEventListener("click", () => {
      if (deletePending) return;
      deleteFinishedRedirect = "";
      setDeleteMode("form");
      setDeleteProgress(0, "Deleting output", "Removing the output.", "Queued.");
      if (typeof deleteModal.showModal === "function") {
        deleteModal.showModal();
      }
    });
    deleteModal.addEventListener("cancel", () => {});
    deleteCancel.addEventListener("click", () => {
      if (deletePending) return;
      deleteModal.close("cancel");
    });
    deleteCloseButtons.forEach((button) => {
      button.addEventListener("click", () => {
        if (deleteFinishedRedirect) {
          window.location.href = deleteFinishedRedirect;
          return;
        }
        deleteModal.close(deletePending ? "hide" : "cancel");
      });
    });
    deleteAccept.addEventListener("click", async () => {
      if (deletePending) return;
      deletePending = true;
      const firstDeleteStep = markFirstOperationStep(
        "deleteOutput",
        "Delete output",
        "Opening delete operation.",
      );
      setDeleteMode("progress");
      setDeleteProgress(
        firstDeleteStep.progress,
        firstDeleteStep.label,
        firstDeleteStep.note,
        "Starting...",
      );
      try {
        let formData = new FormData(deleteForm);
        let response = await fetch(formActionPath(deleteForm), {
          method: "POST",
          credentials: "same-origin",
          body: formData,
          headers: {
            Accept: "application/json",
            "X-Requested-With": "fetch",
          },
        });
        if (response.status === 403 && await refreshCsrfTokens()) {
          formData = new FormData(deleteForm);
          response = await fetch(formActionPath(deleteForm), {
            method: "POST",
            credentials: "same-origin",
            body: formData,
            headers: {
              Accept: "application/json",
              "X-Requested-With": "fetch",
            },
          });
        }
        const { payload, text } = await readResponsePayload(response);
        if (!response.ok) {
          throw new Error(responseErrorMessage(response, payload, text, "Delete request failed", {
            method: "POST",
            path: formActionPath(deleteForm),
          }));
        }
        const operationId = payload.operation_id;
        if (!operationId) {
          setDeleteProgress(100, "Delete output", "Output deleted.", "Done.");
          finishDeleteWithoutReload(payload.redirect_url || "/?tab=outputs");
          return;
        }
        setDeleteProgress(
          firstDeleteStep.progress,
          firstDeleteStep.label,
          firstDeleteStep.note,
          `operation #${operationId}`,
        );
        watchOperation(operationId, {
          intervalMs: 1000,
          onProgress: (operation) => {
            const progress = operationProgress(operation, firstDeleteStep.progress, "deleteOutput");
            const label = progress.phase || "Delete output";
            const note = progress.substate || progress.message || "Deleting output.";
            const statusText = progress.message || String(operation.status || "Working.");
            setDeleteProgress(progress.progress, label, note, statusText);
          },
          onSuccess: (operation) => {
            const progress = operationProgress(operation, 100, "deleteOutput");
            setDeleteProgress(100, "Delete output", progress.message || "Output deleted.", "Done.");
            finishDeleteWithoutReload(progress.details.redirect_url || "/?tab=outputs");
          },
          onFailure: (operation) => {
            deletePending = false;
            setDeleteMode("form");
            const progress = operationProgress(operation, 100, "deleteOutput");
            setDeleteProgress(100, "Delete failed", progress.flashError || progress.message || "Delete failed.", "Failed.");
            renderFlashBanner("error", progress.flashError || "Delete failed.");
          },
          onError: (error) => {
            deletePending = false;
            setDeleteMode("form");
            setDeleteProgress(100, "Delete failed", error instanceof Error ? error.message : "Delete failed.", "Failed.");
            renderFlashBanner("error", error instanceof Error ? error.message : "Delete failed.");
          },
        }).catch((error) => {
          deletePending = false;
          setDeleteMode("form");
          setDeleteProgress(100, "Delete failed", error instanceof Error ? error.message : "Delete failed.", "Failed.");
          renderFlashBanner("error", error instanceof Error ? error.message : "Delete failed.");
        });
      } catch (error) {
        deletePending = false;
        setDeleteMode("form");
        renderFlashBanner("error", error instanceof Error ? error.message : "Delete failed.");
      }
    });
  }

  const resumeDeleteOperation = (operation) => {
    if (!deleteModal || deletePending) return;
    deletePending = true;
    const firstDeleteStep = markFirstOperationStep(
      "deleteOutput",
      "Delete output",
      "Opening delete operation.",
    );
    setDeleteMode("progress");
    setDeleteProgress(
      firstDeleteStep.progress,
      firstDeleteStep.label,
      firstDeleteStep.note,
      "Resuming...",
    );
    openProgressDialog(deleteModal);
    watchOperation(operation.id, {
      intervalMs: 1000,
      onProgress: (payload) => {
        const progress = operationProgress(payload, firstDeleteStep.progress, "deleteOutput");
        const label = progress.phase || "Delete output";
        const note = progress.substate || progress.message || "Deleting output.";
        const statusText = progress.message || String(payload.status || "Working.");
        setDeleteProgress(progress.progress, label, note, statusText);
      },
      onSuccess: (payload) => {
        const progress = operationProgress(payload, 100, "deleteOutput");
        setDeleteProgress(100, "Delete output", progress.message || "Output deleted.", "Done.");
        finishDeleteWithoutReload(progress.details.redirect_url || "/?tab=outputs");
      },
      onFailure: (payload) => {
        deletePending = false;
        setDeleteMode("form");
        const progress = operationProgress(payload, 100, "deleteOutput");
        setDeleteProgress(100, "Delete failed", progress.flashError || progress.message || "Delete failed.", "Failed.");
        renderFlashBanner("error", progress.flashError || "Delete failed.");
      },
      onError: (error) => {
        deletePending = false;
        setDeleteMode("form");
        setDeleteProgress(100, "Delete failed", error instanceof Error ? error.message : "Delete failed.", "Failed.");
        renderFlashBanner("error", error instanceof Error ? error.message : "Delete failed.");
      },
    }).catch((error) => {
      deletePending = false;
      setDeleteMode("form");
      setDeleteProgress(100, "Delete failed", error instanceof Error ? error.message : "Delete failed.", "Failed.");
      renderFlashBanner("error", error instanceof Error ? error.message : "Delete failed.");
    });
  };

  const cloneStates = cloneModal ? Array.from(cloneModal.querySelectorAll("[data-clone-state]")) : [];
  const cloneActionGroups = cloneModal ? Array.from(cloneModal.querySelectorAll("[data-clone-actions]")) : [];
  const setCloneMode = (mode) => {
    cloneStates.forEach((section) => {
      section.hidden = section.dataset.cloneState !== mode;
    });
    cloneActionGroups.forEach((group) => {
      group.hidden = group.dataset.cloneActions !== (mode === "form" ? "form" : "progress");
    });
  };

  const setCloneProgress = (percent, label, note) => {
    const normalized = Math.max(0, Math.min(100, Number(percent) || 0));
    if (cloneProgressFill) cloneProgressFill.style.width = `${normalized}%`;
    if (cloneProgressValue) cloneProgressValue.textContent = `${Math.round(normalized)}%`;
    if (cloneProgressLabel && label) cloneProgressLabel.textContent = label;
    if (cloneProgressNote && note) cloneProgressNote.textContent = note;
  };

  const transferStates = transferModal ? Array.from(transferModal.querySelectorAll("[data-transfer-state]")) : [];
  const transferActionGroups = transferModal ? Array.from(transferModal.querySelectorAll("[data-transfer-actions]")) : [];

  const setTransferTargetOpen = (isOpen) => {
    if (!transferTargetPicker || !transferTargetToggle || !transferTargetList) return;
    transferTargetPicker.classList.toggle("is-open", isOpen);
    transferTargetToggle.setAttribute("aria-expanded", String(isOpen));
    transferTargetList.hidden = !isOpen;
  };

  const selectedTransferTargetIndex = () => {
    const index = transferTargetOptions.findIndex((option) => option.getAttribute("aria-selected") === "true");
    return index >= 0 ? index : 0;
  };

  const focusTransferTargetOption = (index) => {
    if (!transferTargetOptions.length) return;
    const normalized = Math.max(0, Math.min(transferTargetOptions.length - 1, index));
    transferTargetOptions[normalized].focus();
  };

  const selectTransferTarget = (option) => {
    if (!option) return;
    transferTargetOptions.forEach((item) => {
      item.setAttribute("aria-selected", item === option ? "true" : "false");
    });
    if (transferTargetInput) transferTargetInput.value = option.dataset.nodeId || "";
    if (transferTargetLabel) transferTargetLabel.textContent = option.dataset.nodeName || "";
    if (transferTargetRole) {
      transferTargetRole.textContent = option.dataset.nodeRole || "";
      transferTargetRole.hidden = !option.dataset.nodeRole;
    }
    setTransferTargetOpen(false);
    transferTargetToggle?.focus();
  };

  const setTransferMode = (mode) => {
    transferStates.forEach((section) => {
      section.hidden = section.dataset.transferState !== mode;
    });
    transferActionGroups.forEach((group) => {
      group.hidden = group.dataset.transferActions !== mode;
    });
    if (mode !== "target") setTransferTargetOpen(false);
  };

  const setTransferProgress = (percent, label, note) => {
    const normalized = Math.max(0, Math.min(100, Number(percent) || 0));
    if (transferProgressFill) transferProgressFill.style.width = `${normalized}%`;
    if (transferProgressValue) transferProgressValue.textContent = `${Math.round(normalized)}%`;
    if (transferProgressLabel && label) transferProgressLabel.textContent = label;
    if (transferProgressNote && note) transferProgressNote.textContent = note;
    if (transferMiniFill) transferMiniFill.style.width = `${normalized}%`;
    if (transferMiniLabel && label) transferMiniLabel.textContent = label;
    if (transferMiniNote && note) transferMiniNote.textContent = note;
  };

  transferTrigger?.addEventListener("click", () => {
    transferPending = false;
    if (transferMini) transferMini.hidden = true;
    setTransferMode("confirm");
    setTransferProgress(0, "Preparing transfer", "Starting transfer.");
    if (typeof transferModal?.showModal === "function") {
      transferModal.showModal();
    }
  });

  transferCancel?.addEventListener("click", () => {
    transferPending = false;
    transferModal?.close("cancel");
  });

  transferNext?.addEventListener("click", () => setTransferMode("target"));
  transferBack?.addEventListener("click", () => setTransferMode("confirm"));

  transferTargetToggle?.addEventListener("click", () => {
    const isOpen = transferTargetToggle.getAttribute("aria-expanded") === "true";
    setTransferTargetOpen(!isOpen);
  });

  transferTargetToggle?.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    setTransferTargetOpen(true);
    const offset = event.key === "ArrowDown" ? 1 : -1;
    focusTransferTargetOption(selectedTransferTargetIndex() + offset);
  });

  transferTargetList?.addEventListener("keydown", (event) => {
    const activeIndex = transferTargetOptions.findIndex((option) => option === document.activeElement);
    if (event.key === "Escape") {
      event.preventDefault();
      setTransferTargetOpen(false);
      transferTargetToggle?.focus();
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const fallback = selectedTransferTargetIndex();
      const offset = event.key === "ArrowDown" ? 1 : -1;
      focusTransferTargetOption((activeIndex >= 0 ? activeIndex : fallback) + offset);
      return;
    }
    if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      focusTransferTargetOption(event.key === "Home" ? 0 : transferTargetOptions.length - 1);
    }
  });

  transferTargetOptions.forEach((option) => {
    option.addEventListener("click", () => selectTransferTarget(option));
  });

  document.addEventListener("click", (event) => {
    if (!transferTargetPicker || !transferTargetList || transferTargetList.hidden) return;
    if (event.target instanceof Node && !transferTargetPicker.contains(event.target)) {
      setTransferTargetOpen(false);
    }
  });

  transferForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (transferPending) return;
    transferPending = true;
    const firstTransferStep = markFirstOperationStep(
      "transferOutput",
      "Confirm transfer",
      "Reading transfer request.",
    );
    setTransferMode("progress");
    setTransferProgress(firstTransferStep.progress, firstTransferStep.label, firstTransferStep.note);
    const requestTransfer = async () => fetch(transferForm.action, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Accept": "application/json",
        "X-Requested-With": "fetch",
      },
      body: new FormData(transferForm),
    });
    try {
      let response = await requestTransfer();
      if (response.status === 403 && await refreshCsrfTokens()) {
        response = await requestTransfer();
      }
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.error || payload.detail || "Transfer failed.");
      }
      if (!payload.operation_id) throw new Error("operation id missing");
      await watchOperation(payload.operation_id, {
        onProgress: (operation) => {
          const progress = operationProgress(operation, firstTransferStep.progress, "transferOutput");
          setTransferProgress(
            progress.progress,
            progress.phase || "Transfer output",
            progress.substate || progress.message || "Working.",
          );
        },
        onSuccess: (operation) => {
          const progress = operationProgress(operation, 100, "transferOutput");
          transferPending = false;
          setTransferProgress(100, "Transfer complete", progress.flashSuccess || progress.message || "Output transferred.");
          window.setTimeout(() => window.location.reload(), 900);
        },
        onFailure: (operation) => {
          const progress = operationProgress(operation, 100, "transferOutput");
          transferPending = false;
          setTransferProgress(100, "Transfer failed", progress.flashError || progress.message || "Transfer failed.");
        },
        onError: (error) => {
          transferPending = false;
          setTransferProgress(100, "Transfer failed", error instanceof Error ? error.message : "Transfer failed.");
        },
      });
    } catch (error) {
      transferPending = false;
      setTransferProgress(100, "Transfer failed", error instanceof Error ? error.message : "Transfer failed.");
    }
  });

  transferClose?.addEventListener("click", () => {
    if (transferPending) {
      if (transferMini) transferMini.hidden = false;
      transferModal?.close("minimize");
      return;
    }
    transferModal?.close("close");
  });

  transferReopen?.addEventListener("click", () => {
    if (transferMini) transferMini.hidden = true;
    if (typeof transferModal?.showModal === "function") {
      transferModal.showModal();
    }
  });

  const resumeTransferOperation = async (operation) => {
    if (!transferModal || transferPending) return;
    transferPending = true;
    const firstTransferStep = markFirstOperationStep(
      "transferOutput",
      "Confirm transfer",
      "Reading transfer request.",
    );
    setTransferMode("progress");
    setTransferProgress(firstTransferStep.progress, firstTransferStep.label, firstTransferStep.note);
    openProgressDialog(transferModal);
    try {
      await watchOperation(operation.id, {
        onProgress: (payload) => {
          const progress = operationProgress(payload, firstTransferStep.progress, "transferOutput");
          setTransferProgress(
            progress.progress,
            progress.phase || "Transfer output",
            progress.substate || progress.message || "Working.",
          );
        },
        onSuccess: (payload) => {
          const progress = operationProgress(payload, 100, "transferOutput");
          transferPending = false;
          setTransferProgress(100, "Transfer complete", progress.flashSuccess || progress.message || "Output transferred.");
          window.setTimeout(() => window.location.reload(), 900);
        },
        onFailure: (payload) => {
          const progress = operationProgress(payload, 100, "transferOutput");
          transferPending = false;
          setTransferProgress(100, "Transfer failed", progress.flashError || progress.message || "Transfer failed.");
        },
        onError: (error) => {
          transferPending = false;
          setTransferProgress(100, "Transfer failed", error instanceof Error ? error.message : "Transfer failed.");
        },
      });
    } catch (error) {
      transferPending = false;
      setTransferProgress(100, "Transfer failed", error instanceof Error ? error.message : "Transfer failed.");
    }
  };

  const appendProgressNarrative = (target, message) => {
    if (!target || !message) return;
    const normalized = String(message).trim();
    if (!normalized) return;
    const lastLine = target.lastElementChild;
    if (lastLine && lastLine.textContent.trim() === normalized) return;
    const line = document.createElement("div");
    line.className = "clone-progress-line";
    line.textContent = normalized;
    target.appendChild(line);
    target.scrollTop = target.scrollHeight;
  };

  const appendCloneNarrative = (message) => {
    appendProgressNarrative(cloneProgressLog, message);
  };

  const renderFlashBanner = (kind, message) => {
    if (!flashBlock) return;
    const normalizedKind = kind === "error" ? "error" : "success";
    const text = String(message || "").trim();
    flashBlock.innerHTML = text ? `<section class="banner ${normalizedKind}">${escapeHtml(text)}</section>` : "";
  };

  const hasLocalOutputSaveProgress = (form) => (
    Boolean(form?.querySelector("[data-output-save-inline-progress]"))
  );

  const clearOutputSaveProgressBanner = () => {
    if (!flashBlock) return;
    if (flashBlock.querySelector(".output-save-progress-banner")) {
      flashBlock.innerHTML = "";
    }
  };


  const renderOutputSaveProgress = ({ kind = "success", title = "Saving output", step = "", note = "", progress = 8 }) => {
    if (!flashBlock) return;
    const normalizedKind = kind === "error" ? "error" : kind === "success" ? "success" : "pending";
    const normalizedProgress = Math.max(0, Math.min(100, Number(progress) || 0));
    const headline = String(title || "Saving output").trim();
    const substep = String(step || headline).trim();
    const detail = String(note || "").trim();
    flashBlock.innerHTML = `
      <section class="banner ${normalizedKind} output-save-progress-banner">
        <div class="output-save-progress-copy">
          <strong>${escapeHtml(headline)}</strong>
          <span>${escapeHtml(substep)}${detail ? ` - ${escapeHtml(detail)}` : ""}</span>
        </div>
        <span class="summary-pill pending">${Math.round(normalizedProgress)}%</span>
        <div
          class="clone-progress-track output-save-progress-track"
          role="progressbar"
          aria-label="${escapeHtml(headline)} progress"
          aria-valuemin="0"
          aria-valuemax="100"
          aria-valuenow="${Math.round(normalizedProgress)}"
        >
          <span class="clone-progress-fill" style="width: ${normalizedProgress}%"></span>
        </div>
      </section>
    `;
  };

  const renderOutputSaveProgressForForm = (form, options) => {
    if (hasLocalOutputSaveProgress(form)) {
      clearOutputSaveProgressBanner();
      return;
    }
    renderOutputSaveProgress(options);
  };

  const startOutputSaveProgress = (title, form = null) => {
    const step = outputSaveProgressSteps[0] || {
      progress: 0,
      substep: "Reading output form",
      note: "Submitting save request.",
    };
    const firstProgress = Math.max(0, Math.min(96, Number(step.progress) || 0));
    operationProgressTrackers.set("outputSave", firstProgress);
    renderOutputSaveProgressForForm(form, {
      title,
      step: step.substep,
      note: step.note,
      progress: firstProgress,
    });
    renderInlineOutputSaveProgress(form, {
      kind: "running",
      title,
      step: step.substep,
      progress: firstProgress,
    });
    return firstProgress;
  };

  const renderInlineOutputSaveProgress = (form, { kind = "running", title = "Saving output", step = "", progress = 8 }) => {
    const shell = form?.querySelector("[data-output-save-inline-progress]");
    if (!shell) return;
    const normalizedProgress = Math.max(0, Math.min(100, Number(progress) || 0));
    const boundedProgress = kind === "running" ? Math.max(4, Math.min(96, normalizedProgress)) : normalizedProgress;
    const label = shell.querySelector("[data-output-save-inline-label]");
    const value = shell.querySelector("[data-output-save-inline-value]");
    const track = shell.querySelector("[data-output-save-inline-track]");
    const fill = shell.querySelector("[data-output-save-inline-fill]");
    shell.hidden = false;
    shell.dataset.state = kind;
    if (label) label.textContent = String(step || title || "Saving").trim();
    if (value) value.textContent = `${Math.round(boundedProgress)}%`;
    if (track) track.setAttribute("aria-valuenow", String(Math.round(boundedProgress)));
    if (fill) fill.style.width = `${boundedProgress}%`;
  };

  const stopOutputSaveProgress = () => {};

  const showShieldCodeError = (message) => {
    renderFlashBanner("error", message);
    const copy = shieldCodeErrorModal?.querySelector("[data-shield-error-copy]");
    if (copy) copy.textContent = message;
    if (shieldCodeErrorModal instanceof HTMLDialogElement && typeof shieldCodeErrorModal.showModal === "function") {
      shieldCodeErrorModal.showModal();
    }
  };

  document.querySelectorAll("[data-shield-error-close]").forEach((button) => {
    button.addEventListener("click", () => {
      if (shieldCodeErrorModal instanceof HTMLDialogElement && shieldCodeErrorModal.open) {
        shieldCodeErrorModal.close();
      }
    });
  });

  const formActionPath = (form) => {
    const rawAction = form?.getAttribute("action") || form?.action || window.location.pathname;
    const url = new URL(rawAction, window.location.origin);
    return `${url.pathname}${url.search}`;
  };

  const readResponsePayload = async (response) => {
    const text = await response.text().catch(() => "");
    if (!text) return { payload: {}, text: "" };
    try {
      return { payload: JSON.parse(text), text };
    } catch {
      return { payload: {}, text };
    }
  };

  const responseErrorMessage = (response, payload, text, fallback, request = {}) => {
    const detail = payload?.flash_error || payload?.error || payload?.detail?.message || payload?.detail || "";
    const requestId = response.headers.get("x-request-id") || payload?.request_id || "";
    const method = request.method ? `${request.method} ` : "";
    const path = request.path || "";
    const target = path ? `${method}${path}` : "";
    const status = `${response.status}${response.statusText ? ` ${response.statusText}` : ""}`;
    const body = detail || String(text || "").replace(/\s+/g, " ").trim().slice(0, 180);
    const suffix = requestId ? ` (${requestId})` : "";
    return `${fallback}: ${target ? `${target} -> ` : ""}${status}${suffix}${body ? ` - ${body}` : ""}`;
  };


  const refreshCsrfTokens = async () => {
    const response = await fetch("/api/csrf", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    const { payload } = await readResponsePayload(response);
    const token = String(payload.csrf_token || "").trim();
    if (!response.ok || !token) return "";
    document.querySelectorAll("input[name='csrf_token']").forEach((input) => {
      input.value = token;
    });
    csrfToken = token;
    return token;
  };

  const setBackupProgress = (percent, label, note) => {
    const normalized = Math.max(0, Math.min(100, Number(percent) || 0));
    if (backupProgressFill) backupProgressFill.style.width = `${normalized}%`;
  };

  const shouldAppendBackupNarrative = (message) => {
    const text = String(message || "").trim();
    if (!text) return false;
    return !/^Exporting path .+: .* of .*\\.$/.test(text);
  };

  const appendBackupNarrative = (message) => {
    if (!shouldAppendBackupNarrative(message)) return;
    appendProgressNarrative(backupProgressLog, message);
  };

  const openDeleteBackupModal = (form) => {
    activeDeleteBackupForm = form;
    const backupLabel = String(form.dataset.backupLabel || `#${form.dataset.backupId || ""}`).trim() || "this backup";
    const backupFile = String(form.dataset.backupFile || "").trim();
    if (deleteBackupCopy) {
      deleteBackupCopy.textContent = backupFile && backupFile !== "-"
        ? `This deletes backup ${backupLabel} and removes ${backupFile}.`
        : `This deletes backup ${backupLabel} and removes it from backup history.`;
    }
    if (typeof deleteBackupModal?.showModal === "function") {
      deleteBackupModal.showModal();
    }
  };

  deleteBackupCancel?.addEventListener("click", () => {
    activeDeleteBackupForm = null;
    deleteBackupModal?.close("cancel");
  });

  deleteBackupModal?.addEventListener("cancel", () => {
    activeDeleteBackupForm = null;
  });

  deleteBackupAccept?.addEventListener("click", async () => {
    if (!activeDeleteBackupForm || backupPending) return;
    const form = activeDeleteBackupForm;
    const submitter = form.querySelector("[data-delete-backup-trigger]");
    activeDeleteBackupForm = null;
    deleteBackupModal?.close("delete");
    try {
      await startBackupLikeOperation({
        form,
        submitter,
        label: "Deleting backup",
        startedMessage: "Starting backup delete.",
        failedMessage: "Backup delete failed.",
        pipeline: "deleteBackup",
      });
    } catch (error) {
      renderFlashBanner("error", error instanceof Error ? error.message : "Backup delete failed.");
    }
  });

  const catalogProgressFor = (pipelineName, phase, message, fallback) => {
    const normalizedPhase = String(phase || "").replaceAll(".", " ").trim().toLowerCase();
    const normalizedMessage = String(message || "").replaceAll(".", " ").trim().toLowerCase();
    const steps = operationProgressPipelines[pipelineName] || [];
    const messageMatched = steps.reduce((highest, step) => {
      const candidates = [
        step.substep,
        step.note,
      ].map((value) => String(value || "").replaceAll(".", " ").trim().toLowerCase());
      return normalizedMessage && candidates.includes(normalizedMessage)
        ? Math.max(highest, Number(step.progress) || 0)
        : highest;
    }, 0);
    if (messageMatched > 0) return Math.max(fallback, messageMatched);
    return steps.reduce((highest, step) => {
      const headline = String(step.headline || "").replaceAll(".", " ").trim().toLowerCase();
      return normalizedPhase && headline === normalizedPhase
        ? Math.max(highest, Number(step.progress) || 0)
        : highest;
    }, fallback);
  };

  const operationProgress = (payload, fallback, pipelineName = "default") => {
    const details = payload?.details || {};
    const progress = Number(details.progress);
    const phase = String(payload?.phase || "");
    const substate = String(details.substate || "");
    const message = String(details.message || payload?.phase || payload?.status || "");
    const rawProgress = Number.isFinite(progress)
      ? progress
      : catalogProgressFor(pipelineName, phase, substate || message, fallback);
    const boundedProgress = Math.max(0, Math.min(100, rawProgress));
    const terminal = !["queued", "running"].includes(String(payload?.status || "").toLowerCase());
    const previous = operationProgressTrackers.get(pipelineName) || 0;
    const nextProgress = terminal ? Math.max(boundedProgress, previous) : Math.max(previous, boundedProgress);
    operationProgressTrackers.set(pipelineName, nextProgress);
    return {
      progress: nextProgress,
      phase,
      substate,
      message,
      flashSuccess: details.flash_success || payload?.flash_success || "",
      flashError: details.flash_error || payload?.flash_error || payload?.error || "",
      details,
    };
  };

  const watchOperation = (operationId, handlers = {}) => (
    window.CNCOperations.watchOperation(operationId, handlers)
  );

  const formSubmitMethod = (form) => String(form?.getAttribute("method") || "POST").toUpperCase();

  const applyOutputEnabledState = (enabled, form, submitButton) => {
    outputEnabled = Boolean(enabled);
    document.body.dataset.outputEnabled = outputEnabled ? "true" : "false";
    const actionInput = form.querySelector("input[name='action']");
    if (actionInput) actionInput.value = enabled ? "disable" : "enable";
    if (outputEnabledChip) {
      outputEnabledChip.textContent = enabled ? "enabled" : "disabled";
      outputEnabledChip.classList.toggle("chip-on", enabled);
      outputEnabledChip.classList.toggle("chip-off", !enabled);
    }
    if (submitButton) {
      submitButton.textContent = enabled ? "Disable output" : "Enable output";
      form.dataset.outputSaveRunning = enabled ? "Disabling output" : "Enabling output";
      form.dataset.outputSaveFallback = enabled ? "Output disabled." : "Output enabled.";
      form.dataset.outputSaveErrorFallback = enabled ? "Disable failed." : "Enable failed.";
    }
    if (outputKind === "app" && outputEnabled) {
      refreshRuntimeSignals({ force: true }).catch(() => {});
      scheduleRuntimeRefresh();
    } else if (outputKind === "app") {
      if (runtimeRefreshTimer !== null) window.clearTimeout(runtimeRefreshTimer);
      runtimeRefreshTimer = null;
      runtimeRefreshAbortController?.abort();
      renderDisabledRuntimeState();
    }
  };

  const renderOutputSaveOperationProgress = (form, title, operation, fallbackProgress = 8) => {
    const progress = operationProgress(operation, fallbackProgress, "outputSave");
    const step = progress.substate || progress.message || title;
    renderOutputSaveProgressForForm(form, {
      kind: "running",
      title,
      step,
      note: progress.phase,
      progress: progress.progress,
    });
    renderInlineOutputSaveProgress(form, {
      kind: "running",
      title,
      step,
      progress: progress.progress,
    });
    return progress;
  };

  const renderOutputSaveOperationResult = (form, title, operation, submitButton, fallbackMessage) => {
    const progress = operationProgress(operation, 100, "outputSave");
    const failed = String(operation?.status || "").toLowerCase() !== "success";
    const message = failed
      ? (progress.flashError || progress.message || fallbackMessage || "Save failed.")
      : (progress.flashSuccess || progress.message || fallbackMessage || "Saved.");
    renderOutputSaveProgressForForm(form, {
      kind: failed ? "error" : "success",
      title: failed ? "Save failed" : title,
      step: message,
      note: failed ? "Review the error before retrying." : "Done.",
      progress: 100,
    });
    renderInlineOutputSaveProgress(form, {
      kind: failed ? "error" : "success",
      title: failed ? "Save failed" : title,
      step: message,
      progress: 100,
    });
    if (Object.prototype.hasOwnProperty.call(progress.details || {}, "enabled")) {
      applyOutputEnabledState(Boolean(progress.details.enabled), form, submitButton);
    }
    if (!failed && String(operation?.kind || "") === "ui.backend.interface") {
      applyInboundInterfaceOperationResult(operation);
    }
    return progress;
  };

  const submitOutputSaveForm = async (form, submitter) => {
    if (form.dataset.submitting === "1") return;
    form.dataset.submitting = "1";
    const submitButton = submitter || form.querySelector("button[type='submit']");
    const originalLabel = submitButton ? submitButton.textContent : "";
    const submitControls = Array.from(form.querySelectorAll("button[type='submit'], input[type='submit']"));
    const originallyDisabled = new WeakMap();
    const runningLabel = form.dataset.outputSaveRunning || "Saving";
    submitControls.forEach((control) => {
      originallyDisabled.set(control, control.disabled);
      control.disabled = true;
    });
    if (submitButton) {
      submitButton.textContent = "Saving...";
    }
    const firstOutputSaveProgress = startOutputSaveProgress(runningLabel, form);
    try {
      let formData = new FormData(form);
      if (submitter?.name) formData.set(submitter.name, submitter.value);
      const requestMethod = formSubmitMethod(form);
      const requestPath = formActionPath(form);
      let response = await fetch(requestPath, {
        method: requestMethod,
        credentials: "same-origin",
        body: formData,
        headers: {
          Accept: "application/json",
          "X-Requested-With": "fetch",
        },
      });
      if (response.status === 403 && await refreshCsrfTokens()) {
        formData = new FormData(form);
        if (submitter?.name) formData.set(submitter.name, submitter.value);
        response = await fetch(requestPath, {
          method: requestMethod,
          credentials: "same-origin",
          body: formData,
          headers: {
            Accept: "application/json",
            "X-Requested-With": "fetch",
          },
        });
      }
      const { payload, text } = await readResponsePayload(response);
      if (!response.ok) {
        const failureMessage = responseErrorMessage(response, payload, text, "Save failed", {
          method: requestMethod,
          path: requestPath,
        });
        if (!hasLocalOutputSaveProgress(form)) {
          renderFlashBanner("error", failureMessage);
        }
        throw new Error(failureMessage);
      }
      if (payload.operation_id) {
        await watchOperation(payload.operation_id, {
          onProgress: (operation) => {
            renderOutputSaveOperationProgress(form, runningLabel, operation, firstOutputSaveProgress);
          },
          onSuccess: (operation) => {
            renderOutputSaveOperationResult(
              form,
              runningLabel,
              operation,
              submitButton,
              form.dataset.outputSaveFallback || "Saved.",
            );
            markOutputAttachFormClean(form);
            refreshRuntimeSignals().catch(() => {});
          },
          onFailure: (operation) => {
            renderOutputSaveOperationResult(
              form,
              runningLabel,
              operation,
              submitButton,
              form.dataset.outputSaveErrorFallback || "Save failed.",
            );
          },
          onError: (error) => {
            const message = error instanceof Error ? error.message : "Save failed.";
            renderOutputSaveProgressForForm(form, {
              kind: "error",
              title: "Save failed",
              step: message,
              note: "Review the error before retrying.",
              progress: 100,
            });
            renderInlineOutputSaveProgress(form, {
              kind: "error",
              title: "Save failed",
              step: message,
              progress: 100,
            });
          },
        });
      } else {
        const message = payload.flash_error || payload.flash_success || form.dataset.outputSaveFallback || "Saved.";
        renderOutputSaveProgressForForm(form, {
          kind: payload.flash_error ? "error" : "success",
          title: payload.flash_error ? "Save failed" : runningLabel,
          step: message,
          note: payload.flash_error ? "Review the error before retrying." : "Done.",
          progress: 100,
        });
        renderInlineOutputSaveProgress(form, {
          kind: payload.flash_error ? "error" : "success",
          title: payload.flash_error ? "Save failed" : runningLabel,
          step: message,
          progress: 100,
        });
        if (Object.prototype.hasOwnProperty.call(payload, "enabled")) {
          applyOutputEnabledState(Boolean(payload.enabled), form, submitButton);
        }
        if (!payload.flash_error) {
          markOutputAttachFormClean(form);
        }
        refreshRuntimeSignals().catch(() => {});
      }
    } catch (error) {
      stopOutputSaveProgress();
      renderInlineOutputSaveProgress(form, {
        kind: "error",
        title: "Save failed",
        step: error instanceof Error ? error.message : "Save failed.",
        progress: 100,
      });
      if (!hasLocalOutputSaveProgress(form) && !String(error?.message || "").startsWith("Save failed")) {
        renderFlashBanner("error", error instanceof Error ? error.message : "Save failed.");
      }
    } finally {
      submitControls.forEach((control) => {
        control.disabled = Boolean(originallyDisabled.get(control));
      });
      if (submitButton) {
        if (submitButton.textContent === "Saving...") submitButton.textContent = originalLabel;
      }
      delete form.dataset.submitting;
    }
  };

  document.querySelectorAll("[data-output-save-form]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const shieldToggle = form.elements.namedItem("shield_enabled");
      const shieldCode = form.elements.namedItem("shield_access_code");
      const hasStoredShieldCode = form.dataset.shieldCodeConfigured === "true";
      if (shieldToggle?.checked && !hasStoredShieldCode && !String(shieldCode?.value || "").trim()) {
        const message = "please enter an access code to enable shield";
        showShieldCodeError(message);
        shieldCode?.focus();
        return;
      }
      submitOutputSaveForm(form, event.submitter || undefined).catch((error) => {
        if (!hasLocalOutputSaveProgress(form)) {
          renderFlashBanner("error", error instanceof Error ? error.message : "Save failed.");
        }
      });
    });
  });

  const outputSaveFormForOperation = (operation) => {
    const kind = String(operation?.kind || "");
    if (kind === "ui.backend.update") {
      return document.getElementById("output-settings-form");
    }
    if (kind === "ui.backend.inputs") {
      return document.querySelector('[data-output-save-form][action$="/inputs"]');
    }
    if (kind === "ui.backend.interface") {
      return document.querySelector("[data-placement-form]");
    }
    if (kind === "ui.backend.state") {
      return document.querySelector('[data-output-save-form][action$="/state"]');
    }
    return null;
  };

  const resumeOutputSaveOperation = async (operation) => {
    const form = outputSaveFormForOperation(operation);
    if (!form || form.dataset.submitting === "1") return;
    form.dataset.submitting = "1";
    const submitButton = form.querySelector("button[type='submit']");
    const controls = Array.from(form.querySelectorAll("button[type='submit'], input[type='submit']"));
    controls.forEach((control) => {
      control.disabled = true;
    });
    const runningLabel = form.dataset.outputSaveRunning || "Saving";
    const firstOutputSaveProgress = startOutputSaveProgress(runningLabel, form);
    try {
      await watchOperation(operation.id, {
        onProgress: (payload) => {
          renderOutputSaveOperationProgress(form, runningLabel, payload, firstOutputSaveProgress);
        },
        onSuccess: (payload) => {
          renderOutputSaveOperationResult(
            form,
            runningLabel,
            payload,
            submitButton,
            form.dataset.outputSaveFallback || "Saved.",
          );
          markOutputAttachFormClean(form);
          refreshRuntimeSignals().catch(() => {});
        },
        onFailure: (payload) => {
          renderOutputSaveOperationResult(
            form,
            runningLabel,
            payload,
            submitButton,
            form.dataset.outputSaveErrorFallback || "Save failed.",
          );
        },
        onError: (error) => {
          const message = error instanceof Error ? error.message : "Save failed.";
          renderOutputSaveProgressForForm(form, {
            kind: "error",
            title: "Save failed",
            step: message,
            note: "Review the error before retrying.",
            progress: 100,
          });
          renderInlineOutputSaveProgress(form, {
            kind: "error",
            title: "Save failed",
            step: message,
            progress: 100,
          });
        },
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : "Save failed.";
      renderInlineOutputSaveProgress(form, {
        kind: "error",
        title: "Save failed",
        step: message,
        progress: 100,
      });
      if (!hasLocalOutputSaveProgress(form)) renderFlashBanner("error", message);
    } finally {
      controls.forEach((control) => {
        control.disabled = false;
      });
      delete form.dataset.submitting;
    }
  };

  const startBackupLikeOperation = async ({
    form,
    submitter,
    label,
    startedMessage,
    failedMessage,
    onSuccess,
    showSuccessBanner = true,
    pipeline = "backup",
  }) => {
    if (backupPending) return;
    backupPending = true;
    const originalLabel = submitter ? submitter.textContent : "";
    const firstBackupStep = markFirstOperationStep(pipeline, label, startedMessage);
    if (submitter) {
      submitter.disabled = true;
      submitter.textContent = label;
    }
    if (backupProgressLog) backupProgressLog.innerHTML = "";
    if (backupProgressShell) backupProgressShell.hidden = false;
    setBackupProgress(firstBackupStep.progress, label, firstBackupStep.note);
    appendBackupNarrative(firstBackupStep.note);
    renderFlashBanner("success", "");
    let formData = new FormData(form);
    try {
      const requestMethod = formSubmitMethod(form);
      const requestPath = formActionPath(form);
      let response = await fetch(requestPath, {
        method: requestMethod,
        credentials: "same-origin",
        body: formData,
        headers: {
          Accept: "application/json",
          "X-Requested-With": "fetch",
        },
      });
      if (response.status === 403 && await refreshCsrfTokens()) {
        formData = new FormData(form);
        response = await fetch(requestPath, {
          method: requestMethod,
          credentials: "same-origin",
          body: formData,
          headers: {
            Accept: "application/json",
            "X-Requested-With": "fetch",
          },
        });
      }
      const { payload, text } = await readResponsePayload(response);
      if (!response.ok) {
        throw new Error(responseErrorMessage(response, payload, text, "Request failed", {
          method: requestMethod,
          path: requestPath,
        }));
      }
      const operationId = payload.operation_id;
      if (!operationId) throw new Error("operation id missing");
      await watchOperation(operationId, {
        onProgress: (operation) => {
          const progress = operationProgress(operation, firstBackupStep.progress, pipeline);
          setBackupProgress(progress.progress, label, progress.message || startedMessage);
          appendBackupNarrative(progress.message || String(operation.phase || "Working."));
        },
        onSuccess: async (operation) => {
          const progress = operationProgress(operation, 100, pipeline);
          setBackupProgress(100, `${label} complete`, progress.message || "Done.");
          appendBackupNarrative(progress.message || "Done.");
          if (showSuccessBanner) {
            renderFlashBanner("success", progress.flashSuccess || progress.message || "Done.");
          }
          await refreshBackupSignals({ loadingMessage: "Refreshing backup history..." }).catch(() => {});
          refreshRuntimeSignals().catch(() => {});
          onSuccess?.(operation);
        },
        onFailure: (operation) => {
          const progress = operationProgress(operation, 100, pipeline);
          setBackupProgress(100, `${label} failed`, progress.message || failedMessage);
          appendBackupNarrative(progress.flashError || progress.message || failedMessage);
          renderFlashBanner("error", progress.flashError || failedMessage);
        },
        onError: (error) => {
          setBackupProgress(100, `${label} failed`, failedMessage);
          renderFlashBanner("error", error instanceof Error ? error.message : failedMessage);
        },
      });
    } catch (error) {
      setBackupProgress(100, `${label} failed`, failedMessage);
      renderFlashBanner("error", error instanceof Error ? error.message : failedMessage);
    } finally {
      if (submitter) {
        submitter.disabled = false;
        submitter.textContent = originalLabel || submitter.textContent;
      }
      backupPending = false;
    }
  };

  const backupResumeConfig = (operation) => {
    const kind = String(operation?.kind || "");
    if (kind === "backup_backend") {
      return {
        label: "Creating backup",
        startedMessage: "Resuming backup progress.",
        failedMessage: "Backup failed.",
        pipeline: "backup",
      };
    }
    if (kind === "restore_backend") {
      return {
        label: "Restoring backup",
        startedMessage: "Resuming restore progress.",
        failedMessage: "Restore failed.",
        pipeline: "restore",
        showSuccessBanner: false,
      };
    }
    if (kind === "import_backend_backup") {
      return {
        label: "Importing backup",
        startedMessage: "Resuming import progress.",
        failedMessage: "Import failed.",
        pipeline: "importBackup",
      };
    }
    return null;
  };

  const resumeBackupOperation = async (operation, config) => {
    if (backupPending) return;
    backupPending = true;
    const firstBackupStep = markFirstOperationStep(
      config.pipeline,
      config.label,
      config.startedMessage,
    );
    if (backupSubmit) backupSubmit.disabled = true;
    if (backupProgressLog) backupProgressLog.innerHTML = "";
    if (backupProgressShell) backupProgressShell.hidden = false;
    setBackupProgress(firstBackupStep.progress, config.label, firstBackupStep.note);
    appendBackupNarrative(firstBackupStep.note);
    try {
      await watchOperation(operation.id, {
        onProgress: (payload) => {
          const progress = operationProgress(payload, firstBackupStep.progress, config.pipeline);
          setBackupProgress(progress.progress, config.label, progress.message || config.startedMessage);
          appendBackupNarrative(progress.message || String(payload.phase || "Working."));
        },
        onSuccess: async (payload) => {
          const progress = operationProgress(payload, 100, config.pipeline);
          setBackupProgress(100, `${config.label} complete`, progress.message || "Done.");
          appendBackupNarrative(progress.message || "Done.");
          if (config.showSuccessBanner !== false) {
            renderFlashBanner("success", progress.flashSuccess || progress.message || "Done.");
          }
          await refreshBackupSignals({ loadingMessage: "Refreshing backup history..." }).catch(() => {});
          refreshRuntimeSignals().catch(() => {});
        },
        onFailure: (payload) => {
          const progress = operationProgress(payload, 100, config.pipeline);
          setBackupProgress(100, `${config.label} failed`, progress.message || config.failedMessage);
          appendBackupNarrative(progress.flashError || progress.message || config.failedMessage);
          renderFlashBanner("error", progress.flashError || config.failedMessage);
        },
        onError: (error) => {
          setBackupProgress(100, `${config.label} failed`, config.failedMessage);
          renderFlashBanner("error", error instanceof Error ? error.message : config.failedMessage);
        },
      });
    } catch (error) {
      setBackupProgress(100, `${config.label} failed`, config.failedMessage);
      renderFlashBanner("error", error instanceof Error ? error.message : config.failedMessage);
    } finally {
      if (backupSubmit) backupSubmit.disabled = false;
      backupPending = false;
    }
  };

  const resumeOutputOperations = async () => {
    if (!window.CNCOperations?.activeOperations) return;
    let operations = [];
    try {
      operations = await window.CNCOperations.activeOperations({
        surface: "output_detail",
        backend_id: backendId,
      });
    } catch (error) {
      console.warn("active output operation lookup failed", error);
      return;
    }
    operations.forEach((operation) => {
      const kind = String(operation?.kind || "");
      if (outputSaveFormForOperation(operation)) {
        void resumeOutputSaveOperation(operation);
        return;
      }
      if (kind === "delete_backend") {
        resumeDeleteOperation(operation);
        return;
      }
      if (kind === "clone_backend") {
        resumeCloneOperation(operation);
        return;
      }
      if (kind === "transfer_backend") {
        void resumeTransferOperation(operation);
        return;
      }
      if (kind === "setup_backend_replica") {
        resumeReplicaSetupOperation(operation);
        return;
      }
      const config = backupResumeConfig(operation);
      if (!config) return;
      void resumeBackupOperation(operation, config);
    });
  };

  const resetCloneModal = () => {
    clonePending = false;
    if (cloneProgressLog) cloneProgressLog.innerHTML = "";
    if (cloneSuccessNote) cloneSuccessNote.textContent = "";
    if (cloneErrorText) cloneErrorText.textContent = "";
    if (cloneOpenLink) cloneOpenLink.setAttribute("href", "#");
    if (cloneSubmit) cloneSubmit.disabled = false;
    setCloneMode("form");
    setCloneProgress(0, "Preparing clone", "Starting clone.");
  };

  if (cloneModal && cloneTrigger && cloneForm) {
    cloneTrigger.addEventListener("click", () => {
      resetCloneModal();
      if (typeof cloneModal.showModal === "function") cloneModal.showModal();
    });
    cloneCancelButtons.forEach((button) => {
      button.addEventListener("click", () => {
        if (clonePending) return;
        cloneModal.close("cancel");
      });
    });
    cloneForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (clonePending) return;
      const name = cloneNameInput?.value.trim() || "";
      const port = clonePortInput?.value.trim() || "";
      if (!name || !port) return;
      clonePending = true;
      const firstCloneStep = markFirstOperationStep(
        "clone",
        "Clone output",
        "Preparing clone.",
      );
      if (cloneSubmit) cloneSubmit.disabled = true;
      setCloneMode("progress");
      if (cloneProgressLog) cloneProgressLog.innerHTML = "";
      setCloneProgress(firstCloneStep.progress, firstCloneStep.label, firstCloneStep.note);
      appendCloneNarrative(firstCloneStep.note);
      try {
        let formData = new FormData();
        formData.set("csrf_token", csrfToken);
        formData.set("name", name);
        formData.set("port", port);
        let response = await fetch(`/ui/backends/${backendId}/clone`, {
          method: "POST",
          headers: { Accept: "application/json" },
          body: formData,
        });
        if (response.status === 403) {
          const token = await refreshCsrfTokens();
          if (token) {
            formData = new FormData();
            formData.set("csrf_token", token);
            formData.set("name", name);
            formData.set("port", port);
            response = await fetch(`/ui/backends/${backendId}/clone`, {
              method: "POST",
              headers: { Accept: "application/json" },
              body: formData,
            });
          }
        }
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(String(payload.error || `clone request failed: ${response.status}`));
        if (!payload.operation_id) throw new Error("operation id missing");
        await watchOperation(payload.operation_id, {
          onProgress: (operation) => {
            const progress = operationProgress(operation, firstCloneStep.progress, "clone");
            setCloneProgress(
              progress.progress,
              progress.phase || "Cloning output",
              progress.substate || progress.message || "Working.",
            );
            appendCloneNarrative(progress.message || String(operation.phase || "Working."));
          },
          onSuccess: (operation) => {
            const progress = operationProgress(operation, 100, "clone");
            const details = progress.details || {};
            setCloneProgress(100, "Clone ready", progress.message || "The new output is ready.");
            appendCloneNarrative(progress.message || "Clone ready.");
            appendCloneNarrative(`Copied ${details.copied_paths ?? 0} mounted path(s) and left inputs detached.`);
            if (cloneSuccessNote) cloneSuccessNote.textContent = String(progress.flashSuccess || progress.message || "");
            if (cloneOpenLink) cloneOpenLink.setAttribute("href", String(details.redirect_url || "#"));
            setCloneMode("success");
          },
          onFailure: (operation) => {
            const progress = operationProgress(operation, 100, "clone");
            setCloneProgress(100, "Clone failed", progress.message || "The clone could not be completed.");
            if (cloneErrorText) cloneErrorText.textContent = String(progress.flashError || progress.message || "Clone failed.");
            setCloneMode("error");
          },
          onError: (error) => {
            setCloneProgress(100, "Clone failed", "The clone could not be completed.");
            if (cloneErrorText) cloneErrorText.textContent = error instanceof Error ? error.message : "Clone failed.";
            setCloneMode("error");
          },
        });
      } catch (error) {
        setCloneProgress(100, "Clone failed", "The clone could not be completed.");
        if (cloneErrorText) cloneErrorText.textContent = error instanceof Error ? error.message : "Clone failed.";
        setCloneMode("error");
      } finally {
        clonePending = false;
        if (cloneSubmit) cloneSubmit.disabled = false;
      }
    });
  }

  const resumeCloneOperation = (operation) => {
    if (!cloneModal || clonePending) return;
    clonePending = true;
    const firstCloneStep = markFirstOperationStep(
      "clone",
      "Clone output",
      "Preparing clone.",
    );
    if (cloneSubmit) cloneSubmit.disabled = true;
    setCloneMode("progress");
    if (cloneProgressLog) cloneProgressLog.innerHTML = "";
    setCloneProgress(firstCloneStep.progress, firstCloneStep.label, firstCloneStep.note);
    appendCloneNarrative(firstCloneStep.note);
    openProgressDialog(cloneModal);
    watchOperation(operation.id, {
      onProgress: (payload) => {
        const progress = operationProgress(payload, firstCloneStep.progress, "clone");
        setCloneProgress(
          progress.progress,
          progress.phase || "Cloning output",
          progress.substate || progress.message || "Working.",
        );
        appendCloneNarrative(progress.message || String(payload.phase || "Working."));
      },
      onSuccess: (payload) => {
        const progress = operationProgress(payload, 100, "clone");
        const details = progress.details || {};
        setCloneProgress(100, "Clone ready", progress.message || "The new output is ready.");
        appendCloneNarrative(progress.message || "Clone ready.");
        appendCloneNarrative(`Copied ${details.copied_paths ?? 0} mounted path(s) and left inputs detached.`);
        if (cloneSuccessNote) cloneSuccessNote.textContent = String(progress.flashSuccess || progress.message || "");
        if (cloneOpenLink) cloneOpenLink.setAttribute("href", String(details.redirect_url || "#"));
        setCloneMode("success");
      },
      onFailure: (payload) => {
        const progress = operationProgress(payload, 100, "clone");
        setCloneProgress(100, "Clone failed", progress.message || "The clone could not be completed.");
        if (cloneErrorText) cloneErrorText.textContent = String(progress.flashError || progress.message || "Clone failed.");
        setCloneMode("error");
      },
      onError: (error) => {
        setCloneProgress(100, "Clone failed", "The clone could not be completed.");
        if (cloneErrorText) cloneErrorText.textContent = error instanceof Error ? error.message : "Clone failed.";
        setCloneMode("error");
      },
    }).catch((error) => {
      setCloneProgress(100, "Clone failed", "The clone could not be completed.");
      if (cloneErrorText) cloneErrorText.textContent = error instanceof Error ? error.message : "Clone failed.";
      setCloneMode("error");
    }).finally(() => {
      clonePending = false;
      if (cloneSubmit) cloneSubmit.disabled = false;
    });
  };

  const runtimePill = document.getElementById("output-runtime-pill");
  const runtimeAlertBlock = document.getElementById("output-runtime-alert-block");
  const statusRows = document.getElementById("output-status-rows");
  const summaryRows = document.getElementById("backup-summary-rows");
  const historyBlock = document.getElementById("backup-history-block");
  const signalsNote = document.getElementById("backup-signals-note");
  const backupHistoryCount = document.getElementById("backup-history-count");
  const metricsShell = document.querySelector(".metrics-chart-shell");
  const metricSelect = document.getElementById("output-metric-select");
  const timeframeSelect = document.getElementById("output-timeframe-select");
  const metricsSummary = document.getElementById("output-metrics-summary");
  const metricsChart = document.getElementById("output-metrics-chart");
  const metricsEmpty = document.getElementById("output-metrics-empty");
  const metricsLoading = document.getElementById("output-metrics-loading");
  const outputKind = document.body.dataset.outputKind || "";
  let outputEnabled = document.body.dataset.outputEnabled === "true";
  const outputHasRuntimeSignals = () => outputKind === "app" && outputEnabled;
  const outputHasMetrics = () => outputKind === "app" || outputKind === "shield";
  const hardeningCard = document.querySelector("[data-hardening-card]");
  const hardeningPhaseOneRun = document.querySelector("[data-hardening-phase-one-run]");
  const hardeningPhaseOneLast = document.querySelector("[data-hardening-phase-one-last]");
  const hardeningPhaseOneActive = document.querySelector("[data-hardening-phase-one-active]");
  const hardeningPhaseOneLabel = document.querySelector("[data-hardening-phase-one-label]");
  const hardeningPhaseOneStartTime = document.querySelector("[data-hardening-phase-one-start-time]");
  const hardeningPhaseOneEndTime = document.querySelector("[data-hardening-phase-one-end-time]");
  const hardeningPhaseOneFill = document.querySelector("[data-hardening-phase-one-fill]");
  const hardeningPhaseOneStart = document.querySelector("[data-hardening-phase-one-start]");
  const hardeningPhaseOneStop = document.querySelector("[data-hardening-phase-one-stop]");
  const hardeningPhaseTwo = document.querySelector("[data-hardening-phase='clone']");
  const hardeningPhaseTwoLocked = document.querySelector("[data-hardening-phase-two-locked]");
  const hardeningPhaseTwoProgress = document.querySelector("[data-hardening-phase-two-progress]");
  const hardeningPhaseTwoNarrative = document.querySelector("[data-hardening-phase-two-narrative]");
  const hardeningPhaseTwoFill = document.querySelector("[data-hardening-phase-two-fill]");
  const hardeningPhaseTwoMeta = document.querySelector("[data-hardening-phase-two-meta]");
  const hardeningPhaseTwoStart = document.querySelector("[data-hardening-phase-two-start]");
  const hardeningPhaseTwoStop = document.querySelector("[data-hardening-phase-two-stop]");
  const hardeningFeatures = document.querySelector("[data-hardening-features]");
  const hardeningFeatureRows = document.querySelector("[data-hardening-feature-rows]");
  const hardeningApplyRecommended = document.querySelector("[data-hardening-apply-recommended]");
  let hardeningPollTimer = null;
  let hardeningPhase2Events = null;
  let hardeningPhase2EventRunId = "";
  let hardeningPhase2FallbackUntil = 0;
  let hardeningPollFailureCount = 0;
  const HARDENING_POLL_INTERVAL_MS = 5000;
  const HARDENING_STREAM_BACKUP_POLL_MS = 15000;
  let hardeningState = {
    phase1: null,
    phase2: null,
    features: [],
  };
  let runtimeRefreshTimer = null;
  let runtimeRefreshInFlight = null;
  let runtimeRefreshAbortController = null;
  let runtimeRefreshFailures = 0;
  let renderedRuntimeStatusRowsKey = null;
  let renderedRuntimeAlertKey = null;
  let lastMetricPayload = null;
  const outputLiveMetrics = (() => {
    try {
      return JSON.parse(metricsShell?.dataset.outputLiveMetrics || "{}");
    } catch {
      return {};
    }
  })();
  const afterInitialLoad = (callback) => {
    const run = () => window.setTimeout(callback, 0);
    if (document.readyState === "complete") {
      run();
    } else {
      window.addEventListener("load", run, { once: true });
    }
  };

  const hardeningRunActive = (run) => run && (run.status === "queued" || run.status === "running");
  const hardeningPhaseTwoCanResume = (run) => {
    const details = run?.details && typeof run.details === "object" ? run.details : {};
    return run?.status === "failed" && (details.interrupted === true || run?.error === "hardening run interrupted by CNC restart");
  };

  const hardeningStatusText = (run) => {
    if (!run) return "";
    if (run.error) return run.error;
    const details = run.details && typeof run.details === "object" ? run.details : {};
    return String(details.message || run.status || "");
  };

  const hardeningWindowTimestamp = (date) => {
    if (!(date instanceof Date) || Number.isNaN(date.getTime())) return "";
    const parts = new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      hour12: true,
    }).formatToParts(date).reduce((result, part) => {
      result[part.type] = part.value;
      return result;
    }, {});
    return [parts.month, parts.day, parts.hour, parts.dayPeriod].filter(Boolean).join(" ");
  };

  const hardeningRunDurationSec = (run) => {
    const details = run?.details && typeof run.details === "object" ? run.details : {};
    const parsed = Number(details.duration_sec);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : 24 * 60 * 60;
  };

  const hardeningProgressDetails = (run) => {
    const details = run?.details && typeof run.details === "object" ? run.details : {};
    return details.progress && typeof details.progress === "object" ? details.progress : null;
  };

  const renderHardeningPhaseTwoProgress = (phase2) => {
    const progress = hardeningProgressDetails(phase2);
    const completed = Number(progress?.completed);
    const total = Number(progress?.total);
    const hasExplicitProgress = Number.isFinite(completed) && Number.isFinite(total) && total > 0;
    if (hardeningPhaseTwoFill) {
      const percent = hasExplicitProgress
        ? Math.round((Math.max(0, Math.min(completed, total)) / total) * 100)
        : Math.round((hardeningState.features.filter((row) => row.phase2).length / Math.max(1, hardeningState.features.length || 1)) * 100);
      hardeningPhaseTwoFill.style.width = `${phase2?.status === "success" ? 100 : Math.min(99, percent)}%`;
    }
    if (hardeningPhaseTwoMeta) {
      if (!hasExplicitProgress) {
        hardeningPhaseTwoMeta.hidden = true;
        hardeningPhaseTwoMeta.textContent = "";
        return;
      }
      const state = String(progress?.state || "").replaceAll("_", " ");
      const substate = String(progress?.substate || "").replaceAll("_", " ");
      const current = String(progress?.current_label || "");
      hardeningPhaseTwoMeta.textContent = [
        `${Math.max(0, Math.min(completed, total))}/${total} flags`,
        current,
        [state, substate].filter(Boolean).join(" / "),
      ].filter(Boolean).join(" - ");
      hardeningPhaseTwoMeta.hidden = false;
    }
  };

  const renderHardeningPhaseOneProgress = (run, running) => {
    const started = new Date(normalizeTimestampValue(run?.started_at || ""));
    const hasStarted = !Number.isNaN(started.getTime());
    const durationSec = hardeningRunDurationSec(run);
    const ended = hasStarted ? new Date(started.getTime() + durationSec * 1000) : null;
    if (hardeningPhaseOneStartTime) hardeningPhaseOneStartTime.textContent = hasStarted ? hardeningWindowTimestamp(started) : "";
    if (hardeningPhaseOneEndTime) hardeningPhaseOneEndTime.textContent = ended ? hardeningWindowTimestamp(ended) : "";
    if (hardeningPhaseOneFill) {
      const elapsedSec = hasStarted ? (Date.now() - started.getTime()) / 1000 : 0;
      const progress = running ? Math.max(0, Math.min(100, (elapsedSec / durationSec) * 100)) : 0;
      hardeningPhaseOneFill.style.width = `${progress}%`;
    }
  };

  const renderHardeningFeatures = () => {
    if (!hardeningFeatures || !hardeningFeatureRows) return;
    const rows = Array.isArray(hardeningState.features) ? hardeningState.features : [];
    hardeningFeatures.hidden = rows.length === 0;
    if (hardeningApplyRecommended) hardeningApplyRecommended.hidden = true;
    let previousGroup = "";
    hardeningFeatureRows.innerHTML = rows.map((row) => {
      const recommendation = String(row.recommendation || "uncertain");
      const phaseText = [
        row.phase1 ? String(row.phase1).replaceAll("_", " ") : "",
        row.phase2 ? String(row.phase2).replaceAll("_", " ") : "",
      ].filter(Boolean).join(" -> ") || "not tested";
      const group = String(row.group || "other");
      const groupHeader = group !== previousGroup
        ? `<tr class="hardening-group-row"><th colspan="3">${escapeHtml(row.group_label || group)}</th></tr>`
        : "";
      const evidence = String(row.evidence || "No evidence recorded.");
      const description = String(row.description || "Podman runtime hardening setting.");
      previousGroup = group;
      return `${groupHeader}
        <tr>
          <td>
            <span class="hardening-flag-cell">
              <span>${escapeHtml(row.label || row.setting || "")}</span>
              <span class="settings-help-badge hardening-flag-badge" tabindex="0" data-tooltip="${escapeHtml(description)}">i</span>
            </span>
          </td>
          <td>
            <span class="hardening-recommendation-cell">
              <span class="summary-pill ${recommendation === "recommended" ? "ok" : recommendation === "do not apply" ? "error" : "pending"}">${escapeHtml(recommendation)}</span>
              <span class="settings-help-badge hardening-evidence-badge" tabindex="0" data-tooltip="${escapeHtml(evidence)}">i</span>
            </span>
          </td>
          <td>${escapeHtml(phaseText)}</td>
        </tr>
      `;
    }).join("");
  };

  const renderHardeningState = () => {
    if (!hardeningCard) return;
    const phase1 = hardeningState.phase1 || null;
    const phase2 = hardeningState.phase2 || null;
    const phaseOneRunning = hardeningRunActive(phase1);
    const phaseTwoRunning = hardeningRunActive(phase2);
    const phaseOneComplete = phase1?.status === "success";
    const phaseTwoUnlocked = phaseOneComplete && !phaseOneRunning;

    if (hardeningPhaseOneRun) hardeningPhaseOneRun.hidden = !phaseOneComplete;
    if (hardeningPhaseOneLast) hardeningPhaseOneLast.textContent = formatLocalTimestamp(phase1?.finished_at || phase1?.started_at || "") || "unknown";
    if (hardeningPhaseOneActive) {
      hardeningPhaseOneActive.hidden = !phaseOneRunning;
      if (hardeningPhaseOneLabel) hardeningPhaseOneLabel.textContent = phaseOneRunning ? hardeningStatusText(phase1) || "Phase 1 monitor running." : "";
      renderHardeningPhaseOneProgress(phase1, phaseOneRunning);
    }
    if (hardeningPhaseOneStart) hardeningPhaseOneStart.disabled = phaseOneRunning || phaseTwoRunning;
    if (hardeningPhaseOneStop) hardeningPhaseOneStop.hidden = !phaseOneRunning;

    hardeningPhaseTwo?.classList.toggle("is-locked", !phaseTwoUnlocked && !phaseTwoRunning);
    if (hardeningPhaseTwoLocked) hardeningPhaseTwoLocked.hidden = phaseTwoUnlocked || phaseTwoRunning;
    if (hardeningPhaseTwoProgress) hardeningPhaseTwoProgress.hidden = !phaseTwoRunning;
    if (hardeningPhaseTwoNarrative) hardeningPhaseTwoNarrative.textContent = hardeningStatusText(phase2) || "Preparing clone test.";
    renderHardeningPhaseTwoProgress(phase2);
    if (hardeningPhaseTwoStart) {
      hardeningPhaseTwoStart.textContent = hardeningPhaseTwoCanResume(phase2) ? "Resume Phase 2" : "Start Phase 2";
      hardeningPhaseTwoStart.disabled = !phaseTwoUnlocked || phaseTwoRunning;
    }
    if (hardeningPhaseTwoStop) hardeningPhaseTwoStop.hidden = !phaseTwoRunning;
    renderHardeningFeatures();
  };

  const applyHardeningPayload = (payload) => {
    hardeningState = {
      phase1: payload.phase1 || null,
      phase2: payload.phase2 || null,
      features: Array.isArray(payload.features) ? payload.features : [],
    };
    renderHardeningState();
  };

  const closeHardeningPhase2Events = () => {
    if (hardeningPhase2Events) {
      hardeningPhase2Events.close();
      hardeningPhase2Events = null;
    }
    hardeningPhase2EventRunId = "";
  };

  const startHardeningPhase2Events = () => {
    const phase2 = hardeningState.phase2 || null;
    if (!hardeningRunActive(phase2) || typeof window.EventSource !== "function") {
      if (!hardeningRunActive(phase2)) closeHardeningPhase2Events();
      return false;
    }
    if (Date.now() < hardeningPhase2FallbackUntil) return false;
    const runId = String(phase2.id || "");
    if (hardeningPhase2Events && hardeningPhase2EventRunId === runId) return true;
    closeHardeningPhase2Events();
    hardeningPhase2EventRunId = runId;
    hardeningPhase2Events = new EventSource(`/api/backends/${backendId}/hardening/phase2/events`);
    hardeningPhase2Events.addEventListener("hardening", (event) => {
      try {
        hardeningPhase2FallbackUntil = 0;
        applyHardeningPayload(JSON.parse(event.data || "{}"));
        if (!hardeningRunActive(hardeningState.phase2)) {
          closeHardeningPhase2Events();
          scheduleHardeningPoll();
        }
      } catch (_error) {
        hardeningPhase2FallbackUntil = Date.now() + 5000;
        closeHardeningPhase2Events();
        scheduleHardeningPoll();
      }
    });
    hardeningPhase2Events.addEventListener("error", () => {
      hardeningPhase2FallbackUntil = Date.now() + 5000;
      closeHardeningPhase2Events();
      scheduleHardeningPoll();
    });
    return true;
  };

  const hardeningRequest = async (path) => {
    let formData = new FormData();
    formData.set("csrf_token", csrfToken);
    let response = await fetch(path, {
      method: "POST",
      headers: { Accept: "application/json" },
      body: formData,
    });
    if (response.status === 403) {
      const token = await refreshCsrfTokens();
      if (token) {
        formData = new FormData();
        formData.set("csrf_token", token);
        response = await fetch(path, {
          method: "POST",
          headers: { Accept: "application/json" },
          body: formData,
        });
      }
    }
    const { payload, text } = await readResponsePayload(response);
    if (!response.ok) {
      throw new Error(responseErrorMessage(response, payload, text, "Hardening request failed", { method: "POST", path }));
    }
    applyHardeningPayload(payload);
    scheduleHardeningPoll();
  };

  const loadHardeningStatus = async () => {
    if (!hardeningCard) return;
    try {
      const response = await fetch(`/api/backends/${backendId}/hardening`, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      const payload = await response.json();
      if (!response.ok || payload.error) throw new Error(payload.error || `hardening request failed: ${response.status}`);
      hardeningPollFailureCount = 0;
      applyHardeningPayload(payload);
      scheduleHardeningPoll();
    } catch {
      hardeningPollFailureCount += 1;
      renderHardeningState();
      if (hardeningRunActive(hardeningState.phase1) || hardeningRunActive(hardeningState.phase2)) {
        scheduleHardeningPoll();
      }
    }
  };

  const scheduleHardeningPoll = () => {
    if (hardeningPollTimer !== null) window.clearTimeout(hardeningPollTimer);
    const phaseOneActive = hardeningRunActive(hardeningState.phase1);
    const phaseTwoActive = hardeningRunActive(hardeningState.phase2);
    const phaseTwoStreaming = startHardeningPhase2Events();
    if (!phaseOneActive && !phaseTwoActive) {
      hardeningPollTimer = null;
      return;
    }
    const baseDelayMs = phaseTwoActive && phaseTwoStreaming
      ? HARDENING_STREAM_BACKUP_POLL_MS
      : HARDENING_POLL_INTERVAL_MS;
    const retryDelayMs = Math.min(30000, baseDelayMs * Math.max(1, hardeningPollFailureCount));
    hardeningPollTimer = window.setTimeout(loadHardeningStatus, retryDelayMs);
  };

  const bindHardening = () => {
    if (!hardeningCard) return;
    renderHardeningState();
    loadHardeningStatus();
    hardeningPhaseOneStart?.addEventListener("click", async () => {
      try {
        await hardeningRequest(`/ui/backends/${backendId}/hardening/phase1`);
      } catch (error) {
        window.alert(error instanceof Error ? error.message : "Hardening request failed.");
      }
    });
    hardeningPhaseOneStop?.addEventListener("click", async () => {
      try {
        await hardeningRequest(`/ui/backends/${backendId}/hardening/phase1/stop`);
      } catch {
        await loadHardeningStatus();
      }
    });
    hardeningPhaseTwoStart?.addEventListener("click", async () => {
      try {
        const resumePath = hardeningPhaseTwoCanResume(hardeningState.phase2) ? "/resume" : "";
        await hardeningRequest(`/ui/backends/${backendId}/hardening/phase2${resumePath}`);
      } catch (error) {
        window.alert(error instanceof Error ? error.message : "Hardening request failed.");
      }
    });
    hardeningPhaseTwoStop?.addEventListener("click", async () => {
      try {
        await hardeningRequest(`/ui/backends/${backendId}/hardening/phase2/stop`);
      } catch {
        await loadHardeningStatus();
      }
    });
  };

  const renderRuntimeAlert = (alert) => {
    if (!runtimeAlertBlock) return;
    const key = JSON.stringify(alert || null);
    if (key === renderedRuntimeAlertKey) return;
    renderedRuntimeAlertKey = key;
    if (!alert || typeof alert !== "object") {
      runtimeAlertBlock.innerHTML = "";
      return;
    }
    const rows = Array.isArray(alert.rows) ? alert.rows : [];
    runtimeAlertBlock.innerHTML = `
      <article class="card output-alert-card ${escapeHtml(alert.tone || "error")}">
        <div class="section-head">
          <div>
            <h2>${escapeHtml(alert.title || "Runtime issue")}</h2>
            <p class="subtle">${escapeHtml(alert.summary || "")}</p>
          </div>
          <span class="pill ${escapeHtml(alert.tone || "error")}">${escapeHtml(alert.pill || "unhealthy")}</span>
        </div>
        <div class="settings-lines">
          ${rows.map(([key, value]) => `
            <div class="settings-line">
              <span class="settings-key">${escapeHtml(key)}</span>
              <span class="settings-value mono">${escapeHtml(value)}</span>
            </div>
          `).join("")}
        </div>
      </article>
    `;
  };

  const currentRuntimeStatusRowsKey = () => {
    if (!statusRows) return "";
    const rows = Array.from(statusRows.querySelectorAll(".settings-line")).map((row) => ({
      label: row.querySelector(".settings-key")?.textContent?.trim() || "",
      value: row.querySelector(".settings-value")?.textContent?.trim() || "",
    }));
    return JSON.stringify(rows);
  };

  const renderRuntimeStatusRows = (rows) => {
    if (!statusRows || !Array.isArray(rows)) return;
    const normalizedRows = rows
      .map((item) => ({
        label: String(item?.label || "").trim(),
        value: String(item?.value || "-").trim() || "-",
        checks: Array.isArray(item?.checks)
          ? item.checks
            .map((check) => ({
              label: String(check?.label || "").trim(),
              ok: check?.ok === true,
            }))
            .filter((check) => check.label)
          : [],
      }))
      .filter((item) => item.label);
    const key = JSON.stringify(normalizedRows);
    if (renderedRuntimeStatusRowsKey === null) {
      renderedRuntimeStatusRowsKey = currentRuntimeStatusRowsKey();
    }
    if (key === renderedRuntimeStatusRowsKey) return;
    renderedRuntimeStatusRowsKey = key;
    statusRows.innerHTML = normalizedRows.map((item) => `
      <div class="settings-line">
        <span class="settings-key">${escapeHtml(item.label)}</span>
        ${item.checks.length ? `
          <span class="settings-value mono status-checks-value">
            <span class="status-check-primary">${escapeHtml(item.value)}</span>
            <span class="status-check-components" aria-label="health components">
              ${item.checks.map((check) => `
                <span class="status-check-item ${check.ok ? "is-ok" : "is-failed"}" aria-label="${escapeHtml(check.label)} ${check.ok ? "passed" : "failed"}">
                  <span class="status-check-icon" aria-hidden="true">${check.ok ? "pass" : "fail"}</span>
                  <span>${escapeHtml(check.label)}</span>
                </span>
              `).join("")}
            </span>
          </span>
        ` : `<span class="settings-value mono">${escapeHtml(item.value)}</span>`}
      </div>
    `).join("");
  };

  const runtimeSignalsAreRenderable = (payload) => {
    if (!payload || typeof payload !== "object" || payload.error) return false;
    const label = String(payload.runtime_health_label || "").trim().toLowerCase();
    const summary = String(payload.runtime_health_summary || "").trim().toLowerCase();
    return Boolean(label && label !== "unknown" && summary !== "unknown" && Array.isArray(payload.output_signal_cards));
  };

  const renderRuntimeSignals = (payload) => {
    if (!runtimeSignalsAreRenderable(payload)) return;
    if (runtimePill) {
      const label = String(payload.runtime_health_label || "").trim();
      const className = `pill ${String(payload.runtime_health_tone || "inactive").trim() || "inactive"}`;
      if (runtimePill.textContent !== label) runtimePill.textContent = label;
      if (runtimePill.className !== className) runtimePill.className = className;
      if (runtimePill.hidden) runtimePill.hidden = false;
    }
    renderRuntimeStatusRows(payload.output_signal_cards);
    renderRuntimeAlert(payload.runtime_alert);
  };

  const renderDisabledRuntimeState = () => {
    if (runtimePill) {
      runtimePill.textContent = "NOT ENABLED";
      runtimePill.className = "pill inactive";
      runtimePill.hidden = false;
    }
    renderRuntimeStatusRows([{ label: "status", value: "NOT ENABLED", checks: [] }]);
    renderRuntimeAlert(null);
  };

  const renderPendingRuntimeState = () => {
    if (runtimePill) {
      runtimePill.textContent = "Loading";
      runtimePill.className = "pill queued";
      runtimePill.hidden = false;
    }
  };

  const runtimeRefreshDelay = (payload = null) => {
    if (payload?.pending === true) {
      const requestedDelay = Number(payload.retry_after_ms);
      return Math.max(500, Math.min(1000, Number.isFinite(requestedDelay) ? requestedDelay : 750));
    }
    return Math.min(60000, 15000 * (2 ** Math.min(runtimeRefreshFailures, 2)));
  };

  const refreshRuntimeSignals = async ({ force = false } = {}) => {
    if (!outputHasRuntimeSignals()) return null;
    if (runtimeRefreshInFlight && !force) return runtimeRefreshInFlight;
    if (force && runtimeRefreshAbortController) runtimeRefreshAbortController.abort();
    const controller = new AbortController();
    runtimeRefreshAbortController = controller;
    const requestPromise = (async () => {
      const response = await fetch(`/api/backends/${backendId}/runtime-signals`, {
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(`runtime signals request failed: ${response.status}`);
      if (payload?.pending === true) {
        renderPendingRuntimeState();
        runtimeRefreshFailures = 0;
        return payload;
      }
      renderRuntimeSignals(payload);
      runtimeRefreshFailures = 0;
      return payload;
    })().catch((error) => {
      if (error?.name !== "AbortError") runtimeRefreshFailures += 1;
      throw error;
    }).finally(() => {
      if (runtimeRefreshAbortController === controller) runtimeRefreshAbortController = null;
      if (runtimeRefreshInFlight === requestPromise) runtimeRefreshInFlight = null;
    });
    runtimeRefreshInFlight = requestPromise;
    return runtimeRefreshInFlight;
  };

  const scheduleRuntimeRefresh = (delay = runtimeRefreshDelay()) => {
    if (runtimeRefreshTimer !== null) window.clearTimeout(runtimeRefreshTimer);
    runtimeRefreshTimer = window.setTimeout(() => {
      if (!outputHasRuntimeSignals()) return;
      if (document.hidden) {
        scheduleRuntimeRefresh();
        return;
      }
      refreshRuntimeSignals()
        .then((payload) => scheduleRuntimeRefresh(runtimeRefreshDelay(payload)))
        .catch(() => scheduleRuntimeRefresh());
    }, delay);
  };

  afterInitialLoad(() => {
    if (!outputHasRuntimeSignals()) return;
    if (!runtimePill || runtimePill.hidden || !runtimePill.textContent.trim()) {
      renderPendingRuntimeState();
    }
    refreshRuntimeSignals()
      .then((payload) => scheduleRuntimeRefresh(runtimeRefreshDelay(payload)))
      .catch(() => scheduleRuntimeRefresh());
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && outputHasRuntimeSignals()) refreshRuntimeSignals({ force: true }).catch(() => {});
  });
  window.addEventListener("pagehide", () => {
    if (runtimeRefreshTimer !== null) window.clearTimeout(runtimeRefreshTimer);
    runtimeRefreshAbortController?.abort();
    metricsAbortController?.abort();
  });

  const formatBytes = (value) => {
    const amount = Number(value);
    if (!Number.isFinite(amount) || amount < 0) return "n/a";
    if (amount === 0) return "0 B";
    if (amount >= 1024 ** 4) return `${(amount / 1024 ** 4).toFixed(1)} TB`;
    if (amount >= 1024 ** 3) return `${(amount / 1024 ** 3).toFixed(1)} GB`;
    if (amount >= 1024 ** 2) return `${Math.round(amount / 1024 ** 2)} MB`;
    if (amount >= 1024) return `${Math.round(amount / 1024)} KB`;
    return `${Math.round(amount)} B`;
  };

  const formatRate = (value) => {
    const bytesPerSecond = Number(value);
    if (!Number.isFinite(bytesPerSecond) || bytesPerSecond < 0) return "n/a";
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

  const formatMetricValue = (value, unitKind) => {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "n/a";
    if (unitKind === "bytes") return formatBytes(value);
    if (unitKind === "rate") return formatRate(value);
    return `${Number(value).toFixed(1)}%`;
  };

  const tickLabelForTime = (ts, timeframeKey) => {
    const date = new Date(ts);
    if (timeframeKey === "hour" || timeframeKey === "day") {
      return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    }
    if (timeframeKey === "week") {
      return date.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
    }
    return date.toLocaleDateString([], { month: "short", day: "numeric" });
  };

  const tooltipTitleForTime = (ts) => {
    const date = new Date(ts);
    const weekday = date.toLocaleDateString([], { weekday: "short" });
    const monthDay = date.toLocaleDateString([], { month: "short", day: "numeric" });
    const time = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
    return `${weekday}, ${monthDay} ${time}`;
  };

  let chartInstance = null;
  let metricsRequestId = 0;
  let metricsAbortController = null;
  const OUTPUT_METRICS_CACHE_VERSION = "v2";
  const OUTPUT_METRICS_CACHE_TTL_MS = 5 * 60 * 1000;

  const outputMetricKey = () => String(metricSelect?.value || "cpu");
  const outputTimeframeKey = () => String(timeframeSelect?.value || "day");
  const outputMetricUnitKind = (metricKey) => {
    if (metricKey === "disk") return "bytes";
    return metricKey === "network" ? "rate" : "percent";
  };
  const outputMetricsCacheKey = () => (
    `cnc:output-metrics:${OUTPUT_METRICS_CACHE_VERSION}:${backendId}:${outputMetricKey()}:${outputTimeframeKey()}`
  );

  const outputMetricValue = (metrics, metricKey) => {
    if (!metrics || typeof metrics !== "object") return null;
    const valueByMetric = {
      cpu: metrics.cpu_percent_of_host,
      memory: metrics.memory_percent,
      disk: metrics.disk_usage_bytes,
      network: metrics.network_total_bps,
    };
    let value = valueByMetric[metricKey];
    if (metricKey === "network" && !Number.isFinite(Number(value))) {
      const rx = Number(metrics.network_rx_bps);
      const tx = Number(metrics.network_tx_bps);
      value = (Number.isFinite(rx) || Number.isFinite(tx))
        ? (Number.isFinite(rx) ? rx : 0) + (Number.isFinite(tx) ? tx : 0)
        : value;
    }
    const numericValue = Number(value);
    return Number.isFinite(numericValue) ? numericValue : null;
  };

  const readCachedMetricPayload = () => {
    try {
      const rawValue = window.sessionStorage?.getItem(outputMetricsCacheKey());
      if (!rawValue) return null;
      const cached = JSON.parse(rawValue);
      if (!cached || typeof cached !== "object") return null;
      if ((Date.now() - Number(cached.cachedAt || 0)) > OUTPUT_METRICS_CACHE_TTL_MS) return null;
      const payload = cached.payload;
      if (!payload || typeof payload !== "object") return null;
      const payloadMetric = String(payload?.metric?.key || "");
      const payloadTimeframe = String(payload?.timeframe?.key || "");
      if (payloadMetric !== outputMetricKey() || payloadTimeframe !== outputTimeframeKey()) return null;
      return payload;
    } catch {
      return null;
    }
  };

  const writeCachedMetricPayload = (payload) => {
    try {
      if (!payload || typeof payload !== "object" || !payload.available) return;
      if (!Array.isArray(payload.series) || payload.series.length === 0) return;
      window.sessionStorage?.setItem(outputMetricsCacheKey(), JSON.stringify({
        cachedAt: Date.now(),
        payload,
      }));
    } catch {
      // Browser storage can be unavailable in private contexts; metric hydration still works without it.
    }
  };

  const setMetricsLoading = (isLoading) => {
    if (!metricsLoading) return;
    metricsLoading.hidden = !isLoading;
    const frame = metricsLoading.parentElement;
    frame?.classList.toggle("is-loading", isLoading);
    frame?.toggleAttribute("aria-busy", isLoading);
  };

  const waitForMetricsLoadingPaint = () => new Promise((resolve) => {
    window.requestAnimationFrame(() => window.requestAnimationFrame(resolve));
  });

  const renderMetricsSummary = (payload) => {
    if (!metricsSummary) return;
    const unitKind = String(payload?.metric?.unit_kind || "percent");
    const summary = payload?.summary || {};
    const rows = [
      { label: "peak", value: formatMetricValue(summary.peak_value, unitKind) },
      { label: "avg", value: formatMetricValue(summary.average_value, unitKind) },
      { label: "last", value: formatMetricValue(summary.latest_value, unitKind) },
    ];
    metricsSummary.innerHTML = rows.map((item) => `
      <div class="metrics-summary-chip">
        <span class="metrics-summary-label">${escapeHtml(item.label)}</span>
        <strong class="metrics-summary-value">${escapeHtml(item.value)}</strong>
      </div>
    `).join("");
  };

  const renderMetricsFromCachedStatus = (metrics) => {
    if (!metricsShell) return false;
    const metricKey = outputMetricKey();
    const priorMetricKey = String(lastMetricPayload?.metric?.key || "");
    const priorTimeframeKey = String(lastMetricPayload?.timeframe?.key || "");
    const priorSummary = (
      priorMetricKey === metricKey && priorTimeframeKey === outputTimeframeKey() && lastMetricPayload?.summary
    ) ? lastMetricPayload.summary : {};
    const latestValue = outputMetricValue(metrics, metricKey) ?? priorSummary.latest_value ?? null;
    renderMetricsSummary({
      metric: { key: metricKey, unit_kind: outputMetricUnitKind(metricKey) },
      summary: {
        peak_value: priorSummary.peak_value ?? latestValue,
        average_value: priorSummary.average_value ?? latestValue,
        latest_value: latestValue,
      },
    });
    return latestValue !== null;
  };

  const hydrateOutputMetrics = () => {
    const cachedPayload = readCachedMetricPayload();
    if (cachedPayload) {
      renderMetricsChart(cachedPayload);
      return true;
    }
    return renderMetricsFromCachedStatus(outputLiveMetrics);
  };

  const renderMetricsChart = (payload) => {
    if (!metricsChart || !metricsEmpty) return;
    lastMetricPayload = payload;
    setMetricsLoading(false);
    if (!window.Chart) {
      metricsEmpty.hidden = false;
      metricsEmpty.textContent = "Chart library unavailable.";
      return;
    }

    const available = Boolean(payload?.available);
    const series = Array.isArray(payload?.series) ? payload.series : [];

    renderMetricsSummary(payload);

    if (chartInstance) { chartInstance.destroy(); chartInstance = null; }

    if (!available) {
      metricsEmpty.hidden = false;
      metricsEmpty.textContent = String(payload?.note || "No metric history is available.");
      return;
    }
    if (!series.length) {
      metricsEmpty.hidden = false;
      metricsEmpty.textContent = "No metric samples yet. CNC needs a little runtime history before this chart can draw.";
      return;
    }
    metricsEmpty.hidden = true;

    const timeframeKey = String(payload?.timeframe?.key || "day");
    const unitKind     = String(payload?.metric?.unit_kind || "percent");
    const metricKey    = String(payload?.metric?.key || "");
    const limitValue   = Number(payload?.limit_value);
    const memoryLimitBytes = payload?.memory_limit_bytes === null || payload?.memory_limit_bytes === undefined
      ? NaN
      : Number(payload.memory_limit_bytes);
    const softLimitValue = Number(payload?.soft_limit_value);
    const softLimitLabel = String(payload?.soft_limit_label || "SOFT");
    const chartEvents = window.cncMetricEventMarkers?.normalizeEvents(payload?.chart_events) || [];
    const cpuLimitPoints = Array.isArray(payload?.cpu_limit_series)
      ? payload.cpu_limit_series
        .map((item) => ({ x: Date.parse(item.timestamp), y: Number(item.value) }))
        .filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y))
      : [];
    const memoryLimitPoints = Array.isArray(payload?.memory_limit_series)
      ? payload.memory_limit_series
        .map((item) => ({ x: Date.parse(item.timestamp), y: Number(item.value) }))
        .filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y))
      : [];

    const avgPoints = series.map((item) => ({ x: Date.parse(item.timestamp), y: Number(item.visual_value ?? item.avg ?? item.value) }));
    const minPoints = series.map((item) => ({ x: Date.parse(item.timestamp), y: Number(item.visual_min ?? item.min ?? item.avg ?? item.value) }));
    const maxPoints = series.map((item) => ({ x: Date.parse(item.timestamp), y: Number(item.visual_max ?? item.max ?? item.avg ?? item.value) }));
    const downPoints = series
      .map((item) => ({ x: Date.parse(item.timestamp), y: Number(item.network_rx_bps) }))
      .filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y));
    const upPoints = series
      .map((item) => ({ x: Date.parse(item.timestamp), y: Number(item.network_tx_bps) }))
      .filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y));
    const hasDirectionalNetwork = downPoints.length || upPoints.length;
    const totalPoints = avgPoints.filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y));
    const seriesByTime = new Map(series.map((item) => [Date.parse(item.timestamp), item]));
    const selectedRangeStart = Date.parse(payload?.range_start_at);
    const selectedRangeEnd = Date.parse(payload?.range_end_at);
    const xMin = Number.isFinite(selectedRangeStart) ? selectedRangeStart : avgPoints[0].x;
    const xMax = Number.isFinite(selectedRangeEnd) ? selectedRangeEnd : avgPoints[avgPoints.length - 1].x;

    // Dynamic y range: zoom to data with headroom, optionally capped by limit
    const chartValuePoints = metricKey === "network" && hasDirectionalNetwork
      ? [...downPoints, ...upPoints]
      : metricKey === "network"
        ? totalPoints
      : [
        ...avgPoints,
        ...minPoints,
        ...maxPoints,
        ...(metricKey === "cpu" ? cpuLimitPoints : []),
        ...(metricKey === "memory" ? memoryLimitPoints : []),
      ];
    const dataValues = chartValuePoints.map((p) => p.y).filter(Number.isFinite);
    const dataMax = dataValues.length ? Math.max(...dataValues) : 1;
    const dataMin = dataValues.length ? Math.min(...dataValues) : 0;
    const dataRange = Math.max(dataMax - dataMin, dataMax * 0.1, 0.01);
    const points = avgPoints.filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y));
    const isMemoryPercent = metricKey === "memory" && unitKind === "percent";
    const payloadYAxisMin = Number(payload?.y_axis_min);
    const payloadYAxisMax = Number(payload?.y_axis_max);
    const fallbackYAxisMin = Math.max(0, dataMin - dataRange * 0.15);
    const fallbackPercentYAxisMax = () => {
      if (isMemoryPercent && Number.isFinite(limitValue) && dataMax <= limitValue) return limitValue;
      const padded = Math.max(dataMax * 1.2, dataMax + 0.1);
      return [1, 2, 5, 10, 20, 50, 100].find((stop) => padded <= stop) || dataMax + dataRange * 0.4;
    };
    const fallbackYAxisMax = unitKind === "percent"
      ? fallbackPercentYAxisMax()
      : dataMax + dataRange * 0.4;
    const yAxisMin = Number.isFinite(payloadYAxisMin) ? payloadYAxisMin : fallbackYAxisMin;
    const effectiveYMax = Number.isFinite(payloadYAxisMax) && payloadYAxisMax > yAxisMin
      ? payloadYAxisMax
      : fallbackYAxisMax;
    const showSoftLimit = isMemoryPercent
      && Number.isFinite(softLimitValue)
      && softLimitValue > yAxisMin
      && softLimitValue < effectiveYMax;
    const showCpuLimit = metricKey === "cpu" && cpuLimitPoints.length > 1;
    const latestCpuLimitPoint = showCpuLimit ? cpuLimitPoints[cpuLimitPoints.length - 1] : null;
    const cpuLimitLabel = latestCpuLimitPoint && Number.isFinite(latestCpuLimitPoint.y)
      ? `CPU LIMIT (${formatMetricValue(latestCpuLimitPoint.y, "percent")})`
      : "";

    const ctx = metricsChart.getContext("2d");

    const datasets = metricKey === "network" ? (hasDirectionalNetwork ? [
      {
        label: "down",
        data: downPoints,
        borderColor: "#68d8ff",
        borderWidth: 2,
        backgroundColor: "transparent",
        fill: false,
        cubicInterpolationMode: "monotone",
        tension: 0.42,
        pointRadius: 0,
        pointHoverRadius: 5,
        pointHoverBackgroundColor: "#68d8ff",
        pointHoverBorderColor: "rgba(9,14,24,0.9)",
        pointHoverBorderWidth: 2.5,
      },
      {
        label: "up",
        data: upPoints,
        borderColor: "#9bf0c7",
        borderWidth: 2,
        backgroundColor: "transparent",
        fill: false,
        cubicInterpolationMode: "monotone",
        tension: 0.42,
        pointRadius: 0,
        pointHoverRadius: 5,
        pointHoverBackgroundColor: "#9bf0c7",
        pointHoverBorderColor: "rgba(9,14,24,0.9)",
        pointHoverBorderWidth: 2.5,
      },
    ] : [
      {
        label: "total",
        data: totalPoints,
        borderColor: "#86dcff",
        borderWidth: 2,
        backgroundColor: "transparent",
        fill: false,
        cubicInterpolationMode: "monotone",
        tension: 0.42,
        pointRadius: totalPoints.length === 1 ? 3 : 0,
        pointHoverRadius: 5,
        pointHoverBackgroundColor: "#86dcff",
        pointHoverBorderColor: "rgba(9,14,24,0.9)",
        pointHoverBorderWidth: 2.5,
      },
    ]) : [
      {
        label: "range-min",
        data: minPoints,
        borderColor: "rgba(70,190,255,0)",
        borderWidth: 0,
        backgroundColor: "rgba(70,190,255,0)",
        pointRadius: 0,
        pointHoverRadius: 0,
        cubicInterpolationMode: "monotone",
        tension: 0.35,
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
        cubicInterpolationMode: "monotone",
        tension: 0.35,
      },
      {
        label: "avg",
        data: points,
        borderColor: "#86dcff",
        borderWidth: 2,
        backgroundColor: "transparent",
        fill: false,
        cubicInterpolationMode: "monotone",
        tension: 0.42,
        pointRadius: 0,
        pointHoverRadius: 5,
        pointHoverBackgroundColor: "#86dcff",
        pointHoverBorderColor: "rgba(9,14,24,0.9)",
        pointHoverBorderWidth: 2.5,
      },
    ];

    if (showSoftLimit) {
      datasets.push({
        label: softLimitLabel.toLowerCase(),
        data: [{ x: xMin, y: softLimitValue }, { x: xMax, y: softLimitValue }],
        borderColor: "rgba(244,203,123,0.75)",
        borderWidth: 1.5,
        borderDash: [5, 5],
        backgroundColor: "transparent",
        fill: false,
        pointRadius: 0,
        tension: 0,
      });
    }
    if (showCpuLimit) {
      datasets.push({
        label: "CPU limit",
        data: cpuLimitPoints,
        borderColor: "rgba(244,203,123,0.75)",
        borderWidth: 1.5,
        borderDash: [5, 5],
        backgroundColor: "transparent",
        fill: false,
        pointRadius: 0,
        tension: 0,
      });
    }
    if (metricKey === "memory" && unitKind === "bytes" && memoryLimitPoints.length > 1) {
      datasets.push({
        label: "hard cap",
        data: memoryLimitPoints,
        borderColor: "rgba(255,141,141,0.78)",
        borderWidth: 1.5,
        borderDash: [5, 5],
        backgroundColor: "transparent",
        fill: false,
        pointRadius: 0,
        tension: 0,
      });
    }

    const limitLabelPlugin = {
      id: "limitLabel",
      afterDraw(chart) {
        const labels = [];
        if (showSoftLimit) labels.push({ value: softLimitValue, text: softLimitLabel });
        if (showCpuLimit && latestCpuLimitPoint && cpuLimitLabel) {
          labels.push({ value: latestCpuLimitPoint.y, text: cpuLimitLabel });
        }
        if (!labels.length) return;
        const { ctx: c, chartArea, scales } = chart;
        c.save();
        c.font = "10px monospace";
        c.letterSpacing = "0.08em";
        c.textAlign = "left";
        labels.forEach((item) => {
          const y = Math.max(chartArea.top + 16, Math.min(chartArea.bottom - 4, scales.y.getPixelForValue(item.value) - 6));
          const x = chartArea.left + 8;
          const width = c.measureText(item.text).width;
          c.fillStyle = "rgba(9,14,24,0.82)";
          c.fillRect(x - 4, y - 12, width + 8, 16);
          c.fillStyle = "rgba(244,203,123,0.92)";
          c.fillText(item.text, x, y);
        });
        c.restore();
      },
    };

    chartInstance = new Chart(ctx, {
      type: "line",
      data: { datasets },
      plugins: [limitLabelPlugin, window.cncMetricEventMarkers?.plugin].filter(Boolean),
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 350, easing: "easeOutQuart" },
        interaction: { mode: "index", intersect: false, axis: "x" },
        plugins: {
          cncMetricEventMarkers: {
            events: chartEvents,
            formatTimestamp: tooltipTitleForTime,
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
              title: (items) => {
                const item = Array.isArray(items) ? items.find((entry) => entry?.parsed) : null;
                return item ? tooltipTitleForTime(item.parsed.x) : "";
              },
              label: (item) => {
                if (metricKey === "memory" && unitKind === "percent" && Number.isFinite(memoryLimitBytes)) {
                  const percent = Number(item.parsed.y);
                  const bytes = memoryLimitBytes * Math.max(0, percent) / 100;
                  return `${Math.round(percent)}% (${formatBytes(bytes)})`;
                }
                if (metricKey === "memory" && unitKind === "bytes") {
                  const row = seriesByTime.get(Number(item.parsed.x)) || {};
                  const percent = Number(row.memory_percent);
                  if (Number.isFinite(percent)) {
                    return `${formatBytes(item.parsed.y)} (${Math.round(percent)}%)`;
                  }
                }
                if (metricKey === "network" && unitKind === "rate") {
                  const label = String(item.dataset?.label || "");
                  return `${label} ${formatMetricValue(item.parsed.y, unitKind)}`;
                }
                return formatMetricValue(item.parsed.y, unitKind);
              },
            },
          },
        },
        scales: {
          x: {
            type: "linear",
            min: xMin,
            max: xMax,
            grid: { color: "rgba(146,174,226,0.09)" },
            ticks: {
              maxTicksLimit: 5,
              color: "rgba(220,231,253,0.55)",
              font: { family: "monospace", size: 11 },
              callback: (val) => tickLabelForTime(val, timeframeKey),
            },
            border: { display: false },
          },
          y: {
            min: yAxisMin,
            max: effectiveYMax,
            grid: { color: "rgba(146,174,226,0.09)" },
            ticks: {
              maxTicksLimit: 5,
              color: "rgba(220,231,253,0.55)",
              font: { family: "monospace", size: 11 },
              callback: (val) => formatMetricValue(val, unitKind),
            },
            border: { display: false },
          },
        },
      },
    });
  };

  const showMetricHistoryError = () => {
    setMetricsLoading(false);
    if (metricsEmpty && !lastMetricPayload) {
      metricsEmpty.hidden = false;
      metricsEmpty.textContent = "Metric history could not be loaded.";
    }
  };

  const loadMetricHistory = async () => {
    if (!metricSelect || !timeframeSelect || !metricsChart || !metricsEmpty) return;
    const requestId = ++metricsRequestId;
    metricsAbortController?.abort();
    const controller = new AbortController();
    metricsAbortController = controller;
    metricsEmpty.hidden = true;
    setMetricsLoading(true);
    const loadingStartedAt = performance.now();
    try {
      await ensureMetricChartAssets();
      if (controller.signal.aborted || requestId !== metricsRequestId) return;
      await waitForMetricsLoadingPaint();
      const chartWidth = Math.round(
        metricsChart.parentElement?.clientWidth || metricsChart.clientWidth || 720
      );
      const query = new URLSearchParams({
        metric: metricSelect.value,
        timeframe: timeframeSelect.value,
        width: String(chartWidth),
      });
      const response = await fetch(`/api/backends/${backendId}/metrics-history?${query.toString()}`, {
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`metric history request failed: ${response.status}`);
      const payload = await response.json();
      if (requestId !== metricsRequestId) return;
      writeCachedMetricPayload(payload);
      const loadingElapsedMs = performance.now() - loadingStartedAt;
      if (loadingElapsedMs < 420) {
        await new Promise((resolve) => setTimeout(resolve, 420 - loadingElapsedMs));
      }
      if (requestId !== metricsRequestId) return;
      renderMetricsChart(payload);
    } catch (error) {
      if (error?.name === "AbortError") return;
      if (requestId === metricsRequestId) showMetricHistoryError();
    } finally {
      if (metricsAbortController === controller) metricsAbortController = null;
    }
  };

  if (outputHasMetrics() && metricSelect && timeframeSelect && metricsChart) {
    const refreshOutputMetrics = () => {
      hydrateOutputMetrics();
      loadMetricHistory();
    };
    const initializeOutputMetrics = async () => {
      try {
        await ensureMetricChartAssets();
      } catch {
        showMetricHistoryError();
        return;
      }
      metricSelect.addEventListener("change", () => {
        refreshOutputMetrics();
      });
      timeframeSelect.addEventListener("change", () => {
        refreshOutputMetrics();
      });
      hydrateOutputMetrics();
      afterInitialLoad(() => loadMetricHistory());
    };
    initializeOutputMetrics();
  }
  bindHardening();

  const bindPlacementForm = () => {
    const form = document.querySelector("[data-placement-form]");
    if (!form) return;
    const enabledToggle = form.querySelector("[data-placement-enabled]");
    const config = form.querySelector("[data-placement-config]");
    const emptyLine = form.querySelector("[data-placement-empty]");
    const modeInput = form.querySelector("[data-placement-mode-input]");
    const activeInput = form.querySelector("[data-placement-active-input]");
    const modeButtons = Array.from(form.querySelectorAll("[data-placement-mode]"));
    const nodeCards = Array.from(form.querySelectorAll("[data-placement-node]"));
    const saveButton = form.querySelector("[data-placement-save]");
    const interfaceAddButton = form.querySelector("[data-placement-interface-add]");
    const interfaceList = form.querySelector("[data-placement-interface-list]");
    const inboundRows = () => Array.from(form.querySelectorAll("[data-placement-interface-inbound-row]"));

    const mode = () => String(modeInput?.value || "failover").trim().toLowerCase();
    const nodeId = (card) => String(card?.dataset.nodeId || "").trim();
    const checkboxFor = (card) => card?.querySelector("[data-placement-node-checkbox]");
    const selectedCards = () => nodeCards.filter((card) => checkboxFor(card)?.checked);
    const nodeSetupComplete = new Set(
      nodeCards
        .filter((card) => card.dataset.nodeSetupReady === "true" || checkboxFor(card)?.checked)
        .map((card) => nodeId(card))
        .filter(Boolean)
    );
    const nodeDisplayName = (card) => (
      String(card?.dataset.nodeName || card?.querySelector(".placement-node-name")?.textContent || nodeId(card) || "node").trim()
    );

    const interfaceRows = () => Array.from(form.querySelectorAll("[data-placement-interface-row]"));
    const interfaceTargetOptions = () => {
      try {
        const parsed = JSON.parse(interfaceList?.dataset.interfaceTargetOptions || "[]");
        return Array.isArray(parsed) ? parsed : [];
      } catch {
        return [];
      }
    };
    const nextInterfaceName = () => {
      const used = new Set(interfaceRows().map((row) => row.querySelector("[data-placement-interface-name]")?.textContent?.trim()).filter(Boolean));
      let index = 0;
      while (used.has(`cnc${index}`)) index += 1;
      return `cnc${index}`;
    };
    const removeInterfaceRow = (row) => {
      row?.remove();
    };
    const interfaceTargetLabel = (select) => {
      const option = select?.options?.[select.selectedIndex];
      return String(option?.textContent || "target").trim() || "target";
    };
    const interfaceDirectionValue = (select) => {
      const value = String(select?.value || "out").trim().toLowerCase();
      return ["out", "in", "bidirectional"].includes(value) ? value : "out";
    };
    const interfacePathParts = ({ current, other, direction, inbound = false }) => {
      const normalized = interfaceDirectionValue({ value: direction });
      if (normalized === "bidirectional") {
        return {
          left: inbound ? other : current,
          right: inbound ? current : other,
          label: "BIDIRECTIONAL <->",
        };
      }
      if (inbound) {
        return normalized === "in"
          ? { left: current, right: other, label: "OUTBOUND ->" }
          : { left: other, right: current, label: "INBOUND ->" };
      }
      return normalized === "in"
        ? { left: other, right: current, label: "INBOUND ->" }
        : { left: current, right: other, label: "OUTBOUND ->" };
    };
    const updateDirectionOptionLabels = (select, parts) => {
      if (!select || !parts) return;
      const selectedOption = select.options?.[select.selectedIndex];
      if (selectedOption) selectedOption.textContent = parts.label;
    };
    const updateInterfaceMeta = (row) => {
      const left = row?.querySelector("[data-placement-interface-path-left]");
      const select = row?.querySelector("[data-placement-interface-target]");
      const direction = row?.querySelector("[data-placement-interface-direction]");
      if (!left) return;
      const parts = interfacePathParts({
        current: backendName,
        other: interfaceTargetLabel(select),
        direction: interfaceDirectionValue(direction),
      });
      left.textContent = parts.left;
      updateDirectionOptionLabels(direction, parts);
    };
    const updateInboundInterfaceMeta = (row, statusLabel) => {
      const left = row?.querySelector("[data-placement-interface-inbound-path-left]");
      const right = row?.querySelector("[data-placement-interface-inbound-path-right]");
      const direction = row?.querySelector("[data-placement-interface-inbound-direction]");
      if (!left || !right) return;
      const parts = interfacePathParts({
        current: String(row?.dataset.inboundTargetName || backendName),
        other: String(row?.dataset.inboundSourceName || "source"),
        direction: interfaceDirectionValue(direction),
        inbound: true,
      });
      left.textContent = parts.left;
      right.textContent = parts.right;
      updateDirectionOptionLabels(direction, parts);
    };
    const setInboundRowBusy = (row, busy) => {
      row?.querySelectorAll("[data-placement-interface-inbound-action]").forEach((button) => {
        button.disabled = Boolean(busy);
      });
    };
    const renderConfirmedInboundRow = (row) => {
      row.dataset.inboundStatus = "accepted";
      const actions = row.querySelector("[data-placement-interface-inbound-actions]");
      if (!actions) return;
      actions.innerHTML = "";
      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "placement-interface-delete";
      removeButton.dataset.placementInterfaceInboundAction = "rejected";
      removeButton.setAttribute("aria-label", `Delete ${row.dataset.inboundInterfaceName || "interface"}`);
      removeButton.textContent = "Delete";
      removeButton.addEventListener("click", () => submitInboundInterfaceAction(row, "rejected"));
      actions.append(removeButton);
      updateInboundInterfaceMeta(row);
    };
    applyInboundInterfaceOperationResult = (operation) => {
      const details = operation?.details && typeof operation.details === "object" ? operation.details : {};
      const sourceId = String(details.source_backend_id || "").trim();
      const interfaceName = String(details.interface_name || "").trim();
      const row = inboundRows().find((candidate) => (
        String(candidate.dataset.inboundSourceId || "").trim() === sourceId
        && String(candidate.dataset.inboundInterfaceName || "").trim() === interfaceName
      ));
      if (!row) return;
      if (String(details.status || "") === "accepted") {
        renderConfirmedInboundRow(row);
      } else {
        row.remove();
      }
    };
    const submitInboundInterfaceAction = async (row, status) => {
      if (!row) return;
      const sourceId = String(row.dataset.inboundSourceId || "").trim();
      const interfaceName = String(row.dataset.inboundInterfaceName || "").trim();
      if (!sourceId || !interfaceName) return;
      const wasAccepted = row.dataset.inboundStatus === "accepted";
      setInboundRowBusy(row, true);
      const formData = new FormData();
      formData.set("csrf_token", csrfToken);
      formData.set("source_backend_id", sourceId);
      formData.set("interface_name", interfaceName);
      formData.set("status", status);
      formData.set("direction", interfaceDirectionValue(row.querySelector("[data-placement-interface-inbound-direction]")));
      const requestPath = `/ui/backends/${backendId}/interfaces/inbound`;
      try {
        let response = await fetch(requestPath, {
          method: "POST",
          credentials: "same-origin",
          body: formData,
          headers: {
            Accept: "application/json",
            "X-Requested-With": "fetch",
          },
        });
        if (response.status === 403 && await refreshCsrfTokens()) {
          formData.set("csrf_token", csrfToken);
          response = await fetch(requestPath, {
            method: "POST",
            credentials: "same-origin",
            body: formData,
            headers: {
              Accept: "application/json",
              "X-Requested-With": "fetch",
            },
          });
        }
        const { payload, text } = await readResponsePayload(response);
        if (!response.ok) {
          throw new Error(responseErrorMessage(response, payload, text, "Interface action failed", {
            method: "POST",
            path: requestPath,
          }));
        }
        if (!payload.operation_id) throw new Error("operation id missing");
        const form = row.closest("form");
        const runningLabel = status === "accepted" ? "Confirming interface" : "Rejecting interface";
        const firstProgress = form ? startOutputSaveProgress(runningLabel, form) : 8;
        await watchOperation(payload.operation_id, {
          onProgress: (operation) => {
            if (form) renderOutputSaveOperationProgress(form, runningLabel, operation, firstProgress);
          },
          onSuccess: (operation) => {
            if (form) {
              renderOutputSaveOperationResult(
                form,
                runningLabel,
                operation,
                null,
                status === "accepted" ? "Interface confirmed." : "Interface rejected.",
              );
            } else {
              applyInboundInterfaceOperationResult(operation);
            }
            renderFlashBanner("success", status === "accepted"
              ? (wasAccepted ? "Interface updated." : "Interface confirmed.")
              : "Interface rejected.");
          },
          onFailure: (operation) => {
            setInboundRowBusy(row, false);
            const progress = operationProgress(operation, 100, "outputSave");
            if (form) renderOutputSaveOperationResult(form, runningLabel, operation, null, "Interface action failed.");
            renderFlashBanner("error", progress.flashError || progress.message || "Interface action failed.");
          },
          onError: (error) => {
            setInboundRowBusy(row, false);
            renderFlashBanner("error", error instanceof Error ? error.message : "Interface action failed.");
          },
        });
      } catch (error) {
        setInboundRowBusy(row, false);
        renderFlashBanner("error", error instanceof Error ? error.message : "Interface action failed.");
      }
    };
    const addInterfaceRow = () => {
      if (!interfaceList) return;
      const name = nextInterfaceName();
      const targets = interfaceTargetOptions();
      const row = document.createElement("div");
      row.className = "placement-interface-row";
      row.dataset.placementInterfaceRow = "";
      row.dataset.interfaceStatus = "pending";

      const nameInput = document.createElement("input");
      nameInput.type = "hidden";
      nameInput.name = "inter_app_interface_names";
      nameInput.value = name;
      nameInput.dataset.placementInterfaceNameInput = "";
      row.append(nameInput);

      const portInput = document.createElement("input");
      portInput.type = "hidden";
      portInput.name = "inter_app_interface_ports";
      portInput.value = "auto";
      portInput.dataset.placementInterfacePortInput = "";
      row.append(portInput);

      const copy = document.createElement("span");
      copy.className = "placement-interface-copy";
      const label = document.createElement("strong");
      label.className = "mono";
      label.dataset.placementInterfaceName = "";
      label.textContent = name;
      copy.append(label);
      row.append(copy);

      const path = document.createElement("span");
      path.className = "placement-interface-path";
      const endpoint = document.createElement("span");
      endpoint.className = "placement-interface-endpoint";
      endpoint.dataset.placementInterfacePathLeft = "";
      path.append(endpoint);

      const direction = document.createElement("select");
      direction.name = "inter_app_interface_directions";
      direction.dataset.placementInterfaceDirection = "";
      direction.setAttribute("aria-label", `${name} direction`);
      [
        ["out", "OUTBOUND ->"],
        ["in", "INBOUND ->"],
        ["bidirectional", "BIDIRECTIONAL <->"],
      ].forEach(([value, labelText]) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = labelText;
        direction.append(option);
      });
      path.append(direction);

      const select = document.createElement("select");
      select.className = "placement-interface-target-select";
      select.name = "inter_app_interface_targets";
      select.dataset.placementInterfaceTarget = "";
      select.setAttribute("aria-label", `${name} target output`);
      if (targets.length) {
        targets.forEach((target) => {
          const option = document.createElement("option");
          option.value = String(target.id || "");
          option.textContent = String(target.name || target.id || "");
          select.append(option);
        });
      } else {
        select.disabled = true;
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "No target outputs";
        select.append(option);
      }
      path.append(select);
      row.append(path);
      direction.addEventListener("change", () => updateInterfaceMeta(row));
      select.addEventListener("change", () => updateInterfaceMeta(row));
      updateInterfaceMeta(row);

      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "placement-interface-remove";
      removeButton.dataset.placementInterfaceRemove = "";
      removeButton.setAttribute("aria-label", `Remove ${name}`);
      removeButton.textContent = "x";
      removeButton.addEventListener("click", () => removeInterfaceRow(row));
      row.append(removeButton);

      interfaceList.append(row);
    };

    const setMode = (nextMode) => {
      const normalized = nextMode === "live" ? "live" : "failover";
      if (modeInput) modeInput.value = normalized;
      config?.classList.toggle("is-live", normalized === "live");
      modeButtons.forEach((button) => {
        button.classList.toggle("is-active", button.dataset.placementMode === normalized);
      });
    };

    const setActiveNode = (nextNodeId) => {
      const normalized = String(nextNodeId || "").trim() || nodeId(nodeCards[0]) || "local";
      if (activeInput) activeInput.value = normalized;
    };

    const ensureSelectedNode = () => {
      if (!enabledToggle?.checked || selectedCards().length) return;
      const currentActiveId = String(activeInput?.value || "").trim();
      const activeCard = nodeCards.find((card) => nodeId(card) === currentActiveId) || nodeCards[0];
      const activeCheckbox = checkboxFor(activeCard);
      if (activeCheckbox) activeCheckbox.checked = true;
      const followerCard = nodeCards.find((card) => card !== activeCard);
      const followerCheckbox = checkboxFor(followerCard);
      if (followerCheckbox) followerCheckbox.checked = true;
      if (activeCard) setActiveNode(nodeId(activeCard));
    };

    const ensureActiveNode = () => {
      const selected = selectedCards();
      if (!selected.length) {
        const firstCheckbox = checkboxFor(nodeCards[0]);
        if (firstCheckbox) firstCheckbox.checked = true;
        if (nodeCards[0]) setActiveNode(nodeId(nodeCards[0]));
        return;
      }
      const activeId = String(activeInput?.value || "").trim();
      if (!selected.some((card) => nodeId(card) === activeId)) {
        setActiveNode(nodeId(selected[0]));
      }
    };

    const renderPlacementNodes = () => {
      ensureSelectedNode();
      ensureActiveNode();
      const enabled = Boolean(enabledToggle?.checked);
      const currentMode = mode();
      const activeId = String(activeInput?.value || "").trim();
      if (config) config.hidden = !enabled;
      if (emptyLine) emptyLine.hidden = enabled;
      form.querySelector(".toggle-switch")?.classList.toggle("is-on", enabled);
      nodeCards.forEach((card) => {
        const checked = Boolean(checkboxFor(card)?.checked);
        const isActive = checked && (currentMode === "live" || nodeId(card) === activeId);
        const status = checked ? (currentMode === "live" || isActive ? "active" : "standby") : "off";
        card.classList.toggle("is-selected", checked);
        card.classList.toggle("is-active", isActive);
        card.querySelector("[data-placement-role]")?.replaceChildren(document.createTextNode(status));
      });
      if (saveButton) {
        saveButton.disabled = enabled && selectedCards().length < 2;
      }
    };

    const setReplicaSetupMode = (state) => {
      replicaSetupStates.forEach((section) => {
        section.hidden = section.dataset.replicaSetupState !== state;
      });
      replicaSetupActionGroups.forEach((group) => {
        group.hidden = group.dataset.replicaSetupActions !== state;
      });
    };

    const setReplicaSetupProgress = (percent, label, note) => {
      const normalized = Math.max(0, Math.min(100, Number(percent) || 0));
      if (replicaSetupProgressFill) replicaSetupProgressFill.style.width = `${normalized}%`;
      if (replicaSetupProgressValue) replicaSetupProgressValue.textContent = `${Math.round(normalized)}%`;
      if (replicaSetupProgressLabel && label) replicaSetupProgressLabel.textContent = label;
      if (replicaSetupProgressNote && note) replicaSetupProgressNote.textContent = note;
    };

    const revertPendingReplicaSetup = () => {
      const card = pendingReplicaSetupCard;
      pendingReplicaSetupCard = null;
      if (!card) return;
      const checkbox = checkboxFor(card);
      if (checkbox) checkbox.checked = false;
      if (nodeId(card) === String(activeInput?.value || "").trim()) {
        const fallback = selectedCards().find((item) => item !== card) || selectedCards()[0];
        if (fallback) setActiveNode(nodeId(fallback));
      }
      renderPlacementNodes();
    };

    const openReplicaSetup = (card) => {
      if (!replicaSetupModal || !replicaSetupForm) return;
      pendingReplicaSetupCard = card;
      replicaSetupPending = false;
      if (replicaSetupTarget) replicaSetupTarget.value = nodeId(card);
      if (replicaSetupNodeName) replicaSetupNodeName.textContent = nodeDisplayName(card);
      if (replicaSetupErrorText) replicaSetupErrorText.textContent = "";
      if (replicaSetupSuccessNote) replicaSetupSuccessNote.textContent = "";
      setReplicaSetupMode("form");
      setReplicaSetupProgress(0, "Setting up node", "Starting setup.");
      if (typeof replicaSetupModal.showModal === "function") {
        replicaSetupModal.showModal();
      }
    };

    const finishReplicaSetup = () => {
      if (pendingReplicaSetupCard) {
        nodeSetupComplete.add(nodeId(pendingReplicaSetupCard));
      }
      pendingReplicaSetupCard = null;
      replicaSetupModal?.close("done");
      renderPlacementNodes();
    };

    replicaSetupCancelButtons.forEach((button) => {
      button.addEventListener("click", () => {
        if (replicaSetupPending) return;
        revertPendingReplicaSetup();
        replicaSetupModal?.close("cancel");
      });
    });

    replicaSetupDone?.addEventListener("click", finishReplicaSetup);
    replicaSetupRetry?.addEventListener("click", () => {
      if (replicaSetupPending) return;
      setReplicaSetupMode("form");
      setReplicaSetupProgress(0, "Setting up node", "Starting setup.");
    });

    replicaSetupModal?.addEventListener("cancel", (event) => {
      if (replicaSetupPending) {
        event.preventDefault();
        return;
      }
      revertPendingReplicaSetup();
    });

    replicaSetupForm?.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (replicaSetupPending || !pendingReplicaSetupCard) return;
      replicaSetupPending = true;
      const firstReplicaSetupStep = markFirstOperationStep(
        "replicaSetup",
        "Setup request",
        "Reading setup request.",
      );
      setReplicaSetupMode("progress");
      setReplicaSetupProgress(
        firstReplicaSetupStep.progress,
        firstReplicaSetupStep.label,
        firstReplicaSetupStep.note,
      );
      const requestSetup = async () => fetch(formActionPath(replicaSetupForm), {
        method: "POST",
        credentials: "same-origin",
        body: new FormData(replicaSetupForm),
        headers: {
          Accept: "application/json",
          "X-Requested-With": "fetch",
        },
      });
      try {
        let response = await requestSetup();
        if (response.status === 403 && await refreshCsrfTokens()) {
          response = await requestSetup();
        }
        const { payload, text } = await readResponsePayload(response);
        if (!response.ok) {
          throw new Error(responseErrorMessage(response, payload, text, "Replica setup failed", {
            method: "POST",
            path: formActionPath(replicaSetupForm),
          }));
        }
        const operationId = payload.operation_id;
        if (!operationId) {
          replicaSetupPending = false;
          nodeSetupComplete.add(nodeId(pendingReplicaSetupCard));
          if (replicaSetupSuccessNote) replicaSetupSuccessNote.textContent = payload.message || "Node setup complete.";
          setReplicaSetupMode("success");
          setReplicaSetupProgress(100, "Node ready", payload.message || "Node setup complete.");
          return;
        }
        watchOperation(operationId, {
          intervalMs: 1000,
          onProgress: (operation) => {
            const progress = operationProgress(operation, firstReplicaSetupStep.progress, "replicaSetup");
            setReplicaSetupProgress(
              progress.progress,
              progress.phase || "Setting up node",
              progress.substate || progress.message || "Working."
            );
          },
          onSuccess: (operation) => {
            replicaSetupPending = false;
            const progress = operationProgress(operation, 100, "replicaSetup");
            nodeSetupComplete.add(nodeId(pendingReplicaSetupCard));
            if (replicaSetupSuccessNote) {
              replicaSetupSuccessNote.textContent = progress.flashSuccess || progress.message || "Node setup complete.";
            }
            setReplicaSetupMode("success");
            setReplicaSetupProgress(100, "Node ready", progress.flashSuccess || progress.message || "Node setup complete.");
          },
          onFailure: (operation) => {
            replicaSetupPending = false;
            const progress = operationProgress(operation, 100, "replicaSetup");
            if (replicaSetupErrorText) {
              replicaSetupErrorText.textContent = progress.flashError || progress.message || "Setup failed.";
            }
            setReplicaSetupMode("error");
            setReplicaSetupProgress(100, "Setup failed", progress.flashError || progress.message || "Setup failed.");
          },
          onError: (error) => {
            replicaSetupPending = false;
            const message = error instanceof Error ? error.message : "Setup failed.";
            if (replicaSetupErrorText) replicaSetupErrorText.textContent = message;
            setReplicaSetupMode("error");
            setReplicaSetupProgress(100, "Setup failed", message);
          },
        }).catch((error) => {
          replicaSetupPending = false;
          const message = error instanceof Error ? error.message : "Setup failed.";
          if (replicaSetupErrorText) replicaSetupErrorText.textContent = message;
          setReplicaSetupMode("error");
          setReplicaSetupProgress(100, "Setup failed", message);
        });
      } catch (error) {
        replicaSetupPending = false;
        const message = error instanceof Error ? error.message : "Setup failed.";
        if (replicaSetupErrorText) replicaSetupErrorText.textContent = message;
        setReplicaSetupMode("error");
        setReplicaSetupProgress(100, "Setup failed", message);
      }
    });

    resumeReplicaSetupOperation = (operation) => {
      if (!replicaSetupModal || replicaSetupPending) return;
      const details = operation?.details || {};
      const targetNode = String(details.target_node || details.target_node_uid || "").trim();
      pendingReplicaSetupCard = targetNode
        ? nodeCards.find((card) => nodeId(card) === targetNode) || null
        : null;
      replicaSetupPending = true;
      const firstReplicaSetupStep = markFirstOperationStep(
        "replicaSetup",
        "Setup request",
        "Reading setup request.",
      );
      if (replicaSetupTarget && targetNode) replicaSetupTarget.value = targetNode;
      if (replicaSetupNodeName) {
        replicaSetupNodeName.textContent = pendingReplicaSetupCard
          ? nodeDisplayName(pendingReplicaSetupCard)
          : "selected node";
      }
      if (replicaSetupErrorText) replicaSetupErrorText.textContent = "";
      if (replicaSetupSuccessNote) replicaSetupSuccessNote.textContent = "";
      setReplicaSetupMode("progress");
      setReplicaSetupProgress(
        firstReplicaSetupStep.progress,
        firstReplicaSetupStep.label,
        firstReplicaSetupStep.note,
      );
      openProgressDialog(replicaSetupModal);
      watchOperation(operation.id, {
        intervalMs: 1000,
        onProgress: (payload) => {
          const progress = operationProgress(payload, firstReplicaSetupStep.progress, "replicaSetup");
          setReplicaSetupProgress(
            progress.progress,
            progress.phase || "Setting up node",
            progress.substate || progress.message || "Working."
          );
        },
        onSuccess: (payload) => {
          replicaSetupPending = false;
          const progress = operationProgress(payload, 100, "replicaSetup");
          if (pendingReplicaSetupCard) nodeSetupComplete.add(nodeId(pendingReplicaSetupCard));
          if (replicaSetupSuccessNote) {
            replicaSetupSuccessNote.textContent = progress.flashSuccess || progress.message || "Node setup complete.";
          }
          setReplicaSetupMode("success");
          setReplicaSetupProgress(100, "Node ready", progress.flashSuccess || progress.message || "Node setup complete.");
        },
        onFailure: (payload) => {
          replicaSetupPending = false;
          const progress = operationProgress(payload, 100, "replicaSetup");
          if (replicaSetupErrorText) {
            replicaSetupErrorText.textContent = progress.flashError || progress.message || "Setup failed.";
          }
          setReplicaSetupMode("error");
          setReplicaSetupProgress(100, "Setup failed", progress.flashError || progress.message || "Setup failed.");
        },
        onError: (error) => {
          replicaSetupPending = false;
          const message = error instanceof Error ? error.message : "Setup failed.";
          if (replicaSetupErrorText) replicaSetupErrorText.textContent = message;
          setReplicaSetupMode("error");
          setReplicaSetupProgress(100, "Setup failed", message);
        },
      }).catch((error) => {
        replicaSetupPending = false;
        const message = error instanceof Error ? error.message : "Setup failed.";
        if (replicaSetupErrorText) replicaSetupErrorText.textContent = message;
        setReplicaSetupMode("error");
        setReplicaSetupProgress(100, "Setup failed", message);
      });
    };

    enabledToggle?.addEventListener("change", () => {
      ensureSelectedNode();
      renderPlacementNodes();
    });

    modeButtons.forEach((button) => {
      button.addEventListener("click", () => {
        setMode(button.dataset.placementMode);
        renderPlacementNodes();
      });
    });

    interfaceAddButton?.addEventListener("click", addInterfaceRow);
    interfaceRows().forEach((row) => {
      row.querySelector("[data-placement-interface-remove]")?.addEventListener("click", () => removeInterfaceRow(row));
      row.querySelector("[data-placement-interface-direction]")?.addEventListener("change", () => updateInterfaceMeta(row));
      row.querySelector("[data-placement-interface-target]")?.addEventListener("change", () => updateInterfaceMeta(row));
    });
    inboundRows().forEach((row) => {
      row.querySelector("[data-placement-interface-inbound-direction]")?.addEventListener("change", () => {
        updateInboundInterfaceMeta(row);
        if (row.dataset.inboundStatus === "accepted") {
          submitInboundInterfaceAction(row, "accepted");
        }
      });
      row.querySelectorAll("[data-placement-interface-inbound-action]").forEach((button) => {
        button.addEventListener("click", () => submitInboundInterfaceAction(row, button.dataset.placementInterfaceInboundAction));
      });
    });

    nodeCards.forEach((card) => {
      card.addEventListener("click", (event) => {
        event.preventDefault();
        if (!enabledToggle?.checked) return;
        const checkbox = checkboxFor(card);
        if (!checkbox) return;
        const wasChecked = checkbox.checked;
        const currentMode = mode();
        if (currentMode === "live") {
          checkbox.checked = !(checkbox.checked && selectedCards().length <= 1);
          if (checkbox.checked) setActiveNode(nodeId(card));
        } else if (nodeId(card) === String(activeInput?.value || "").trim()) {
          const fallback = selectedCards().find((item) => item !== card);
          if (fallback) setActiveNode(nodeId(fallback));
          checkbox.checked = true;
        } else {
          checkbox.checked = true;
          setActiveNode(nodeId(card));
        }
        renderPlacementNodes();
        if (!wasChecked && checkbox.checked && !nodeSetupComplete.has(nodeId(card))) {
          openReplicaSetup(card);
        }
      });
    });

    setMode(mode());
    renderPlacementNodes();
  };

  bindPlacementForm();

  document.querySelectorAll(".toggle-switch").forEach((sw) => {
    const cb = sw.querySelector("input[type='checkbox']");
    if (!cb) return;
    const sync = () => sw.classList.toggle("is-on", cb.checked);
    cb.addEventListener("change", sync);
    sync();
  });

  const refreshBackupSignals = async ({ loadingMessage = "Refreshing backup history..." } = {}) => {
    if (!summaryRows || !historyBlock) return;
    const requestId = ++backupSignalsRequestId;
    if (signalsNote) {
      signalsNote.hidden = false;
      signalsNote.textContent = loadingMessage;
    }
    let payload;
    try {
      const response = await fetch(`/api/backends/${backendId}/backup-signals`, {
        headers: { Accept: "application/json" },
      });
      if (requestId !== backupSignalsRequestId) return { stale: true };
      if (!response.ok) throw new Error(`backup signals request failed: ${response.status}`);
      payload = await response.json();
      if (requestId !== backupSignalsRequestId) return { stale: true };
    } catch (error) {
      if (requestId !== backupSignalsRequestId) return { stale: true };
      throw error;
    }
    const backupSummaryRows = Array.isArray(payload.backup_summary_rows) ? payload.backup_summary_rows : [];
    const backupHistory = Array.isArray(payload.backup_history) ? payload.backup_history : [];

    summaryRows.innerHTML = backupSummaryRows.map(([key, value]) => `
      <div class="settings-line">
        <span class="settings-key">${escapeHtml(key)}</span>
        <span class="settings-value mono">${escapeHtml(value)}</span>
      </div>
    `).join("");

    if (backupHistoryCount) {
      backupHistoryCount.textContent = `${backupHistory.length} saved`;
    }

    if (!backupHistory.length) {
      historyBlock.innerHTML = '<p class="subtle">No backups yet.</p>';
    } else {
      historyBlock.innerHTML = `
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>ID</th><th>Created</th><th>Status</th><th>Size</th><th>Covered paths</th><th>File</th><th>Action</th>
              </tr>
            </thead>
            <tbody>
              ${backupHistory.map((item) => `
                <tr>
                  <td class="mono">#${escapeHtml(item.id)}</td>
                  <td class="mono" data-local-time="${escapeHtml(item.created_at_raw || "")}">${escapeHtml(formatLocalTimestamp(item.created_at_raw || item.created_at) || item.created_at)}</td>
                  <td><span class="pill ${escapeHtml(item.tone)}">${escapeHtml(item.status)}</span></td>
                  <td class="mono">${escapeHtml(item.size)}</td>
                  <td>${escapeHtml(item.covered_paths_summary)}</td>
                  <td class="mono">${escapeHtml(item.bundle)}</td>
                  <td>
                    <div class="backup-action-buttons">
                      <form method="post" action="/ui/backends/${backendId}/restore" data-restore-form>
                        <input type="hidden" name="csrf_token" value="${escapeHtml(csrfToken)}">
                        <input type="hidden" name="backup_id" value="${escapeHtml(item.id)}">
                        <button type="submit" class="ghost"${item.restorable ? "" : " disabled"}>Restore</button>
                      </form>
                      <form method="post" action="/ui/backends/${backendId}/backups/${escapeHtml(item.id)}/delete" data-delete-backup-form data-backup-id="${escapeHtml(item.id)}" data-backup-label="#${escapeHtml(item.id)}" data-backup-file="${escapeHtml(item.bundle)}">
                        <input type="hidden" name="csrf_token" value="${escapeHtml(csrfToken)}">
                        <button type="button" class="danger" data-delete-backup-trigger>Delete</button>
                      </form>
                    </div>
                  </td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      `;
    }

    if (signalsNote) signalsNote.hidden = true;
    return { stale: false };
  };

  if (backupForm && backupSubmit && backupProgressShell) {
    backupForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (backupPending) return;
      try {
        await startBackupLikeOperation({
          form: backupForm,
          submitter: backupSubmit,
          label: "Creating backup",
          startedMessage: "Backup requested.",
          failedMessage: "Backup failed.",
          showSuccessBanner: false,
          pipeline: "backup",
        });
      } catch (error) {
        renderFlashBanner("error", error instanceof Error ? error.message : "Backup failed.");
      }
    });
  }

  const importBackupForm = document.querySelector("[data-import-backup-form]");
  if (importBackupForm) {
    importBackupForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const submitter = event.submitter || importBackupForm.querySelector("button[type='submit']");
      await startBackupLikeOperation({
        form: importBackupForm,
        submitter,
        label: "Importing backup",
        startedMessage: "Uploading backup bundle.",
        failedMessage: "Import failed.",
        pipeline: "importBackup",
      });
    });
  }

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.matches("[data-restore-form]")) return;
    event.preventDefault();
    const submitter = event.submitter || form.querySelector("button[type='submit']");
    await startBackupLikeOperation({
      form,
      submitter,
      label: "Restoring backup",
      startedMessage: "Starting restore.",
      failedMessage: "Restore failed.",
      pipeline: "restore",
    });
  });

  document.addEventListener("click", (event) => {
    const trigger = event.target instanceof Element ? event.target.closest("[data-delete-backup-trigger]") : null;
    if (!trigger) return;
    const form = trigger.closest("[data-delete-backup-form]");
    if (!(form instanceof HTMLFormElement)) return;
    event.preventDefault();
    if (backupPending) return;
    openDeleteBackupModal(form);
  });

  resumeOutputOperations();

  if (!summaryRows || !historyBlock) return;

  afterInitialLoad(() => refreshBackupSignals({ loadingMessage: "Loading backup details." })
    .catch(() => {
      if (signalsNote) {
        signalsNote.hidden = false;
        signalsNote.textContent = "Backup details are still loading.";
      }
    }));
})();
