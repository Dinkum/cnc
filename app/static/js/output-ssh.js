(() => {
  const $ = id => document.getElementById(id);
  if (!$('key-dialog')) return;
  const config = JSON.parse($('output-detail-config').textContent);
  const endpoint = `/api/backends/${config.backendId}/ssh-keys`;
  let keys = [], mode = 'create', pendingRevoke = null, connection = null, busy = false;

  async function request(url, fields) {
    const response = await fetch(url, fields ? {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      body: new URLSearchParams({csrf_token: config.csrfToken, ...fields})
    } : {credentials: 'same-origin', cache: 'no-store'});
    const payload = await response.json();
    if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'SSH access update failed. Check the key list before trying again.');
    return payload;
  }

  function renderKeys() {
    $('keys').replaceChildren();
    for (const key of keys) {
      const row = document.createElement('div'); row.className = 'ssh-key-row';
      const info = document.createElement('div');
      const name = document.createElement('div'); name.className = 'ssh-key-name'; name.textContent = key.name;
      const fingerprint = document.createElement('div'); fingerprint.className = 'ssh-fingerprint'; fingerprint.textContent = key.fingerprint;
      info.append(name, fingerprint);
      const date = document.createElement('div'); date.className = 'ssh-date';
      const label = document.createElement('span'); label.textContent = 'Created';
      date.append(label, new Date(key.created_at).toLocaleDateString(undefined, {year: 'numeric', month: 'short', day: 'numeric'}));
      const revoke = document.createElement('button'); revoke.type = 'button'; revoke.className = 'ghost ssh-revoke';
      revoke.textContent = 'Revoke'; revoke.setAttribute('aria-label', `Revoke ${key.name}`);
      revoke.onclick = () => { pendingRevoke = key; $('revoke-name').textContent = key.name; $('revoke-error').hidden = true; $('revoke-dialog').showModal(); };
      row.append(info, date, revoke); $('keys').append(row);
    }
    if (!keys.length) {
      const empty = document.createElement('div'); empty.className = 'ssh-empty';
      empty.textContent = 'No SSH keys. Create a key or add your device’s public key.'; $('keys').append(empty);
    }
    $('key-count').textContent = keys.length;
  }

  async function refreshKeys() { keys = (await request(endpoint)).keys; renderKeys(); }
  function showError(id, error) { $(id).textContent = error.message; $(id).hidden = false; }
  function openForm(nextMode) {
    mode = nextMode; $('key-form').reset(); $('form-error').hidden = true;
    const creating = mode === 'create';
    $('dialog-title').textContent = creating ? 'Create SSH key' : 'Add public key';
    $('public-field').hidden = creating; $('public-key').required = !creating; $('create-note').hidden = !creating;
    $('submit-key').textContent = creating ? 'Create & download' : 'Add public key';
    $('key-dialog').showModal(); $('key-name').focus();
  }
  $('create-open').onclick = () => openForm('create');
  $('import-open').onclick = () => openForm('import');
  document.querySelectorAll('.ssh-dialog .close-dialog').forEach(button => button.onclick = () => { if (!busy) $('key-dialog').close(); });
  for (const id of ['key-dialog', 'revoke-dialog']) $(id).addEventListener('cancel', event => { if (busy) event.preventDefault(); });

  $('key-form').onsubmit = async event => {
    event.preventDefault(); if (busy) return;
    busy = true; $('submit-key').disabled = true; $('form-error').hidden = true;
    try {
      const payload = await request(endpoint, {name: $('key-name').value.trim(), mode, public_key: $('public-key').value});
      keys.push(payload.key); renderKeys(); $('key-dialog').close();
      $('notice').textContent = `${payload.key.name} ${mode === 'create' ? 'created' : 'added'}.`;
      if (payload.private_key) {
        connection = {...payload.key, alias: `${config.backendName}-${payload.key.id}`};
        $('key-location').value = `~/Downloads/${connection.filename}`;
        updateConnectionInstructions();
        const url = URL.createObjectURL(new Blob([payload.private_key], {type: 'application/octet-stream'}));
        delete payload.private_key;
        const link = document.createElement('a'); link.href = url; link.download = connection.filename;
        document.body.append(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        $('connect-dialog').showModal();
      }
    } catch (error) {
      showError('form-error', error);
      // A lost response may follow a completed create. Never retry it automatically.
      refreshKeys().catch(() => {});
    } finally { busy = false; $('submit-key').disabled = false; }
  };
  $('revoke-cancel').onclick = () => { if (!busy) $('revoke-dialog').close(); };
  $('revoke-form').onsubmit = async event => {
    event.preventDefault(); if (busy || !pendingRevoke) return;
    busy = true; $('revoke-confirm').disabled = true;
    try {
      await request(`${endpoint}/${pendingRevoke.id}/revoke`, {});
      keys = keys.filter(key => key.id !== pendingRevoke.id); renderKeys();
      $('notice').textContent = `${pendingRevoke.name} revoked.`; $('revoke-dialog').close();
    } catch (error) { showError('revoke-error', error); }
    finally { busy = false; $('revoke-confirm').disabled = false; }
  };

  // Quote editable paths as shell arguments while allowing a leading ~/ shortcut.
  function keyPathArgument(value) {
    const quote = text => "'" + text.replaceAll("'", "'\\''") + "'";
    if (/^~\/[a-zA-Z0-9_./-]+$/.test(value)) return value;
    if (value.startsWith('~/')) return '~/' + quote(value.slice(2));
    return quote(value);
  }
  function updateConnectionInstructions() {
    if (!connection) return;
    const location = $('key-location').value.trim();
    const installedKey = `~/.ssh/${connection.filename}`;
    // Config directives and the heredoc must never receive arbitrary host/name text.
    if (!/^[a-zA-Z0-9][a-zA-Z0-9.:-]*$/.test(config.sshHost) || !/^[a-zA-Z0-9_-]+$/.test(config.backendName)) {
      $('connect-feedback').textContent = 'Configure a valid SSH hostname to show connection instructions.'; return;
    }
    const directConnection = `ssh -i ${installedKey} ${config.backendName}@${config.sshHost}`;
    $('key-command').textContent = location ? `mkdir -p ~/.ssh &&\ncp -- ${keyPathArgument(location)} ${installedKey} &&\nchmod 600 ${installedKey}` : 'Enter the downloaded key file location above.';
    $('shortcut-command').textContent = `cat >> ~/.ssh/config <<'EOF'

Host ${connection.alias}
    HostName ${config.sshHost}
    User ${config.backendName}
    IdentityFile ${installedKey}
    IdentitiesOnly yes
    IdentityAgent none
    ForwardAgent no
    ConnectTimeout 10
EOF`;
    $('setup-command').textContent = `ssh ${connection.alias}`;
    $('agent-instructions').textContent = `## Production access — CNC

${config.backendName} production runs inside a CNC-managed app container.
Connect over Tailscale using the private key at ${installedKey}.

Read connection guidance:
${directConnection} llm-help

Open the app’s shell:
${directConnection}

Run an app command:
${directConnection} '<command>'

Keep work inside the app container and within the current task’s scope. Do not use CNC host/root access.

If the app’s shell hangs, stop further connection attempts and report the failure for CNC operator recovery.

Never commit or share the private SSH key.`;
    $('copy-key').disabled = !location; $('connect-feedback').textContent = '';
  }
  $('connect-close').onclick = () => $('connect-dialog').close();
  $('key-location').addEventListener('input', updateConnectionInstructions);
  for (const [id, target] of [['copy-key','key-command'], ['copy-shortcut','shortcut-command'], ['copy-setup','setup-command'], ['copy-agent','agent-instructions']]) {
    $(id).onclick = async () => {
      try { await navigator.clipboard.writeText($(target).textContent); $('connect-feedback').textContent = id === 'copy-agent' ? 'Agent instructions copied.' : 'Command copied.'; }
      catch { $('connect-feedback').textContent = 'Copy failed. Select and copy the instructions above.'; }
    };
  }
  refreshKeys().catch(error => { $('keys').textContent = 'SSH keys could not be loaded. Reload to try again.'; $('notice').textContent = error.message; });
})();
