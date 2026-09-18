(function () {
  // Reels use a separate route, and a profile with one post is still a valid grid.
  const links = Array.from(document.querySelectorAll('a[href*="/p/"], a[href*="/reel/"]'));
  if (!links.length) return null;
  const rects = links.map(function (a) {
    return a.getBoundingClientRect();
  });
  const firstTop = rects[0].top;
  const perRow =
    rects.filter(function (r) {
      return Math.abs(r.top - firstTop) < 5;
    }).length || 3;
  const rowH = perRow < rects.length ? rects[perRow].top - rects[0].top : rects[0].height;
  return { perRow, rowH, headerH: rects[0].top + window.scrollY };
})();
