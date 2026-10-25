'use strict';
const $=id=>document.getElementById(id);
const escapeHtml=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const session=localStorage.getItem('frontier-session')||crypto.randomUUID();localStorage.setItem('frontier-session',session);
let state=null,source=null,busy=false,selected=null,selectedPlacement=null,toastTimer=null;
$('name').value=localStorage.getItem('frontier-name')||'';
const preselect=new URLSearchParams(location.search).get('room');if(preselect)$('roomInput').value=preselect.toUpperCase();
function toast(s){const el=$('toast');el.textContent=s;el.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove('show'),3200);}
function hide(...ids){for(const id of ids)$(id).hidden=true;}
function show(...ids){for(const id of ids)$(id).hidden=false;}
function getName(){const s=$('name').value.trim()||'Странник';localStorage.setItem('frontier-name',s);return s;}
async function call(url,body){const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,session})});const data=await response.json();if(!response.ok)throw Error(data.error||'Ошибка сервера.');return data;}
async function run(func){if(busy)return;busy=true;try{await func();}catch(e){toast(e.message||'Ошибка связи.');}finally{busy=false;}}
async function enter(endpoint,more={}){return run(async()=>{const data=await call(endpoint,{name:getName(),...more});state=data.state;selected=null;selectedPlacement=null;history.replaceState({},'',`?room=${state.code}`);listen();render();});}
$('create').onclick=()=>enter('/api/create');$('quick').onclick=()=>enter('/api/quick');$('bot').onclick=()=>enter('/api/create',{bot:true});
$('join').onclick=()=>{const code=$('roomInput').value.trim().toUpperCase();if(code.length!==6)return toast('Введи код из 6 символов.');enter('/api/join',{room:code});};
$('roomInput').addEventListener('keydown',e=>{if(e.key==='Enter')$('join').click();});
$('leave').onclick=()=>{source?.close();source=null;state=null;selected=null;history.replaceState({},'','/');render();};
async function copyLink(){const url=`${location.origin}/?room=${encodeURIComponent(state.code)}`;try{await navigator.clipboard.writeText(url);toast('Ссылка скопирована!');}catch{prompt('Скопируй эту ссылку:',url);}}
$('copyRoom').onclick=copyLink;$('waitingCopy').onclick=copyLink;
function listen(){source?.close();source=new EventSource(`/events?room=${encodeURIComponent(state.code)}&session=${encodeURIComponent(session)}`);source.onmessage=e=>{try{const next=JSON.parse(e.data);if(next.error)return toast(next.error);state=next;render();}catch{}};source.onerror=()=>{/* EventSource reconnects automatically. */};}
async function restore(){const room=new URLSearchParams(location.search).get('room');if(!room)return;try{const d=await call('/api/state',{room:room.toUpperCase()});state=d.state;listen();render();}catch{toast('Чтобы присоединиться, введи имя и нажми «Войти по коду».');}}
async function sendAction(action){return run(async()=>{const d=await call('/api/action',{room:state.code,action});state=d.state;render();});}
function current(p){return state?.players[p]?.name||'Ожидание';}
function viewToWorld(y){return state.me===0?y:5-y;}
function worldToView(y){return state.me===0?y:5-y;}
function selectedUnit(){return state?.board.find(u=>u.id===selected&&u.hp>0);}
function unitById(id){return state?.board.find(u=>u.id===id&&u.hp>0);}
function distance(a,b){return Math.abs(a.x-b.x)+Math.abs(a.y-b.y);}
function canAct(u){return u?.owner===state.me&&state.turn===state.me&&state.phase==='battle';}
function classifyCell(x,wy,unit){const choice=selectedUnit();let cls=['cell'];if((x+wy)%2)cls.push('alt');if(wy===(state.me===0?2:3))cls.push('enemy-half');if(unit&&unit.id===selected)cls.push('selected');if(state.phase==='place'&&!unit&&validPlacement(x,wy))cls.push('available');if(state.phase==='battle'&&canAct(choice)&&choice?.id!==unit?.id){if(unit&&unit.owner!==state.me&&!choice.acted&&distance(choice,unit)<=state.library[choice.type].range)cls.push('target');if(!unit&&choice.moves>0&&!choice.acted&&distance(choice,{x,y:wy})===1)cls.push('available');if(unit&&choice.type==='druid'&&unit.owner===state.me&&!choice.acted&&distance(choice,unit)<=2&&unit.hp<state.library[unit.type].hp)cls.push('available');}return cls.join(' ');}
function validPlacement(x,y){const p=state.me;if(p===0&&(y<3||x<1||x>3))return false;if(p===1&&y>2)return false;return !!selectedPlacement;}
function clickCell(x,wy){if(state.phase==='place'){if(state.placed[state.me])return;if(!selectedPlacement)return toast('Сначала выбери карту из списка под полем.');if(!validPlacement(x,wy))return toast('Расставляй карты на своей половине поля.');sendAction({type:'place',unit:selectedPlacement,x,y:wy});return;}
if(state.phase!=='battle')return;const unit=state.board.find(u=>u.x===x&&u.y===wy&&u.hp>0);const current=selectedUnit();
if(unit?.owner===state.me){if(current?.type==='druid'&&current.id!==unit.id&&canAct(current)&&!current.acted&&distance(current,unit)<=2&&unit.hp<state.library[unit.type].hp){sendAction({type:'heal',unit:current.id,target:unit.id});return;}selected=selected===unit.id?null:unit.id;render();return;}
if(!current)return toast('Выбери своё существо.');if(!canAct(current))return toast('Сейчас ход соперника.');if(unit){sendAction({type:'attack',unit:current.id,target:unit.id});return;}
sendAction({type:'move',unit:current.id,x,y:wy});}
function renderBoard(){const board=$('board');const isFormation=state.phase==='place';const units=isFormation?state.draft.filter(u=>u.x!==null):state.board.filter(u=>u.hp>0);let h='';for(let visualY=0;visualY<6;visualY++)for(let x=0;x<5;x++){
const wy=viewToWorld(visualY);const u=units.find(a=>a.x===x&&a.y===wy);const k=u?state.library[u.type]:null;const label=u?`${k.name}, ${u.hp??k.hp} здоровья`:'Свободная клетка';const css=classifyCell(x,wy,u);
const content=u?`<div class="unit ${u.owner===state.me||isFormation?'own':'foe'} ${u.acted?'sspent':''}"><span class="emblem">${k.icon}</span><span class="unitname">${escapeHtml(k.name)}</span>${!isFormation?`<span class="hp">♥${u.hp}</span><span class="mvs">${u.owner===state.me?'↝'+u.moves:''}</span>`:''}</div>`:'';
h+=`<button type="button" role="gridcell" aria-label="${escapeHtml(label)}" class="${css}" data-x="${x}" data-y="${wy}">${content}</button>`;
}board.innerHTML=h;board.querySelectorAll('.cell').forEach(el=>el.onclick=()=>clickCell(Number(el.dataset.x),Number(el.dataset.y)));}
function renderDraft(){const p=state.me;const count=state.draft.length;$('gold').textContent=state.gold;$('silver').textContent=state.silver;
$('myDraft').innerHTML=count?state.draft.map(u=>`<button class="chip" data-remove="${u.id}" ${state.ready[p]?'disabled':''}>${state.library[u.type].icon} ${escapeHtml(state.library[u.type].name)} <span>×</span></button>`).join(''):'<span class="placeholder">Здесь появятся набранные карты. Нажми 🟡 или ⚪ на карте.</span>';
$('myDraft').querySelectorAll('[data-remove]').forEach(el=>el.onclick=()=>sendAction({type:'remove',unit:el.dataset.remove}));
$('cards').innerHTML=state.offer.map(type=>{const k=state.library[type];const copies=state.draft.filter(u=>u.type===type).length;const can=count<6&&copies<2&&!state.ready[p];const gold=can&&state.gold>=k.price;const silver=can&&k.grade!=='elite'&&state.silver>=k.price;return `<article class="c-card ${k.grade==='elite'?'elite':''}"><div class="symbol">${k.icon}</div><h3>${escapeHtml(k.name)}</h3><small>${escapeHtml(k.element)} · ${k.grade==='elite'?'Элита':'Рядовая'}</small><div class="statline">♥ ${k.hp} &nbsp;↝ ${k.move} &nbsp;⚔ ${k.damage.join('/')} &nbsp;◎ ${k.range}</div><div class="desc">${escapeHtml(k.blurb)}</div><div class="cost-buttons"><button data-card="${type}" data-currency="gold" ${gold?'':'disabled'}>🟡 ${k.price}</button>${k.grade==='elite'?'':`<button data-card="${type}" data-currency="silver" ${silver?'':'disabled'}>⚪ ${k.price}</button>`}</div></article>`;}).join('');
$('cards').querySelectorAll('[data-card]').forEach(el=>el.onclick=()=>sendAction({type:'draft',card:el.dataset.card,currency:el.dataset.currency}));
$('draftHint').textContent=state.ready[p]?'Ожидаем готовность второго игрока…':`${count}/6 существ · Минимум 3`;
$('ready').disabled=count<3||state.ready[p];$('ready').textContent=state.ready[p]?'Отряд готов ✓':'Завершить набор →';$('ready').onclick=()=>sendAction({type:'ready'});
}
function renderFormation(){const list=state.draft.filter(u=>u.x===null);if(list.length&&!list.some(u=>u.id===selectedPlacement))selectedPlacement=list[0].id;if(!list.length)selectedPlacement=null;
$('bench').hidden=false;$('bench').innerHTML=`<h3>Твои существа для расстановки · осталось ${list.length}</h3><div class="benchItems">${list.map(u=>`<button class="benchItem ${u.id===selectedPlacement?'active':''}" data-unit="${u.id}">${state.library[u.type].icon} ${escapeHtml(state.library[u.type].name)}</button>`).join('')||'<span class="placeholder">Все карты расставлены.</span>'}</div>`;
$('bench').querySelectorAll('[data-unit]').forEach(el=>el.onclick=()=>{selectedPlacement=el.dataset.unit;renderBoard();renderFormation();});
$('step').textContent='ШАГ 2 / 2';$('boardTitle').textContent=state.placed[state.me]?'Ожидаем расстановку соперника':'Расставь своих существ';$('boardHelp').textContent=state.placed[state.me]?'Ты готов к бою. Как только соперник расставит карты, начнётся сражение.':'Выбери существо ниже. Первый игрок размещает карты в центральных трёх столбцах.';
$('unitPanel').innerHTML='<p>Условия прототипа: по одной карте на клетку; существа противника откроются после начала сражения.</p>';
$('boardActions').innerHTML=`<span>${state.draft.length-list.length}/${state.draft.length} на поле</span><button class="primary" id="completePlace" ${list.length||state.placed[state.me]?'disabled':''}>${state.placed[state.me]?'Готово ✓':'Начать бой →'}</button>`;
$('completePlace').onclick=()=>sendAction({type:'placed'});}
function renderCombat(){ $('step').textContent=`ХОД ${state.turnNumber}`;$('boardTitle').textContent=state.turn===state.me?'Твой ход':'Ход соперника';$('boardHelp').textContent=state.turn===state.me?'Выбери своё существо, затем свободную соседнюю клетку для движения или врага для атаки.':'Ожидаем действия противника. Поле обновляется автоматически.';
$('bench').hidden=true;const chosen=selectedUnit();if(chosen){const k=state.library[chosen.type];$('unitPanel').innerHTML=`<h3>${k.icon} ${escapeHtml(k.name)}</h3><p>♥ Здоровье ${chosen.hp}/${k.hp} · ↝ Осталось движения ${chosen.moves} · ⚔ Удар ${k.damage.join('/')} · ◎ Дальность ${k.range} · ${chosen.acted?'Уже действовал':'Может атаковать'}</p><p>${escapeHtml(k.blurb)}</p>`;}else $('unitPanel').innerHTML='<p>Нажми на своё существо, чтобы посмотреть его характеристики. Атака выполняется по выбранному врагу. Бросок кубика определяет слабый, средний или сильный удар.</p>';
$('boardActions').innerHTML=`<span>${state.turn===state.me?'⚔ Управляй отрядом':'⌛ Ожидание другого игрока'}</span><button class="primary" id="endTurn" ${state.turn===state.me?'':'disabled'}>Завершить ход →</button>`;
$('endTurn').onclick=()=>{selected=null;sendAction({type:'end'});};}
function render(){if(!state){show('home');hide('game');return;}hide('home');show('game');$('roomCode').textContent=state.code;$('playerName').textContent=current(state.me);$('enemyName').textContent=current(1-state.me);$('phaseLabel').textContent=({waiting:'ОЖИДАНИЕ',draft:'НАБОР ОТРЯДА',place:'РАССТАНОВКА',battle:'СРАЖЕНИЕ',finished:'ФИНАЛ'})[state.phase]||'';$('turnLabel').textContent=state.phase==='battle'?`ХОД ${state.turnNumber}`:'VS';
hide('waitingPanel','draftPanel','boardPanel','resultPanel','logPanel');
if(state.phase==='waiting'){show('waitingPanel');$('waitingCode').textContent=state.code;return;}
if(state.phase==='draft'){show('draftPanel');renderDraft();}
if(state.phase==='place'||state.phase==='battle'||state.phase==='finished'){
 show('boardPanel');renderBoard();if(state.phase==='place')renderFormation();else renderCombat();
 if(state.phase==='finished'){show('resultPanel');$('resultPanel').innerHTML=`<h2>${state.winner===state.me?'🏆 Победа!':'⚔ Поражение'}</h2><p>${state.winner===state.me?'Твой отряд одержал победу.':'Противник победил. Попробуй другую тактику!'}</p><button id="again" class="primary">Новая партия</button>`;$('again').onclick=()=>{$('leave').click();};$('boardActions').innerHTML='';}
}
if(state.logs.length){show('logPanel');$('log').innerHTML=state.logs.slice(0,16).map(log=>`<p>${escapeHtml(log)}</p>`).join('');}
}
render();restore();
