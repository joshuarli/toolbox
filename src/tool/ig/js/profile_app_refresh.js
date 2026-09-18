(async function () {
  try {
    if ("serviceWorker" in navigator) {
      const regs = await navigator.serviceWorker.getRegistrations();
      await Promise.all(
        regs.map((r) => {
          return r.unregister();
        }),
      );
    }
  } catch {}
  try {
    if ("caches" in window) {
      const keys = await caches.keys();
      await Promise.all(
        keys.map((k) => {
          return caches.delete(k);
        }),
      );
    }
  } catch {}
  return true;
})();
