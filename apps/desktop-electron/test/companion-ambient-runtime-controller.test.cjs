const assert = require("node:assert/strict");
const test = require("node:test");
const { CompanionAmbientRuntimeController } = require("../src/companion/ambient-runtime-controller.cjs");

test("ambient runtime forwards quiet flags and presents two actions without focus", async () => {
  const calls=[]; const shown=[];
  const controller=new CompanionAmbientRuntimeController({offer:async(flags)=>{calls.push(flags);return {event:{event_id:"event_1",scene:"场景",state:"offered",revision:1,options:[{id:"a",label:"甲"},{id:"b",label:"乙"}]}}},choose:async()=>{},idle:async()=>({active:false}),present:(event)=>shown.push(event),flags:()=>({quiet:true,sleeping:false,game:false}),setIntervalFn:()=>1,clearIntervalFn:()=>{}});
  await controller.tick();
  assert.equal(calls[0].quiet,true); assert.equal(shown[0].actions.length,2); assert.equal(shown[0].requires_ack,false);
});

test("ambient choice is single-owner and replaces scene with result", async () => {
  const shown=[]; let choices=0;
  const event={event_id:"event_1",scene:"场景",state:"offered",revision:2,options:[{id:"a",label:"甲"},{id:"b",label:"乙"}]};
  const controller=new CompanionAmbientRuntimeController({offer:async()=>({event}),choose:async(id,option,revision)=>{choices++;assert.deepEqual([id,option,revision],["event_1","a",2]);return {event:{...event,state:"settled",result:{text:"完成"}}}},idle:async()=>({active:false}),present:(value)=>shown.push(value)});
  await controller.tick(); assert.equal(controller.owns("event_1","a"),true);
  await controller.act("event_1","a"); assert.equal(choices,1); assert.equal(shown[1].text,"完成"); assert.equal(controller.active,null);
});

test("idle companionship is non-focusing once per idle stretch and resets after activity", async () => {
  const shown=[]; let idleSeconds=1200; let offers=0;
  const controller=new CompanionAmbientRuntimeController({offer:async()=>{offers++;return {event:null}},choose:async()=>{},idle:async()=>idleSeconds>=1200?{active:true,message:"喝口水吧"}:{active:false,message:null},present:(value)=>shown.push(value),flags:()=>({idleSeconds})});
  await controller.tick(); await controller.tick();
  assert.equal(shown.length,1); assert.equal(shown[0].requires_ack,false); assert.equal(offers,1);
  idleSeconds=0; await controller.tick(); idleSeconds=1200; await controller.tick();
  assert.equal(shown.length,2);
});
