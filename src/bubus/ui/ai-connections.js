(() => {
  'use strict';
  const panel = document.getElementById('ai-connections');
  const cards = panel.querySelector('.ai-providers');
  const active = panel.querySelector('#ai-active');
  const editor = panel.querySelector('#ai-editor');
  const key = editor.elements.key;
  const model = editor.elements.model;
  const file = panel.querySelector('#ai-file');
  const notice = panel.querySelector('#ai-notice');
  const endpoint = '/api/desk/ai-connections';
  let state, editing, imported;

  const button = (label, run) => {
    const el = document.createElement('button');
    el.type = 'button'; el.className = 'button button-white'; el.textContent = label;
    el.addEventListener('click', run);
    return el;
  };

  async function request(data) {
    const options = {cache: 'no-store'};
    if (data) Object.assign(options, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
    const response = await fetch(endpoint, options);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Impossible de joindre BUBUS.');
    return result;
  }

  async function update(data, message) {
    panel.setAttribute('aria-busy', 'true');
    try {
      render(await request(data));
      editor.hidden = true; key.value = ''; imported = null;
      document.getElementById('ai-import-save').hidden = true;
      notice.textContent = message;
      document.dispatchEvent(new Event('bubus-ai-changed'));
    } catch (error) {
      notice.textContent = error.message;
      if (state) active.value = state.selected;
    } finally { panel.removeAttribute('aria-busy'); }
  }

  function edit(row) {
    editing = row;
    document.getElementById('ai-provider-name').textContent = row.label;
    editor.hidden = false; key.value = '';
    key.placeholder = row.saved ? 'Nouvelle clé API' : 'Colle ta clé API';
    model.closest('label').hidden = !row.chat; model.value = row.model;
    document.getElementById('ai-forget').hidden = !row.saved;
    const inspect = document.getElementById('ai-inspect');
    inspect.hidden = row.chat || !row.configured;
    inspect.textContent = row.id === 'cursor' ? 'Vérifier l’accès Cursor' : 'Afficher mes voix';
    key.focus(); notice.textContent = '';
  }

  function render(result) {
    state = result;
    cards.replaceChildren(); active.replaceChildren(new Option('Choix automatique', ''));
    for (const row of state.providers) {
      const card = button(row.label, () => edit(row));
      const info = document.createElement('small');
      info.textContent = row.id === state.selected ? 'Utilisé dans Discussion' : row.configured ? 'Clé disponible' : 'Ajouter une clé';
      card.append(info); cards.append(card);
      card.disabled = !state.writable;
      if (row.chat && row.configured) active.add(new Option(row.label, row.id));
    }
    active.value = state.selected; active.disabled = !state.writable;
    file.disabled = !state.writable;
    if (!state.writable) notice.textContent = 'Le coffre de cet appareil est indisponible.';
  }

  editor.addEventListener('submit', event => {
    event.preventDefault();
    const record = {key: key.value.trim(), model: model.value.trim()};
    update({action: 'save', keys: {[editing.id]: record}}, 'Clé enregistrée pour ' + editing.label + '.');
  });
  document.getElementById('ai-forget').onclick = () => update({action: 'forget', provider: editing.id}, 'Clé retirée de BUBUS.');
  document.getElementById('ai-inspect').onclick = async () => {
    try { notice.textContent = (await request({action: 'inspect', provider: editing.id})).message; }
    catch (error) { notice.textContent = error.message; }
  };
  active.onchange = () => update({action: 'select', provider: active.value}, 'Fournisseur de discussion mis à jour.');

  // Seules les lignes immédiatement précédées d’un nom de fournisseur reconnu sont examinées.
  // Un mot de passe, une URL de compte ou une ligne de texte libre ne constitue pas une clé.
  function importKeys(text) {
    const formats = [
      ['openrouter', /^(openrouter|openrouteur)$/, /^sk-or-[\w-]{20,200}$/],
      ['openai', /^(openai|openia)$/, /^sk-(?:proj-|svcacct-)?[\w-]{20,300}$/],
      ['claude', /^(claude|anthropic)$/, /^sk-ant-[\w-]{20,200}$/],
      ['deepseek', /^deepseek$/, /^sk-[a-f\d]{32,128}$/i],
      ['kimi', /^(kimi|moonshot)$/, /^sk-[\w-]{24,200}$/],
      ['zai', /^(z\.?ai|zcode)$/, /^[\da-f]{32}\.[\w-]{12,128}$/i],
      ['groq', /^groq(?: free)?$/, /^gsk_[\w-]{24,200}$/],
      ['elevenlabs', /^elevenlabs$/, /^sk_[\w-]{24,200}$/],
      ['cursor', /^cursor$/, /^[\w-]{40,200}$/]
    ];
    const lines = text.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    const found = {};
    for (let i = 0; i + 1 < lines.length; i++) {
      const label = lines[i].toLowerCase().replace(/\b(api|key|clé)\b/g, '').replace(/[:=]/g, '').trim();
      const format = formats.find(item => item[1].test(label));
      if (!format) continue;
      const candidate = lines[i + 1].trim();
      if (format[2].test(candidate)) found[format[0]] = {key: candidate, model: ''};
    }
    return found;
  }

  const saveImport = document.getElementById('ai-import-save');
  file.onchange = async () => {
    imported = null; saveImport.hidden = true;
    const selected = file.files[0];
    if (!selected) return;
    try {
      if (selected.size > 262144) throw new Error('Choisis un fichier texte de moins de 256 Ko.');
      imported = importKeys(await selected.text());
      const matches = state.providers.filter(row => imported[row.id]);
      notice.textContent = matches.length ? matches.map(row => row.label).join(' · ') : 'Aucune clé IA reconnue dans ce fichier.';
      saveImport.textContent = 'Enregistrer les clés (' + matches.length + ')';
      saveImport.hidden = !matches.length;
    } catch (error) { notice.textContent = error.message; }
    finally { file.value = ''; }
  };
  saveImport.onclick = () => imported && update({action: 'save', keys: imported}, 'Clés IA enregistrées.');
  request().then(render).catch(error => {notice.textContent = error.message;});
})();
