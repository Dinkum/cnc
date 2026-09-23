(() => {
  const selector = '[data-output-settings-section]';
  const states = new WeakMap();
  const controls = form => Array.from(form.querySelectorAll('input[name], select[name], textarea[name]')).filter(control => control.name !== 'csrf_token');
  const value = control => ['checkbox', 'radio'].includes(control.type) ? control.checked : control.value;
  const fingerprint = form => JSON.stringify(controls(form).map(control => [control.name, value(control)]));
  const snapshot = form => ({
    fingerprint: fingerprint(form),
    fields: controls(form).map(control => ({ control, value: value(control) })),
    lists: Array.from(form.querySelectorAll('[data-output-tags], [data-placement-interface-list]')).map(list => ({ list, children: Array.from(list.childNodes) })),
  });
  const stateFor = form => {
    if (!states.has(form)) states.set(form, { saved: snapshot(form), pending: null, busy: false, result: '', error: '' });
    return states.get(form);
  };
  const sync = form => {
    const state = stateFor(form);
    const dirty = fingerprint(form) !== state.saved.fingerprint;
    form.dataset.dirty = String(dirty);
    form.classList.toggle('has-unsaved-changes', dirty);
    const feedback = form.querySelector('[data-section-feedback]');
    const actions = form.querySelector('[data-section-actions]');
    if (actions) actions.hidden = !(dirty || state.busy || state.error);
    if (feedback) {
      const kind = state.error ? 'error' : state.busy ? 'saving' : dirty ? 'dirty' : state.result ? 'saved' : '';
      feedback.dataset.state = kind;
      feedback.textContent = state.error || (state.busy ? 'Saving…' : dirty ? 'Unsaved changes' : state.result ? 'Saved' : '');
    }
    const cancel = form.querySelector('[data-section-cancel]');
    if (cancel) cancel.disabled = state.busy;
  };
  const conditionalFields = form => {
    const mode = form.querySelector('[name="healthcheck_mode"]');
    if (mode) form.querySelectorAll('[data-http-health-field]').forEach(row => { row.hidden = mode.value !== 'http'; });
  };
  const begin = form => {
    if (!form?.matches(selector)) return;
    const state = stateFor(form);
    state.pending = snapshot(form);
    state.busy = true; state.error = ''; state.result = '';
    sync(form);
  };
  const acceptValues = (form, values) => {
    if (!form?.matches(selector) || !values) return;
    const state = stateFor(form);
    const baseline = state.pending || state.saved;
    baseline.fields.forEach(field => {
      const key = field.control.name;
      if (!Object.prototype.hasOwnProperty.call(values, key)) return;
      const canonical = ['checkbox', 'radio'].includes(field.control.type) ? Boolean(values[key]) : String(values[key] ?? '');
      if (value(field.control) === field.value) {
        if (['checkbox', 'radio'].includes(field.control.type)) field.control.checked = canonical;
        else field.control.value = canonical;
      }
      field.value = canonical;
    });
    baseline.fingerprint = JSON.stringify(baseline.fields.map(field => [field.control.name, field.value]));
  };
  const render = (form, { kind, step }) => {
    if (!form?.matches(selector)) return false;
    const state = stateFor(form);
    if (kind === 'error') {
      state.busy = false; state.pending = null; state.error = step || 'Save failed.';
    } else if (kind === 'success') {
      // A submitted snapshot is the saved baseline. Edits made while it was
      // running remain dirty rather than being falsely reported as saved.
      state.saved = state.pending || state.saved;
      state.pending = null; state.busy = false; state.error = ''; state.result = 'saved';
      if (form.dataset.outputSettingsSection === 'shield' && form.querySelector('[name="shield_access_code"]')?.value.trim()) form.dataset.shieldCodeConfigured = 'true';
    } else if (kind === 'running') {
      state.busy = true;
    }
    sync(form);
    return true;
  };
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll(selector).forEach(form => {
      stateFor(form); conditionalFields(form); sync(form);
      const changed = () => { conditionalFields(form); sync(form); };
      form.addEventListener('input', changed);
      form.addEventListener('change', changed);
      // Tag pickers and placement controls also mutate hidden inputs.
      form.addEventListener('click', () => queueMicrotask(changed));
      form.querySelector('[data-section-cancel]')?.addEventListener('click', () => {
        const state = stateFor(form);
        if (state.busy) return;
        state.saved.lists.forEach(({ list, children }) => list.replaceChildren(...children));
        state.saved.fields.forEach(({ control, value: savedValue }) => {
          if (['checkbox', 'radio'].includes(control.type)) control.checked = savedValue;
          else control.value = savedValue;
        });
        state.error = ''; state.result = '';
        form.dispatchEvent(new Event('output-section-restored'));
        conditionalFields(form); sync(form);
      });
    });
  });
  window.addEventListener('beforeunload', event => {
    const pending = Array.from(document.querySelectorAll(selector)).some(form => stateFor(form).busy || fingerprint(form) !== stateFor(form).saved.fingerprint);
    if (pending) { event.preventDefault(); event.returnValue = ''; }
  });
  document.addEventListener('click', event => {
    document.querySelectorAll('.output-actions-menu[open]').forEach(menu => {
      if (!menu.contains(event.target) || event.target.closest('button')) menu.open = false;
    });
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') document.querySelectorAll('.output-actions-menu[open]').forEach(menu => { menu.open = false; menu.querySelector('summary').focus(); });
  });
  window.CNCOutputSettings = { begin, render, sync, acceptValues };
})();
