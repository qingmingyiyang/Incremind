import { useEffect, useState } from 'react';
import './SettingsSupport.css';

export function DevicePairing({ value }) {
  const [now,setNow] = useState(Date.now());
  useEffect(() => {
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()),1000);
    return () => clearInterval(timer);
  },[value]);
  if (!value) return null;
  const seconds = Math.max(0,Math.ceil((Date.parse(value.expires_at)-now)/1000));
  return <div className={`settings-device-qr settings-pairing-layout${seconds > 0 ? '' : ' is-expired'}`}>
    <img src={value.qr} alt="设备配对二维码"/><div className="settings-pairing-info">{seconds > 0 ? <>
      <span className="ui-row-meta" role="timer">{String(Math.floor(seconds/60)).padStart(2,'0')}:{String(seconds%60).padStart(2,'0')}</span>
      <a href={value.url} rel="noreferrer" aria-label="配对链接">配对链接</a>
    </> : <span className="ui-row-meta">已过期</span>}</div>
  </div>;
}
