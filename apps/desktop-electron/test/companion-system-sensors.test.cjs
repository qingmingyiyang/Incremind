const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const test = require("node:test");
const { CompanionNetworkHealthAdapter } = require("../src/companion/network-health-adapter.cjs");
const { CompanionSystemSensorRuntimeController } = require("../src/companion/system-sensor-runtime-controller.cjs");

test("network adapter stays local without an origin and bounds a slow HTTPS probe", async () => {
  let now = 100;
  const net = { request(options) {
    assert.deepEqual(options, { method: "HEAD", url: "https://health.example/", redirect: "error" });
    const request = new EventEmitter();
    request.end = () => { now = 480; queueMicrotask(() => request.emit("response", { resume() {} })); };
    return request;
  } };
  const adapter = new CompanionNetworkHealthAdapter({ net, isOnline: () => true, now: () => now });
  assert.deepEqual(await adapter.probe(null), { network_state: "normal", latency_ms: 0 });
  assert.deepEqual(await adapter.probe("https://health.example"), { network_state: "slow", latency_ms: 380 });
});

test("network adapter reports offline without issuing a request", async () => {
  const adapter = new CompanionNetworkHealthAdapter({ net: { request() { throw new Error("must not run"); } }, isOnline: () => false });
  assert.deepEqual(await adapter.probe("https://health.example"), { network_state: "offline", latency_ms: null });
});

test("sensor runtime starts only when enabled and projects game transitions and alerts", async () => {
  const games=[]; const alerts=[]; const snapshots=[]; let timerCallback=null;
  const enabled={config:{enabled:true,network_enabled:true,health_origin:null,game_behavior:"hide"}};
  const sampled={...enabled,sample:{game_active:true,should_alert:true,resource:"hot"}};
  const controller=new CompanionSystemSensorRuntimeController({readStatus:async()=>enabled,sample:async(body)=>{assert.deepEqual(body,{network_state:"normal",latency_ms:12});return sampled;},probeNetwork:async()=>({network_state:"normal",latency_ms:12}),onSnapshot:(value)=>snapshots.push(value),onGameModeChanged:(value,previous)=>games.push([value,previous]),onAlert:(value)=>alerts.push(value),setIntervalFn:(callback)=>{timerCallback=callback;return 7;},clearIntervalFn:()=>{}});
  await controller.refresh();
  assert.equal(typeof timerCallback,"function");
  await controller.tick();
  assert.equal(controller.isGameQuiet(),true); assert.equal(snapshots.length,1); assert.equal(alerts[0].resource,"hot"); assert.deepEqual(games[0][0],{active:true,behavior:"hide"});
});

test("disabled sensor runtime owns no interval", async () => {
  let scheduled=0;
  const controller=new CompanionSystemSensorRuntimeController({readStatus:async()=>({config:{enabled:false}}),sample:async()=>{},probeNetwork:async()=>{},onSnapshot:()=>{},onGameModeChanged:()=>{},onAlert:()=>{},setIntervalFn:()=>{scheduled++;return 1;},clearIntervalFn:()=>{}});
  await controller.refresh();
  assert.equal(scheduled,0); assert.equal(controller.timer,null);
});
