(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const themeChoices = new Set(['clair', 'foret', 'ocean', 'nuit']);
  function applyTheme(theme) {
    const selected = themeChoices.has(theme) ? theme : 'nuit';
    document.documentElement.dataset.theme = selected;
    document.querySelectorAll('[data-theme-choice]').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.themeChoice === selected));
    });
    document.dispatchEvent(new Event('bubus-theme-change'));
  }
  let savedTheme = 'nuit';
  try { savedTheme = localStorage.getItem('bubus.theme') || 'nuit'; } catch {}
  applyTheme(savedTheme);
  document.querySelectorAll('[data-theme-choice]').forEach(button => button.addEventListener('click', () => {
    const selected = button.dataset.themeChoice;
    if (!themeChoices.has(selected)) return;
    applyTheme(selected);
    try { localStorage.setItem('bubus.theme', selected); } catch {}
  }));
  const state = {exchange: [], restExchange: [], coingecko: [], watchlist: [], source: 'exchange', selected: null,
    period: '1h', candles: [], chartRequest: 0, refreshBusy: false, streamTimer: null,
    scanTimer: null, scanBusy: false, streamQuotes: new Map(), marketRows: new Map()};
  const labels = {markets: 'Marchés', watchlist: 'Favoris', portfolio: 'Portefeuille',
    autopilot: 'Automatisation', activity: 'Activité', settings: 'Connexions'};
  const api = async (path, payload) => {
    const response = await fetch(path, {cache: 'no-store', ...(payload === undefined ? {} : {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'La demande n’a pas abouti.');
    return result;
  };
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const feeStatus = node('p', 'Frais Spot : lecture en attente.', 'inline-status');
  feeStatus.id = 'fee-status'; $('transaction-status').after(feeStatus);
  const balanceHistoryCard = node('section', undefined, 'card history-card');
  const balanceHistoryHeading = node('div', undefined, 'card-heading');
  const balanceHistoryTitle = node('div');
  balanceHistoryTitle.append(node('h2', 'Historique cash Crypto.com'),
    node('p', 'Soldes cash renvoyés par Exchange · H1, 24 points maximum.', 'microcopy'));
  balanceHistoryHeading.append(balanceHistoryTitle, node('span', 'CASH OBSERVÉ', 'outline-tag'));
  const balanceHistoryStatus = node('p', 'Aucun historique cash demandé.', 'inline-status');
  const balanceHistoryRows = node('div', undefined, 'transaction-list');
  balanceHistoryCard.append(balanceHistoryHeading, balanceHistoryStatus, balanceHistoryRows,
    node('p', 'Crypto.com signale des écarts temporaires possibles. Ce relevé ne représente ni l’equity complète ni le P&L; il reste exclu de Risk.', 'microcopy'));
  const portfolioGrid = $('view-portfolio').querySelector('.portfolio-grid');
  portfolioGrid.insertBefore(balanceHistoryCard, $('transaction-status').closest('section'));
  const fundingHistoryCard = node('section', undefined, 'card history-card');
  const fundingHeading = node('div', undefined, 'card-heading');
  const fundingTitle = node('div');
  fundingTitle.append(node('h2', 'Dépôts et retraits Crypto.com'),
    node('p', 'Historique dédié de financement, montants, devises et frais.', 'microcopy'));
  fundingHeading.append(fundingTitle, node('span', 'LECTURE SEULE', 'outline-tag'));
  const fundingStatus = node('p', 'Aucun dépôt ou retrait lu.', 'inline-status');
  const fundingRows = node('div', undefined, 'transaction-list');
  fundingHistoryCard.append(fundingHeading, fundingStatus, fundingRows,
    node('p', 'La pagination complète de la période est conservée avec son empreinte. Cela ne prouve pas à lui seul le solde d’ouverture/fermeture.', 'microcopy'));
  portfolioGrid.insertBefore(fundingHistoryCard, $('transaction-status').closest('section'));
  const valuationHistoryCard = node('section', undefined, 'card history-card');
  const valuationHistoryHeading = node('div', undefined, 'card-heading');
  const valuationHistoryTitle = node('div');
  valuationHistoryTitle.append(node('h2', 'Valorisations Spot observées'),
    node('p', 'Historique local des estimations candidates.', 'microcopy'));
  valuationHistoryHeading.append(valuationHistoryTitle, node('span', 'OBSERVATION', 'outline-tag'));
  const valuationHistoryStatus = node('p', 'Aucune valorisation conservée.', 'inline-status');
  const valuationHistoryRows = node('div', undefined, 'transaction-list');
  let valuationHistoryInvalid = false;
  let reconciliationInvalid = false;
  valuationHistoryCard.append(valuationHistoryHeading, valuationHistoryStatus, valuationHistoryRows,
    node('p', 'Les variations ne sont pas un P&L : dépôts, retraits, conversions et frais ne sont pas rapprochés. Ces observations ne servent ni à Risk ni aux ordres.', 'microcopy'));
  portfolioGrid.insertBefore(valuationHistoryCard, balanceHistoryCard);
  const reconciliationCard = node('section', undefined, 'card history-card');
  const reconciliationHeading = node('div', undefined, 'card-heading');
  const reconciliationTitle = node('div');
  reconciliationTitle.append(node('h2', 'Rapprochement du portefeuille'),
    node('p', 'Contrôle des preuves Crypto.com, sans calcul de P&L.', 'microcopy'));
  reconciliationHeading.append(reconciliationTitle, node('span', 'NON AUTORITAIRE', 'outline-tag'));
  const reconciliationStatus = node('p', 'Contrôle des preuves locales en attente.', 'inline-status');
  const reconciliationRows = node('div', undefined, 'transaction-list');
  reconciliationCard.append(reconciliationHeading, reconciliationStatus, reconciliationRows,
    node('p', 'Les journaux ne décrivent pas tous les mouvements de chaque devise. Aucune performance, limite Risk ou autorisation d’ordre n’en est déduite.', 'microcopy'));
  portfolioGrid.insertBefore(reconciliationCard, valuationHistoryCard);
  const riskAuditPanel = node('section', undefined, 'risk-audit-panel');
  riskAuditPanel.append(node('h3', 'Contrôle de préparation Risk'));
  const riskAuditStatus = node('p', 'Vérification de sécurité en attente.', 'inline-status');
  riskAuditStatus.setAttribute('role', 'status');
  const riskAuditReasons = node('ul', undefined, 'risk-audit-reasons');
  riskAuditPanel.append(riskAuditStatus, riskAuditReasons,
    node('p', 'Ce contrôle inspecte uniquement les preuves locales. Il ne valide aucun ordre, ne calcule pas le P&L quotidien et ne transforme pas un brouillon en politique active.', 'microcopy'));
  $('risk-limits-form').after(riskAuditPanel);
  const orderChainPanel = node('section', undefined, 'risk-audit-panel');
  orderChainPanel.append(node('h3', 'Audit de la chaîne d’ordres Crypto.com'));
  const orderChainStatus = node('p', 'Vérification des barrières d’exécution en attente.', 'inline-status');
  orderChainStatus.setAttribute('role', 'status');
  const orderChainReasons = node('ul', undefined, 'risk-audit-reasons');
  orderChainPanel.append(orderChainStatus, orderChainReasons,
    node('p', 'Audit en lecture seule : autorisation unique, anti-doublon durable, réponse réseau incertaine, lecture de statut et rapprochement des exécutions. Cette page ne transmet aucun ordre.', 'microcopy'));
  riskAuditPanel.after(orderChainPanel);
  const format = value => Number(value).toLocaleString('fr-BE', {maximumSignificantDigits: 9});
  const compact = value => Number(value).toLocaleString('fr-BE', {notation: 'compact', maximumFractionDigits: 2});
  const changeText = value => value === null || value === undefined ? '—' :
    `${Number(value) >= 0 ? '+' : ''}${Number(value).toLocaleString('fr-BE', {maximumFractionDigits: 2})} %`;
  const hour = value => {const date = new Date(value); return Number.isNaN(date.getTime()) ? '—' :
    date.toLocaleTimeString('fr-BE', {hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: 'Europe/Brussels'});};
  let toastTimer;
  function toast(message) {
    $('toast').textContent = message; $('toast').hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => {$('toast').hidden = true;}, 5000);
  }
  function view(name) {
    if (!labels[name]) name = 'markets';
    document.querySelectorAll('.view').forEach(section => {section.hidden = section.id !== `view-${name}`;});
    document.querySelectorAll('.nav-item').forEach(button => button.classList.toggle('active', button.dataset.view === name));
    $('breadcrumb').textContent = labels[name];
    if (location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);
    if (name === 'watchlist') renderWatchlist();
    if (name === 'activity') loadActivity();
    if (name === 'portfolio') {loadSavedPortfolio(); loadSavedTransactions(); loadSavedTrades(); loadSavedBalanceHistory(); loadPortfolioValuations(); loadReconciliation(); loadPortfolioObserverState();}
    if (name === 'autopilot') { loadRiskLimits(); loadRiskReadiness(); loadOrderChainAudit(); }
    if (name === 'markets') requestAnimationFrame(drawChart);
  }
  document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => view(button.dataset.view)));
  $('connection-shortcut').addEventListener('click', () => view('settings'));
  window.addEventListener('hashchange', () => view(location.hash.slice(1)));
  const clock = () => {$('clock').textContent = new Date().toLocaleTimeString('fr-BE', {
    timeZone: 'Europe/Brussels', hour: '2-digit', minute: '2-digit'}) + ' · Bruxelles';};
  clock(); setInterval(clock, 30000);
  function metrics() {
    const rows = state.exchange;
    $('metric-count').textContent = rows.length ? rows.length.toLocaleString('fr-BE') : '—';
    $('nav-market-count').textContent = rows.length || '—';
    $('metric-up').textContent = rows.length ? rows.filter(row => Number(row.change_24h_pct) > 0).length : '—';
    $('metric-down').textContent = rows.length ? rows.filter(row => Number(row.change_24h_pct) < 0).length : '—';
    $('nav-watch-count').textContent = state.watchlist.length;
  }
  function rows() {
    const query = $('search').value.trim().toUpperCase();
    const result = state[state.source].filter(row => `${row.symbol} ${row.name || ''}`.toUpperCase().includes(query));
    const sorting = $('sort').value;
    result.sort(sorting === 'name' ? (a, b) => a.symbol.localeCompare(b.symbol) : sorting === 'gainers' ?
      (a, b) => Number(b.change_24h_pct) - Number(a.change_24h_pct) : sorting === 'losers' ?
      (a, b) => Number(a.change_24h_pct) - Number(b.change_24h_pct) :
      (a, b) => Number(b.volume_24h_usd) - Number(a.volume_24h_usd));
    return result;
  }
  function renderMarkets() {
    const body = $('market-rows'); body.replaceChildren();
    state.marketRows.clear();
    const data = rows();
    if (!data.length) {const tr = node('tr'); const td = node('td', state[state.source].length ?
      'Aucun actif ne correspond à votre recherche.' : 'Aucune cotation disponible pour cette source.', 'table-empty');
      td.colSpan = 6; tr.append(td); body.append(tr);}
    const fragment = document.createDocumentFragment();
    for (const market of data) {
      const exchange = state.source === 'exchange';
      const row = node('tr'); row.dataset.symbol = market.symbol;
      if (exchange && state.selected === market.symbol) row.classList.add('selected-row');
      const favorite = node('td', undefined, 'favorite-col');
      const star = node('button', state.watchlist.includes(market.symbol) ? '★' : '☆', 'favorite-button');
      star.classList.toggle('saved', state.watchlist.includes(market.symbol));
      star.setAttribute('aria-label', `${state.watchlist.includes(market.symbol) ? 'Retirer' : 'Ajouter'} ${market.symbol} ${exchange ? 'des' : 'aux'} favoris`);
      star.disabled = !exchange; star.title = exchange ? 'Favori enregistré sur ce PC' : 'Les favoris portent sur les paires Crypto.com';
      star.addEventListener('click', event => {event.stopPropagation(); toggleWatch(market.symbol);});
      favorite.append(star); row.append(favorite);
      const identityCell = node('td'); const identity = node('div', undefined, 'market-identity');
      const coin = node('span', market.symbol.split('_')[0].slice(0, 3), 'mini-coin');
      const description = node('div'); description.append(node('strong', market.name || market.symbol.replace('_', ' / ')),
        node('small', exchange ? 'Crypto.com Exchange' : `${market.symbol} · CoinGecko`));
      if (exchange) {
        const rules = market.instrument_rules;
        description.append(node('small', rules
          ? `Pas ${rules.price_tick_size} ${rules.quote_currency} · quantité ${rules.quantity_tick_size} ${rules.base_currency}`
          : 'Règles Spot officielles indisponibles'));
      }
      identity.append(coin, description); identityCell.append(identity); row.append(identityCell);
      const priceCell = node('td', `${format(market.price)} ${market.currency}`, 'number');
      row.append(priceCell);
      row.append(node('td', changeText(market.change_24h_pct), `number ${market.change_24h_pct === null ? '' : Number(market.change_24h_pct) >= 0 ? 'positive' : 'negative'}`));
      const providerTimeCell = node('td', hour(market.provider_timestamp));
      row.append(node('td', compact(market.volume_24h_usd) + ' $', 'number'), providerTimeCell);
      if (exchange) state.marketRows.set(market.symbol, {price: priceCell, providerTime: providerTimeCell});
      row.addEventListener('click', () => {if (exchange) select(market.symbol); else toast('Prix agrégé CoinGecko. Sélectionnez une paire Crypto.com pour son historique de plateforme.');});
      fragment.append(row);
    }
    body.append(fragment);
    $('table-count').textContent = `${data.length} affiché${data.length > 1 ? 's' : ''} · ${state[state.source].length} reçu${state[state.source].length > 1 ? 's' : ''}`;
    $('table-attribution').replaceChildren();
    if (state.source === 'coingecko') {
      const attribution = node('a', 'Données fournies par CoinGecko ↗');
      attribution.href = 'https://www.coingecko.com/'; attribution.target = '_blank'; attribution.rel = 'noopener noreferrer';
      $('table-attribution').append(attribution);
    } else {
      const activeTicks = state.streamQuotes.size;
      $('table-attribution').textContent = activeTicks
        ? `Crypto.com Exchange · dernier trade · ${activeTicks} ticks WebSocket récents · autres paires REST (15 s)`
        : 'Crypto.com Exchange · dernier trade · REST (15 s) · Bid/ask conservés séparément';
    }
    metrics();
  }
  $('search').addEventListener('input', renderMarkets); $('sort').addEventListener('change', renderMarkets);
  document.querySelectorAll('[data-source]').forEach(button => button.addEventListener('click', () => {
    state.source = button.dataset.source;
    document.querySelectorAll('[data-source]').forEach(other => other.classList.toggle('active', other === button));
    renderMarkets();
  }));
  let exchangeBusy = false, geckoBusy = false;
  function applyStreamQuotes() {
    state.exchange = state.restExchange.map(row => {
      const restored = {...row};
      if (restored.price_source === 'Crypto.com Exchange public ticker WebSocket') {
        restored.price = restored.rest_price;
        restored.provider_timestamp = restored.rest_provider_timestamp;
        delete restored.price_source; delete restored.rest_price; delete restored.rest_provider_timestamp;
      }
      return restored;
    });
    const now = Date.now();
    const freshQuotes = new Map();
    const exchangeBySymbol = new Map(state.exchange.map(row => [row.symbol, row]));
    for (const quote of state.streamQuotes.values()) {
      const received = Date.parse(quote.received_at);
      if (!Number.isFinite(received) || now < received || now - received > 30000) continue;
      freshQuotes.set(quote.symbol, quote);
      const market = exchangeBySymbol.get(quote.symbol);
      if (market) {market.price = quote.last_trade_price || quote.price; market.best_bid = quote.best_bid;
        market.best_ask = quote.best_ask; market.quote_status = quote.quote_status;
        market.provider_timestamp = quote.provider_timestamp;
        market.price_source = 'Crypto.com Exchange public ticker WebSocket';}
    }
    state.streamQuotes = freshQuotes;
    for (const [symbol, cells] of state.marketRows) {
      const market = exchangeBySymbol.get(symbol);
      if (market) {cells.price.textContent = `${format(market.price)} ${market.currency}`;
        cells.providerTime.textContent = hour(market.provider_timestamp);}
    }
    if (state.source === 'exchange') {
      $('table-attribution').textContent = state.streamQuotes.size
        ? `Crypto.com Exchange · dernier trade · ${state.streamQuotes.size} ticks WebSocket récents · autres paires REST (15 s)`
        : 'Crypto.com Exchange · dernier trade · REST (15 s) · Bid/ask conservés séparément';
    }
  }
  async function refreshExchange() {
    if (exchangeBusy) return; exchangeBusy = true;
    try {
      const snapshot = await api('/api/market-data/crypto-overview'); state.restExchange = snapshot.markets || [];
      applyStreamQuotes();
      if (snapshot.realtime?.status === 'PARTIAL') {
        $('metric-coverage').textContent = `${snapshot.realtime.updated_pairs} paires temps réel · ${snapshot.available_spot_count} paires suivies`;
      } else {
        $('metric-coverage').textContent = `${snapshot.available_spot_count} paires · REST actualisé ${hour(snapshot.fetched_at)}`;
      }
      $('exchange-status').textContent = 'Données reçues';
      if (state.exchange.length && !state.selected) await select(state.exchange[0].symbol);
      else { updateSelected(); if (state.selected) await loadChart(); }
    } catch (error) {
      state.exchange = []; state.candles = []; state.chartRequest += 1;
      state.restExchange = [];
      $('exchange-status').textContent = 'Indisponible'; $('metric-coverage').textContent = 'Source indisponible';
      $('chart-price').textContent = '—'; $('chart-empty').textContent = error.message; $('chart-empty').hidden = false;
    } finally {exchangeBusy = false; renderMarkets(); renderWatchlist();}
  }
  async function refreshGecko() {
    if (geckoBusy) return; geckoBusy = true;
    try {
      const result = await api('/api/market-data/coingecko-overview');
      state.coingecko = (result.markets || []).map(coin => ({...coin, price: coin.price_usd, currency: 'USD'}));
    } catch {state.coingecko = []; if (state.source === 'coingecko') toast('CoinGecko est momentanément indisponible.');}
    finally {geckoBusy = false; if (state.source === 'coingecko') renderMarkets();}
  }
  $('refresh').addEventListener('click', async () => {
    $('refresh').disabled = true;
    try {await Promise.all([refreshExchange(), refreshGecko()]); if (state.selected) await loadChart();}
    finally {$('refresh').disabled = false;}
  });
  async function toggleWatch(symbol) {
    try {const result = await api('/api/desk/watchlist', {symbol, enabled: !state.watchlist.includes(symbol)});
      state.watchlist = result.watchlist; renderMarkets(); renderWatchlist(); updateSelected();}
    catch (error) {toast(error.message);}
  }
  $('chart-watch').addEventListener('click', () => {if (state.selected) toggleWatch(state.selected);});
  function updateSelected() {
    const market = state.exchange.find(row => row.symbol === state.selected);
    $('chart-watch').disabled = !market;
    $('chart-watch').textContent = state.watchlist.includes(state.selected) ? '★' : '☆';
    if (!market) { $('chart-price').textContent = '—'; $('chart-change').textContent = '—'; return; }
    $('chart-symbol').textContent = market.symbol.replace('_', ' / '); $('chart-icon').textContent = market.symbol.split('_')[0].slice(0, 3);
    $('chart-price').textContent = format(market.price); $('chart-currency').textContent = market.currency;
    $('chart-change').textContent = changeText(market.change_24h_pct);
    $('chart-change').classList.toggle('negative', Number(market.change_24h_pct) < 0);
    $('chart-time').textContent = `Cours : ${hour(market.provider_timestamp)}`;
  }
  async function select(symbol) {state.selected = symbol; updateSelected(); renderMarkets(); await loadChart();}
  async function loadChart() {
    if (!state.selected) return;
    const request = ++state.chartRequest; state.candles = [];
    $('chart-empty').textContent = 'Chargement de l’historique réel…'; $('chart-empty').hidden = false;
    try {
      const response = await api(`/api/desk/candles?symbol=${encodeURIComponent(state.selected)}&timeframe=${encodeURIComponent(state.period)}`);
      if (request !== state.chartRequest) return;
      state.candles = response.candles || [];
      if (state.candles.length < 2) throw new Error('Historique insuffisant pour tracer une courbe.');
      $('chart-caption').textContent = `${state.candles.length} bougies · ${state.period} · Crypto.com`;
      $('live-signal-detail-results').replaceChildren();
      $('chart-empty').hidden = true; drawChart();
    } catch (error) {if (request === state.chartRequest) {$('chart-empty').textContent = error.message; $('chart-empty').hidden = false; $('chart-caption').textContent = 'Historique indisponible';}}
  }
  document.querySelectorAll('[data-period]').forEach(button => button.addEventListener('click', () => {
    state.period = button.dataset.period;
    document.querySelectorAll('[data-period]').forEach(other => other.classList.toggle('selected', other === button)); loadChart();
  }));
  const canvas = $('price-chart');
  function drawChart() {
    const bounds = canvas.getBoundingClientRect(); if (!bounds.width || !bounds.height) return;
    const scale = window.devicePixelRatio || 1; canvas.width = Math.round(bounds.width * scale); canvas.height = Math.round(bounds.height * scale);
    const ctx = canvas.getContext('2d'); ctx.scale(scale, scale); ctx.clearRect(0, 0, bounds.width, bounds.height);
    if (state.candles.length < 2) return;
    const w = bounds.width, h = bounds.height, left = 3, right = 69, bottom = 25, top = 13;
    const prices = state.candles.map(c => Number(c.c));
    const max = Math.max(...prices), min = Math.min(...prices), padding = Math.max((max - min) * .15, max * .0005);
    const low = min - padding, high = max + padding;
    const x = i => left + i / (prices.length - 1) * (w - left - right);
    const y = value => top + (high - value) / (high - low) * (h - top - bottom);
    const colors = getComputedStyle(document.documentElement);
    ctx.font = '9px Segoe UI'; ctx.fillStyle = colors.getPropertyValue('--chart-label').trim();
    ctx.strokeStyle = colors.getPropertyValue('--chart-grid').trim(); ctx.lineWidth = 1;
    for (let i = 0; i < 4; i++) {const value = low + (high - low) * i / 3, yy = y(value);
      ctx.beginPath(); ctx.moveTo(left, yy); ctx.lineTo(w - right, yy); ctx.stroke(); ctx.fillText(format(value), w - right + 10, yy + 3);}
    const gradient = ctx.createLinearGradient(0, top, 0, h - bottom);
    gradient.addColorStop(0, colors.getPropertyValue('--chart-fill-start').trim());
    gradient.addColorStop(1, colors.getPropertyValue('--chart-fill-end').trim());
    ctx.beginPath(); prices.forEach((price, i) => i ? ctx.lineTo(x(i), y(price)) : ctx.moveTo(x(i), y(price)));
    ctx.lineTo(x(prices.length - 1), h - bottom); ctx.lineTo(left, h - bottom); ctx.closePath(); ctx.fillStyle = gradient; ctx.fill();
    ctx.beginPath(); prices.forEach((price, i) => i ? ctx.lineTo(x(i), y(price)) : ctx.moveTo(x(i), y(price)));
    ctx.strokeStyle = colors.getPropertyValue('--chart-line').trim(); ctx.lineWidth = 1.8; ctx.stroke();
    for (let i = 0; i < 4; i++) {const index = Math.round((prices.length - 1) * i / 3);
      const date = new Date(state.candles[index].timestamp); ctx.fillStyle = '#9ca892';
      const label = state.period === '1D' ? date.toLocaleDateString('fr-BE', {month: 'short', day: 'numeric'}) : date.toLocaleTimeString('fr-BE', {hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Brussels'});
      ctx.fillText(label, Math.min(x(index), w - right - 28), h - 5);}
  }
  document.addEventListener('bubus-theme-change', drawChart);
  new ResizeObserver(drawChart).observe(canvas);
  canvas.addEventListener('mousemove', event => {
    if (state.candles.length < 2) return;
    const bounds = canvas.getBoundingClientRect(), index = Math.max(0, Math.min(state.candles.length - 1, Math.round((event.clientX - bounds.left) / (bounds.width - 69) * (state.candles.length - 1))));
    const candle = state.candles[index]; $('chart-tooltip').textContent = `${format(candle.c)} · ${hour(candle.timestamp)}`;
    $('chart-tooltip').hidden = false; $('chart-tooltip').style.left = Math.max(0, Math.min(event.clientX - bounds.left, bounds.width - 155)) + 'px'; $('chart-tooltip').style.top = '12px';
  });
  canvas.addEventListener('mouseleave', () => {$('chart-tooltip').hidden = true;});
  function renderWatchlist() {
    $('watchlist-content').replaceChildren();
    if (!state.watchlist.length) {$('watchlist-content').append(empty('Une sélection qui vous ressemble.', 'Ajoutez une paire aux favoris avec l’étoile dans la liste des marchés.')); return;}
    for (const symbol of state.watchlist) {
      const market = state.exchange.find(row => row.symbol === symbol); const row = node('div', undefined, 'watch-row');
      const button = node('button', symbol.replace('_', ' / '), 'text-link');
      button.addEventListener('click', () => {view('markets'); select(symbol);});
      row.append(button, node('span', market ? `${format(market.price)} ${market.currency}` : 'Cotation indisponible'), node('span', market ? changeText(market.change_24h_pct) : '—'));
      const remove = node('button', '×', 'icon-button'); remove.setAttribute('aria-label', `Retirer ${symbol} des favoris`); remove.addEventListener('click', () => toggleWatch(symbol)); row.append(remove); $('watchlist-content').append(row);
    }
  }
  function empty(title, description) {const container = node('div', undefined, 'empty-state'); container.append(node('span', '◎'), node('h3', title), node('p', description)); return container;}
  async function loadActivity() {
    try {const result = await api('/api/desk/activity'); $('activity-content').replaceChildren();
      if (!result.events.length) $('activity-content').append(empty('Le journal est prêt.', 'Les modifications de votre liste de favoris apparaîtront ici. Aucun ordre n’a été activé.'));
      for (const event of result.events) {const row = node('div', undefined, 'event-row'); row.append(node('p', event.message), node('time', new Date(event.created_at).toLocaleString('fr-BE'))); $('activity-content').append(row);}}
    catch (error) {toast(error.message);}
  }
  $('activity-refresh').addEventListener('click', loadActivity);
  async function vaultState() {
    try {const config = await api('/api/cryptocom/portfolio/config'); $('remember').disabled = !config.available;
      legacyCredentialsPersisted = config.credentials_persisted === true;
      $('vault-note').textContent = config.credentials_persisted ? 'Clé conservée dans le coffre Windows · aucune lecture privée avant ton clic sur « Actualiser le compte ».' : config.available ? 'Enregistrement facultatif dans le coffre Windows, après une lecture réussie.' : 'Coffre indisponible. La lecture ponctuelle reste possible.';}
    catch {legacyCredentialsPersisted = null; $('vault-note').textContent = 'État du coffre indisponible.';}
    renderSavedCredentialControls();
  }
  let legacyCredentialsPersisted = null, observerStateBusy = false, observerLastSuccess = null,
    observerCredentialsPersisted = null;
  function renderSavedCredentialControls() {
    const resolved = legacyCredentialsPersisted !== null && observerCredentialsPersisted !== null;
    const hasCredentials = legacyCredentialsPersisted === true || observerCredentialsPersisted === true;
    $('saved-controls').hidden = !resolved || !hasCredentials;
    $('saved-refresh').disabled = !resolved || !hasCredentials;
    $('saved-forget').hidden = legacyCredentialsPersisted !== true;
    const historyReady = resolved && hasCredentials;
    $('history-load').disabled = !historyReady;
    $('history-last-day').disabled = !historyReady;
  }
  function selectedHistoryCredentials() {
    const key = $('account-key').value, secret = $('account-secret').value;
    if (key && secret) return {api_key: key, api_secret: secret};
    if (observerCredentialsPersisted === null || legacyCredentialsPersisted === null)
      throw new Error('Vérification des clés en cours. Réessaie lorsque leur état est affiché.');
    if (observerCredentialsPersisted) return {use_observer_credentials: true};
    if (legacyCredentialsPersisted) return {use_saved_credentials: true};
    throw new Error('Aucune clé enregistrée. Connecte le compte avec une clé de lecture.');
  }
  async function loadPortfolioObserverState() {
    if (observerStateBusy) return;
    observerStateBusy = true;
    try {
      const result = await api('/api/desk/portfolio-observer');
      const observer = result.observer;
      observerCredentialsPersisted = result.credentials_persisted === true;
      renderSavedCredentialControls();
      const active = observer.status === 'RUNNING';
      const blocking = observer.status !== 'STOPPED';
      $('observer-key-status').textContent = result.credentials_persisted
        ? 'Clé observateur dédiée conservée dans le coffre Windows. Ses permissions ne peuvent pas être vérifiées par BUBUS.'
        : 'Aucune clé observateur dédiée enregistrée.';
      $('observer-start').disabled = !result.credentials_persisted || blocking;
      $('observer-stop').disabled = !active;
      $('observer-save').disabled = blocking;
      $('observer-status').textContent = active ? 'Suivi actif · relevés locaux en lecture seule.'
        : observer.status === 'STOPPING' ? 'Arrêt en cours…' : 'Suivi arrêté.';
      const cycle = observer.last_collection || {};
      $('observer-detail').textContent = observer.last_error_code
        ? 'La dernière lecture a échoué. Vérifie la clé dédiée et la connexion; aucun ordre n’a été envoyé.'
        : cycle.portfolio_recorded && cycle.valuation_recorded
          ? `Dernier relevé et valorisation conservés · ${hour(observer.last_success_at)}. Observation seule; Risk reste désarmé.`
          : cycle.portfolio_recorded
            ? `Dernier relevé conservé · valorisation non produite (${hour(observer.last_success_at)}). Vérifie le flux et les paires suivies.`
            : 'Le suivi collecte les relevés et leur valeur Spot estimée en EUR environ une fois par minute. Il ne déclenche aucun ordre.';
      if (active && observer.last_success_at && observer.started_at
          && Date.parse(observer.last_success_at) >= Date.parse(observer.started_at)
          && observer.last_success_at !== observerLastSuccess) {
        observerLastSuccess = observer.last_success_at;
        await loadSavedPortfolio();
        await loadPortfolioValuations();
        await loadReconciliation();
      }
    } catch { observerCredentialsPersisted = null; renderSavedCredentialControls(); $('observer-status').textContent = 'État du suivi indisponible.'; }
    finally { observerStateBusy = false; }
  }
  $('observer-key-form').addEventListener('submit', async event => {
    event.preventDefault();
    const button = $('observer-save'); button.disabled = true;
    try {
      await api('/api/cryptocom/portfolio-observer/credentials', {
        api_key: $('observer-key').value, api_secret: $('observer-secret').value,
        confirm_read_only: $('observer-read-only-confirm').checked});
      $('observer-key-form').reset();
      $('observer-key-status').textContent = 'Clé séparée enregistrée. BUBUS ne peut pas vérifier ses permissions; démarre le suivi quand tu le souhaites.';
      toast('Clé observateur enregistrée dans Windows.');
      await loadPortfolioObserverState();
    } catch (error) { toast(error.message); }
    finally {button.disabled = false; $('observer-key').value = ''; $('observer-secret').value = '';}
  });
  $('observer-start').addEventListener('click', async () => {
    $('observer-start').disabled = true;
    try {await api('/api/cryptocom/portfolio-observer/start', {confirm_read_only: true});
      observerLastSuccess = null; await loadPortfolioObserverState();}
    catch (error) {toast(error.message); await loadPortfolioObserverState();}
  });
  $('observer-stop').addEventListener('click', async () => {
    $('observer-stop').disabled = true;
    try {await api('/api/cryptocom/portfolio-observer/stop', {stop: true}); await loadPortfolioObserverState();}
    catch (error) {toast(error.message); await loadPortfolioObserverState();}
  });
  let portfolioPersistent = false, portfolioFetchedAt = null;
  function updatePortfolioCandidateAvailability() {
    const observed = Date.parse(portfolioFetchedAt || '');
    const age = (Date.now() - observed) / 1000;
    const fresh = Number.isFinite(age) && age >= 0 && age <= 30;
    $('portfolio-candidate').disabled = !portfolioPersistent || !fresh || valuationHistoryInvalid || reconciliationInvalid;
    $('portfolio-candidate').title = fresh ? '' : 'Actualise le portefeuille : le relevé doit dater de moins de 30 secondes.';
    if (portfolioPersistent && !fresh) {
      $('candidate-status').textContent = 'Relevé périmé pour cette valorisation · clique sur « Actualiser le compte », puis recalcule.';
    }
  }
  function portfolio(snapshot) {
    $('holdings').className = ''; $('holdings').replaceChildren(); $('open-orders').replaceChildren(); $('derivatives').replaceChildren();
    portfolioPersistent = snapshot.persistent === true;
    portfolioFetchedAt = snapshot.fetched_at;
    $('account-status').textContent = 'Compte consulté'; $('holdings-time').textContent = hour(snapshot.fetched_at);
    const wallet = snapshot.wallet_balances || [];
    if (wallet.length) {
      for (const balance of wallet) {const row = node('div', undefined, 'holdings-row');
        const identity = node('div'); identity.append(node('strong', balance.currency), node('small', `Actif du compte ${balance.account_currency}`));
        const amount = node('div', undefined, 'holdings-number'); amount.append(node('strong', format(balance.quantity)), node('small', `Valeur Exchange : ${format(balance.market_value)} USD`)); row.append(identity, amount); $('holdings').append(row);}
    } else if ((snapshot.balances || []).length) {
      $('holdings').append(empty('Actifs du compte non détaillés.', 'Crypto.com a renvoyé un résumé de marge, sans liste de quantités par actif.'));
    }
    if (!wallet.length && !(snapshot.balances || []).length) $('holdings').append(empty('Aucun solde renvoyé.', 'Vérifiez le compte et les actifs dans Crypto.com Exchange.'));
    $('balance-summary').replaceChildren();
    if ((snapshot.balances || []).length) $('balance-summary').append(node('h3', 'Résumé de marge Exchange'));
    for (const balance of snapshot.balances || []) {const row = node('div', undefined, 'holdings-row');
      const imBreakdown = balance.total_position_im === null || balance.total_position_im === undefined
        ? '' : ` · IM positions ${format(balance.total_position_im)} · décote collatéral ${format(balance.total_haircut)}`;
      const values = node('div'); values.append(node('strong', `${balance.currency} · disponible pour ordre`), node('small', `Cash wallet ${format(balance.total_cash)} · marge ${format(balance.total_margin)} · marge initiale ${format(balance.initial_margin)}${imBreakdown}${Number(balance.total_borrow) ? ` · emprunt ${format(balance.total_borrow)}` : ''}${Number(balance.isolated_cash) ? ` · isolé ${format(balance.isolated_cash)}` : ''}${balance.is_liquidating ? ' · LIQUIDATION' : ''}${balance.has_isolated_positions ? ' · positions isolées' : ''}`));
      row.append(values, node('strong', format(balance.available))); $('balance-summary').append(row);}
    const orders = snapshot.open_orders || [];
    if (orders.length) $('open-orders').append(node('h2', `Ordres ouverts · ${orders.length} · lecture seule`));
    for (const order of orders) {
      const row = node('div', undefined, 'holdings-row');
      const details = node('div');
      details.append(node('strong', `${order.side} ${order.instrument_name}`),
        node('small', `${order.status} · ${order.order_type} · ID ${order.order_id}`));
      const amount = node('div', undefined, 'holdings-number');
      const [baseCurrency, quoteCurrency] = order.instrument_name.split('_');
      amount.append(node('strong', `${order.quantity} ${baseCurrency || ''}`),
        node('small', `${order.cumulative_quantity} exécuté · ${order.limit_price ? `limite ${order.limit_price} ${quoteCurrency || ''}` : 'prix marché'}`));
      row.append(details, amount); $('open-orders').append(row);
    }
    if ((snapshot.positions || []).length) $('derivatives').append(node('h2', 'Positions dérivées · séparées du spot'));
    for (const position of snapshot.positions || []) {const row = node('div', undefined, 'holdings-row'); row.append(node('strong', position.instrument_name), node('span', `${format(position.quantity)} · P&L ${format(position.open_position_pnl_usd)} USD`)); $('derivatives').append(row);}
    $('portfolio-status').textContent = snapshot.persistence_warning || snapshot.credential_warning ||
      (snapshot.persistent ? `Relevé enregistré sur ce PC · observation seulement · ${hour(snapshot.stored_at || snapshot.fetched_at)}.` : 'Compte consulté. Aucun ordre envoyé.');
    $('candidate-status').textContent = 'Choisis une devise, puis connecte le flux Spot Crypto.com avant le calcul.';
    $('candidate-result').replaceChildren();
    updatePortfolioCandidateAvailability();
  }
  let savedPortfolioBusy = false;
  async function loadSavedPortfolio() {
    if (savedPortfolioBusy) return; savedPortfolioBusy = true;
    try {
      const result = await api('/api/desk/portfolio');
      if (result.snapshot) {
        portfolio(result.snapshot);
        $('account-status').textContent = 'Dernier relevé enregistré · actualisation manuelle';
      }
    } catch (error) { portfolioFailure(error); }
    finally {savedPortfolioBusy = false;}
  }
  $('live-signal-run').addEventListener('click', async () => {
    if (!state.selected || state.source !== 'exchange') {
      $('live-signal-status').textContent = 'Sélectionne une paire Crypto.com Spot.'; return;
    }
    const button = $('live-signal-run'); button.disabled = true;
    $('live-signal-status').textContent = `Lecture du carnet live et des bougies clôturées de ${state.selected}…`;
    $('live-signal-detail-results').replaceChildren();
    try {
      const params = new URLSearchParams({symbol: state.selected, timeframe: state.period});
      const result = await api(`/api/desk/live-signal?${params}`);
      const statusNames = {SIGNAL_OBSERVED: 'Signal observé', NO_SIGNAL: 'Aucun signal', BLOCKED: 'Évaluation bloquée'};
      $('live-signal-status').textContent = `${statusNames[result.status] || result.status} · ${result.direction || 'aucune direction'} · ${hour(result.observed_at)} · ${result.reason_codes.join(', ') || 'conditions observées'}.`;
      const list = node('dl', undefined, 'live-signal-metrics');
      for (const metric of result.metrics) {
        list.append(node('dt', metric.name.replaceAll('_', ' ')), node('dd', String(metric.value)));
      }
      const note = node('p', 'Observation seulement · aucun calcul de quantité et aucun ordre autorisé.', 'microcopy');
      $('live-signal-detail-results').append(list, note);
    } catch (error) { $('live-signal-status').textContent = error.message; }
    finally { button.disabled = false; }
  });
  if (document.querySelector('.cloud-preview-banner')) {
    $('live-signal-run').disabled = true;
    $('live-signal-status').textContent = 'Cette observation live est disponible dans l’application locale. L’aperçu Railway reste limité aux marchés publics.';
    $('live-scan-status').textContent = 'Le scan continu nécessite le flux local Crypto.com; l’aperçu cloud ne démarre aucun flux.';
    for (const id of ['stream-connect', 'stream-connect-all', 'stream-disconnect']) $(id).disabled = true;
  }
  function renderTransactionHistory(snapshot) {
    const list = $('transaction-rows'); list.replaceChildren();
    if (!snapshot) { $('transaction-status').textContent = 'Aucun historique Crypto.com enregistré.'; return; }
    const coverage = snapshot.page_status === 'PAGINATED_COMPLETE'
      ? `${snapshot.page_count} pages lues; intervalle demandé couvert, historique du compte non exhaustif`
      : snapshot.page_status === 'PAGINATION_INCOMPLETE'
        ? `${snapshot.page_count || 0} pages conservées; pagination incomplète (${(snapshot.incomplete_intervals || []).length} intervalle(s) non couverts)`
        : snapshot.page_status === 'PAGE_LIMIT_REACHED'
          ? 'limite de 100 atteinte; historique incomplet'
          : 'une page lue; exhaustivité non garantie';
    $('transaction-status').textContent = `${snapshot.item_count} événements · période demandée : ${new Date(snapshot.start_time_ms).toLocaleString('fr-BE')}–${new Date(snapshot.end_time_ms).toLocaleString('fr-BE')} · dernière lecture ${new Date(snapshot.observed_at).toLocaleString('fr-BE')} · ${coverage} · observation seulement.`;
    for (const item of [...(snapshot.items || [])].reverse()) {
      const row = node('div', undefined, 'transaction-row');
      row.append(node('strong', item.journal_type), node('span', `${item.instrument_name}${item.side ? ` · ${item.side}` : ''}`),
        node('span', `P&L réalisé ${format(item.realized_pnl)} · coût ${format(item.transaction_cost)}`),
        node('span', `${hour(item.occurred_at)} · ${item.trade_id ? `Trade ${item.trade_id}` : `Journal ${item.journal_id}`}`));
      list.append(row);
    }
  }
  function renderFundingHistory(snapshot) {
    if (!snapshot) return;
    fundingStatus.textContent += `${fundingStatus.textContent ? ' · ' : ''}${snapshot.direction === 'DEPOSIT' ? 'Dépôts' : 'Retraits'} : ${snapshot.item_count} mouvement(s), ${snapshot.pages.length} page(s), ${snapshot.complete ? 'pagination terminée' : `incomplet, prochaine page ${snapshot.next_page}`}.`;
    fundingRows.append(node('h3', snapshot.direction === 'DEPOSIT' ? 'Dépôts' : 'Retraits', 'history-subheading'));
    const transferStatuses = snapshot.direction === 'DEPOSIT'
      ? {'0': 'Non arrivé', '1': 'Arrivé', '2': 'Échoué', '3': 'En attente'}
      : {'0': 'En attente', '1': 'En traitement', '2': 'Rejeté', '3': 'Paiement en cours',
         '4': 'Paiement échoué', '5': 'Terminé', '6': 'Annulé'};
    for (const item of [...snapshot.items].reverse()) {
      const row = node('div', undefined, 'transaction-row');
      row.append(node('strong', `${item.direction === 'DEPOSIT' ? 'Dépôt' : 'Retrait'} · ${item.currency}`),
        node('span', `${format(Number(item.amount))} ${item.currency} · frais ${format(Number(item.fee))} ${item.currency}`),
        node('span', `Statut ${transferStatuses[item.status] || 'Inconnu'} · ${hour(item.created_at)}`),
        node('span', `Réf. ${item.transfer_id}`));
      fundingRows.append(row);
    }
  }
  async function readFundingHistory(credentials, range, direction) {
    const result = await api('/api/cryptocom/portfolio/funding-history', {
      direction, start_time_ms: range.startMs, end_time_ms: range.endMs, ...credentials});
    renderFundingHistory(result.snapshot);
  }
  async function loadSavedFundingHistory() {
    fundingStatus.textContent = '';
    try {
      renderFundingHistory((await api('/api/desk/funding-history?direction=DEPOSIT')).snapshot);
      renderFundingHistory((await api('/api/desk/funding-history?direction=WITHDRAWAL')).snapshot);
      if (!fundingStatus.textContent) fundingStatus.textContent = 'Aucun dépôt ou retrait conservé.';
    }
    catch (error) {fundingStatus.textContent = error.message;}
  }
  async function loadSavedTransactions() {
    try {renderTransactionHistory((await api('/api/desk/transactions')).snapshot);}
    catch (error) {$('transaction-status').textContent = error.message;}
  }
  function renderTrades(snapshot) {
    const list = $('trade-rows'); list.replaceChildren();
    if (!snapshot) {$('trade-status').textContent = 'Aucune exécution Crypto.com conservée.'; return;}
    const paged = snapshot.page_status === 'PAGINATED_COMPLETE' || snapshot.page_status === 'PAGINATED_INCOMPLETE';
    const capped = snapshot.page_status === 'PAGE_LIMIT_REACHED' || snapshot.item_count === 100
      || snapshot.page_status === 'PAGINATED_INCOMPLETE';
    const coverage = paged ? `${snapshot.page_count} pages${snapshot.expected_page_count
      ? `/${snapshot.expected_page_count} attendues` : ''} / ${snapshot.request_count} requêtes · `
      + (snapshot.persistence_complete === false ? 'lot enregistré partiellement'
        : snapshot.coverage_complete ? 'période couverte'
          : `${(snapshot.incomplete_intervals || []).length} intervalle(s) incomplet(s)`)
      : capped ? 'limite atteinte, historique incomplet' : 'une page lue; exhaustivité non garantie';
    $('trade-status').textContent = `${snapshot.item_count} exécutions · ${coverage} · dernière lecture ${new Date(snapshot.observed_at).toLocaleString('fr-BE')} · lecture seule.`;
    for (const item of [...(snapshot.items || [])].reverse()) {
      const row = node('div', undefined, 'transaction-row');
      row.append(node('strong', item.side), node('span', item.instrument_name),
        node('span', `${format(item.quantity)} à ${format(item.price)} · frais ${format(item.fees)} ${item.fee_currency}`),
        node('span', `${hour(item.occurred_at)} · ordre ${item.order_id} · trade ${item.trade_id}`));
      list.append(row);
    }
  }
  async function loadSavedTrades() {
    try {renderTrades((await api('/api/desk/trades')).snapshot);}
    catch (error) {$('trade-status').textContent = error.message;}
  }
  async function readTradeHistory(credentials, range = null) {
    const end = BigInt(range?.endMs ?? Date.now()) * 1_000_000n;
    const start = BigInt(range?.startMs ?? Number(end / 1_000_000n) - 86_400_000) * 1_000_000n;
    const payload = {start_time_ns: start.toString(), end_time_ns: end.toString(), ...credentials};
    $('trade-status').textContent = 'Récupération des exécutions Crypto.com…';
    try {renderTrades((await api('/api/cryptocom/portfolio/trades', payload)).snapshot);}
    catch (error) {$('trade-status').textContent = `Exécutions indisponibles : ${error.message}`;}
  }
  function renderBalanceHistory(snapshot) {
    balanceHistoryRows.replaceChildren();
    if (!snapshot) {balanceHistoryStatus.textContent = 'Aucun historique cash conservé sur ce PC.'; return;}
    balanceHistoryStatus.textContent = `${snapshot.point_count} points ${snapshot.timeframe} · ${snapshot.currency} · relevé ${new Date(snapshot.observed_at).toLocaleString('fr-BE')} · ${snapshot.warning}`;
    for (const point of [...snapshot.points].reverse()) {
      const row = node('div', undefined, 'transaction-row');
      row.append(node('strong', new Date(point.timestamp).toLocaleString('fr-BE')),
        node('span', `Cash total · ${snapshot.currency}`), node('span', format(point.total_cash)), node('span', 'Pas une mesure d’equity'));
      balanceHistoryRows.append(row);
    }
  }
  async function loadSavedBalanceHistory() {
    try {renderBalanceHistory((await api('/api/desk/balance-history')).snapshot);}
    catch (error) {balanceHistoryStatus.textContent = error.message;}
  }
  function renderPortfolioValuations(items) {
    valuationHistoryRows.replaceChildren();
    if (!Array.isArray(items)) throw new Error('Historique de valorisation invalide.');
    const validAmount = value => typeof value === 'string' && value.trim() !== '' &&
      Number.isFinite(Number(value)) && Number(value) >= 0;
    const validEntry = item => item && item.persistent === true &&
      item.authority_status === 'BROKER_SPOT_PORTFOLIO_CANDIDATE_ONLY' &&
      item.usable_for_risk === false && item.usable_for_execution === false && item.orders_enabled === false &&
      typeof item.base_currency === 'string' && /^[A-Z]{3}$/.test(item.base_currency) &&
      typeof item.as_of === 'string' && !Number.isNaN(new Date(item.as_of).getTime()) &&
      [item.total_equity, item.cash_value, item.spot_assets_value].every(validAmount) &&
      Array.isArray(item.market_snapshot_ids) && Number.isInteger(item.valuation_id) && item.valuation_id > 0;
    if (!items.every(validEntry)) throw new Error('L’historique contient des données incohérentes.');
    if (!items.length) {valuationHistoryStatus.textContent = 'Aucune estimation candidate conservée sur ce PC.'; return;}
    valuationHistoryStatus.textContent = `${items.length} dernières observations locales · aucune utilisée par Risk ou les ordres · pas un P&L.`;
    for (const item of items) {
      const row = node('div', undefined, 'transaction-row');
      row.append(node('strong', `${format(item.total_equity)} ${item.base_currency}`),
        node('span', new Date(item.as_of).toLocaleString('fr-BE')),
        node('span', `Cash ${format(item.cash_value)} · actifs Spot ${format(item.spot_assets_value)}`),
        node('span', `${item.market_snapshot_ids.length} cours vérifiés · ID ${item.valuation_id}`));
      valuationHistoryRows.append(row);
    }
  }
  async function loadPortfolioValuations() {
    try {
      renderPortfolioValuations((await api('/api/desk/portfolio-valuations')).valuations);
      valuationHistoryInvalid = false;
      updatePortfolioCandidateAvailability();
    }
    catch (error) {
      valuationHistoryInvalid = true;
      updatePortfolioCandidateAvailability();
      valuationHistoryRows.replaceChildren();
      valuationHistoryStatus.textContent = `Historique indisponible : ${error.message} Aucun chiffre conservé n’est affiché.`;
      $('candidate-result').replaceChildren();
      $('portfolio-candidate').disabled = true;
    }
  }
  const reconciliationReasonLabels = {
    TWO_VALUATIONS_REQUIRED: 'Deux valorisations chronologiques sont nécessaires',
    VALUATION_CHRONOLOGY: 'Dates des valorisations incohérentes',
    VALUATION_CURRENCY_MISMATCH: 'Devises des valorisations différentes',
    PORTFOLIO_OBSERVATION_MISSING: 'Dernier relevé de compte absent',
    OPEN_ORDERS_OBSERVATION_INVALID: 'Lecture des ordres ouverts invalide',
    OPEN_ORDERS_OBSERVATION_MISSING: 'Lecture des ordres ouverts absente',
    OPEN_ORDER_RESERVATIONS_NOT_RECONCILED: 'Les réservations des ordres ouverts ne sont pas rapprochées',
    PORTFOLIO_VALUATION_SOURCE_MISMATCH: 'La valorisation ne correspond plus au dernier relevé : actualise le compte, puis recalcule sa valeur Spot',
    OPEN_ORDER_RESERVATIONS_MISMATCH: 'Les ordres ouverts ne correspondent pas aux montants réservés dans la valorisation',
    DERIVATIVE_POSITIONS_UNSUPPORTED: 'Positions dérivées présentes',
    ITEMIZED_WALLET_MISSING: 'Soldes détaillés par actif absents',
    CASH_HISTORY_MISSING: 'Historique cash absent',
    CASH_SERIES_NOT_EQUITY: 'Historique cash différent de la valeur totale du portefeuille',
    TRANSACTION_HISTORY_MISSING: 'Aucun journal des transactions conservé',
    TRANSACTION_PAGE_AT_LIMIT: 'Page de 100 transactions potentiellement tronquée',
    TRANSACTION_WINDOW_GAPS: 'Période entre valorisations non couverte par les journaux',
    TRADE_HISTORY_MISSING: 'Aucun historique des exécutions conservé',
    TRADE_PAGE_AT_LIMIT: 'Page de 100 exécutions potentiellement tronquée',
    TRADE_BATCH_PAGE_MISSING: 'Un lot d’exécutions conservé ne contient pas toutes ses pages',
    TRADE_BATCH_METADATA_CONFLICT: 'Les métadonnées de pagination d’un lot se contredisent',
    TRADE_WINDOW_GAPS: 'Période entre valorisations non couverte par les exécutions',
    TRANSACTION_TRADING_JOURNAL_UNMATCHED: 'Une ligne de trade ne correspond à aucune exécution Crypto.com',
    TRADE_JOURNAL_IDENTITY_CONFLICT: 'Identifiants ou sens de trade incompatibles entre les deux journaux',
    CONFLICTING_TRADE_ID: 'Même exécution avec des contenus contradictoires',
    EXECUTED_TRADES_NOT_FULLY_RECONCILED: 'Exécutions, frais et mouvements du compte encore incomplets',
    CONFLICTING_JOURNAL_ID: 'Même événement avec des contenus contradictoires',
    PER_ASSET_POSTINGS_MISSING: 'Mouvements signés par actif manquants',
    DEPOSIT_WITHDRAWAL_PROOF_MISSING: 'Dépôts et retraits non rapprochés',
    FUNDING_HISTORY_WINDOW_GAPS: 'Historique dédié des dépôts/retraits incomplet, non final ou hors période',
    CONVERSION_LEGS_UNMATCHED: 'Deux jambes des conversions non rapprochées',
    FEE_CURRENCY_AND_POSTINGS_MISSING: 'Devises et montants réellement débités en frais inconnus',
    COMPLETE_PROVIDER_PAGINATION_UNPROVEN: 'Exhaustivité des pages Crypto.com non prouvée',
    OPENING_CLOSING_BALANCES_UNPROVEN: 'Soldes initiaux et finaux non attestés',
    PNL_NOT_COMPUTED: 'P&L journalier non calculé; le contrôle de perte journalière reste indisponible'
  };
  async function loadReconciliation() {
    reconciliationRows.replaceChildren();
    reconciliationStatus.textContent = 'Vérification des preuves conservées…';
    try {
      const report = (await api('/api/desk/reconciliation')).reconciliation;
      if (!report || !['BLOCKED_INCOMPLETE', 'BLOCKED_CONTRADICTORY'].includes(report.status) ||
          report.usable_for_pnl !== false || report.usable_for_risk !== false ||
          report.usable_for_execution !== false || report.orders_enabled !== false ||
          !Array.isArray(report.reasons) || !report.event_counts) {
        throw new Error('Réponse de rapprochement non fiable.');
      }
      reconciliationStatus.textContent = report.status === 'BLOCKED_CONTRADICTORY'
        ? 'BLOQUÉ · preuves contradictoires · aucune autorisation Risk'
        : 'INCOMPLET · données insuffisantes pour un P&L ou Risk';
      const counts = report.event_counts;
      const summary = node('div', undefined, 'transaction-row');
      summary.append(node('strong', `${report.distinct_journals} journaux distincts`),
        node('span', `Dépôts ${counts.deposits} · retraits ${counts.withdrawals} · conversions ${counts.conversions}`),
        node('span', `Trades ${counts.trades} (${report.executed_trade_count} fills) · frais ${counts.fees} · autres ${counts.other}`));
      const funding = report.funding_history_counts || {};
      summary.append(node('span', `Historique dédié : dépôts ${funding.completed_deposits || 0}/${funding.deposits || 0} arrivés · retraits ${funding.completed_withdrawals || 0}/${funding.withdrawals || 0} terminés · période ${report.funding_history_coverage_verified ? 'couverte' : 'incomplète'}`));
      summary.append(node('span', `Ordres ouverts : ${report.open_order_count || 0} · ${report.open_orders_bound_to_latest_valuation ? 'réservations reliées' : 'réservations non rapprochées'}`));
      reconciliationRows.append(summary);
      for (const reason of report.reasons) {
        reconciliationRows.append(node('div', reconciliationReasonLabels[reason] || 'Contrôle bloquant : ' + reason, 'transaction-row'));
      }
      for (const warning of report.warnings || []) {
        if (warning === 'CASH_HISTORY_IS_CASH_ONLY') {
          reconciliationRows.append(node('p', 'L’historique de cash reste informatif; il ne sert pas d’equity totale.', 'microcopy'));
        }
      }
      if (report.period_pnl && report.period_pnl.authority_status === 'RECONCILED_PERIOD_OBSERVATION_ONLY' &&
          report.period_pnl.daily_pnl === false) {
        const period = report.period_pnl;
        reconciliationRows.append(node('p',
          `Variation de valeur rapprochée sur la période ${new Date(period.start).toLocaleString('fr-BE')}–${new Date(period.end).toLocaleString('fr-BE')} : ${period.amount} ${period.currency}. Ce n’est pas un P&L journalier et cela n’autorise aucun ordre.`, 'microcopy'));
      }
      const daily = report.daily_pnl_observation;
      if (daily && daily.authority_status === 'RECONCILED_UTC_DAY_OBSERVATION_ONLY' &&
          daily.daily_pnl === true && daily.usable_for_risk === false &&
          daily.usable_for_execution === false && daily.orders_enabled === false &&
          typeof daily.amount === 'string' && typeof daily.currency === 'string' &&
          typeof daily.start === 'string' && Number.isFinite(new Date(daily.start).getTime()) &&
          typeof daily.end === 'string' && Number.isFinite(new Date(daily.end).getTime())) {
        reconciliationRows.append(node('p',
          `Observation rapprochée du ${new Date(daily.start).toLocaleDateString('fr-BE')} (minuit UTC–${new Date(daily.end).toLocaleTimeString('fr-BE')}) : ${daily.amount} ${daily.currency}. Elle reste informative et n’autorise ni Risk ni les ordres.`, 'microcopy'));
      }
      reconciliationRows.append(node('p', `Preuve SHA-256 : ${report.evidence_sha256} · P&L journalier et autorisation Risk non établis.`, 'microcopy'));
      reconciliationInvalid = false;
      updatePortfolioCandidateAvailability();
    } catch (error) {
      reconciliationInvalid = true;
      updatePortfolioCandidateAvailability();
      reconciliationRows.replaceChildren();
      $('candidate-result').replaceChildren();
      $('portfolio-candidate').disabled = true;
      reconciliationStatus.textContent = `Rapprochement indisponible : ${error.message} · Risk et ordres désactivés.`;
    }
  }
  async function readBalanceHistory(credentials) {
    balanceHistoryStatus.textContent = 'Lecture de l’historique cash Crypto.com…';
    try {
      const result = await api('/api/cryptocom/portfolio/balance-history', {timeframe: 'H1', limit: 24, ...credentials});
      renderBalanceHistory(result.snapshot);
    } catch (error) {balanceHistoryStatus.textContent = `Historique cash indisponible : ${error.message}`;}
  }
  async function readTransactionHistory(credentials, range = null) {
    const end = range?.endMs ?? Date.now();
    const payload = {start_time_ms: range?.startMs ?? end - 86_400_000, end_time_ms: end, ...credentials};
    $('transaction-status').textContent = 'Lecture de l’historique Crypto.com…';
    try {const result = await api('/api/cryptocom/portfolio/transactions', payload); renderTransactionHistory(result.snapshot);}
    catch (error) {$('transaction-status').textContent = `Portefeuille lu. Historique indisponible : ${error.message}`;}
  }
  function selectedHistoryRange() {
    const from = $('history-from').value, to = $('history-to').value;
    if (!from || !to) throw new Error('Choisis une date de début et une date de fin.');
    const startParts = from.split('-').map(Number), endParts = to.split('-').map(Number);
    const startMs = new Date(startParts[0], startParts[1] - 1, startParts[2]).getTime();
    const endMs = new Date(endParts[0], endParts[1] - 1, endParts[2] + 1).getTime();
    if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || startMs >= endMs)
      throw new Error('La période choisie est invalide.');
    if (endMs - startMs > 7 * 86_400_000)
      throw new Error('Choisis au maximum sept jours; répète ensuite la lecture pour la semaine précédente.');
    if (endMs > Date.now() + 60_000) throw new Error('La période ne peut pas être dans le futur.');
    return {startMs, endMs};
  }
  async function readSelectedHistory(range) {
    $('history-load').disabled = true; $('history-last-day').disabled = true;
    $('history-range-status').textContent = 'Lecture des écritures, exécutions, dépôts et retraits… Les réponses peuvent prendre quelques secondes.';
    try {
      const credentials = selectedHistoryCredentials();
      fundingRows.replaceChildren(); fundingStatus.textContent = '';
      await readTransactionHistory(credentials, range);
      await readTradeHistory(credentials, range);
      await readFundingHistory(credentials, range, 'DEPOSIT');
      await readFundingHistory(credentials, range, 'WITHDRAWAL');
      $('history-range-status').textContent = `Période interrogée : ${new Date(range.startMs).toLocaleString('fr-BE')} – ${new Date(range.endMs).toLocaleString('fr-BE')}. La complétude affichée dépend des réponses de Crypto.com.`;
      await loadReconciliation();
    } catch (error) { $('history-range-status').textContent = `Lecture impossible : ${error.message}`; }
    finally { renderSavedCredentialControls(); }
  }
  const historyToday = new Date();
  $('history-to').value = new Date(historyToday.getTime() - historyToday.getTimezoneOffset() * 60_000).toISOString().slice(0, 10);
  $('history-from').value = new Date(historyToday.getTime() - historyToday.getTimezoneOffset() * 60_000 - 6 * 86_400_000).toISOString().slice(0, 10);
  $('history-load').addEventListener('click', () => {
    try { readSelectedHistory(selectedHistoryRange()); }
    catch (error) { $('history-range-status').textContent = error.message; }
  });
  $('history-last-day').addEventListener('click', () => readSelectedHistory({startMs: Date.now() - 86_400_000, endMs: Date.now()}));
  loadSavedFundingHistory();
  async function readFeeSchedule(credentials) {
    feeStatus.textContent = 'Frais Spot : lecture du barème effectif Crypto.com…';
    try {
      const fee = await api('/api/cryptocom/portfolio/fees', credentials);
      feeStatus.textContent = `Frais Spot effectifs · maker ${format(Number(fee.maker_rate_bps) / 100)} % · taker ${format(Number(fee.taker_rate_bps) / 100)} % · niveau ${fee.spot_tier} · ${new Date(fee.observed_at).toLocaleString('fr-BE')} · observation seulement.`;
    } catch (error) {feeStatus.textContent = `Frais Spot indisponibles : ${error.message}`;}
  }
  function portfolioFailure(error) {
    portfolioPersistent = false; portfolioFetchedAt = null;
    $('portfolio-status').textContent = error.message; $('account-status').textContent = 'Lecture échouée';
    $('portfolio-candidate').disabled = true; $('candidate-result').replaceChildren();
    $('holdings').replaceChildren(empty('Relevé indisponible.', 'La lecture a échoué. Les anciens soldes ne sont plus affichés.')); $('balance-summary').replaceChildren(); $('open-orders').replaceChildren(); $('derivatives').replaceChildren(); $('holdings-time').textContent = '—';
  }
  $('account-form').addEventListener('submit', async event => {
    event.preventDefault(); $('account-connect').disabled = true; $('portfolio-status').textContent = 'Lecture du compte…';
    const credentials = {api_key: $('account-key').value, api_secret: $('account-secret').value};
    try {portfolio(await api('/api/cryptocom/portfolio/import', {...credentials, remember_credentials: $('remember').checked})); await vaultState(); await readTransactionHistory(credentials); await readTradeHistory(credentials); await readFeeSchedule(credentials); await readBalanceHistory(credentials); await loadReconciliation();}
    catch (error) {portfolioFailure(error);}
    finally {$('account-key').value = ''; $('account-secret').value = ''; $('account-connect').disabled = false;}
  });
  $('saved-refresh').addEventListener('click', async () => {
    $('saved-refresh').disabled = true;
    try {const credentials = selectedHistoryCredentials(); portfolio(await api('/api/cryptocom/portfolio/refresh', credentials)); await readTransactionHistory(credentials); await readTradeHistory(credentials); await readFeeSchedule(credentials); await readBalanceHistory(credentials); await loadReconciliation();}
    catch (error) {$('portfolio-status').textContent = error.message;} finally {renderSavedCredentialControls();}
  });
  const riskReasonLabels = {
    REAL_ORDER_EXECUTION_DISABLED: 'Exécution réelle désactivée',
    RISK_POLICY_NOT_APPROVED: 'Aucune politique Risk approuvée',
    RISK_RESERVATIONS_NOT_IMPLEMENTED: 'Réservation atomique des fonds réels non raccordée',
    RISK_INPUT_LINEAGE_NOT_APPROVED: 'Preuves non homologuées pour Risk',
    DAILY_PNL_UNVERIFIED: 'Perte quotidienne réelle non vérifiée',
    OWNER_LIMITS_MISSING: 'Plafonds du propriétaire absents',
    OWNER_LIMITS_ARE_DRAFT_ONLY: 'Plafonds enregistrés seulement comme brouillon',
    COMPLETE_RISK_POLICY_MISSING: 'Politique Risk complète non définie',
    BROKER_PORTFOLIO_MISSING: 'Relevé Crypto.com du compte absent',
    BROKER_PORTFOLIO_OBSERVATION_ONLY: 'Relevé Crypto.com : observation seulement',
    DERIVATIVES_OR_MARGIN_NOT_RECONCILED: 'Dérivés ou marge non rapprochés',
    VALUATION_EVIDENCE_MISSING: 'Valorisation Spot candidate absente',
    VALUATION_CANDIDATE_ONLY: 'Valorisation non autoritaire',
    VALUATION_CURRENCY_MISMATCH: 'Devise du brouillon et de la valorisation incompatibles',
    PORTFOLIO_RECONCILIATION_BLOCKED_INCOMPLETE: 'Rapprochement financier incomplet',
    PORTFOLIO_RECONCILIATION_BLOCKED_CONTRADICTORY: 'Rapprochement financier contradictoire',
    UNEXPECTED_ORDER_GATE_CONNECTED: 'ALERTE : une passerelle d’ordre inattendue a été détectée',
    ORDER_AUTHORIZATION_GATE_ABSENT: 'Aucune passerelle d’autorisation d’ordre connectée'
  };
  async function loadRiskReadiness() {
    riskAuditReasons.replaceChildren();
    riskAuditStatus.textContent = 'Inspection locale des contrôles Risk…';
    try {
      const report = (await api('/api/desk/risk-readiness')).readiness;
      if (!report || report.status !== 'BLOCKED' || report.trading_state !== 'NOT_READY' ||
          report.authority_status !== 'READ_ONLY_RISK_PREPARATION' ||
          report.risk_policy_active !== false || report.risk_approved !== false ||
          report.armed !== false || report.orders_enabled !== false ||
          report.usable_for_risk !== false || report.usable_for_execution !== false ||
          !Array.isArray(report.reason_codes) || !report.audit_sha256 ||
          !/^[0-9a-f]{64}$/.test(report.audit_sha256)) {
        throw new Error('Statut Risk incohérent : refus de présenter une autorisation.');
      }
      riskAuditStatus.textContent = 'BLOQUÉ · trading non armé · aucun ordre autorisé';
      for (const reason of report.reason_codes) {
        riskAuditReasons.append(node('li', riskReasonLabels[reason] || 'Blocage Risk : ' + reason));
      }
      riskAuditReasons.append(node('li', 'Audit SHA-256 : ' + report.audit_sha256));
    } catch (error) {
      riskAuditReasons.replaceChildren();
      riskAuditStatus.textContent = 'Contrôle Risk indisponible : ' + error.message +
        ' · aucune autorisation ne peut être inférée.';
    }
  }
  const orderChainLabels = {
    RISK_NOT_READY: 'Risk non approuvé ou données financières non réconciliées',
    DURABLE_ORDER_JOURNAL_MISSING: 'Journal durable de production non raccordé',
    ATOMIC_APPROVAL_CONSUMPTION_NOT_IMPLEMENTED: 'Approbation atomique de production non raccordée',
    CROSS_RESTART_IDEMPOTENCY_UNVERIFIED: 'Anti-doublon du circuit réel après redémarrage non prouvé',
    UNCERTAIN_POST_RESPONSE_RECOVERY_NOT_IMPLEMENTED: 'Reprise après réponse incertaine non raccordée',
    ORDER_FILL_ACCOUNT_RECONCILIATION_MISSING: 'Exécutions non rapprochées aux soldes du compte',
    REAL_ORDER_ROUTES_DISABLED: 'Routes d’ordres réels désactivées',
    ORDER_GATE_NOT_CONNECTED: 'Passerelle Risk / Politique / validation humaine absente',
    UNEXPECTED_ORDER_GATE_CONNECTED: 'ALERTE : passerelle d’ordres connectée sans homologation',
    SPOT_INSTRUMENT_VERIFIER_MISSING: 'Métadonnées Spot non vérifiées'
  };
  async function loadOrderChainAudit() {
    orderChainReasons.replaceChildren();
    orderChainStatus.textContent = 'Vérification de la chaîne d’ordres…';
    try {
      const report = (await api('/api/desk/order-chain-audit')).audit;
      if (!report || report.status !== 'BLOCKED' ||
          report.authority_status !== 'ORDER_CHAIN_READ_ONLY_AUDIT' ||
          report.armed !== false || report.orders_enabled !== false ||
          report.can_submit !== false || report.retry_automatically !== false ||
          report.recovery_ready !== false || typeof report.durable_journal_ready !== 'boolean' ||
          typeof report.manual_readback_available !== 'boolean' ||
          report.usable_for_execution !== false || report.usable_for_risk !== false ||
          !Array.isArray(report.reasons) || !/^[0-9a-f]{64}$/.test(report.audit_sha256)) {
        throw new Error('La chaîne d’ordres a renvoyé un état non fiable.');
      }
      orderChainStatus.textContent = 'BLOQUÉ · aucun ordre · aucune reprise automatique';
      orderChainReasons.append(node('li', report.durable_journal_ready
        ? 'Journal durable : intégrité contrôlée; son état ne constitue pas une autorisation d’ordre.'
        : 'Journal durable : indisponible; aucun ordre autorisé.'));
      if (report.manual_readback_available)
        orderChainReasons.append(node('li', 'Relecture manuelle du statut Exchange raccordée; aucune nouvelle tentative ne sera envoyée.'));
      const journal = (await api('/api/desk/live-order-journal')).journal;
      if (!journal || journal.status !== 'INTEGRITY_OK' ||
          journal.authority_status !== 'LIVE_ORDER_JOURNAL_EVIDENCE_ONLY' ||
          journal.orders_enabled !== false || !Array.isArray(journal.intents)) {
        throw new Error('Le journal durable des ordres est invalide.');
      }
      if (!journal.intents.length) {
        orderChainReasons.append(node('li', 'Aucune tentative d’ordre réel enregistrée.'));
      }
      for (const item of journal.intents.slice(-20).reverse()) {
        if (!item || typeof item.client_oid !== 'string' || typeof item.state !== 'string' ||
            !Number.isInteger(item.event_count) || !Number.isInteger(item.readback_count) ||
            typeof item.submission_uncertain !== 'boolean') {
          throw new Error('Une entrée du journal durable est invalide.');
        }
        const orderRef = item.client_oid.slice(0, 8);
        const readback = item.exchange_status
          ? ` · ${item.exchange_status} · exécuté ${item.executed_quantity}` : ' · lecture Exchange en attente';
        const uncertain = item.submission_uncertain ? ' · envoi à réconcilier' : '';
        const entry = node('li', `Ordre ${orderRef} · ${item.state}${readback}${uncertain}`);
        if (report.manual_readback_available && ['ATTEMPTED', 'UNCERTAIN', 'ACKNOWLEDGED'].includes(item.state)) {
          const button = node('button', 'Relire le statut', 'button button-white');
          button.type = 'button';
          button.addEventListener('click', async () => {
            button.disabled = true;
            try {
              const result = (await api('/api/cryptocom/orders/readback', {
                use_saved_credentials: true, client_oid: item.client_oid})).order;
              if (!result || result.client_oid !== item.client_oid ||
                  result.authority_status !== 'BROKER_ORDER_READBACK_EVIDENCE_ONLY' ||
                  result.orders_enabled !== false) throw new Error('La relecture n’est pas une preuve Exchange reconnue.');
              toast(`Statut relu : ${result.status} · exécuté ${result.executed_quantity} · aucune reprise automatique.`);
              await loadOrderChainAudit();
            } catch (error) { toast(`Relecture impossible : ${error.message}`); }
            finally { button.disabled = false; }
          });
          entry.append(button);
        }
        orderChainReasons.append(entry);
      }
      for (const reason of report.reasons) {
        orderChainReasons.append(node('li', orderChainLabels[reason] || 'Contrôle bloquant : ' + reason));
      }
      orderChainReasons.append(node('li', 'Audit SHA-256 : ' + report.audit_sha256));
    } catch (error) {
      orderChainReasons.replaceChildren();
      orderChainStatus.textContent = 'Audit indisponible : ' + error.message +
        ' · aucun ordre autorisé.';
    }
  }
  async function loadRiskLimits() {
    try {
      const {candidate} = await api('/api/desk/risk-limits');
      if (!candidate) return;
      $('risk-currency').value = candidate.base_currency;
      $('risk-capital').value = candidate.capital_limit;
      $('risk-order').value = candidate.max_order_notional;
      $('risk-daily-loss').value = candidate.max_daily_loss;
      $('risk-limits-summary').textContent = `${candidate.capital_limit} ${candidate.base_currency} de capital · ${candidate.max_order_notional} ${candidate.base_currency} par ordre · ${candidate.max_daily_loss} ${candidate.base_currency} de perte/jour.`;
      $('risk-limit-status').textContent = 'Brouillon';
      $('risk-limits-status').textContent = 'Brouillon local en attente; non approuvé et non actif.';
    } catch (error) { $('risk-limits-status').textContent = error.message; }
  }
  $('risk-limits-form').addEventListener('submit', async event => {
    event.preventDefault();
    const submit = $('risk-limits-submit'); submit.disabled = true;
    $('risk-limits-status').textContent = 'Vérification et enregistrement du brouillon…';
    try {
      const {candidate} = await api('/api/desk/risk-limits', {
        base_currency: $('risk-currency').value,
        capital_limit: $('risk-capital').value,
        max_order_notional: $('risk-order').value,
        max_daily_loss: $('risk-daily-loss').value,
      });
      $('risk-limits-summary').textContent = `${candidate.capital_limit} ${candidate.base_currency} de capital · ${candidate.max_order_notional} ${candidate.base_currency} par ordre · ${candidate.max_daily_loss} ${candidate.base_currency} de perte/jour.`;
      $('risk-limit-status').textContent = 'Brouillon';
      $('risk-limits-status').textContent = 'Enregistré localement comme candidat en attente. Aucun ordre ne peut utiliser ce brouillon.';
      await loadRiskReadiness();
    } catch (error) { $('risk-limits-status').textContent = error.message; }
    finally { submit.disabled = false; }
  });
  $('portfolio-candidate').addEventListener('click', async () => {
    const baseCurrency = $('portfolio-base').value;
    if (!baseCurrency) { $('candidate-status').textContent = 'Choisis d’abord la devise d’estimation.'; return; }
    $('portfolio-candidate').disabled = true; $('candidate-status').textContent = 'Vérification du relevé et des cours Spot temps réel…';
    $('candidate-result').replaceChildren();
    try {
      const result = await api('/api/cryptocom/portfolio/candidate', {base_currency: baseCurrency});
      $('candidate-status').textContent = `Estimation candidate · ${result.authority_status} · aucun ordre`;
      const summary = node('div', undefined, 'candidate-summary');
      summary.append(node('strong', `${format(result.total_equity)} ${result.base_currency}`),
        node('small', `Cash ${format(result.cash_value)} · actifs Spot ${format(result.spot_assets_value)} · ${result.market_snapshot_ids.length} cours vérifiés`));
      $('candidate-result').append(summary);
      for (const item of result.held_asset_values || []) {
        $('candidate-result').append(node('div', `${item.asset} · ${format(item.value)} ${result.base_currency}`, 'candidate-row'));
      }
      for (const item of result.order_reservations || []) {
        $('candidate-result').append(node('div', `Ordre ${item.order_id} réservé · ${format(item.value)} ${result.base_currency}`, 'candidate-row'));
      }
      await loadPortfolioValuations();
      await loadReconciliation();
    } catch (error) { $('candidate-status').textContent = error.message; }
    finally { updatePortfolioCandidateAvailability(); }
  });
  $('saved-forget').addEventListener('click', async () => {
    try {await api('/api/cryptocom/portfolio/forget', {forget_saved_credentials: true});
      portfolioPersistent = false; portfolioFetchedAt = null;
      $('holdings').replaceChildren(empty('Votre portefeuille, ici.', 'La clé enregistrée a été oubliée.')); $('open-orders').replaceChildren(); $('derivatives').replaceChildren(); $('account-status').textContent = 'Non connecté'; $('holdings-time').textContent = '—'; $('portfolio-status').textContent = 'Clé oubliée; le dernier relevé reste conservé localement.'; await vaultState();}
    catch (error) {toast(error.message);}
  });
  async function loadSignalScan() {
    if (state.scanBusy || state.source !== 'exchange') return;
    state.scanBusy = true;
    try {
      const scan = await api('/api/desk/live-signals');
      const freshnessWindow = Number.isFinite(scan.freshness_window_seconds) ? scan.freshness_window_seconds : 30;
      const isFresh = row => {
        if (row.fresh === true) return true;
        if (row.fresh === false) return false;
        const age = (Date.now() - Date.parse(row.observed_at)) / 1000;
        return Number.isFinite(age) && age >= 0 && age <= freshnessWindow;
      };
      const freshRows = scan.results.filter(isFresh);
      const counts = scan.fresh_status_counts || freshRows.reduce((result, row) => {
        result[row.status] = (result[row.status] || 0) + 1; return result;
      }, {});
      const signalRows = scan.results.filter(row => row.status === 'SIGNAL_OBSERVED');
      const freshSignalRows = signalRows.filter(isFresh);
      const freshObservationCount = Number.isInteger(scan.fresh_observations_count)
        ? scan.fresh_observations_count : freshRows.length;
      const staleObservationCount = Number.isInteger(scan.stale_observations_count)
        ? scan.stale_observations_count : Math.max(0, scan.results.length - freshObservationCount);
      const freshSignalCount = Number.isInteger(scan.fresh_signals_count) ? scan.fresh_signals_count : freshSignalRows.length;
      const staleSignalCount = Number.isInteger(scan.stale_signals_count) ? scan.stale_signals_count
        : Math.max(0, signalRows.length - freshSignalRows.length);
      const progress = `${freshObservationCount} / ${scan.symbols_total} paires observées depuis ${freshnessWindow} s · passage ${scan.scan_pass} (${scan.scanned_count} parcourues) · ${freshSignalCount} signal(aux) récent(s) · ${staleSignalCount} périmé(s) · ${counts.NO_SIGNAL || 0} sans signal · ${counts.BLOCKED || 0} bloquée(s) · ${staleObservationCount} observation(s) périmée(s)`;
      $('live-scan-status').textContent = scan.status === 'RUNNING'
        ? `Scan continu actif · ${progress}${scan.current_symbol ? ` · en cours : ${scan.current_symbol}` : ''}. Aucun ordre n’est envoyé.`
        : `Scan ${scan.status.toLowerCase()} · ${progress}. Aucun ordre n’est envoyé.`;
      const results = $('live-scan-results'); results.replaceChildren();
      const signals = freshSignalRows
        .sort((left, right) => Date.parse(right.observed_at) - Date.parse(left.observed_at)).slice(0, 20);
      if (!signals.length) results.append(node('p', scan.stale_signals_count
        ? 'Aucun signal assez récent. Les signaux plus anciens sont masqués.'
        : 'Aucun signal qualifié et récent dans les paires déjà parcourues.', 'microcopy'));
      else for (const signal of signals) {
        const row = node('div', undefined, 'live-scan-row');
        const age = Number.isFinite(signal.age_seconds) ? signal.age_seconds
          : Math.max(0, (Date.now() - Date.parse(signal.observed_at)) / 1000);
        row.append(node('strong', signal.symbol.replace('_', ' / ')), node('span', signal.direction || 'Signal observé'),
          node('span', `${Math.round(age)} s`), node('span', (signal.reason_codes || []).join(', ') || 'Conditions satisfaites'));
        results.append(row);
      }
    } catch (error) { $('live-scan-status').textContent = `Scan indisponible : ${error.message}`; }
    finally { state.scanBusy = false; }
  }
  async function streamState() {
    try {const result = await api('/api/market-data/state');
      const streamActive = Array.isArray(result.symbols) && result.symbols.length > 0 &&
        result.connection_status !== 'DISCONNECTED';
      $('stream-connect').disabled = streamActive;
      $('stream-connect-all').disabled = streamActive;
      $('stream-disconnect').disabled = !streamActive;
      const requested = (result.symbols || []).length, subscribed = (result.subscribed_symbols || []).length;
      const candleFeed = result.candlestick_timeframe ? ` · bougies ${result.candlestick_timeframe}` : '';
      $('stream-status').textContent = result.connection_status === 'CONNECTED'
        ? `Flux actif · ${subscribed} / ${requested} paires souscrites${candleFeed}`
        : result.connection_status === 'SUBSCRIBING'
          ? `Connexion en cours · ${subscribed} / ${requested} paires confirmées${candleFeed}`
          : result.connection_status === 'ERROR'
            ? result.subscription_error || 'Le flux public est en erreur. Déconnectez puis réessayez.'
            : result.connection_status;
      const now = Date.now();
      const freshQuotes = (result.quotes || []).filter(quote => {
        const received = Date.parse(quote.received_at);
        return Number.isFinite(received) && now >= received && now - received <= 30000;
      });
      state.streamQuotes = new Map(freshQuotes.map(quote => [quote.symbol, quote]));
      applyStreamQuotes();
      if (result.connection_status === 'CONNECTED' && !state.scanTimer) {
        state.scanTimer = setInterval(loadSignalScan, 5000); await loadSignalScan();
      } else if (result.connection_status !== 'CONNECTED' && state.scanTimer) {
        clearInterval(state.scanTimer); state.scanTimer = null;
      }
      if (result.connection_status !== 'CONNECTED') {
        $('live-scan-status').textContent = 'Connecte Crypto.com pour lancer le scan continu.';
      }
      $('stream-quotes').replaceChildren();
      for (const quote of freshQuotes.slice(0, 24)) {const row = node('div', undefined, 'stream-row');
        const detail = quote.quote_status === 'COMPLETE' ? `Bid ${format(quote.best_bid)} × ${format(quote.best_bid_size || 0)} · Ask ${format(quote.best_ask)} × ${format(quote.best_ask_size || 0)}` : `Carnet ${quote.quote_status === 'CROSSED' ? 'croisé' : 'incomplet'}`;
        row.append(node('strong', quote.symbol), node('span', `Trade ${format(quote.last_trade_price || quote.price)} ${quote.currency || ''}`), node('span', detail), node('span', hour(quote.provider_timestamp)));
        const evidence = node('button', 'Évaluer pour Risk', 'text-link');
        evidence.disabled = quote.quote_status !== 'COMPLETE';
        evidence.title = evidence.disabled ? 'Un bid/ask complet est requis' : 'Archive la preuve de prix; aucune décision ni aucun ordre';
        evidence.addEventListener('click', async event => {event.stopPropagation(); evidence.disabled = true;
          try {const result = await api('/api/market-evidence', {symbol: quote.symbol});
            toast(result.eligible_for_risk_price_candidate
              ? `${result.symbol} · prix médian ${format(result.price)} ${result.currency} admissible comme candidat Risk · aucun ordre`
              : `${result.symbol} archivé mais refusé par Risk : ${(result.assessment_reasons || []).join(', ')}`);}
          catch (error) {toast(error.message);} finally {evidence.disabled = false;}});
        row.append(evidence); $('stream-quotes').append(row);}
      if (freshQuotes.length > 24) $('stream-quotes').append(node('p', `${freshQuotes.length - 24} autres cotations sont visibles dans Marchés.`,'microcopy'));}
    catch {$('stream-status').textContent = 'État du flux indisponible.'; $('stream-quotes').replaceChildren(); state.streamQuotes.clear(); applyStreamQuotes(); await refreshExchange();}
    if (state.source === 'exchange') updateSelected();
  }
  async function connectStream(path, payload) {
    $('stream-connect').disabled = true; $('stream-connect-all').disabled = true;
    try {await api(path, payload);
      $('stream-disconnect').disabled = false; if (state.streamTimer) clearInterval(state.streamTimer);
      state.streamTimer = setInterval(streamState, 1000); await streamState();}
    catch (error) {toast(error.message); $('stream-connect').disabled = false; $('stream-connect-all').disabled = false;}
  }
  $('stream-form').addEventListener('submit', event => {
    event.preventDefault(); connectStream('/api/market-data/connect', {provider: 'cryptocom', symbols: $('stream-symbols').value});
  });
  $('stream-connect-all').addEventListener('click', () =>
    connectStream('/api/market-data/connect-all', {provider: 'cryptocom', all_spot: true}));
  $('stream-disconnect').addEventListener('click', async () => {
    try {await api('/api/market-data/disconnect', {confirm_disconnect: true}); clearInterval(state.streamTimer); state.streamTimer = null;
      state.streamQuotes.clear(); $('stream-connect').disabled = false; $('stream-connect-all').disabled = false; $('stream-disconnect').disabled = true; $('stream-quotes').replaceChildren(); $('stream-status').textContent = 'Flux arrêté.'; await refreshExchange();}
    catch (error) {toast(error.message);}
  });
  api('/api/desk/state').then(result => {state.watchlist = result.watchlist; metrics(); renderMarkets(); renderWatchlist();}).catch(error => toast(error.message));
  setInterval(updatePortfolioCandidateAvailability, 1000);
  setInterval(() => {if (!document.querySelector('.cloud-preview-banner')) loadPortfolioObserverState();}, 5000);
  view(location.hash.slice(1)); vaultState(); refreshExchange(); refreshGecko();
  if (!document.querySelector('.cloud-preview-banner')) {
    state.streamTimer = setInterval(streamState, 1000); streamState();
  }
  setInterval(refreshExchange, 15000); setInterval(refreshGecko, 60000);
})();
