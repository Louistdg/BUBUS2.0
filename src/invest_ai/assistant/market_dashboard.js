(() => {
  const api = async (path, options = {}) => {
    const response = await fetch(path, {
      cache: 'no-store',
      ...options,
      headers: { ...(options.headers || {}), ...(options.method === 'POST' ? {
        'Content-Type': 'application/json',
      } : {}) },
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'La connexion au fournisseur a échoué.');
    return payload;
  };
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  };
  const timeText = (value) => {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? 'Heure indisponible' : date.toLocaleString('fr-BE', { timeZone: 'UTC' }) + ' UTC';
  };
  const coinGeckoRows = document.getElementById('coingecko-overview-rows');
  const coinGeckoBadge = document.getElementById('coingecko-overview-badge');
  const coinGeckoSearch = document.getElementById('coingecko-overview-search');
  const coinGeckoRefresh = document.getElementById('coingecko-overview-refresh');
  const coinGeckoCount = document.getElementById('coingecko-overview-count');
  const coinGeckoUpdated = document.getElementById('coingecko-overview-updated');
  let coinGeckoMarkets = [];
  function renderCoinGeckoOverview() {
    const query = coinGeckoSearch.value.trim().toUpperCase();
    const visible = coinGeckoMarkets.filter((coin) => coin.name.toUpperCase().includes(query) || coin.symbol.includes(query));
    coinGeckoRows.replaceChildren();
    if (!visible.length) {
      const row = element('tr');
      const cell = element('td', 'empty', coinGeckoMarkets.length ? 'Aucune crypto ne correspond à cette recherche.' : 'Aucune cotation CoinGecko reçue.');
      cell.colSpan = 6;
      row.append(cell);
      coinGeckoRows.append(row);
    }
    for (const coin of visible) {
      const row = element('tr');
      row.append(element('td', '', coin.market_cap_rank ?? '—'));
      const identity = element('td');
      identity.append(element('strong', 'market-symbol', `${coin.name} · ${coin.symbol}`));
      row.append(identity);
      row.append(element('td', 'market-price', `$${Number(coin.price_usd).toLocaleString('en-US', { maximumSignificantDigits: 10 })}`));
      const change = coin.change_24h_pct === null ? null : Number(coin.change_24h_pct);
      row.append(element('td', change === null ? '' : change >= 0 ? 'market-positive' : 'market-negative', change === null ? '—' : `${change >= 0 ? '+' : ''}${change.toLocaleString('fr-BE', { maximumFractionDigits: 2 })} %`));
      row.append(element('td', '', coin.market_cap_usd === '0' ? '—' : `$${Number(coin.market_cap_usd).toLocaleString('en-US', { maximumFractionDigits: 0 })}`));
      row.append(element('td', 'market-time', timeText(coin.provider_timestamp)));
      coinGeckoRows.append(row);
    }
    coinGeckoCount.textContent = `${visible.length} affichée(s) · ${coinGeckoMarkets.length} reçue(s) · tri par capitalisation`;
  }
  coinGeckoSearch.addEventListener('input', renderCoinGeckoOverview);
  async function refreshCoinGeckoOverview() {
    coinGeckoRefresh.disabled = true;
    coinGeckoRefresh.textContent = 'Chargement…';
    try {
      const snapshot = await api('/api/market-data/coingecko-overview');
      coinGeckoMarkets = Array.isArray(snapshot.markets) ? snapshot.markets : [];
      coinGeckoBadge.textContent = `${snapshot.count} CRYPTOS`;
      coinGeckoBadge.className = 'status-badge connected';
      coinGeckoUpdated.replaceChildren();
      const attribution = element('a', '', snapshot.attribution_text);
      attribution.href = snapshot.attribution_url;
      attribution.target = '_blank';
      attribution.rel = 'noopener noreferrer';
      coinGeckoUpdated.append(attribution, document.createTextNode(` · reçu ${timeText(snapshot.fetched_at)} · cours fournisseur horodatés dans la liste · rafraîchissement manuel limité à une fois par ${snapshot.refresh_after_seconds} s`));
      renderCoinGeckoOverview();
      setTimeout(() => { coinGeckoRefresh.disabled = false; coinGeckoRefresh.textContent = 'Actualiser'; }, snapshot.refresh_after_seconds * 1000);
    } catch (cause) {
      coinGeckoBadge.textContent = 'INDISPONIBLE';
      coinGeckoBadge.className = 'status-badge error';
      coinGeckoMarkets = [];
      coinGeckoCount.textContent = cause instanceof Error ? cause.message : 'Marchés CoinGecko momentanément indisponibles.';
      renderCoinGeckoOverview();
      setTimeout(() => { coinGeckoRefresh.disabled = false; coinGeckoRefresh.textContent = 'Réessayer'; }, 60000);
    }
  }
  coinGeckoRefresh.addEventListener('click', refreshCoinGeckoOverview);
  refreshCoinGeckoOverview();
  setInterval(refreshCoinGeckoOverview, 60000);

  const cryptoOverviewRows = document.getElementById('crypto-overview-rows');
  const cryptoOverviewBadge = document.getElementById('crypto-overview-badge');
  const cryptoOverviewSearch = document.getElementById('crypto-overview-search');
  const cryptoOverviewCount = document.getElementById('crypto-overview-count');
  const cryptoOverviewUpdated = document.getElementById('crypto-overview-updated');
  let cryptoOverviewMarkets = [];
  function renderCryptoOverview() {
    const filter = cryptoOverviewSearch.value.trim().toUpperCase();
    const visible = cryptoOverviewMarkets.filter((market) => market.symbol.includes(filter));
    cryptoOverviewRows.replaceChildren();
    if (!visible.length) {
      const row = element('tr');
      const cell = element('td', 'empty', cryptoOverviewMarkets.length ? 'Aucune paire ne correspond à cette recherche.' : 'Aucune cotation reçue. Vérifie ta connexion internet et actualise la page.');
      cell.colSpan = 5;
      row.append(cell);
      cryptoOverviewRows.append(row);
    }
    for (const market of visible) {
      const row = element('tr');
      row.append(element('td', 'market-symbol', market.symbol.replace('_', ' / ')));
      row.append(element('td', 'market-price', `${Number(market.price).toLocaleString('fr-BE', { maximumSignificantDigits: 10 })} ${market.currency}`));
      const change = Number(market.change_24h_pct);
      row.append(element('td', change >= 0 ? 'market-positive' : 'market-negative', `${change >= 0 ? '+' : ''}${change.toLocaleString('fr-BE', { maximumFractionDigits: 2 })} %`));
      row.append(element('td', '', Number(market.volume_24h_usd).toLocaleString('fr-BE', { maximumFractionDigits: 0 }) + ' $'));
      row.append(element('td', 'market-time', timeText(market.provider_timestamp)));
      cryptoOverviewRows.append(row);
    }
    cryptoOverviewCount.textContent = `${visible.length} paire(s) affichée(s) · ${cryptoOverviewMarkets.length} reçue(s) · volume décroissant`;
  }
  cryptoOverviewSearch.addEventListener('input', renderCryptoOverview);
  async function refreshCryptoOverview() {
    try {
      const snapshot = await api('/api/market-data/crypto-overview');
      cryptoOverviewMarkets = Array.isArray(snapshot.markets) ? snapshot.markets : [];
      cryptoOverviewBadge.textContent = `${snapshot.count} MARCHÉS`;
      cryptoOverviewBadge.className = 'status-badge connected';
      cryptoOverviewUpdated.textContent = `Crypto.com Exchange · ${snapshot.available_spot_count} paires spot disponibles · reçu ${timeText(snapshot.fetched_at)} · rafraîchissement ${snapshot.refresh_after_seconds} s · lecture seule`;
      renderCryptoOverview();
    } catch (cause) {
      cryptoOverviewBadge.textContent = 'INDISPONIBLE';
      cryptoOverviewBadge.className = 'status-badge error';
      cryptoOverviewMarkets = [];
      cryptoOverviewCount.textContent = cause instanceof Error ? cause.message : 'Marchés Crypto.com momentanément indisponibles.';
      cryptoOverviewUpdated.textContent = 'Source indisponible · les cotations ne sont pas affichées.';
      renderCryptoOverview();
    }
  }
  refreshCryptoOverview();
  setInterval(refreshCryptoOverview, 15000);

  const stockWidgetContainer = document.querySelector('.tradingview-widget-container');
  if (stockWidgetContainer) {
    const config = document.querySelector('.tradingview-widget-config');
    if (config) {
      const widgetConfig = document.createElement('script');
      widgetConfig.src = 'https://s3.tradingview.com/external-embedding/embed-widget-market-overview.js';
      widgetConfig.async = true;
      widgetConfig.textContent = config.textContent;
      stockWidgetContainer.append(widgetConfig);
    }
  }
  const quoteBody = document.getElementById('market-quotes');
  const marketStatus = document.getElementById('market-status');
  const marketBadge = document.getElementById('market-badge');
  const marketForm = document.getElementById('market-connect-form');
  const marketProvider = document.getElementById('market-provider');
  const marketApiKeyField = document.getElementById('market-api-key-field');
  const apiKeyInput = document.getElementById('market-api-key');
  const symbolsInput = document.getElementById('market-symbols');
  const providerHelp = document.getElementById('market-provider-help');
  const connectButton = document.getElementById('market-connect');
  const disconnectButton = document.getElementById('market-disconnect');
  let pollingTimer = null;

  function updateMarketProviderFields() {
    const isCryptoCom = marketProvider.value === 'cryptocom';
    marketApiKeyField.hidden = isCryptoCom;
    apiKeyInput.required = !isCryptoCom;
    symbolsInput.placeholder = isCryptoCom ? 'Ex. BTC_USDT, ETH_USDT' : 'Ex. AAPL, ASML, EUR/USD, BTC/USD';
    providerHelp.textContent = isCryptoCom
      ? 'Cours Crypto.com Exchange · aucune clé API ni clé de compte · paires crypto disponibles sur l’Exchange.'
      : 'Actions, ETF, devises, crypto, indices et autres symboles disponibles selon le marché et ton abonnement.';
  }
  marketProvider.addEventListener('change', updateMarketProviderFields);
  updateMarketProviderFields();

  marketForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    connectButton.disabled = true;
    marketProvider.disabled = true;
    marketStatus.textContent = 'Connexion au fournisseur…';
    try {
      const provider = marketProvider.value;
      const request = provider === 'cryptocom'
        ? { provider, symbols: symbolsInput.value }
        : { provider, api_key: apiKeyInput.value, symbols: symbolsInput.value };
      const state = await api('/api/market-data/connect', {
        method: 'POST',
        body: JSON.stringify(request),
      });
      marketStatus.textContent = provider === 'cryptocom'
        ? 'Flux public Crypto.com demandé · aucun compte ni clé API utilisé.'
        : 'Flux Twelve Data demandé · les cours apparaîtront à leur réception par le fournisseur.';
      renderMarketState(state);
      if (pollingTimer) clearInterval(pollingTimer);
      pollingTimer = setInterval(refreshMarketState, 1000);
    } catch (cause) {
      marketStatus.textContent = cause instanceof Error ? cause.message : 'Connexion au flux impossible.';
      marketBadge.textContent = 'ÉCHEC';
      marketBadge.className = 'status-badge error';
    } finally {
      apiKeyInput.value = '';
      connectButton.disabled = false;
      if (!pollingTimer) marketProvider.disabled = false;
    }
  });

  disconnectButton.addEventListener('click', async () => {
    disconnectButton.disabled = true;
    try {
      const state = await api('/api/market-data/disconnect', {
        method: 'POST', body: JSON.stringify({ confirm_disconnect: true }),
      });
      if (pollingTimer) clearInterval(pollingTimer);
      pollingTimer = null;
      marketStatus.textContent = 'Flux arrêté · clé retirée de la mémoire du connecteur.';
      renderMarketState(state);
      marketProvider.disabled = false;
    } catch (cause) {
      marketStatus.textContent = cause instanceof Error ? cause.message : 'Déconnexion impossible.';
    } finally {
      disconnectButton.disabled = false;
    }
  });

  async function refreshMarketState() {
    try {
      renderMarketState(await api('/api/market-data/state'));
    } catch {
      marketStatus.textContent = 'État du flux momentanément indisponible.';
    }
  }

  function renderMarketState(state) {
    const connected = ['CONNECTED', 'PARTIAL'].includes(state.connection_status);
    marketBadge.textContent = state.connection_status === 'CONNECTED' ? 'FLUX CONNECTÉ' : state.connection_status;
    marketBadge.className = `status-badge ${connected ? 'connected' : state.connection_status === 'ERROR' ? 'error' : ''}`;
    disconnectButton.disabled = state.connection_status === 'DISCONNECTED';
    if (state.connection_status === 'DISCONNECTED' && pollingTimer) {
      clearInterval(pollingTimer);
      pollingTimer = null;
      marketProvider.disabled = false;
      connectButton.disabled = false;
    }
    const quotes = Array.isArray(state.quotes) ? state.quotes : [];
    quoteBody.replaceChildren();
    if (!quotes.length) {
      const row = element('tr');
      const cell = element('td', 'empty', state.connection_status === 'DISCONNECTED'
        ? 'Connecte le flux pour afficher les cours disponibles.'
        : 'En attente des premières données du fournisseur.');
      cell.colSpan = 6;
      row.append(cell);
      quoteBody.append(row);
      return;
    }
    for (const quote of quotes) {
      const row = element('tr');
      const symbol = element('td');
      symbol.append(element('strong', '', quote.symbol));
      const price = element('td');
      price.append(element('strong', '', `${quote.price}${quote.currency ? ` ${quote.currency}` : ''}`));
      const details = element('td');
      details.append(element('span', '', quote.instrument_type || 'Type non précisé'));
      details.append(element('span', 'subcell', quote.exchange || 'Place non précisée'));
      const providerTime = element('td', '', timeText(quote.provider_timestamp));
      const received = element('td');
      received.append(element('span', '', timeText(quote.received_at)));
      const hash = element('span', 'subcell', `SHA-256 ${quote.payload_sha256}`);
      received.append(hash);
      const source = element('td', '', quote.source);
      row.append(symbol, price, details, providerTime, received, source);
      quoteBody.append(row);
    }
  }

  const cryptoForm = document.getElementById('cryptocom-read-form');
  const cryptoKey = document.getElementById('cryptocom-api-key');
  const cryptoSecret = document.getElementById('cryptocom-api-secret');
  const cryptoButton = document.getElementById('cryptocom-read-button');
  const cryptoStatus = document.getElementById('cryptocom-status');
  const cryptoRemember = document.getElementById('cryptocom-remember-credentials');
  const cryptoSavedControls = document.getElementById('cryptocom-saved-controls');
  const cryptoSavedStatus = document.getElementById('cryptocom-saved-status');
  const cryptoRefreshSaved = document.getElementById('cryptocom-refresh-saved');
  const cryptoForgetSaved = document.getElementById('cryptocom-forget-saved');
  const cryptoDashboardStatus = document.getElementById('crypto-dashboard-status');
  const cryptoBalances = document.getElementById('crypto-dashboard-balances');
  const cryptoPositions = document.getElementById('crypto-dashboard-positions');
  const cryptoProvenance = document.getElementById('crypto-dashboard-provenance');
  function renderCryptoCredentialState(config) {
    const available = Boolean(config.available);
    const persisted = Boolean(config.credentials_persisted);
    cryptoRemember.disabled = !available;
    cryptoSavedControls.hidden = !persisted;
    cryptoSavedStatus.textContent = persisted
      ? 'Clé conservée dans le coffre Windows de cet utilisateur. Aucun accès au compte ne démarre sans clic sur « Lire / actualiser ».'
      : available
        ? 'Le coffre Windows est prêt; l’enregistrement reste facultatif.'
        : 'Coffre Windows indisponible; la lecture ponctuelle reste possible sans conserver la clé.';
  }
  api('/api/cryptocom/portfolio/config').then(renderCryptoCredentialState).catch(() => {
    cryptoSavedStatus.textContent = 'État du coffre Windows indisponible.';
  });
  cryptoForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    cryptoButton.disabled = true;
    cryptoStatus.textContent = 'Lecture du compte Crypto.com Exchange…';
    try {
      const snapshot = await api('/api/cryptocom/portfolio/import', {
        method: 'POST', body: JSON.stringify({
          api_key: cryptoKey.value,
          api_secret: cryptoSecret.value,
          remember_credentials: cryptoRemember.checked,
        }),
      });
      renderCryptoPortfolio(snapshot);
      if (snapshot.credentials_persisted) {
        cryptoSavedControls.hidden = false;
        cryptoSavedStatus.textContent = 'Clé conservée dans le coffre Windows de cet utilisateur.';
      } else if (!cryptoSavedControls.hidden) {
        cryptoSavedStatus.textContent = 'Lecture ponctuelle effectuée. La clé déjà enregistrée reste inchangée; le bouton d’actualisation utilise celle du coffre.';
      }
      cryptoStatus.textContent = snapshot.credential_warning || 'Lecture terminée · observation seule · aucun ordre envoyé.';
    } catch (cause) {
      cryptoStatus.textContent = cause instanceof Error ? cause.message : 'Lecture Crypto.com indisponible.';
      cryptoDashboardStatus.textContent = 'Lecture échouée · aucun relevé récent disponible.';
      renderEmpty(cryptoBalances, 'Aucun solde affiché.');
      renderEmpty(cryptoPositions, 'Aucune position affichée.');
      cryptoProvenance.textContent = 'Aucune donnée de compte n’a été conservée.';
    } finally {
      cryptoKey.value = '';
      cryptoSecret.value = '';
      cryptoButton.disabled = false;
    }
  });

  cryptoRefreshSaved.addEventListener('click', async () => {
    cryptoRefreshSaved.disabled = true;
    cryptoStatus.textContent = 'Lecture du compte Crypto.com depuis le coffre Windows…';
    try {
      const snapshot = await api('/api/cryptocom/portfolio/refresh', {
        method: 'POST', body: JSON.stringify({ use_saved_credentials: true }),
      });
      renderCryptoPortfolio(snapshot);
      cryptoStatus.textContent = 'Lecture actualisée · observation seule · aucun ordre envoyé.';
      renderCryptoCredentialState({ available: true, credentials_persisted: true });
    } catch (cause) {
      cryptoStatus.textContent = cause instanceof Error ? cause.message : 'Lecture Crypto.com indisponible.';
      cryptoDashboardStatus.textContent = 'Lecture échouée · aucun relevé récent disponible.';
      renderEmpty(cryptoBalances, 'Aucun solde affiché.');
      renderEmpty(cryptoPositions, 'Aucune position affichée.');
      cryptoProvenance.textContent = 'Aucune donnée de compte n’a été conservée.';
    } finally {
      cryptoRefreshSaved.disabled = false;
    }
  });

  cryptoForgetSaved.addEventListener('click', async () => {
    cryptoForgetSaved.disabled = true;
    try {
      await api('/api/cryptocom/portfolio/forget', {
        method: 'POST', body: JSON.stringify({ forget_saved_credentials: true }),
      });
      cryptoDashboardStatus.textContent = '';
      renderEmpty(cryptoBalances, 'Aucun relevé chargé.');
      renderEmpty(cryptoPositions, 'Aucun relevé chargé.');
      cryptoProvenance.textContent = '';
      cryptoStatus.textContent = 'Clé enregistrée oubliée et relevé retiré de la page.';
      renderCryptoCredentialState({ available: true, credentials_persisted: false });
    } catch (cause) {
      cryptoStatus.textContent = cause instanceof Error ? cause.message : 'Suppression de la clé impossible.';
    } finally {
      cryptoForgetSaved.disabled = false;
    }
  });

  function renderEmpty(target, message) {
    target.replaceChildren(element('p', 'empty', message));
  }

  function renderCryptoPortfolio(snapshot) {
    const balances = Array.isArray(snapshot.balances) ? snapshot.balances : [];
    const positions = Array.isArray(snapshot.positions) ? snapshot.positions : [];
    cryptoDashboardStatus.textContent = `${balances.length} actif(s) · ${positions.length} position(s) dérivée(s) · reçu ${timeText(snapshot.fetched_at)}`;
    cryptoBalances.replaceChildren();
    for (const balance of balances) {
      const card = element('div', 'data-card');
      card.append(element('strong', '', balance.currency));
      card.append(element('span', '', `Disponible : ${balance.available}`));
      card.append(element('span', '', `Marge totale : ${balance.total_margin}`));
      cryptoBalances.append(card);
    }
    if (!balances.length) renderEmpty(cryptoBalances, 'Aucun actif renvoyé par l’Exchange.');
    cryptoPositions.replaceChildren();
    for (const position of positions) {
      const card = element('div', 'data-card');
      card.append(element('strong', '', position.instrument_name));
      card.append(element('span', '', `${position.type} · quantité ${position.quantity}`));
      card.append(element('span', '', `P&L ouvert : ${position.open_position_pnl_usd} USD`));
      card.append(element('span', '', `Mis à jour : ${timeText(position.updated_at)}`));
      cryptoPositions.append(card);
    }
    if (!positions.length) renderEmpty(cryptoPositions, 'Aucune position dérivée ouverte. Les actifs Spot figurent dans les soldes.');
    cryptoProvenance.textContent = `${snapshot.provider} · ${snapshot.source} · non sauvegardé · Risk et exécution désactivés · SHA soldes ${snapshot.payload_sha256} · SHA positions ${snapshot.positions_payload_sha256}`;
  }

  const listAccountsButton = document.getElementById('broker-list-accounts');
  const accountSelect = document.getElementById('broker-account-select');
  const importAccountButton = document.getElementById('broker-import-account');
  const brokerStatus = document.getElementById('broker-portfolio-status');
  const brokerResult = document.getElementById('broker-portfolio-result');
  listAccountsButton.addEventListener('click', async () => {
    listAccountsButton.disabled = true;
    brokerStatus.textContent = 'Lecture de la liste des comptes auprès du Gateway local…';
    try {
      const data = await api('/api/ibkr/portfolio/accounts');
      accountSelect.replaceChildren(new Option('Choisir un compte', ''));
      for (const account of data.accounts || []) {
        const option = new Option(`${account.display_name} · ${account.currency}`, account.account_id);
        accountSelect.add(option);
      }
      accountSelect.disabled = false;
      importAccountButton.disabled = false;
      brokerStatus.textContent = `${(data.accounts || []).length} compte(s) trouvé(s).`;
    } catch (cause) {
      brokerStatus.textContent = cause instanceof Error ? cause.message : 'Gateway local indisponible.';
    } finally {
      listAccountsButton.disabled = false;
    }
  });
  importAccountButton.addEventListener('click', async () => {
    if (!accountSelect.value) return;
    importAccountButton.disabled = true;
    brokerStatus.textContent = 'Lecture du compte sélectionné…';
    try {
      const snapshot = await api('/api/ibkr/portfolio/import', {
        method: 'POST', body: JSON.stringify({ account_id: accountSelect.value }),
      });
      renderIbkrPortfolio(snapshot);
      brokerStatus.textContent = `Lecture terminée · ${timeText(snapshot.fetched_at)} · observation seule.`;
    } catch (cause) {
      brokerStatus.textContent = cause instanceof Error ? cause.message : 'Lecture IBKR indisponible.';
      renderEmpty(brokerResult, 'Aucune donnée IBKR récente disponible.');
    } finally {
      importAccountButton.disabled = false;
    }
  });

  function renderIbkrPortfolio(snapshot) {
    brokerResult.replaceChildren();
    const account = element('div', 'data-card');
    account.append(element('strong', '', `${snapshot.account.display_name} · ${snapshot.account.account_id}`));
    account.append(element('span', '', `Devise : ${snapshot.account.currency}`));
    brokerResult.append(account);
    for (const position of snapshot.positions || []) {
      const card = element('div', 'data-card');
      card.append(element('strong', '', `${position.symbol} · ${position.asset_class}`));
      card.append(element('span', '', `Quantité ${position.quantity} ${position.currency}`));
      if (position.market_value !== null) card.append(element('span', '', `Valeur : ${position.market_value} ${position.currency}`));
      card.append(element('span', '', `Source ${position.source} · ${timeText(position.provider_timestamp)}`));
      brokerResult.append(card);
    }
    for (const metric of snapshot.metrics || []) {
      const card = element('div', 'data-card');
      card.append(element('strong', '', metric.name));
      card.append(element('span', '', `${metric.amount} ${metric.currency} · ${timeText(metric.provider_timestamp)}`));
      brokerResult.append(card);
    }
    if (!(snapshot.positions || []).length && !(snapshot.metrics || []).length) {
      brokerResult.append(element('p', 'empty', 'Le compte n’a renvoyé aucun solde ni aucune position lisible.'));
    }
  }

  api('/api/ibkr/portfolio/config').then((config) => {
    if (config.configured) {
      listAccountsButton.disabled = false;
      brokerStatus.textContent = 'Gateway configuré · aucune lecture lancée.';
    }
  }).catch(() => {});
})();
