// Practice-management UI: tabbed patient chart, insurance claims, morning huddle and KPIs.
// Loaded after the main Hub script; it replaces patientDialog and extends the dashboard + appointment popup.
const money=(n,d=2)=>'$'+(+n||0).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
const mon0=n=>money(n,0);
const clock=s=>new Date(s).toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});
const dshort=s=>s?new Date(s).toLocaleDateString([],{month:'short',day:'numeric'}):'—';
const chips=a=>a.map(x=>`<span class="achip">${esc(x)}</span>`).join('');
const pill=s=>`<span class="pill ${esc(s)}">${esc(s)}</span>`;
const COND_COLORS={cavity:'#fecaca',filled:'#bfdbfe',crown:'#ddd6fe',root_canal:'#fed7aa',implant:'#99f6e4',missing:'#e2e8f0',watch:'#fef08a',healthy:'#fff'};
let PTAB='chart',FEES=null,EDIT_NOTE=null;
const TABS=[['chart','Chart'],['plan','Treatment plan'],['med','Medical & insurance'],['notes','Notes'],['acct','Account']];

// ---------------------------------------------------------------- patient chart (tabbed)
async function patientDialog(id,selTooth,tab){
  if(tab&&tab!==PTAB){PTAB=tab;EDIT_NOTE=null}
  if(!FEES)FEES=await api('/api/fees');
  const [r,ch,pr,es,lk]=await Promise.all([api('/api/patients/'+id),api(`/api/patients/${id}/chart`),api(`/api/patients/${id}/profile`),
    api(`/api/patients/${id}/estimate`),api(`/api/patients/${id}/plan-link`)]);
  const p=r.patient,d=$('#dlg'),ins=pr.insurance,med=pr.medical;
  const body={chart:()=>tabChart(r,ch,selTooth),plan:()=>tabPlan(ch,es,pr,lk),med:()=>tabMed(med,ins,pr),notes:()=>tabNotes(pr),acct:()=>tabAcct(r,pr,id)}[PTAB]();
  d.style.minWidth='min(780px,94vw)';
  d.innerHTML=`<div class="phead"><div><h2>${esc(p.name)}</h2><div class="hint">${phone(p.phone)} · ${esc(p.email||'no email on file')} · ${ins?esc(ins.carrier):'Self-pay'}</div></div>
    <div class="pbal ${pr.balance>0?'due':''}">Balance due<b>${money(pr.balance)}</b></div></div>
    ${pr.alerts.length?`<div class="alertbar">⚠ ${chips(pr.alerts)}</div>`:''}
    <div class="tabs">${TABS.map(([k,l])=>`<button data-tab="${k}" class="${k===PTAB?'on':''}">${l}</button>`).join('')}</div>
    <div id="tabbody">${body}</div><p id="cmsg" class="hint"></p>
    <div class="row" style="margin-top:8px"><button class="btn alt" id="x">Close</button></div>`;
  const msg=(t,ok)=>{const el=$('#cmsg');if(el){el.textContent=t;el.className=ok?'msgok':'msgbad'}};
  const again=()=>patientDialog(id,selTooth,PTAB);
  d.onclick=async e=>{const t=e.target.closest('button,a');if(!t)return;const ds=t.dataset;
    try{
      if(t.id==='x'){d.close();d.style.minWidth='';return}
      if(ds.tab)return patientDialog(id,selTooth,ds.tab);
      if(ds.tooth)return patientDialog(id,+ds.tooth,'chart');
      if(t.id==='tsave'){await api(`/api/patients/${id}/teeth/${selTooth}`,{method:'PUT',body:{condition:$('#tc').value,note:$('#tn').value||null}});return again()}
      if(ds.ts){await api('/api/treatments/'+ds.tid,{method:'PATCH',body:{status:ds.ts}});return again()}
      if(t.id==='tadd'){const code=$('#tcode').value,fee=$('#tf').value;
        await api(`/api/patients/${id}/treatments`,{method:'POST',body:{code:code||null,procedure:$('#tp').value,tooth:$('#tt').value?+$('#tt').value:null,fee:fee!==''?+fee:null}});return again()}
      if(t.id==='sendplan'){const o=await api(`/api/patients/${id}/plan/send`,{method:'POST'});
        if(o.emailed&&o.local_link)return msg(`Plan emailed to ${o.email}, but the link points to this computer. Set PUBLIC_URL to your public address so patients can open it.`,false);
        return msg(o.emailed?`Plan emailed to ${o.email}.`:(o.email?'Saved to the Messages outbox. Gmail is not configured, so nothing was emailed.':'This patient has no email on file.'),o.emailed)}
      if(t.id==='copylink'){const u=lk.url||location.origin+lk.path;try{await navigator.clipboard.writeText(u.startsWith('http')?u:location.origin+u)}catch(_){}return msg('Link copied.',true)}
      if(t.id==='msave'){await api(`/api/patients/${id}/medical`,{method:'PUT',body:{allergies:$('#ma').value,conditions:$('#mc').value,medications:$('#mm').value,premed:$('#mp').checked}});return again()}
      if(t.id==='isave'){const v=k=>$('#i_'+k).value,n=k=>+v(k)||0;
        await api(`/api/patients/${id}/insurance`,{method:'PUT',body:{carrier:v('carrier'),member_id:v('member'),group_no:v('group'),subscriber:v('sub'),annual_max:n('max'),used:n('used'),
          deductible:n('ded'),deductible_met:n('dmet'),preventive_pct:n('pp'),basic_pct:n('bp'),major_pct:n('mp')}});return again()}
      if(t.id==='irem'){await api(`/api/patients/${id}/insurance`,{method:'DELETE'});return again()}
      if(t.id==='nsave'){const b={subjective:$('#n_s').value,objective:$('#n_o').value,assessment:$('#n_a').value,plan:$('#n_p').value};
        if(EDIT_NOTE){await api('/api/notes/'+EDIT_NOTE,{method:'PUT',body:b});EDIT_NOTE=null}else await api(`/api/patients/${id}/notes`,{method:'POST',body:b});return again()}
      if(ds.nedit){const n=pr.notes.find(x=>x.id==ds.nedit);EDIT_NOTE=n.id;await again();
        $('#n_s').value=n.subjective||'';$('#n_o').value=n.objective||'';$('#n_a').value=n.assessment||'';$('#n_p').value=n.plan||'';return}
      if(ds.nsign){await api(`/api/notes/${ds.nsign}/sign`,{method:'POST'});return again()}
      if(ds.pay){await api(`/api/invoices/${ds.pay}/pay`,{method:'POST'});return again()}
    }catch(err){msg(err.message,false)}};
  if(!d.open)d.showModal()}

function tabChart(r,ch,sel){
  const by={};ch.teeth.forEach(t=>by[t.tooth]=t);
  const tb=n=>`<button class="tooth c-${by[n]?.condition||'healthy'} ${sel===n?'sel':''}" data-tooth="${n}" title="${by[n]?by[n].condition+(by[n].note?': '+by[n].note:''):'healthy'}">${n}</button>`;
  const upper=Array.from({length:16},(_,i)=>tb(i+1)).join(''),lower=Array.from({length:16},(_,i)=>tb(32-i)).join('');
  const editor=sel?`<div class="row"><b>Tooth #${sel}</b><select id="tc" style="width:auto">${TOOTH_CONDS.map(c=>`<option ${by[sel]?.condition===c?'selected':''}>${c}</option>`).join('')}</select>
    <input id="tn" style="flex:1;width:auto" placeholder="Note (e.g. MOD decay)" value="${esc(by[sel]?.note||'')}"><button class="btn sm" id="tsave">Save tooth</button></div>`:
    '<p class="hint">Click a tooth to record its condition.</p>';
  return `<div class="teeth">${upper}</div><div class="teeth">${lower}</div>
    <div class="legend">${Object.entries(COND_COLORS).map(([k,c])=>`<span><i style="background:${c}"></i>${k.replace('_',' ')}</span>`).join('')}</div>${editor}
    <h3>Visits</h3>${r.appointments.slice(0,8).map(a=>`<div>${when(a.start)} · ${esc(CFG.types[a.type]?.name||a.type)} ${pill(a.status)}</div>`).join('')||'<div class="hint">None yet</div>'}`}

function tabPlan(ch,es,pr,lk){
  const E={};es.items.forEach(i=>E[i.id]=i);const b=es.benefits;
  const rows=ch.treatments.map(t=>{const e=E[t.id],open=t.status==='planned'||t.status==='accepted';
    return `<tr><td>${esc(t.code||'')}</td><td>${t.tooth?'#'+t.tooth:''}</td><td>${esc(t.procedure)}</td><td class="r">${money(t.fee)}</td>
      <td class="r">${e?money(e.insurance):'<span class="hint">—</span>'}</td><td class="r">${e?'<b>'+money(e.patient)+'</b>':'<span class="hint">—</span>'}</td><td>${pill(t.status)}</td>
      <td>${t.status==='planned'?`<button class="btn sm alt" data-ts="accepted" data-tid="${t.id}">Accept</button> `:''}${open?`<button class="btn sm ok" data-ts="done" data-tid="${t.id}">Done + bill</button> <button class="btn sm alt" data-ts="declined" data-tid="${t.id}">Decline</button>`:''}</td></tr>`}).join('');
  const cats=[...new Set(FEES.map(f=>f.category))];
  const opts=`<option value="">Custom procedure…</option>`+cats.map(c=>`<optgroup label="${esc(c)}">${FEES.filter(f=>f.category===c).map(f=>`<option value="${f.code}">${f.code} · ${esc(f.name)} · ${mon0(f.fee)}</option>`).join('')}</optgroup>`).join('');
  const t=es.totals;
  return `<div class="hint" style="margin-bottom:8px">${b?`<b>${esc(es.plan.carrier)}</b>: ${mon0(b.remaining)} of ${mon0(b.annual_max)} left this year · deductible remaining ${mon0(b.deductible_remaining)}`:'No insurance on file: patient pays the full fee. Add a plan under Medical &amp; insurance.'}</div>
    <table><thead><tr><th>Code</th><th>Tooth</th><th>Procedure</th><th class="r">Fee</th><th class="r">Insurance est.</th><th class="r">Patient est.</th><th>Status</th><th></th></tr></thead>
    <tbody>${rows||'<tr><td colspan="8" class="hint">No treatment planned yet.</td></tr>'}
    ${es.items.length?`<tr class="tot"><td colspan="3">Open plan total</td><td class="r">${money(t.fee)}</td><td class="r">${money(t.insurance)}</td><td class="r">${money(t.patient)}</td><td colspan="2"></td></tr>`:''}</tbody></table>
    <div class="row" style="margin-top:10px"><select id="tcode" style="flex:2;min-width:220px">${opts}</select><input id="tp" placeholder="Name (custom only)" style="width:150px">
      <input id="tt" type="number" min="1" max="32" placeholder="Tooth #" style="width:80px"><input id="tf" type="number" min="0" placeholder="Fee override" style="width:110px"><button class="btn" id="tadd">Add</button></div>
    <div class="row"><a class="btn alt" target="_blank" href="${lk.path}">Present / print estimate</a><button class="btn alt" id="sendplan">Email plan to patient</button><button class="btn alt" id="copylink">Copy patient link</button></div>
    <p class="hint">The patient link opens a clean estimate they can accept online. Finishing a treatment raises a claim for the insurance share and invoices only the patient share.</p>`}

function tabMed(m,i,pr){
  const v=(k,d='')=>esc(i?.[k]??d),used=i?Math.min(100,100*i.used/Math.max(1,i.annual_max)):0;
  return `<div class="two"><div class="sub"><h3>Medical history &amp; alerts</h3>
      <label class="hint">Allergies (comma separated)</label><input id="ma" style="width:100%" value="${esc(m.allergies)}" placeholder="Penicillin, Latex">
      <label class="hint">Medical conditions</label><input id="mc" style="width:100%" value="${esc(m.conditions)}" placeholder="Hypertension, Diabetes">
      <label class="hint">Medications</label><input id="mm" style="width:100%" value="${esc(m.medications)}" placeholder="Warfarin, Lisinopril">
      <label style="display:block;margin:8px 0"><input type="checkbox" id="mp" ${m.premed?'checked':''}> Needs antibiotic premedication before treatment</label>
      <button class="btn" id="msave">Save medical history</button>
      <p class="hint">These show as red alerts on the chart, the calendar popup and the morning huddle.</p></div>
    <div class="sub"><h3>Dental insurance</h3>
      <div class="fld"><label class="hint full">Carrier<input id="i_carrier" style="width:100%" value="${v('carrier')}" placeholder="Delta Dental PPO"></label>
        <label class="hint">Member ID<input id="i_member" style="width:100%" value="${v('member_id')}"></label><label class="hint">Group #<input id="i_group" style="width:100%" value="${v('group_no')}"></label>
        <label class="hint full">Subscriber<input id="i_sub" style="width:100%" value="${v('subscriber')}"></label>
        <label class="hint">Annual max ($)<input id="i_max" type="number" style="width:100%" value="${v('annual_max',1500)}"></label><label class="hint">Used this year ($)<input id="i_used" type="number" style="width:100%" value="${v('used',0)}"></label>
        <label class="hint">Deductible ($)<input id="i_ded" type="number" style="width:100%" value="${v('deductible',50)}"></label><label class="hint">Deductible met ($)<input id="i_dmet" type="number" style="width:100%" value="${v('deductible_met',0)}"></label>
        <label class="hint">Preventive %<input id="i_pp" type="number" style="width:100%" value="${v('preventive_pct',100)}"></label><label class="hint">Basic %<input id="i_bp" type="number" style="width:100%" value="${v('basic_pct',80)}"></label>
        <label class="hint">Major %<input id="i_mp" type="number" style="width:100%" value="${v('major_pct',50)}"></label></div>
      ${i?`<div class="meter"><i style="width:${used}%"></i></div><div class="hint">${mon0(pr.benefits.remaining)} remaining of ${mon0(i.annual_max)}</div>`:''}
      <div class="row" style="margin-top:8px"><button class="btn" id="isave">${i?'Update plan':'Add plan'}</button>${i?'<button class="btn alt" id="irem">Remove plan</button>':''}</div></div></div>`}

function tabNotes(pr){
  const f=(id,l,ph)=>`<label class="hint">${l}</label><textarea id="${id}" rows="2" style="width:100%" placeholder="${ph}"></textarea>`;
  return `<div class="sub"><h3>${EDIT_NOTE?'Edit draft note':'New clinical note (SOAP)'}</h3>${f('n_s','Subjective: what the patient reports','Pain on lower left when chewing…')}
    ${f('n_o','Objective: findings and exam','Visible decay #19, percussion positive…')}${f('n_a','Assessment: diagnosis','Irreversible pulpitis…')}${f('n_p','Plan: treatment and follow-up','Root canal, then crown…')}
    <div class="row" style="margin-top:8px"><button class="btn" id="nsave">${EDIT_NOTE?'Save changes':'Save draft'}</button></div></div>
    <h3>History</h3>${pr.notes.map(n=>`<div class="note ${n.signed_at?'locked':''}"><div class="row" style="margin:0"><b>${dshort(n.created_at)} · ${esc(n.provider_name||'')}</b>
      ${n.signed_at?`<span class="pill paid">Signed ${dshort(n.signed_at)} · locked</span>`:`<span class="pill draft">Draft</span><button class="btn sm alt" data-nedit="${n.id}">Edit</button><button class="btn sm ok" data-nsign="${n.id}">Sign &amp; lock</button>`}</div>
      ${[['S',n.subjective],['O',n.objective],['A',n.assessment],['P',n.plan]].filter(x=>x[1]).map(([k,x])=>`<p><b>${k}</b> ${esc(x)}</p>`).join('')}</div>`).join('')||'<p class="hint">No notes yet.</p>'}`}

function tabAcct(r,pr,id){
  return `<div class="row"><a class="btn alt" target="_blank" href="/statement/${id}">Print statement</a></div>
    <h3>Invoices</h3>${r.invoices.map(i=>`<div class="row" style="margin:3px 0"><span style="min-width:70px"><b>${money(i.amount)}</b></span><span style="flex:1">${esc(i.description)}</span>${pill(i.status)}
      ${i.status==='unpaid'?`<button class="btn sm ok" data-pay="${i.id}">Mark paid</button>`:''}</div>`).join('')||'<div class="hint">None</div>'}
    <h3>Insurance claims</h3>${pr.claims.map(c=>`<div class="row" style="margin:3px 0"><span style="min-width:70px"><b>${money(c.expected)}</b></span><span style="flex:1">${esc(c.carrier)}: ${esc(c.description)}${c.note?` <span class="hint">(${esc(c.note)})</span>`:''}</span>${pill(c.status)}</div>`).join('')||'<div class="hint">None</div>'}
    <h3>Messages</h3>${r.messages.slice(0,5).map(m=>`<div class="hint">${esc(m.kind)}: ${esc(m.body)}</div>`).join('')||'<div class="hint">None</div>'}`}

// ---------------------------------------------------------------- insurance claims page
async function loadClaims(){
  const all=await api('/api/claims'),st=$('#claimStatus').value,list=st?all.filter(c=>c.status===st):all;
  const sum=s=>all.filter(c=>c.status===s).reduce((a,c)=>a+(s==='paid'?c.paid:c.expected),0),cnt=s=>all.filter(c=>c.status===s).length;
  $('#claimStats').innerHTML=[['Ready to submit','draft'],['Waiting on insurance','submitted'],['Paid','paid'],['Denied','denied']].map(([l,s])=>
    `<div class="card stat"><b>${mon0(sum(s))}</b><span>${l} · ${cnt(s)} claim(s)</span></div>`).join('');
  $('#claimbody').innerHTML=list.map(c=>`<tr><td>${c.id}</td><td>${esc(c.patient_name)}</td><td>${esc(c.code||'')} ${esc(c.description)}</td><td>${esc(c.carrier)}</td><td>${money(c.billed)}</td>
    <td>${money(c.expected)}</td><td>${c.status==='paid'?money(c.paid):'—'}</td><td>${pill(c.status)}${c.note?`<div class="hint">${esc(c.note)}</div>`:''}</td>
    <td>${c.status==='draft'?`<button class="btn sm" data-cs="submit" data-cid="${c.id}">Submit</button>`:''}${c.status==='submitted'?`<button class="btn sm ok" data-cs="pay" data-cid="${c.id}">Mark paid</button> <button class="btn sm bad" data-cs="deny" data-cid="${c.id}">Denied</button>`:''}</td></tr>`).join('')
    ||'<tr><td colspan="9" class="hint">No claims. Finish a treatment for a patient with insurance to create one.</td></tr>'}
$('#claimStatus').onchange=loadClaims;
$('#claimbody').onclick=async e=>{const b=e.target.closest('button');if(!b||!b.dataset.cs)return;const id=b.dataset.cid,all=await api('/api/claims'),c=all.find(x=>x.id==id),d=$('#dlg');
  try{
    if(b.dataset.cs==='submit'){await api(`/api/claims/${id}/submit`,{method:'POST'});return loadClaims()}
    d.innerHTML=b.dataset.cs==='pay'?`<h2>Insurance payment</h2><p>${esc(c.patient_name)} · ${esc(c.description)}<br><span class="hint">Expected ${money(c.expected)}. If the payment is lower, the difference is billed to the patient.</span></p>
        <label>Amount received ($)</label><input id="cpay" type="number" step="0.01" value="${c.expected}"><p id="cerr2" class="msgbad"></p>
        <div class="row"><button class="btn ok" id="ok">Record payment</button><button class="btn alt" id="x">Cancel</button></div>`:
      `<h2>Claim denied</h2><p>${esc(c.patient_name)} · ${esc(c.description)}<br><span class="hint">The ${money(c.expected)} insurance share moves to the patient's balance.</span></p>
        <label>Reason</label><select id="creason"><option>Procedure not covered</option><option>Frequency limit reached</option><option>Annual maximum reached</option><option>Waiting period</option><option>Missing information</option></select><p id="cerr2" class="msgbad"></p>
        <div class="row"><button class="btn bad" id="ok">Record denial</button><button class="btn alt" id="x">Cancel</button></div>`;
    d.onclick=async ev=>{if(ev.target.id==='x')return d.close();if(ev.target.id!=='ok')return;
      try{await api(`/api/claims/${id}/${b.dataset.cs}`,{method:'POST',body:b.dataset.cs==='pay'?{amount:+$('#cpay').value}:{reason:$('#creason').value}});d.close();loadClaims()}
      catch(err){$('#cerr2').textContent=err.message}};d.showModal();
  }catch(err){alert(err.message)}};

// ---------------------------------------------------------------- dashboard: morning huddle + KPIs
const _loadDash=loadDash;
loadDash=async function(){await _loadDash();try{await pmDash()}catch(e){console.error(e)}};
async function pmDash(){
  const [h,k]=await Promise.all([api('/api/huddle'),api('/api/kpis')]);
  const label=new Date(h.date+'T12:00').toLocaleDateString([],{weekday:'long',month:'long',day:'numeric'});
  $('#huddle').innerHTML=`<div class="row"><h2 style="margin:0">Morning huddle</h2><span class="hint">${h.is_today?'Today':'Next busy day'}: ${label}</span>
      <span class="pill info">${h.appointments.length} patients</span><span class="pill paid">${mon0(h.production)} scheduled production</span>
      ${h.alerts_count?`<span class="pill unpaid">${h.alerts_count} with medical alerts</span>`:''}</div>
    ${h.appointments.length?`<table><thead><tr><th>Time</th><th>Patient</th><th>Visit</th><th>Provider</th><th>Insurance</th><th class="r">Balance</th><th class="r">Accepted tx</th></tr></thead><tbody>
    ${h.appointments.map(a=>`<tr class="click" data-pid="${a.patient_id}"><td>${clock(a.start)}</td><td><b>${esc(a.patient_name)}</b>${a.alerts.length?'<div>'+chips(a.alerts)+'</div>':''}</td>
      <td>${esc(CFG.types[a.type]?.name||a.type)}${a.status==='completed'?' '+pill('done'):''}</td><td><span class="provdot" style="background:${a.provider_color}"></span>${esc(a.provider_name)}</td>
      <td>${a.carrier?`${esc(a.carrier)}<div class="hint">${mon0(a.benefits_left)} left</div>`:'<span class="hint">Self-pay</span>'}</td>
      <td class="r" style="${a.balance>0?'color:var(--bad);font-weight:600':''}">${a.balance>0?money(a.balance):'—'}</td><td class="r">${a.tx_accepted>0?money(a.tx_accepted):'—'}</td></tr>`).join('')}</tbody></table>`:'<p class="hint">No appointments scheduled.</p>'}
    ${h.unscheduled.length?`<div style="margin-top:10px"><b>Accepted treatment not yet scheduled: ${mon0(h.unscheduled_total)}</b> <span class="hint">call these patients to book</span><br>
      ${h.unscheduled.map(u=>`<span class="opp" data-pid="${u.patient_id}">${esc(u.name)} · ${mon0(u.amount)}</span>`).join('')}</div>`:''}`;
  $('#huddle').onclick=e=>{const t=e.target.closest('[data-pid]');if(t)patientDialog(+t.dataset.pid)};
  const K=[[mon0(k.production_30d),'Production (30 days)',''],[mon0(k.collections_30d),'Collections (30 days)',`${k.collection_ratio}% of production`],
    [k.case_acceptance+'%','Case acceptance',`${mon0(k.accepted_total)} of ${mon0(k.presented_total)} presented`],[mon0(k.unscheduled_total),'Accepted, not scheduled','revenue waiting for a date'],
    [mon0(k.unbilled_claims),'Claims ready to submit',`${k.claims_open} open claim(s)`]];
  $('#kpis').innerHTML=K.map(([v,l,s])=>`<div class="card kpi"><b>${v}</b><span>${l}</span><small>${s}</small></div>`).join('');
  const ar=(o,color)=>{const m=Math.max(1,...Object.values(o));return Object.entries(o).map(([b,v])=>`<div class="ar"><span>${b} d</span><div><div class="bar" style="width:${100*v/m}%;background:${b==='90+'&&v>0?'var(--bad)':color}"></div></div><b>${mon0(v)}</b></div>`).join('')};
  const pm=Math.max(1,...k.by_provider.map(x=>x.production));
  $('#kpi2').innerHTML=`<div class="card"><h2>Patient balances by age</h2>${ar(k.patient_ar,'#2563eb')}</div>
    <div class="card"><h2>Insurance waiting to be paid</h2>${ar(k.insurance_ar,'#7c3aed')}</div>
    <div class="card"><h2>Production by provider (30 days)</h2>${k.by_provider.map(x=>`<div class="ar" style="grid-template-columns:120px 1fr 80px"><span><span class="provdot" style="background:${x.color}"></span>${esc(x.provider)}</span><div><div class="bar" style="width:${100*x.production/pm}%;background:${x.color}"></div></div><b>${mon0(x.production)}</b></div>`).join('')||'<p class="hint">No completed visits yet.</p>'}
      <p class="hint">Estimated from completed visits at your fee schedule.</p></div>`}

// ---------------------------------------------------------------- appointment popup: alerts + jump to chart
const _apptDialog=apptDialog;
apptDialog=function(a){_apptDialog(a);const d=$('#dlg');
  d.insertAdjacentHTML('beforeend','<div class="row"><button class="btn sm alt" id="openchart">Open patient chart</button></div>');
  d.querySelector('#openchart').onclick=()=>{d.close();patientDialog(a.patient_id)};
  api(`/api/patients/${a.patient_id}/profile`).then(p=>{if(p.alerts.length&&d.open&&d.querySelector('h2'))d.querySelector('h2').insertAdjacentHTML('afterend',`<div class="alertbar">⚠ ${chips(p.alerts)}</div>`)}).catch(()=>{})};
