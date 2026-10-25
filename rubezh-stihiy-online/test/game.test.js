'use strict';
const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const {server}=require('../server');
let base;
before(async()=>{await new Promise(done=>server.listen(0,'127.0.0.1',done));base=`http://127.0.0.1:${server.address().port}`;});
after(async()=>{await new Promise(done=>server.close(done));});
const ident=()=>`test-${crypto.randomUUID()}`;
async function post(route,session,fields={}){const resp=await fetch(base+'/api/'+route,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session,...fields})});const body=await resp.json();return {status:resp.status,...body};}
async function action(session,room,a){const r=await post('action',session,{room,action:a});assert.equal(r.status,200,JSON.stringify(r));return r.state;}
function slotsFor(p){return p===0?[[1,3],[2,3],[3,3]]:[[1,0],[2,0],[3,0]];}
test('two browser sessions can meet, draft, deploy and play a turn',async()=>{
 const a=ident(),b=ident();let x=await post('create',a,{name:'Первый'});assert.equal(x.status,200);let room=x.state.code;
 assert.equal(x.state.phase,'waiting');const y=await post('join',b,{room,name:'Второй'});assert.equal(y.state.phase,'draft');
 const bad=await post('action',b,{room,action:{type:'end'}});assert.equal(bad.status,400);
 for(const s of [a,b]){
  for(const [card,currency] of [['guard','gold'],['wolf','silver'],['archer','silver']])await action(s,room,{type:'draft',card,currency});
  const st=await action(s,room,{type:'ready'});assert.equal(st.draft.length,3);
 }
 let st=await post('state',a,{room});assert.equal(st.state.phase,'place');
 const sessionList=[a,b];for(let p=0;p<2;p++){
  const sess=sessionList[p];let curr=(await post('state',sess,{room})).state;
  for(const [i,u] of curr.draft.entries())await action(sess,room,{type:'place',unit:u.id,x:slotsFor(p)[i][0],y:slotsFor(p)[i][1]});
  st={state:await action(sess,room,{type:'placed'})};
 }
 assert.equal(st.state.phase,'battle');assert.equal(st.state.board.length,6);assert.equal(st.state.turn,0);
 const first=st.state.board.find(u=>u.owner===0&&u.type==='archer');assert.ok(first);
 const moves=(await post('action',b,{room,action:{type:'move',unit:first.id,x:2,y:4}}));assert.equal(moves.status,400);
 const next=await action(a,room,{type:'end'});assert.equal(next.turn,1);
 const won=await post('state',b,{room});assert.equal(won.state.turn,1);
});
test('bot is playable and finishes a turn automatically',async()=>{
 const a=ident();const created=await post('create',a,{name:'Solo',bot:true});const room=created.state.code;
 assert.equal(created.state.phase,'draft');assert.equal(created.state.players[1].isBot,true);
 for(const [card,currency] of [['guard','gold'],['wolf','silver'],['archer','silver']])await action(a,room,{type:'draft',card,currency});
 let st=await action(a,room,{type:'ready'});assert.equal(st.phase,'place');assert.equal(st.placed[1],true);
 for(const [i,u] of st.draft.entries())st=await action(a,room,{type:'place',unit:u.id,x:slotsFor(0)[i][0],y:slotsFor(0)[i][1]});
 st=await action(a,room,{type:'placed'});assert.equal(st.phase,'battle');
 st=await action(a,room,{type:'end'});assert.equal(st.turn,1);
 await new Promise(resolve=>setTimeout(resolve,850));const update=await post('state',a,{room});assert.equal(update.state.turn,0);
});
test('public quick match joins only another session',async()=>{
 const a=ident(),b=ident();const first=await post('quick',a,{name:'Лиса'});const second=await post('quick',b,{name:'Кедр'});
 assert.equal(first.state.code,second.state.code);assert.equal(second.state.phase,'draft');
});
test('damage uses server dice and invalid repeated attacks are blocked',async()=>{
 const a=ident(),b=ident();const first=await post('create',a,{name:'Страж'});const room=first.state.code;await post('join',b,{room,name:'Охотник'});
 for(const s of [a,b]){for(const card of ['guard','archer','spear'])await action(s,room,{type:'draft',card,currency:card==='guard'?'gold':'silver'});await action(s,room,{type:'ready'});}
 const positions=[[[1,3],[2,3],[3,3]],[[1,2],[2,2],[3,2]]];let st;
 for(const [p,s] of [a,b].entries()){
   st=(await post('state',s,{room})).state;
   for(const [i,u] of st.draft.entries())await action(s,room,{type:'place',unit:u.id,x:positions[p][i][0],y:positions[p][i][1]});
   st=await action(s,room,{type:'placed'});
 }
 const attacker=st.board.find(u=>u.owner===0&&u.type==='guard');const target=st.board.find(u=>u.owner===1&&u.type==='guard');
 const hit=await action(a,room,{type:'attack',unit:attacker.id,target:target.id});
 assert.ok(hit.board.find(u=>u.id===target.id).hp<9);
 const second=await post('action',a,{room,action:{type:'attack',unit:attacker.id,target:target.id}});assert.equal(second.status,400);
});
