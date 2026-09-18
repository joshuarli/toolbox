(function () {
  const a = document.querySelector("article");
  const btn = a && a.querySelector('button[aria-label="Next"]');
  if (!btn) return null;
  const r = btn.getBoundingClientRect();
  if (!r.width || !r.height) return null;
  return {
    docX: r.left + window.scrollX + r.width / 2,
    docY: r.top + window.scrollY + r.height / 2,
    viewH: window.innerHeight,
    disabled: btn.disabled,
    pointerEvents: getComputedStyle(btn).pointerEvents,
  };
})();
