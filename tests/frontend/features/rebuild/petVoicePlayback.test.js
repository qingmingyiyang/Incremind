import { describe, expect, it, vi } from "vitest";

import { calculateRms, startPetVoicePlayback } from "@src/features/rebuild/petVoicePlayback";

function wav() {
  const bytes = new Uint8Array(44);
  bytes.set([82, 73, 70, 70], 0);
  bytes.set([87, 65, 86, 69], 8);
  return bytes;
}

describe("pet voice playback", () => {
  it("calculates centered PCM amplitude", () => {
    expect(calculateRms(new Uint8Array([128, 128, 128]))).toBe(0);
    expect(calculateRms(new Uint8Array([0, 255]))).toBeGreaterThan(0.9);
  });

  it("drives hysteretic mouth state and closes on audio end", async () => {
    const frames=[];const mouths=[];let ended;let samples=new Uint8Array(256).fill(128);let clock=100;
    const source={connect:vi.fn(),start:vi.fn(),stop:vi.fn(),disconnect:vi.fn(),set onended(value){ended=value},get onended(){return ended}};
    const analyser={fftSize:0,smoothingTimeConstant:0,connect:vi.fn(),disconnect:vi.fn(),getByteTimeDomainData:(target)=>target.set(samples)};
    const context={destination:{},createBufferSource:()=>source,createAnalyser:()=>analyser,decodeAudioData:vi.fn(async()=>({})),resume:vi.fn(async()=>{}),close:vi.fn(async()=>{})};
    const session=await startPetVoicePlayback(wav(),{audioContextFactory:()=>context,onMouth:(open)=>mouths.push(open),requestFrame:(callback)=>{frames.push(callback);return frames.length},cancelFrame:vi.fn(),now:()=>clock});
    samples=new Uint8Array(256).fill(160);frames.shift()();expect(mouths).toEqual([true]);
    clock=200;samples=new Uint8Array(256).fill(128);frames.shift()();expect(mouths).toEqual([true,false]);
    ended();expect(context.close).toHaveBeenCalled();expect(session.stop()).toBe(false);
  });

  it("holds the mouth state longer for reduced-motion playback", async () => {
    const frames=[];const mouths=[];let samples=new Uint8Array(256).fill(128);let clock=100;
    const source={connect:vi.fn(),start:vi.fn(),stop:vi.fn(),disconnect:vi.fn()};
    const analyser={connect:vi.fn(),disconnect:vi.fn(),getByteTimeDomainData:(target)=>target.set(samples)};
    const context={destination:{},createBufferSource:()=>source,createAnalyser:()=>analyser,decodeAudioData:vi.fn(async()=>({})),resume:vi.fn(async()=>{}),close:vi.fn(async()=>{})};
    await startPetVoicePlayback(wav(),{audioContextFactory:()=>context,onMouth:(open)=>mouths.push(open),minimumHoldMs:180,requestFrame:(callback)=>{frames.push(callback);return frames.length},cancelFrame:vi.fn(),now:()=>clock});
    samples=new Uint8Array(256).fill(160);frames.shift()();expect(mouths).toEqual([true]);
    clock=200;samples=new Uint8Array(256).fill(128);frames.shift()();expect(mouths).toEqual([true]);
    clock=300;frames.shift()();expect(mouths).toEqual([true,false]);
  });

  it("rejects unbounded audio before creating an audio context", async () => {
    await expect(startPetVoicePlayback(new Uint8Array(4),{audioContextFactory:()=>{throw new Error("must not run")}})).rejects.toThrow(/invalid/);
    await expect(startPetVoicePlayback(new Uint8Array(44),{audioContextFactory:()=>{throw new Error("must not run")}})).rejects.toThrow(/invalid/);
  });
});
