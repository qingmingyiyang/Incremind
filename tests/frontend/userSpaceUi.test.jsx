import React, { useEffect } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { SettingsUsers } from '../../src/frontend/src/features/settings/SettingsUsers';
import { DeviceGate } from '../../src/frontend/src/features/settings/DeviceGate';
import { Shell } from '../../src/frontend/src/shared/ui/Shell';
import { saveDeviceCredential, forgetDeviceCredential, readUserSpace, selectUserSpace } from '../../src/frontend/src/shared/api/deviceTransport';

let wire;
const user = { user_id:'user-other',name:'乙',role:'user',revision:1,disabled_at:null };
const response = value => ({ok:true,status:200,json:async()=>value});
beforeEach(() => {
  localStorage.clear(); forgetDeviceCredential(); history.replaceState(null,'','/');
  saveDeviceCredential({key:'k'.repeat(43),device:{device_id:'device-admin',user_id:'local-user'}});
  wire = vi.fn().mockImplementation(async url => response(String(url).endsWith('/audit') ? {items:[]}
    : String(url).endsWith('/pair') ? {expires_at:new Date(Date.now()+600000).toISOString(),qr:'data:image/png;base64,c3ludGhldGlj',url:'http://localhost/pair#code='+ 'p'.repeat(43)}
      : String(url).endsWith('/devices') ? {mode:'server',items:[]}
        : {caller:{role:'admin'},items:[user]}));
  vi.stubGlobal('fetch',wire);
});
afterEach(() => { cleanup();vi.unstubAllGlobals();vi.restoreAllMocks();forgetDeviceCredential();history.replaceState(null,'','/'); });

it('pairs a device for the actual expanded user without changing the active space',async()=>{
  render(<SettingsUsers projectId="default"/>);
  fireEvent.click(await screen.findByRole('button',{name:'乙'}));
  fireEvent.click(screen.getByRole('button',{name:'给乙添加设备'}));
  await screen.findByAltText('设备配对二维码');
  const request = wire.mock.calls.find(([url])=>String(url).endsWith('/users/user-other/pair'));
  expect(request).toBeTruthy();expect(request[1].method).toBe('POST');
  expect(JSON.parse(request[1].body)).toEqual({});
  expect(readUserSpace()).toBeNull();
});

it('shows the selected space avatar, clears old private route ids and remounts on return',async()=>{
  let mounts=0,unmounts=0;
  function View(){useEffect(()=>{mounts+=1;return()=>{unmounts+=1;};},[]);return <Shell project="default"><p>空间页面</p></Shell>;}
  history.replaceState(null,'','/#view=library&project_id=secret-project&document_id=old-doc&thread_id=old-thread');
  render(<DeviceGate><View/></DeviceGate>);
  await screen.findByText('空间页面');
  act(()=>selectUserSpace(user));
  const avatar = await screen.findByRole('button',{name:'乙 · 返回自己的空间'});
  expect(avatar.closest('.ui-shell-header')).not.toBeNull();
  expect(avatar.closest('.ui-shell-header').querySelector('.ui-project-switcher')).not.toBeNull();
  expect(location.hash).toBe('#view=workbench&project_id=default');
  expect(readUserSpace().user_id).toBe('user-other');
  expect(mounts).toBe(2);expect(unmounts).toBe(1);
  fireEvent.click(avatar);
  await waitFor(()=>expect(readUserSpace()).toBeNull());
  expect(screen.queryByRole('button',{name:'乙 · 返回自己的空间'})).toBeNull();
  expect(mounts).toBe(3);expect(unmounts).toBe(2);
});
