import { useEffect, useMemo, useState } from 'react';
import { deviceIdentity, isDeviceIdentity } from '../../shared/api/deviceTransport';
import { createCompanionApi } from './companionApi';

export function useReminderNotices(projects, identityEpoch) {
  const api = useMemo(() => createCompanionApi(), []);
  const projectIds = new Set(projects.filter(row => row.private !== true).map(row => row.id));
  // 日常可以先有提醒、后有资料。项目目录未登记时仍读取日常，由服务端核对私密资格。
  if (projects.length && !projects.some(row => row.id === 'default')) projectIds.add('default');
  const projectKey = [...projectIds].sort().join('\0');
  const [snapshot, setSnapshot] = useState({key:null, items:[], failed:false}), [refresh, setRefresh] = useState(0);
  const key = `${identityEpoch}:${projectKey}`;
  useEffect(() => {
    const changed = () => setRefresh(value => value + 1);
    window.addEventListener('chriptmas-reminders-changed', changed);
    return () => window.removeEventListener('chriptmas-reminders-changed', changed);
  }, []);
  useEffect(() => {
    const controller = new AbortController(), identity = deviceIdentity();
    let timer;
    async function poll() {
      const ids = projectKey ? projectKey.split('\0') : [];
      const results = await Promise.allSettled(ids.map(projectId => api.listDeliveredReminders({projectId,signal:controller.signal})));
      if (controller.signal.aborted || !isDeviceIdentity(identity)) return;
      const items = [], failed = results.some(result => result.status === 'rejected');
      results.forEach((result,index) => {
        if (result.status !== 'fulfilled' || !Array.isArray(result.value)) return;
        for (const row of result.value) if (row.kind === 'reminder' && row.state === 'delivered'
          && row.project_id === ids[index] && row.action !== 'closed') items.push(row);
      });
      items.sort((left,right) => left.delivery_at.localeCompare(right.delivery_at) || left.id.localeCompare(right.id));
      setSnapshot({key, items, failed});
      timer = window.setTimeout(poll, failed ? 30000 : 15000);
    }
    poll();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [api,key,projectKey,refresh]);
  const visible = snapshot.key === key ? snapshot : {items:[],failed:false};
  return {...visible, items:visible.items.map(row => ({...row,project_name:projects.find(project => project.id === row.project_id)?.name || (row.project_id === 'default' ? '日常' : row.project_id)}))};
}
