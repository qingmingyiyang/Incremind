// 页面和分享 worker 共用持久队列；凭据只由既有 productFetch 读取。
globalThis.chriptmasCaptureStore = (() => {
  const open = () => new Promise((resolve, reject) => {
    const request = indexedDB.open('chriptmas-share-capture', 1);
    request.onupgradeneeded = () => {
      request.result.createObjectStore('captures', { keyPath: 'id' });
      request.result.createObjectStore('meta');
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(new Error('capture_storage_unavailable'));
  });
  async function operation(store, mode, action) {
    const db = await open();
    try {
      return await new Promise((resolve, reject) => {
        const tx = db.transaction(store, mode), request = action(tx.objectStore(store));
        tx.oncomplete = () => resolve(request.result);
        tx.onerror = tx.onabort = () => reject(new Error('capture_storage_unavailable'));
      });
    } finally { db.close(); }
  }
  return {
    get: id => operation('captures', 'readonly', store => store.get(id)),
    list: () => operation('captures', 'readonly', store => store.getAll()),
    put: value => operation('captures', 'readwrite', store => store.put(value)),
    remove: id => operation('captures', 'readwrite', store => store.delete(id)),
    owner: () => operation('meta', 'readonly', store => store.get('owner')),
    setOwner: value => operation('meta', 'readwrite', store => store.put(value, 'owner')),
  };
})();
