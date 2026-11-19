(() => {
  const $ = id => document.getElementById(id);
  const element = (tag, text) => Object.assign(document.createElement(tag), {textContent:text});
  let wallet, busy=false, hidden=localStorage.getItem('bibus.privacy')==='true', rules=[];
  try { rules=JSON.parse(localStorage.getItem('bibus.priceAlerts') || '[]'); } catch {}
  const persist=()=>localStorage.setItem('bibus.priceAlerts',JSON.stringify(rules));
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
  function showRules(){
    $('rules').replaceChildren(...rules.map(rule=>{
      const row=element('li',rule.symbol+' '+(rule.direction==='above'?'≥':'≤')+' '+number(rule.limit)+' '+rule.currency);
      const del=element('button','Supprimer');del.onclick=()=>{rules=rules.filter(r=>r!==rule);persist();showRules();};row.append(del);return row;
    }));
  }
  function checkRules(){
    for(const rule of rules){
      const quote=wallet.market.find(q=>q.symbol===rule.symbol && q.currency===rule.currency);
      if(!quote || quote.price==null) continue;
      const hit=rule.direction==='above'?Number(quote.price)>=rule.limit:Number(quote.price)<=rule.limit;
      if(hit&&!rule.hit){$('alert').textContent='Seuil atteint pour '+rule.symbol+' · '+number(quote.price)+' '+rule.currency+' · cours du '+time(quote.stamp*1000);}
      rule.hit=hit;
    }
    persist();
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
      wallet=next;render();checkRules();
    }catch(e){$('status').textContent=(wallet?'Dernier relevé du '+time(wallet.observed)+' · ':'')+'Actualisation impossible. '+e.message;}
    finally{busy=false;$('refresh').disabled=false;}
  }
  $('rule').onsubmit=e=>{
    e.preventDefault();const quote=wallet?.market.find(q=>q.symbol===$('asset').value),limit=Number($('threshold').value);
    if(!quote||!(limit>0)||!Number.isFinite(limit))return;
    rules.push({symbol:quote.symbol,currency:quote.currency,direction:$('direction').value,limit,hit:false});
    showRules();checkRules();$('threshold').value='';
  };
  $('privacy').onclick=()=>{hidden=!hidden;localStorage.setItem('bibus.privacy',hidden);if(wallet)render();};
  document.documentElement.classList.toggle('light',localStorage.getItem('bibus.light')==='true');
  $('theme').onclick=()=>localStorage.setItem('bibus.light',document.documentElement.classList.toggle('light'));
  $('search').oninput=transactions;$('refresh').onclick=refresh;showRules();lucide.createIcons();refresh();
  setInterval(()=>{if(!document.hidden)refresh();},30000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
})();
