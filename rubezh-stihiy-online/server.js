'use strict';
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const PORT = Number(process.env.PORT || 3000);
const rooms = new Map();
const clients = new Map(); // room -> Set({session,res})
const MAX_PLAYERS = 2;
const BOARD_W = 5, BOARD_H = 6;
const KINDS = Object.freeze({
  guard: {name:'Горный страж', icon:'🛡️', element:'Земля', grade:'elite', price:8, hp:9, move:1, damage:[1,2,3], range:1, blurb:'Мощный воин, высокая стойкость.'},
  archer: {name:'Лесной стрелок', icon:'🏹', element:'Лес', grade:'common', price:5, hp:5, move:2, damage:[1,2,3], range:3, blurb:'Атакует на расстоянии до 3 клеток.'},
  spear: {name:'Степной копейщик', icon:'🔱', element:'Степь', grade:'common', price:4, hp:6, move:2, damage:[1,2,3], range:1, blurb:'Подвижный боец переднего края.'},
  mage: {name:'Искровой маг', icon:'🔥', element:'Огонь', grade:'elite', price:7, hp:4, move:1, damage:[2,3,4], range:2, blurb:'Сильная атака на дистанции.'},
  wolf: {name:'Серый волк', icon:'🐺', element:'Лес', grade:'common', price:5, hp:5, move:3, damage:[0,2,4], range:1, blurb:'Очень быстро перемещается.'},
  golem: {name:'Глиняный голем', icon:'🪨', element:'Земля', grade:'elite', price:9, hp:11, move:1, damage:[1,2,3], range:1, blurb:'Медлительный, но живучий.'},
  druid: {name:'Луговой ведун', icon:'🌿', element:'Лес', grade:'common', price:6, hp:6, move:2, damage:[1,2,3], range:2, blurb:'Поддержка отряда, умеет лечить на 2 здоровья.'},
  scout: {name:'Ночной разведчик', icon:'🗡️', element:'Тьма', grade:'common', price:4, hp:4, move:3, damage:[1,3,4], range:1, blurb:'Быстро обходит защиту врага.'},
  shaman: {name:'Грозовой шаман', icon:'⚡', element:'Огонь', grade:'elite', price:8, hp:5, move:2, damage:[1,3,5], range:2, blurb:'Непредсказуемые мощные удары.'},
  knight: {name:'Пограничный рыцарь', icon:'⚔️', element:'Степь', grade:'common', price:6, hp:8, move:1, damage:[1,2,3], range:1, blurb:'Надёжная опора отряда.'},
  frost: {name:'Ледяная ведьма', icon:'❄️', element:'Север', grade:'elite', price:7, hp:5, move:1, damage:[2,2,4], range:3, blurb:'Поражает дальние цели.'},
  spirit: {name:'Речной дух', icon:'💧', element:'Вода', grade:'common', price:5, hp:6, move:2, damage:[1,2,2], range:2, blurb:'Гибкий боец средней дистанции.'}
});
const OFFER = ['guard','archer','spear','mage','wolf','golem','druid','scout','shaman','knight','frost','spirit'];
function ident(n=6){const alphabet='ABCDEFGHJKLMNPQRSTUVWXYZ23456789'; return Array.from(crypto.randomBytes(n),v=>alphabet[v%alphabet.length]).join('');}
function safeName(s){return String(s||'Игрок').replace(/[<>\u0000-\u001f]/g,'').trim().slice(0,22)||'Игрок';}
function send(res, code, obj) {res.writeHead(code,{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});res.end(JSON.stringify(obj));}
function fail(msg){throw Error(msg);}
function pid(room,session){return room.players.findIndex(p=>p && p.session===session);}
function notice(room,text) {room.logs.unshift(text);room.logs.length=Math.min(room.logs.length,32);}
function createRoom(session,name,bot=false){let code;do{code=ident()}while(rooms.has(code));const room={code,created:Date.now(),touched:Date.now(),phase:'waiting',players:[{session,name:safeName(name),isBot:false},null],gold:[24,25],silver:[22,23],draft:[[],[]],ready:[false,false],placed:[false,false],board:[],turn:0,turnNumber:1,logs:[],winner:null,mode:bot?'bot':'human'};rooms.set(code,room);notice(room,'Комната создана.');if(bot){room.players[1]={session:'bot-'+code,name:'Страж арены',isBot:true};room.phase='draft';notice(room,'Бот готов к сражению!');seedBot(room);}return room;}
function joinRoom(code,session,name){const r=rooms.get(String(code||'').toUpperCase());if(!r)fail('Комната не найдена. Проверь код.');if(pid(r,session)>=0)return r;if(r.players[1]||r.phase!=='waiting')fail('Эта комната уже занята.');r.players[1]={session,name:safeName(name),isBot:false};r.phase='draft';r.touched=Date.now();notice(r,`${safeName(name)} присоединился к битве.`);publish(r);return r;}
function newUnit(kind,owner){const k=KINDS[kind];return {id:crypto.randomUUID(),type:kind,owner,hp:k.hp,x:null,y:null,moves:k.move,acted:false};}
function addDraft(room,player,type,currency){if(room.ready[player])fail('Ты уже завершил набор отряда.');if(!KINDS[type]||!OFFER.includes(type))fail('Неизвестная карта.');const k=KINDS[type];if(room.draft[player].length>=6)fail('Максимум 6 существ в этой версии.');if(k.grade==='elite' && currency!=='gold')fail('Элитная карта покупается за золото.');if(currency!=='gold'&&currency!=='silver')fail('Выбери золото или серебро.');if(room[currency][player]<k.price)fail('Недостаточно кристаллов.');if(room.draft[player].filter(u=>u.type===type).length>=2)fail('В отряде не более 2 одинаковых карт.');room[currency][player]-=k.price;const u=newUnit(type,player);u.currency=currency;room.draft[player].push(u);return u;}
function seedBot(r){for(const [type,currency] of [['guard','gold'],['spear','silver'],['archer','silver'],['mage','gold'],['wolf','silver'],['scout','silver']])addDraft(r,1,type,currency);r.ready[1]=true;}
function formation(r,p,unitId,x,y) {if(r.phase!=='place')fail('Сейчас нельзя расставлять существ.');if(r.placed[p])fail('Расстановка уже завершена.');const u=r.draft[p].find(t=>t.id===unitId);if(!u)fail('Карта не найдена в твоём отряде.');if(!Number.isInteger(x)||!Number.isInteger(y)||x<0||x>=5||y<0||y>=6)fail('Неверная клетка.');if((p===0 && y<3)||(p===1 && y>2))fail('Расставляй существ только на своей половине.');if(p===0 && (x<1||x>3))fail('Первый игрок начинает в центральных 3 столбцах.');if(r.draft.flat().some(other=>other.id!==unitId&&other.x===x&&other.y===y))fail('Клетка занята.');u.x=x;u.y=y;return u;}
function botPlace(r){const pos=[[1,0],[2,0],[3,0],[1,1],[2,1],[3,1]];r.draft[1].forEach((u,i)=>formation(r,1,u.id,pos[i][0],pos[i][1]));r.placed[1]=true;}
function beginIfReady(room) {if(room.ready.every(Boolean)&&room.phase==='draft'){room.phase='place';notice(room,'Отряды набраны. Расставьте карты на поле.');if(room.players[1].isBot)botPlace(room);}if(room.placed.every(Boolean)&&room.phase==='place'){room.phase='battle';room.board=room.draft.flat();room.board.forEach(u=>{u.moves=KINDS[u.type].move;u.acted=false;});room.turn=0;notice(room,'Сражение началось. Первый ход за игроком 1.');}}
function living(room,p){return room.board.filter(u=>u.owner===p&&u.hp>0);}
function occupied(room,x,y){return room.board.find(u=>u.hp>0&&u.x===x&&u.y===y);}
function dist(a,b){return Math.abs(a.x-b.x)+Math.abs(a.y-b.y);}
function canAttack(room,u,target){return u&&target&&u.owner!==target.owner&&target.hp>0&&dist(u,target)<=KINDS[u.type].range&&!u.acted;}
function attack(room,p,unitId,targetId){const u=room.board.find(v=>v.id===unitId&&v.owner===p&&v.hp>0);const t=room.board.find(v=>v.id===targetId&&v.hp>0);if(!canAttack(room,u,t))fail('Нельзя атаковать эту цель.');const roll=1+crypto.randomInt(6);const raw=KINDS[u.type].damage[Math.min(2,Math.floor((roll-1)/2))];const val=raw+((u.type==='scout'&&t.hp<=3)?1:0);t.hp=Math.max(0,t.hp-val);u.acted=true;notice(room,`${KINDS[u.type].icon} ${KINDS[u.type].name} бросает 🎲 ${roll} и наносит ${val} урона ${KINDS[t.type].name} (${t.hp} ❤️).`);if(!t.hp)notice(room,`${KINDS[t.type].name} повержен!`);checkWin(room);}
function checkWin(r){if(r.phase!=='battle')return;for(let p=0;p<2;p++)if(living(r,p).length===0){r.phase='finished';r.winner=1-p;notice(r,`🏆 Победил ${r.players[1-p].name}!`);return;}}
function move(room,p,unitId,x,y){const u=room.board.find(v=>v.id===unitId&&v.owner===p&&v.hp>0);if(!u)fail('Это не твоё существо.');if(u.acted||u.moves<=0)fail('У существа закончились действия или движение.');if(!Number.isInteger(x)||!Number.isInteger(y)||x<0||x>=5||y<0||y>=6||Math.abs(u.x-x)+Math.abs(u.y-y)!==1)fail('Перемещение только на соседнюю клетку по прямой.');if(occupied(room,x,y))fail('Клетка занята.');u.x=x;u.y=y;u.moves--;}
function heal(room,p,unitId,targetId){const u=room.board.find(v=>v.id===unitId&&v.owner===p&&v.hp>0);const t=room.board.find(v=>v.id===targetId&&v.owner===p&&v.hp>0);if(!u||u.type!=='druid'||u.acted||!t||dist(u,t)>2||t.hp>=KINDS[t.type].hp)fail('Недоступное лечение.');t.hp=Math.min(KINDS[t.type].hp,t.hp+2);u.acted=true;notice(room,`🌿 ${KINDS[u.type].name} лечит ${KINDS[t.type].name} на 2 здоровья.`);}
function endTurn(r,p){if(r.turn!==p)fail('Сейчас не твой ход.');r.turn=1-p;r.turnNumber++;for(const u of living(r,r.turn)){u.moves=KINDS[u.type].move;u.acted=false;}notice(r,`Ход ${r.turnNumber}: ${r.players[r.turn].name}.`);if(r.players[r.turn].isBot) setTimeout(()=>botPlay(r),500);}
function botPlay(r){if(r.phase!=='battle'||r.turn!==1)return;for(const u of living(r,1)){
  if(r.phase!=='battle')break;
  let foes=living(r,0);
  let target=foes.filter(t=>canAttack(r,u,t)).sort((a,b)=>a.hp-b.hp)[0];
  if(!target){for(let i=0;i<KINDS[u.type].move;i++){
    foes=living(r,0);if(!foes.length)break;const nearest=foes.sort((a,b)=>dist(u,a)-dist(u,b))[0];
    const choices=[[1,0],[-1,0],[0,1],[0,-1]].map(([dx,dy])=>({x:u.x+dx,y:u.y+dy})).filter(v=>v.x>=0&&v.x<5&&v.y>=0&&v.y<6&&!occupied(r,v.x,v.y)).sort((a,b)=>dist(a,nearest)-dist(b,nearest));
    if(!choices.length||dist(choices[0],nearest)>=dist(u,nearest))break;
    try{move(r,1,u.id,choices[0].x,choices[0].y);}catch{break;}
    target=living(r,0).filter(t=>canAttack(r,u,t)).sort((a,b)=>a.hp-b.hp)[0];if(target)break;
  }}
  if(target)attack(r,1,u.id,target.id);
}
if(r.phase==='battle'){endTurn(r,1);notice(r,'Бот завершил ход.');}publish(r);
}
function stateFor(r,session){const p=pid(r,session);if(p<0)return {error:'Нет доступа к комнате.'};return {code:r.code,phase:r.phase,me:p,players:r.players.map(x=>({name:x?.name||'Ожидание соперника',isBot:!!x?.isBot})),gold:r.gold[p],silver:r.silver[p],draft:r.draft[p].map(u=>({id:u.id,type:u.type,currency:u.currency,x:u.x,y:u.y})),rivalCount:r.draft[1-p].length,ready:r.ready,placed:r.placed,board:(r.phase==='battle'||r.phase==='finished')?r.board.map(u=>({...u})):[],turn:r.turn,turnNumber:r.turnNumber,winner:r.winner,logs:r.logs,library:KINDS,offer:OFFER};}
function publish(r){r.touched=Date.now();for(const cl of (clients.get(r.code)||[])){if(cl.res.destroyed)continue;cl.res.write(`data: ${JSON.stringify(stateFor(r,cl.session))}\n\n`);}}
function action(r,p,a){if(a.type==='draft'){if(r.phase!=='draft')fail('Сейчас не идёт набор отряда.');addDraft(r,p,String(a.card||''),String(a.currency||''));}
else if(a.type==='remove'){if(r.phase!=='draft'||r.ready[p])fail('Нельзя убрать карту сейчас.');const i=r.draft[p].findIndex(u=>u.id===a.unit);if(i<0)fail('Карта не найдена.');const u=r.draft[p].splice(i,1)[0];r[u.currency][p]+=KINDS[u.type].price;}
else if(a.type==='ready'){if(r.phase!=='draft'||r.ready[p])fail('Отряд уже готов.');if(r.draft[p].length<3)fail('Выбери минимум 3 карты.');r.ready[p]=true;notice(r,`${r.players[p].name} набрал отряд.`);beginIfReady(r);}
else if(a.type==='place'){formation(r,p,a.unit,a.x,a.y);}
else if(a.type==='placed'){if(r.phase!=='place')fail('Расстановка не идёт.');if(r.draft[p].some(u=>u.x===null))fail('Сначала расставь все карты.');r.placed[p]=true;notice(r,`${r.players[p].name} закончил расстановку.`);beginIfReady(r);}
else if(a.type==='move'||a.type==='attack'||a.type==='heal'||a.type==='end'){if(r.phase!=='battle'||r.turn!==p)fail('Сейчас не твой ход.');if(a.type==='move')move(r,p,a.unit,a.x,a.y);if(a.type==='attack')attack(r,p,a.unit,a.target);if(a.type==='heal')heal(r,p,a.unit,a.target);if(a.type==='end')endTurn(r,p);}
else fail('Неизвестное действие.');publish(r);}
const TYPES={'.html':'text/html; charset=utf-8','.css':'text/css; charset=utf-8','.js':'text/javascript; charset=utf-8','.svg':'image/svg+xml','.json':'application/json; charset=utf-8'};
function staticFile(req,res){const parsed=new URL(req.url,'http://localhost');const rel=parsed.pathname==='/'?'index.html':parsed.pathname.slice(1);if(!['index.html','style.css','app.js','favicon.svg'].includes(rel))return send(res,404,{error:'Не найдено.'});const filename=path.join(__dirname,'public',rel);fs.readFile(filename,(err,body)=>{if(err)return send(res,404,{error:'Нет файла.'});res.writeHead(200,{'Content-Type':TYPES[path.extname(filename)]||'application/octet-stream','Cache-Control':'public, max-age=60','X-Content-Type-Options':'nosniff'});res.end(body);});}
const server=http.createServer(async(req,res)=>{
  if(req.method==='GET'&&req.url.startsWith('/events?')){
    const q=new URL(req.url,'http://localhost').searchParams;const r=rooms.get(String(q.get('room')||''));const session=String(q.get('session')||'');if(!r||pid(r,session)<0)return send(res,403,{error:'Доступ запрещён.'});
    res.writeHead(200,{'Content-Type':'text/event-stream; charset=utf-8','Cache-Control':'no-cache, no-transform','Connection':'keep-alive','X-Accel-Buffering':'no','Access-Control-Allow-Origin':'*'});res.write(`retry: 2000\ndata: ${JSON.stringify(stateFor(r,session))}\n\n`);const c={session,res};if(!clients.has(r.code))clients.set(r.code,new Set());clients.get(r.code).add(c);const timer=setInterval(()=>{if(!res.destroyed)res.write(': ping\n\n');},20000);req.on('close',()=>{clearInterval(timer);clients.get(r.code)?.delete(c);});return;
  }
  if(req.method==='POST'&&req.url.startsWith('/api/')){
    let raw='';try{for await(const chunk of req){raw+=chunk;if(raw.length>16384)fail('Слишком большой запрос.');}const body=JSON.parse(raw);const session=String(body.session||'');if(!/^[a-zA-Z0-9-]{16,100}$/.test(session))fail('Неверный идентификатор игрока.');const endpoint=req.url.split('?')[0];
      if(endpoint==='/api/create'){const r=createRoom(session,body.name,body.bot===true);return send(res,200,{state:stateFor(r,session)});}
      if(endpoint==='/api/quick'){const existing=[...rooms.values()].find(r=>r.phase==='waiting'&&pid(r,session)<0&&Date.now()-r.created<60*60*1000);const r=existing?joinRoom(existing.code,session,body.name):createRoom(session,body.name);return send(res,200,{state:stateFor(r,session)});}
      if(endpoint==='/api/join'){const r=joinRoom(body.room,session,body.name);return send(res,200,{state:stateFor(r,session)});}
      const r=rooms.get(String(body.room||'').toUpperCase());if(!r||pid(r,session)<0)fail('Комната недоступна.');
      if(endpoint==='/api/state')return send(res,200,{state:stateFor(r,session)});
      if(endpoint==='/api/action'){action(r,pid(r,session),body.action||{});return send(res,200,{state:stateFor(r,session)});}
      return send(res,404,{error:'Не найдено.'});
    }catch(e){return send(res,400,{error:e.message||'Ошибка запроса.'});}
  }
  if(req.method==='GET'&&req.url==='/health')return send(res,200,{status:'ok',rooms:rooms.size});
  if(req.method!=='GET')return send(res,405,{error:'Метод не поддерживается.'});staticFile(req,res);
});
setInterval(()=>{for(const [code,r] of rooms){if(Date.now()-r.touched>8*3600*1000){rooms.delete(code);clients.delete(code);}}},3600*1000).unref();
if(require.main===module)server.listen(PORT,'0.0.0.0',()=>console.log(`Рубеж стихий: http://localhost:${PORT}`));
module.exports={server,rooms,KINDS};
