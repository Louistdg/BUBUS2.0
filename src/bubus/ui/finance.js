(() => {
  const $ = id => document.getElementById(id);
  const element = (tag, text) => Object.assign(document.createElement(tag), {textContent:text});
  let wallet, busy=false, hidden=localStorage.getItem('bibus.privacy')==='true', rules=[], pushKey=null, pushSubscription=null;
  const number=n=>Number(n).toLocaleString('fr-FR',{maximumFractionDigits:10});
  const euro=n=>hidden?'•••• €':n==null?'Indisponible':Number(n).toLocaleString('fr-FR',{style:'currency',currency:'EUR'});
  const time=t=>new Date(t).toLocaleString('fr-FR');
  function listRow(title,detail,value){
    const li=element('li',''),body=element('span',title);
    body.append(element('small',detail));li.append(body,element('b',value));return li;
  }
  function transactions(){
    const section=wallet?.transactions;
    $('tx-status').textContent=section?.partial?'Historique partiel.':'';
    const q=$('search').value.trim().toLowerCase();
    const rows=(section?.rows||[]).map(r=>{
      const ts=Number(r.processed_date || r.created_date);
      const detail=[ts?time(ts<1e11?ts*1000:ts):'',r.status].filter(Boolean).join(' · ');
      const value=hidden?'••••':[r.source,r.destination].filter(Boolean).join(' → ');
      return listRow(r.type||'Opération',detail,value);
    }).filter(row=>row.textContent.toLowerCase().includes(q));
    $('transactions').replaceChildren(...(rows.length?rows:[element('li',q?'Aucun résultat.':'Aucune opération reçue.')]));
  }
  const alertStatus = message => { $('alert').textContent = message; };
  async function alertApi(payload) {
    const response = await fetch('/api/desk/price-alerts', payload ? {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)
    } : {cache:'no-store'});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error);
    rules = result.rules;
    pushKey = result.public_key;
    showRules();
  }
  function pushOptions() {
    $('enable-push').disabled = !pushKey || !!pushSubscription;
    $('test-push').disabled = !pushSubscription;
    $('rule').querySelector('button').disabled = !pushSubscription;
  }
  async function loadAlerts() {
    try {
      await alertApi();
      if (!('serviceWorker' in navigator) || !('PushManager' in window)) throw new Error('Ouvre BUBUS dans Chrome sur Android.');
      await navigator.serviceWorker.register('/sw.js');
      const registration = await navigator.serviceWorker.ready;
      pushSubscription = await registration.pushManager.getSubscription();
    } catch (error) { alertStatus(error.message); }
    pushOptions();
  }
  $('enable-push').onclick = async () => {
    $('enable-push').disabled = true;
    try {
      if (await Notification.requestPermission() !== 'granted') throw new Error('Autorise les notifications de BUBUS dans Chrome.');
      const padded = pushKey.replace(/-/g, '+').replace(/_/g, '/') + '=';
      const applicationServerKey = Uint8Array.from(atob(padded), value => value.charCodeAt(0));
      const registration = await navigator.serviceWorker.ready;
      pushSubscription = await registration.pushManager.subscribe({userVisibleOnly:true, applicationServerKey});
      let previous = [];
      try { previous = JSON.parse(localStorage.getItem('bibus.priceAlerts') || '[]'); } catch {}
      if (Array.isArray(previous)) {
        for (const item of previous) {
          if (!item?.symbol || !item.currency || !item.limit) continue;
          try {
            await alertApi({action:'add', symbol:item.symbol, currency:item.currency,
                            direction:item.direction, threshold:item.limit, subscription:pushSubscription});
          } catch { continue; }
        }
      }
      alertStatus('Les notifications sont activées sur cet appareil.');
    } catch (error) { alertStatus(error.message); }
    pushOptions();
  };
  $('test-push').onclick = async () => {
    const button = $('test-push');
    button.disabled = true;
    try {
      await alertApi({action:'test', subscription:pushSubscription});
      alertStatus('Ferme BUBUS. Un franchissement fictif sera testé dans 25 à 45 secondes.');
    } catch (error) { alertStatus(error.message); }
    finally { button.disabled = false; }
  };
  function showRules(){
    $('rules').replaceChildren(...rules.map(rule=>{
      const status = {active:'En attente', sending:'Envoi lancé', uncertain:'Envoi non confirmé', sent:'Envoyée', failed:'Échec de notification'}[rule.state];
      const row = element('li', rule.symbol+' '+(rule.direction==='above'?'≥':'≤')+' '+number(rule.threshold)+' '+rule.currency+' · '+status);
      const remove = element('button','Supprimer');
      remove.onclick = async () => {
        try { await alertApi({action:'remove', id:rule.id}); }
        catch (error) { alertStatus(error.message); }
      };
      row.append(remove);
      return row;
    }));
  }
  function render(){
    $('total').textContent=euro(wallet.total_eur);
    $('status').textContent='Relevé du '+time(wallet.observed)+(wallet.missing.length?' · Valorisation incomplète : '+wallet.missing.join(', '):'');
    $('balances').replaceChildren(...wallet.balances.map(r=>listRow(r.currency,hidden?'••••':number(r.total)+' '+r.currency,euro(r.eur))));
    $('market').replaceChildren(...wallet.market.map(r=>listRow(r.symbol,r.source+(r.stamp?' · '+time(r.stamp*1000):''),r.price==null?'Indisponible':number(r.price)+' '+r.currency)));
    const selected=$('asset').value;
    $('asset').replaceChildren(...wallet.market.filter(r=>r.price!=null).map(r=>new Option(r.symbol+' ('+r.currency+')',r.symbol)));
    if(wallet.market.some(r=>r.symbol===selected)) $('asset').value=selected;
    transactions();
  }
  async function refresh(){
    if(busy)return;busy=true;$('refresh').disabled=true;
    try{
      const response=await fetch('/api/desk/patrimoine',{cache:'no-store'});
      if(response.status===401){location.assign('/login');return;}
      if(!response.ok)throw new Error('Service indisponible.');
      const next=await response.json();if(!next.connected)throw new Error(next.reason);
      wallet=next;render();
    }catch(e){$('status').textContent=(wallet?'Dernier relevé du '+time(wallet.observed)+' · ':'')+'Actualisation impossible. '+e.message;}
    finally{busy=false;$('refresh').disabled=false;}
  }
  $('rule').onsubmit = async event => {
    event.preventDefault();
    const quote = wallet?.market.find(q => q.symbol === $('asset').value);
    const threshold = $('threshold').value;
    if (!quote || !pushSubscription || !(Number(threshold) > 0)) return;
    $('rule').querySelector('button').disabled = true;
    try {
      await alertApi({action:'add', symbol:quote.symbol, currency:quote.currency,
                      direction:$('direction').value, threshold, subscription:pushSubscription});
      $('threshold').value = '';
      alertStatus('Seuil enregistré. Une notification sera envoyée au prochain franchissement.');
    } catch (error) { alertStatus(error.message); }
    finally { pushOptions(); }
  };
  $('privacy').onclick=()=>{hidden=!hidden;localStorage.setItem('bibus.privacy',hidden);if(wallet)render();};
  document.documentElement.classList.toggle('light',localStorage.getItem('bibus.light')==='true');
  $('theme').onclick=()=>localStorage.setItem('bibus.light',document.documentElement.classList.toggle('light'));
  $('search').oninput=transactions;$('refresh').onclick=refresh;pushOptions();loadAlerts();lucide.createIcons();refresh();
  setInterval(()=>{if(!document.hidden)refresh();},30000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
})();
