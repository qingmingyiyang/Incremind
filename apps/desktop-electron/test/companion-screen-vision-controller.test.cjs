const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { CompanionScreenVisionController, cleanOwnedTempFiles, requireJpeg, safeName } = require("../src/companion/screen-vision-controller.cjs");
const { createTemporaryRootTracker } = require("./support/temporary-root.cjs");

const temporaryRoot = createTemporaryRootTracker(test);

function jpeg(){const value=Buffer.alloc(64);value[0]=0xff;value[1]=0xd8;value[2]=0xff;return value;}
function json(value,status=200){return new Response(JSON.stringify(value),{status,headers:{"content-type":"application/json"}});}
function image(width=800,height=500){return{isEmpty:()=>false,getSize:()=>({width,height}),toDataURL:()=>"data:image/png;base64,AA==",toJPEG:()=>jpeg(),resize:()=>image(width,height)};}
function fixture(overrides={}){
  const calls=[];let phase=0;const desktopCapturer={getSources:async(options)=>{calls.push(options);phase+=1;return[{id:"screen:1:0",name:" Secret\nScreen ",thumbnail:image()}];}};
  const tempRoot=temporaryRoot("chriptmas-screen-vision-test-");
  const controller=new CompanionScreenVisionController({desktopCapturer,nativeImage:{createFromBuffer:()=>image()},tempRoot,sessionProvider:()=>({origin:"http://127.0.0.1:1234",secret:"s".repeat(43),instance_id:"instance"}),createFileGrantFn:async({filePath})=>({file_path:filePath}),uploadFileGrantFn:async(_grant,_session,options)=>{calls.push(options);return{grant:{grant_id:"vision-grant-"+"a".repeat(48)}};},fetchFn:async(url,options)=>{calls.push({url,options});if(url.endsWith("/api/ai/turns"))return json({status:"waiting_approval",current_sequence:4});if(url.endsWith("/events")){const eventCalls=calls.filter((item)=>item?.url?.endsWith("/events")).length;return json(eventCalls===1?{events:[{type:"approval.required",event_id:"event-"+"b".repeat(32)}],presentation:null}:{events:[],presentation:{content:{text:"看到了",status:"completed"}}});}if(url.includes("/actions"))return json({status:"completed",current_sequence:7});return json({});},...overrides});
  return{controller,calls,tempRoot};
}

test("enumerates bounded local previews and captures only an opaque selected source",async()=>{const value=fixture();const listed=await value.controller.listSources();assert.equal(listed.items.length,1);assert.equal(listed.items[0].name,"Secret Screen");assert.equal(JSON.stringify(listed).includes("screen:1:0"),false);const captured=await value.controller.captureSource({session_id:listed.session_id,item_id:listed.items[0].item_id});assert.equal(captured.width,800);assert.equal(Buffer.from(captured.bytes).length,64);assert.deepEqual(value.calls[1].thumbnailSize,{width:1600,height:1600});});

test("confirmation submits one scoped Turn and durable approval then removes the Electron temp file",async()=>{const value=fixture();const listed=await value.controller.listSources();const captured=await value.controller.captureSource({session_id:listed.session_id,item_id:listed.items[0].item_id});const result=await value.controller.confirm({capture_id:captured.capture_id,bytes:new Uint8Array(jpeg()),question:"帮我看看",project_id:"project-vision"});assert.equal(result.text,"看到了");assert.equal(value.calls.some((item)=>item?.endpointPath==="/api/rebuild/companion/vision/grants"),true);const request=value.calls.find((item)=>item?.url?.endsWith("/api/ai/turns"));const turn=JSON.parse(request.options.body);assert.equal(turn.scope.project_id,"project-vision");assert.equal(turn.desired_outcome,"companion.vision.analyze");assert.equal(JSON.stringify(turn).includes("confirm_egress"),false);const action=value.calls.find((item)=>item?.url?.endsWith("/actions"));assert.equal(JSON.parse(action.options.body).type,"approve");assert.equal(value.calls.some((item)=>item?.url?.endsWith("/vision/analyze")),false);assert.deepEqual(fs.readdirSync(value.tempRoot),[]);await assert.rejects(value.controller.confirm({capture_id:captured.capture_id,bytes:new Uint8Array(jpeg()),question:"重放"}),/expired/);});

test("vision follow-up Turn requests use a renewed session",async()=>{
  let session={origin:"http://127.0.0.1:1234",secret:"old-secret",instance_id:"instance"};
  const seen=[];
  const value=fixture({sessionProvider:()=>session,uploadFileGrantFn:async(_grant,active)=>{seen.push(active.secret);session={...session,secret:"new-secret"};return{grant:{grant_id:"vision-grant-"+"a".repeat(48)}};},fetchFn:async(url,options)=>{seen.push(options.headers["X-Chriptmas-Desktop-Session"]);if(url.endsWith("/api/ai/turns"))return json({status:"waiting_approval",current_sequence:4});if(url.endsWith("/events"))return json(seen.length===3?{events:[{type:"approval.required",event_id:"event-"+"b".repeat(32)}],presentation:null}:{events:[],presentation:{content:{text:"完成"}}});return json({status:"completed"});}});
  const listed=await value.controller.listSources();const captured=await value.controller.captureSource({session_id:listed.session_id,item_id:listed.items[0].item_id});
  assert.equal((await value.controller.confirm({capture_id:captured.capture_id,bytes:new Uint8Array(jpeg()),question:"确认"})).text,"完成");
  assert.deepEqual(seen,["old-secret","new-secret","new-secret","new-secret","new-secret"]);
});

test("rejects expired selections, invalid images and sensitive title controls",async()=>{let clock=1;const value=fixture({now:()=>clock});const listed=await value.controller.listSources();clock+=120001;await assert.rejects(value.controller.captureSource({session_id:listed.session_id,item_id:listed.items[0].item_id}),/expired/);assert.throws(()=>requireJpeg(new Uint8Array(64),{createFromBuffer:()=>image()},{width:800,height:500}),/invalid/);assert.equal(safeName("a\u0000 b\n c"),"a b c");});

test("startup cleanup removes only owned regular JPEG files",()=>{const root=fs.mkdtempSync(path.join(os.tmpdir(),"chriptmas-screen-clean-"));const owned=path.join(root,"screen-123e4567-e89b-42d3-a456-426614174000.jpg");const unrelated=path.join(root,"keep.jpg");fs.writeFileSync(owned,jpeg());fs.writeFileSync(unrelated,jpeg());fs.mkdirSync(path.join(root,"screen-123e4567-e89b-42d3-a456-426614174001.jpg"));cleanOwnedTempFiles(root);assert.equal(fs.existsSync(owned),false);assert.equal(fs.existsSync(unrelated),true);assert.equal(fs.existsSync(path.join(root,"screen-123e4567-e89b-42d3-a456-426614174001.jpg")),true);fs.rmSync(root,{recursive:true,force:true});});

test("backend discovery failure preserves the capture for a retry",async()=>{let available=false;const value=fixture({sessionProvider:()=>available?{origin:"http://127.0.0.1:1234",secret:"s".repeat(43),instance_id:"instance"}:null});const listed=await value.controller.listSources();const captured=await value.controller.captureSource({session_id:listed.session_id,item_id:listed.items[0].item_id});const payload={capture_id:captured.capture_id,bytes:new Uint8Array(jpeg()),question:"帮我看看"};await assert.rejects(value.controller.confirm(payload),/backend_unavailable/);available=true;assert.equal((await value.controller.confirm(payload)).text,"看到了");});

test("cancellation invalidates a late source enumeration",async()=>{let resolveSources;const deferred=new Promise((resolve)=>{resolveSources=resolve;});const value=fixture({desktopCapturer:{getSources:()=>deferred}});const pending=value.controller.listSources();value.controller.cancel();resolveSources([{id:"screen:1:0",name:"screen",thumbnail:image()}]);await assert.rejects(pending,/cancelled/);});
