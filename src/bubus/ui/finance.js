(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const accountNames = ['Compte courant', 'Épargne / Coffre-fort', 'Actions & ETF', 'Crypto-actifs'];
  const initial = [4320.4, 10000, 7240, 3290];
  const ago = days => new Date(Date.now() - days * 86400000).toISOString();
  const examples = [['Salaire', 2350, 0, 3], ['Courses', -64.8, 0, 1], ['Dividende', 28.4, 2, 5], ['Abonnement', -12, 0, 6], ['Intérêts', 16.5, 1, 8]];
  let state = {balances:initial.slice(), tx:examples.map(([label,amount,account,days]) => ({label,amount,account,date:ago(days)})), frozen:false, hidden:false, light:false};
  try { const saved = JSON.parse(localStorage.getItem('bibus.finance.v1')); if(saved?.balances?.length === 4 && Array.isArray(saved.tx)) state = saved; } catch {}
  let period = '1M', type = 'Toutes', account = null, action, chart;
  const durations = {'1J':1,'1S':7,'1M':30,'1A':365,'Tout':720};
  let quotes = {};
  const total = () => state.balances.reduce((a,b) => a+b,0);
  const money = n => state.hidden ? '•••••• €' : n.toLocaleString('fr-FR',{style:'currency',currency:'EUR'});
  function save(){try {localStorage.setItem('bibus.finance.v1',JSON.stringify(state));} catch {notice('Stockage local indisponible.');}}
  function notice(message){$('notice').textContent=message;setTimeout(()=>{if($('notice').textContent===message)$('notice').textContent='';},2500);}
  function el(tag,text){const item=document.createElement(tag);item.textContent=text;return item;}
  function button(text,fn,pressed){const btn=el('button',text);btn.type='button';btn.onclick=fn;if(pressed!==undefined)btn.setAttribute('aria-pressed',pressed);return btn;}
  function series(){
    const days=durations[period], base=initial.reduce((a,b)=>a+b);
    return Array.from({length:41},(_,i)=>{
      const back=days*(40-i)/40;
      return i===40?total():base/(1+0.0014*back)+42*Math.sin(back/3)+24*Math.sin(back*1.5);
    });
  }
  function draw(){
    const data=series(),labels=data.map((_,i)=>new Date(Date.now()-durations[period]*(40-i)/40*86400000).toLocaleString('fr-FR', period==='1J'?{hour:'2-digit',minute:'2-digit'}:{month:'short',day:'numeric'}));
    const delta=total()-data[0];
    $('change').textContent=state.hidden?'••••••':(delta>=0?'+':'')+(delta/data[0]*100).toFixed(2)+' % · '+money(delta)+' sur '+period;
    const ctx=$('chart').getContext('2d'),fill=ctx.createLinearGradient(0,0,0,240);
    fill.addColorStop(0,'#6366f199');fill.addColorStop(1,'#6366f100');
    if(!chart) chart=new Chart(ctx,{type:'line',data:{labels,datasets:[{data,tension:.35,borderColor:'#8198ff',backgroundColor:fill,fill:true,pointRadius:0,pointHoverRadius:4}]},options:{responsive:true,maintainAspectRatio:false,interaction:{intersect:false,mode:'index'},plugins:{legend:{display:false},tooltip:{callbacks:{label:v=>money(v.parsed.y)}}},scales:{x:{grid:{display:false},ticks:{maxTicksLimit:5}},y:{display:false}}}});
    else {chart.data.labels=labels;chart.data.datasets[0].data=data;chart.update();}
  }
  function render(){
    document.documentElement.classList.toggle('light',state.light);
    $('total').textContent=money(total());$('privacy').setAttribute('aria-label',state.hidden?'Afficher les montants':'Masquer les montants');
    $('card').classList.toggle('frozen',state.frozen);$('freeze').checked=state.frozen;
    $('accounts').replaceChildren(...accountNames.map((name,i)=>{const b=button('',()=>{account=account===i?null:i;render();$('operations').scrollIntoView();},account===i);b.append(el('small',name),el('b',money(state.balances[i])));return b;}));
    $('periods').replaceChildren(...Object.keys(durations).map(p=>button(p,()=>{period=p;render();},period===p)));
    $('types').replaceChildren(...['Toutes','Entrées','Dépenses'].map(t=>button(t,()=>{type=t;render();},type===t)));
    const needle=$('search').value.toLocaleLowerCase('fr');
    const rows=state.tx.filter(r=>(account===null||r.account===account)&&(type==='Toutes'||(type==='Entrées'?r.amount>0:r.amount<0))&&r.label.toLocaleLowerCase('fr').includes(needle)).sort((a,b)=>b.date.localeCompare(a.date));
    $('transactions').replaceChildren(...rows.map(r=>{const li=el('li',''),icon=el('i','');icon.dataset.lucide=r.amount>=0?'arrow-down-left':'arrow-up-right';const body=el('span',r.label);body.append(el('small',new Date(r.date).toLocaleDateString('fr-FR')+' · Comptabilisée'));const amount=el('b',(r.amount>0&&!state.hidden?'+':'')+money(r.amount));amount.className=r.amount>=0?'up':'down';li.append(icon,body,amount);return li;}));
    if(!rows.length)$('transactions').append(el('li','Aucune transaction pour cette sélection.'));
    lucide.createIcons();draw();
  }
  function openAction(name){
    action=name;$('confirm').disabled=false;$('title').textContent=name;$('fields').replaceChildren();$('estimate').textContent='';$('confirm').hidden=name==='Plus';
    if(name==='Plus'){
      const link=el('a','Ouvrir le portefeuille Revolut X');link.href='/#portfolio';$('fields').append(link);
    }else{
      const label=el('label','Montant en euros'),input=el('input','');Object.assign(input,{id:'amount',type:'number',min:0.01,max:10000000,step:0.01,required:true});label.append(input);$('fields').append(label);
      if(name==='Envoyer'){const to=el('label','Bénéficiaire'),field=el('input','');Object.assign(field,{id:'recipient',required:true,maxLength:70});to.append(field);$('fields').append(to);}
      if(name==='Convertir'){
        const target=el('label','Devise cible'),sel=el('select','');
        sel.id='currency';
        for(const code of ['USD','BTC']) sel.add(new Option(code,code));
        target.append(sel);$('fields').append(target);
        quotes={};
        function calc(){
          const quote=quotes[sel.value];
          $('confirm').disabled=!quote;
          $('estimate').textContent=quote
            ? (Number(input.value)*quote.rate).toLocaleString('fr-FR',{maximumFractionDigits:8})+' '+sel.value+' · cours du '+new Date(quote.timestamp*1000).toLocaleString('fr-FR')
            : 'Cours indisponible pour cette devise.';
        }
        sel.onchange=input.oninput=calc;
        $('confirm').disabled=true;
        $('estimate').textContent='Lecture du dernier cours…';
        fetch('/api/desk/conversion-quotes',{cache:'no-store'}).then(r=>r.ok?r.json():{}).catch(()=>({})).then(data=>{
          if(sel.isConnected){quotes=data;calc();}
        });
      }
    }
    $('modal').showModal();
  }
  $('form').onsubmit=e=>{
    e.preventDefault();const n=Number($('amount').value),now=new Date().toISOString();
    if(!(n>0)||!Number.isFinite(n))return;
    if(action!=='Ajouter'&&n>state.balances[0]){$('estimate').textContent='Solde du compte courant insuffisant.';return;}
    if(action==='Convertir' && !quotes[$('currency').value]) return;
    const signed=action==='Ajouter'?n:-n;
    const label=action==='Envoyer'?'Envoi à '+$('recipient').value.trim():action==='Ajouter'?'Ajout au compte':'Conversion en '+$('currency').value;
    state.balances[0]=Math.round((state.balances[0]+signed)*100)/100;
    state.tx.unshift({label,amount:signed,date:now,account:0});
    if(action==='Convertir'){
      const fx=$('currency').value,destination=fx==='BTC'?3:1;
      state.balances[destination]+=n;state.tx.unshift({label:'Conversion reçue · '+(n*quotes[fx].rate).toLocaleString('fr-FR',{maximumFractionDigits:8})+' '+fx,amount:n,date:now,account:destination});
    }
    account=null;type='Toutes';$('search').value='';save();render();$('modal').close();notice('Simulation enregistrée.');
  };
  for(const [name,icon] of [['Ajouter','plus'],['Envoyer','send'],['Convertir','repeat-2'],['Plus','ellipsis']]){const b=button('',()=>openAction(name));const i=el('i','');i.dataset.lucide=icon;b.append(i,el('span',name));$('actions').append(b);}
  $('close').onclick=()=>$('modal').close();
  $('privacy').onclick=()=>{state.hidden=!state.hidden;save();render();};
  $('theme').onclick=()=>{state.light=!state.light;save();render();};
  $('search').oninput=render;
  $('card').onclick=()=>$('card').classList.toggle('flipped');
  $('freeze').onchange=()=>{state.frozen=$('freeze').checked;save();render();};
  $('copy').onclick=async()=>{try{await navigator.clipboard.writeText('4242424242424242');notice('Numéro de simulation copié.');}catch{notice('Retourne la carte pour lire son numéro.');}};
  render();
})();
