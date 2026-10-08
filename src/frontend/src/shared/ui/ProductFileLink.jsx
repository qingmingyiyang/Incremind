import { useState } from 'react';
import { useRequestScope } from '../lib/useRequestScope';
import { downloadProductFile, isProductUrl, readDeviceCredential } from '../api/deviceTransport';

export function ProductFileLink({ href, name, scopeKey = href, children }) {
  const gate = useRequestScope(scopeKey), scope = gate.scope;
  const [error, setError] = useState(false);
  async function download(event) {
    if (!readDeviceCredential() || !isProductUrl(href)) return;
    event.preventDefault(); setError(false);
    const token = gate.issue('download', scope);
    try { await downloadProductFile(href, name, () => gate.isCurrent(token)); }
    catch { if (gate.isCurrent(token)) setError(true); }
  }
  return <><a href={href} target="_blank" rel="noreferrer" onClick={download}>{children}</a>{error && <span role="alert">原件未打开 · 重试</span>}</>;
}
