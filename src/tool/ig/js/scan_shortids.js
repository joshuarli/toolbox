Array.from(document.querySelectorAll('a[href*="/p/"], a[href*="/reel/"]'))
  .filter((a) => {
    var cell = a.closest("div[class]");
    if (cell && cell.querySelector('svg[aria-label="Pinned post icon"]')) return false;
    var m = (a.getAttribute("href") || "").match(/\/(?:p|reel)\/([A-Za-z0-9_-]+)/);
    return !!m;
  })
  .map((a) => {
    var m = (a.getAttribute("href") || "").match(/\/(?:p|reel)\/([A-Za-z0-9_-]+)/);
    return m ? m[1] : null;
  })
  .filter(Boolean);
