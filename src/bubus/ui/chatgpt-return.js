(async () => {
  const params = new URLSearchParams(location.search);
  history.replaceState(null, '', location.pathname);
  const data = {};
  for (const key of ['state', 'code', 'error', 'client_id']) {
    if (params.has(key)) data[key] = params.get(key);
  }
  const status = document.getElementById('status');
  try {
    const response = await fetch('/api/desk/chatgpt/finish', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'La connexion a échoué.');
    location.replace('/#chat');
  } catch (error) {
    status.textContent = error instanceof TypeError
      ? 'Connexion interrompue. Reviens dans la discussion pour réessayer.'
      : error.message;
  }
})();
