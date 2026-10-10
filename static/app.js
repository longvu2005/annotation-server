'use strict';
const kind = document.body.dataset.kind;
const $ = id => document.getElementById(id);
const api = `/${kind}/api/`;
let meta = [], current = -1, task = null, selected = new Set(), assignments = {};

async function request(url, opts) {
  const res = await fetch(url, opts);
  let body;
  try { body = await res.json(); } catch { throw new Error(`HTTP ${res.status}`); }
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}
function message(msg, error=false) { $('message').textContent=msg; $('message').className=error?'error':'ok'; }
function status(msg, error=false) { $('status').textContent=msg; $('status').className=error?'error':'muted'; }
function el(tag, props={}, children=[]) {
  const n=document.createElement(tag);
  for (const [k,v] of Object.entries(props)) { if (k==='className') n.className=v; else if(k==='textContent') n.textContent=v; else n.setAttribute(k,v); }
  for (const child of children) n.append(child);
  return n;
}
async function overview() {
  const result=await request(api+'tasks');
  meta=result.tasks;
  $('progress').textContent=`${result.completed}/${result.total} completed · ${result.pending} pending`;
  drawQueue();
}
function drawQueue() {
  const container=$('queue'); container.replaceChildren();
  const text=$('search').value.toLowerCase(), filter=$('filter').value;
  for (const t of meta) {
    const done=Boolean(t.reviewed ?? t.completed);
    if (!t.sample_id.toLowerCase().includes(text) || (filter==='pending' && done) || (filter==='done' && !done)) continue;
    const btn=el('button',{className:'task'+(done?' done':'')+(t.index===current?' active':''),textContent:`${t.index+1}. ${t.sample_id} · ${t.case_type}`});
    btn.onclick=()=>loadTask(t.index); container.append(btn);
  }
}
function imageView(container, source, withBoxes=false) {
  container.replaceChildren();
  const photo=el('div',{className:'photo'});
  const img=el('img',{src:source.image_url,alt:source.image_id,loading:'lazy'});
  photo.append(img); container.append(photo);
  if (withBoxes) {
    for (const b of source.boxes) {
      const overlay=el('div',{className:'box',title:b.identity_id});
      overlay.style.left=b.x+'%';overlay.style.top=b.y+'%';overlay.style.width=b.width+'%';overlay.style.height=b.height+'%';
      overlay.append(el('span',{textContent:b.identity_id}));
      overlay.dataset.identity=b.identity_id;
      photo.append(overlay);
    }
    // Percentage coordinates must be relative to actual displayed image, not the surrounding container.
    img.onload=()=>{ photo.style.width=img.getBoundingClientRect().width+'px'; };
  }
}
function setCase() {
  const type=$('case').value;
  const two=type==='DUAL'||type==='RELATIONAL';
  for(const id of Object.keys(assignments)) if (!two && assignments[id]===2) assignments[id]=0;
  drawIdentities();
}
function drawIdentities() {
  const container=$('identities'); container.replaceChildren();
  const two=['DUAL','RELATIONAL'].includes($('case').value);
  container.append(el('h3',{textContent:'Assign identities to subjects'}));
  for(const id of task.candidate_identity_ids) {
    const row=el('div',{className:'identity'});
    row.append(el('span',{textContent:id}));
    const select=el('select');
    for (const [v,text] of [['0','Not selected'],['1','Subject 1'],...(two?[['2','Subject 2']]:[])]) {
      select.append(el('option',{value:v,textContent:text}));
    }
    select.value=String(assignments[id]||0);
    select.onchange=()=>{assignments[id]=Number(select.value); highlight();};
    row.append(select); container.append(row);
  }
  highlight();
}
function highlight() {
  document.querySelectorAll('.box').forEach(b=> b.classList.toggle('selected',Boolean(assignments[b.dataset.identity])));
}
function renderReview() {
  $('case').value=task.case_type;
  $('desc').value=task.final_desc;
  $('change').value=task.final_change;
  assignments=Object.fromEntries(task.candidate_identity_ids.map(x=>[x,0]));
  for (const s of task.subjects) for (const id of s.identity_ids) assignments[id]=s.subject_id;
  imageView($('query'),task.query,true); imageView($('target'),task.target,true);
  drawIdentities();
}
function renderPositives() {
  $('desc').textContent=task.final_desc;
  $('change').textContent=task.final_change;
  selected=new Set(task.positive_image_ids);
  imageView($('query'),task.query);
  const container=$('candidates');container.replaceChildren();
  for(const c of task.candidates) {
    const card=el('div',{className:'candidate'+(selected.has(c.image_id)?' selected':'')});
    card.append(el('img',{src:c.image_url,alt:c.image_id,loading:'lazy'}));
    const title=el('div',{className:'name'});
    title.append(el('strong',{textContent:c.image_id}));
    if(c.is_seed) title.append(el('span',{className:'seed',textContent:' · SEED (required)'}));
    card.append(title);
    const check=el('input',{type:'checkbox'});
    check.checked=selected.has(c.image_id);check.disabled=c.is_seed;
    const update=()=>{if(c.is_seed)return;if(selected.has(c.image_id))selected.delete(c.image_id);else selected.add(c.image_id);card.classList.toggle('selected',selected.has(c.image_id));check.checked=selected.has(c.image_id);$('selectedCount').textContent=`${selected.size} selected`;};
    card.onclick=ev=>{if(ev.target!==check)update();};
    check.onchange=update;card.append(check);container.append(card);
  }
  $('selectedCount').textContent=`${selected.size} selected`;
}
async function loadTask(index) {
  if(index<0 || index>=meta.length)return;
  try {
    status('Loading…');message('');
    const result=await request(api+`task?index=${index}`);
    current=index;task=result.task;
    $('sample').textContent=`${index+1}/${meta.length} · ${task.sample_id} · ${task.case_type}`;
    if(kind==='review')renderReview();else renderPositives();
    drawQueue();status((result.reviewed??result.is_completed)?'Already saved — editable':'Pending');
    $('previous').disabled=index===0;$('next').disabled=index===meta.length-1;
  }catch(e){status(e.message,true);}
}
async function save() {
  if(!task)return;
  let payload;
  if(kind==='review') {
    const caseType=$('case').value;
    const number=['DUAL','RELATIONAL'].includes(caseType)?2:1;
    const subjects=Array.from({length:number},(_,i)=>({subject_id:i+1,identity_ids:task.candidate_identity_ids.filter(id=>assignments[id]===i+1)}));
    payload={sample_id:task.sample_id,case_type:caseType,subjects,final_desc:$('desc').value,final_change:$('change').value};
  }else payload={sample_id:task.sample_id,positive_image_ids:task.candidates.filter(c=>selected.has(c.image_id)).map(c=>c.image_id)};
  try{
    $('save').disabled=true;message('Saving…');
    const result=await request(api+(kind==='review'?'review':'positive'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    message('Saved to data/'+(kind==='review'?'reviewed.jsonl':'positive_sets.jsonl'));
    await overview();
    task= {...task,...(kind==='review'?result.review:{positive_image_ids:result.positive.positive_image_ids})};
    $('sample').textContent=`${current+1}/${meta.length} · ${task.sample_id} · ${task.case_type}`;
  }catch(e){message(e.message,true);}finally{$('save').disabled=false;}
}
$('previous').onclick=()=>loadTask(current-1);
$('next').onclick=()=>loadTask(current+1);
$('nextPending').onclick=()=>{if(!meta.length)return;for(let step=1;step<=meta.length;step++){const t=meta[(current+step)%meta.length];if(!(t.reviewed??t.completed)){loadTask(t.index);return;}}status('All tasks completed');};
$('search').oninput=drawQueue;$('filter').onchange=drawQueue;
$('save').onclick=save;
if(kind==='review')$('case').onchange=setCase;
overview().then(()=>{if(meta.length)loadTask(0);else status('Add the corresponding input JSONL file to data/ and restart the server.');}).catch(e=>status(e.message,true));
