const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { CompanionVoiceController, normalizeLoopbackOrigin } = require("../src/companion/voice-controller.cjs");
const { createTemporaryRootTracker } = require("./support/temporary-root.cjs");

const temporaryRoot = createTemporaryRootTracker(test);

function wav(bytes = 64) { const value=Buffer.alloc(bytes);value.write("RIFF",0);value.writeUInt32LE(bytes-8,4);value.write("WAVE",8);return value; }
function fixture(fetchFn = async () => new Response(wav(), { status: 200, headers: { "content-type": "audio/wav" } })) {
  const root=temporaryRoot("chriptmas-voice-");const reference=path.join(root,"reference.wav");fs.writeFileSync(reference,wav());
  const controller=new CompanionVoiceController({statePath:path.join(root,"voice.json"),fetchFn});return {root,reference,controller};
}

test("voice settings are disabled by default and project no reference path", () => {
  const value=fixture();assert.deepEqual(value.controller.status(),{state:"ready",enabled:false,origin:"http://127.0.0.1:9880",text_lang:"zh",prompt_lang:"zh",prompt_text:"",has_reference:false,reference_name:null,reference_state:"missing",speaking:false});
  value.controller.setReference(value.reference);const status=value.controller.configure({enabled:true,origin:"http://localhost:9880",text_lang:"zh",prompt_lang:"zh",prompt_text:"你好"});
  assert.equal(status.reference_name,"reference.wav");assert.equal(JSON.stringify(status).includes(value.root),false);
});

test("sends the current GPT-SoVITS v2 POST contract and returns bounded WAV", async () => {
  let call;const value=fixture(async(url,options)=>{call={url,options};return new Response(wav(128),{status:200,headers:{"content-type":"audio/wav"}})});
  value.controller.setReference(value.reference);value.controller.configure({enabled:true,origin:"http://127.0.0.1:9880",text_lang:"zh",prompt_lang:"zh",prompt_text:"参考文本"});
  const result=await value.controller.speak("你好");const body=JSON.parse(call.options.body);
  assert.equal(call.url,"http://127.0.0.1:9880/tts");assert.equal(body.text,"你好");assert.equal(body.ref_audio_path,value.reference);assert.equal(body.media_type,"wav");assert.equal(body.streaming_mode,false);assert.equal(result.audio.length,128);
});

test("rejects remote origins, changed references, non-WAV and oversized responses", async () => {
  for(const origin of ["https://127.0.0.1:9880","http://192.168.1.2:9880","http://user@localhost:9880","http://localhost:9880/tts"]) assert.throws(()=>normalizeLoopbackOrigin(origin),/origin_invalid/);
  const value=fixture();value.controller.setReference(value.reference);value.controller.configure({enabled:true,origin:"http://127.0.0.1:9880",text_lang:"zh",prompt_lang:"zh",prompt_text:""});fs.appendFileSync(value.reference,"changed");await assert.rejects(value.controller.speak("你好"),/reference_changed/);
  const bad=fixture(async()=>new Response(Buffer.from("not wav"),{status:200,headers:{"content-type":"audio/wav"}}));bad.controller.setReference(bad.reference);bad.controller.configure({enabled:true,origin:"http://127.0.0.1:9880",text_lang:"zh",prompt_lang:"zh",prompt_text:""});await assert.rejects(bad.controller.speak("你好"),/wav_invalid/);
  const large=fixture(async()=>({ok:true,headers:{get:(name)=>name==="content-type"?"audio/wav":String(9*1024*1024)},body:null,arrayBuffer:async()=>wav()}));large.controller.setReference(large.reference);large.controller.configure({enabled:true,origin:"http://127.0.0.1:9880",text_lang:"zh",prompt_lang:"zh",prompt_text:""});await assert.rejects(large.controller.speak("你好"),/too_large/);
});

test("corrupt state fails closed and is not overwritten", () => {
  const value=fixture();const statePath=path.join(value.root,"voice.json");fs.writeFileSync(statePath,"{bad");const controller=new CompanionVoiceController({statePath,fetchFn:async()=>{}});assert.equal(controller.status().state,"invalid");assert.throws(()=>controller.configure({}),/state_invalid/);assert.equal(fs.readFileSync(statePath,"utf8"),"{bad");
});

test("a newer reply cancels the active request and timeout aborts stalled synthesis", async () => {
  let calls=0;let firstAborted=false;
  const value=fixture((_url,{signal})=>{calls+=1;if(calls===2)return Promise.resolve(new Response(wav(),{status:200,headers:{"content-type":"audio/wav"}}));return new Promise((_resolve,reject)=>signal.addEventListener("abort",()=>{firstAborted=true;reject(signal.reason)},{once:true}));});
  value.controller.setReference(value.reference);value.controller.configure({enabled:true,origin:"http://127.0.0.1:9880",text_lang:"zh",prompt_lang:"zh",prompt_text:""});
  const first=value.controller.speak("第一句");const second=value.controller.speak("第二句");
  await assert.rejects(first,/superseded/);assert.equal(firstAborted,true);assert.equal((await second).audio.length,64);

  let fireTimeout;const timed=fixture((_url,{signal})=>new Promise((_resolve,reject)=>signal.addEventListener("abort",()=>reject(signal.reason),{once:true})));
  timed.controller.setTimeoutFn=(callback)=>{fireTimeout=callback;return 1};timed.controller.clearTimeoutFn=()=>{};
  timed.controller.setReference(timed.reference);timed.controller.configure({enabled:true,origin:"http://127.0.0.1:9880",text_lang:"zh",prompt_lang:"zh",prompt_text:""});
  const pending=timed.controller.speak("超时");fireTimeout();await assert.rejects(pending,/timeout/);assert.equal(timed.controller.status().speaking,false);
});
