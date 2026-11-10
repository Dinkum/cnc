(() => {
  const dialog = document.querySelector('#cloudflare-tunnel-setup');
  if (!dialog) return;
  const form = dialog.querySelector('form');
  const message = (node, text, error = false) => {
    node.textContent = text;
    node.classList.toggle('is-error', error);
  };
  const configure = async (action, token = '') => {
    const data = new FormData();
    data.set('csrf_token', form.elements.csrf_token.value);
    data.set('action', action);
    data.set('tunnel_token', token);
    const response = await fetch('/ui/settings/cloudflare-tunnel', {
      method: 'POST', body: data, headers: { Accept: 'application/json' },
      signal: AbortSignal.timeout(120000),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || 'The connector could not be configured.');
    window.location.assign('/?tab=settings');
  };
  document.addEventListener('click', async event => {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.matches('[data-tunnel-open]')) {
      form.reset();
      message(form.querySelector('[data-tunnel-connect-message]'), '');
      dialog.showModal();
    } else if (button.matches('[data-tunnel-close]')) {
      form.reset();
      dialog.close();
    } else if (button.matches('[data-tunnel-retest], [data-tunnel-disconnect]')) {
      const output = button.closest('[data-tunnel-settings]').querySelector('[data-tunnel-message]');
      button.disabled = true;
      message(output, 'Checking connector…');
      try { await configure(button.matches('[data-tunnel-retest]') ? 'retest' : 'disconnect'); }
      catch (error) { message(output, error.message, true); }
      finally { button.disabled = false; }
    }
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('[type="submit"]');
    const output = form.querySelector('[data-tunnel-connect-message]');
    button.disabled = true;
    message(output, 'Starting and checking connector…');
    try { await configure('connect', form.elements.tunnel_token.value); }
    catch (error) { message(output, error.message, true); }
    finally { form.elements.tunnel_token.value = ''; button.disabled = false; }
  });
})();
