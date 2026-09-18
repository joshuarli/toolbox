(function () {
  if (document.querySelector('script[type="application/ld+json"]')) return "jsonld";
  if (
    document.querySelector('article button[aria-label="Next"]') ||
    document.querySelector('button[aria-label="Next"]')
  )
    return "carousel";
  const og = document.querySelector('meta[property="og:image"]');
  if (og && og.getAttribute("content")) return "og";
  return null;
})();
