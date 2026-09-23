(() => {
  const setup = document.querySelector('#cloudflare-setup');
  const previewDialog = document.querySelector('#cloudflare-preview');
  if (!setup || !previewDialog) return;
  let preview = null;
  let operationId = null;
  const csrf = () => setup.querySelector('[name="csrf_token"]').value;
  const message = (element, text, error = false) => { element.textContent = text; element.classList.toggle('is-error', error); };
  const post = async (url, data) => {
    const response = await fetch(url, { method: 'POST', body: data, headers: { Accept: 'application/json' }, signal: AbortSignal.timeout(60000) });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || 'The request could not be completed.');
    return body;
  };
  const connectionAction = async (action, token = '') => {
    const data = new FormData();
    data.set('csrf_token', csrf()); data.set('action', action); data.set('api_token', token);
    return post('/ui/settings/cloudflare', data);
  };
  document.addEventListener('click', async event => {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.matches('[data-cf-open]')) {
      setup.querySelector('[name="api_token"]').value = '';
      message(setup.querySelector('[data-cf-connect-message]'), '');
      setup.showModal();
    }
    if (button.matches('[data-cf-close]')) {
      if (operationId) { window.location.assign('/?tab=inputs'); return; }
      button.closest('dialog').close();
    }
    if (button.matches('[data-cf-retest], [data-cf-disconnect]')) {
      const output = button.closest('[data-cloudflare-settings]').querySelector('[data-cf-settings-message]');
      button.disabled = true;
      message(output, button.matches('[data-cf-retest]') ? 'Testing connection…' : 'Disconnecting…');
      try { await connectionAction(button.matches('[data-cf-retest]') ? 'retest' : 'disconnect'); window.location.assign('/?tab=settings'); }
      catch (error) { message(output, error.message, true); }
      finally { button.disabled = false; }
    }
    if (button.matches('[data-cf-apply]') && preview) {
      const output = previewDialog.querySelector('[data-cf-preview-message]');
      button.disabled = true;
      const data = new FormData(); data.set('csrf_token', csrf()); data.set('preview_id', preview.preview_id);
      try {
        const result = await post(`/ui/inputs/${preview.input_id}/cloudflare/apply`, data);
        operationId = result.operation_id;
        message(output, 'Preparing the changes…');
        while (previewDialog.open) {
          const response = await fetch(`/api/operations/${operationId}`, { headers: { Accept: 'application/json' }, signal: AbortSignal.timeout(15000) });
          if (!response.ok) throw new Error('Could not read progress. Reopen the Input to check the result.');
          const status = await response.json();
          message(output, status.error || status.details?.message || status.phase || status.status, ['failed', 'partial', 'cancelled'].includes(status.status));
          if (!['queued', 'running'].includes(status.status)) {
            previewDialog.querySelectorAll('[data-cf-close]').forEach(control => { if (control.textContent.trim() === 'Cancel') control.textContent = 'Done'; });
            button.hidden = true;
            break;
          }
          await new Promise(resolve => setTimeout(resolve, 750));
        }
      } catch (error) { message(output, error.message, true); button.disabled = false; }
    }
  });
  document.addEventListener('change', event => {
    if (!event.target.matches('[data-cf-domain-form] [name="manage_dns"]')) return;
    const field = event.target.form.querySelector('[data-cf-ipv4-field]');
    field.hidden = !event.target.checked;
    field.querySelector('input').disabled = !event.target.checked;
    field.querySelector('input').required = event.target.checked;
  });
  document.addEventListener('submit', async event => {
    const form = event.target;
    if (!form.matches('[data-cf-connect-form], [data-cf-domain-form]')) return;
    event.preventDefault();
    const connect = form.matches('[data-cf-connect-form]');
    const output = form.querySelector(connect ? '[data-cf-connect-message]' : '[data-cf-domain-message]');
    const button = form.querySelector('[type="submit"]');
    button.disabled = true;
    message(output, connect ? 'Testing token and domain access…' : 'Checking the current configuration…');
    try {
      if (connect) {
        const result = await connectionAction('connect', form.elements.api_token.value);
        form.elements.api_token.value = '';
        message(output, result.message);
        window.location.assign('/?tab=settings');
      } else {
        const result = await post(`/ui/inputs/${form.dataset.inputId}/cloudflare/preview`, new FormData(form));
        preview = result.preview; operationId = null;
        previewDialog.querySelector('[data-cf-preview-host]').textContent = preview.hostname;
        previewDialog.querySelector('[data-cf-preview-certificate]').textContent = preview.certificate;
        previewDialog.querySelector('[data-cf-preview-before]').textContent = preview.dns?.before || 'Unchanged';
        previewDialog.querySelector('[data-cf-preview-after]').textContent = preview.dns?.after || preview.dns_note;
        message(previewDialog.querySelector('[data-cf-preview-message]'), preview.conflict || '', Boolean(preview.conflict));
        const apply = previewDialog.querySelector('[data-cf-apply]'); apply.hidden = false; apply.disabled = Boolean(preview.conflict);
        previewDialog.querySelectorAll('[data-cf-close]').forEach(control => { if (control.textContent.trim() === 'Done') control.textContent = 'Cancel'; });
        message(output, ''); previewDialog.showModal();
      }
    } catch (error) { message(output, error.message, true); }
    finally { button.disabled = false; }
  });
})();
