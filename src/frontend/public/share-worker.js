importScripts('/capture-store.js');
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || url.pathname !== '/share-capture' || event.request.method !== 'POST') return;
  // 网页跨源表单不能借本机设备登录态入库；系统分享可以没有 Origin 或使用 none。
  const origin = event.request.headers.get('Origin');
  const site = event.request.headers.get('Sec-Fetch-Site');
  if ((origin && origin !== self.location.origin && !(origin === 'null' && site === 'none'))
      || site === 'cross-site' || site === 'same-site') {
    event.respondWith(new Response('分享来源不受支持', { status: 403, headers: { 'Content-Type': 'text/plain; charset=utf-8' } }));
    return;
  }
  event.respondWith((async () => {
    try {
      const form = await event.request.formData();
      const text = ['title', 'text', 'url'].map(name => form.get(name) || '').join('\n');
      const files = form.getAll('files').filter(value => value instanceof File && value.size);
      if (text.length > 60000 || files.length > 16 || files.reduce((size, file) => size + file.size, 0) > 2 * 1024 * 1024 * 1024) {
        return Response.redirect(new URL('/#capture-error=size', self.location.origin), 303);
      }
      // Cookie 导入由设置管理；分享队列不接受常见凭据文本和文件。
      if (/authorization\s*:|bearer\s+[\w.-]{16,}|(?:^|\n)\s*(?:cookie|set-cookie)\s*:|# Netscape HTTP Cookie File/i.test(text)
          || files.some(file => /^(cookies?(?:[-_.]export)?\.(txt|json)|credentials?\.json|authorization\.txt|\.env(\..*)?)$/i.test(file.name))) {
        return Response.redirect(new URL('/#capture-error=credentials', self.location.origin), 303);
      }
      for (const match of text.matchAll(/https?:\/\/[^\s<>]+/g)) {
        const target = new URL(match[0]);
        if (target.username || target.password || [...target.searchParams.keys()].some(key =>
          ['authorization', 'cookie', 'api_key', 'access_token', 'refresh_token', 'device_key'].includes(key.toLowerCase()))) {
          return Response.redirect(new URL('/#capture-error=credentials', self.location.origin), 303);
        }
      }
      for (const file of files) {
        if (/\.(txt|md)$/i.test(file.name)) {
          if (file.size > 12 * 1024 * 1024) return Response.redirect(new URL('/#capture-error=size', self.location.origin), 303);
          if (/authorization\s*:|(?:^|\n)\s*(?:cookie|set-cookie)\s*:|# Netscape HTTP Cookie File/i.test(await file.text())) {
            return Response.redirect(new URL('/#capture-error=credentials', self.location.origin), 303);
          }
        }
      }
      const id = crypto.randomUUID();
      await chriptmasCaptureStore.put({ id, text, files, owner: await chriptmasCaptureStore.owner() || null,
        createdAt: Date.now(), state: 'pending' });
      return Response.redirect(new URL('/#capture=' + id, self.location.origin), 303);
    } catch {
      return Response.redirect(new URL('/#capture-error=storage', self.location.origin), 303);
    }
  })());
});
