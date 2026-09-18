(function () {
  const CDN_RE = /\/t\d+\.\d+-(?:15|6)\//;
  const CDN_PATH_RE = /t\d+\.\d+-(?:15|6)/g;
  const scripts = document.querySelectorAll("script:not([src])");
  const seen = {};
  const results = [];
  for (let i = 0; i < scripts.length; i++) {
    const txt = scripts[i].textContent || "";
    CDN_PATH_RE.lastIndex = 0;
    if (!CDN_PATH_RE.test(txt)) continue;
    CDN_PATH_RE.lastIndex = 0;
    if (txt.indexOf("PolarisPostRootQuery") === -1) continue;
    let match = null;
    while ((match = CDN_PATH_RE.exec(txt)) !== null) {
      const urlIdx = match.index;
      const qStart = txt.lastIndexOf('"', urlIdx);
      if (qStart === -1) continue;
      const qEnd = txt.indexOf('"', urlIdx);
      if (qEnd === -1) continue;
      const rawUrl = txt.substring(qStart + 1, qEnd);
      const url = rawUrl.replace(/\\\//g, "/");
      if (url.indexOf("http") === 0 && CDN_RE.test(url)) {
        const fname = url.split("?")[0].split("/").pop();
        if (!seen[fname]) {
          seen[fname] = true;
          results.push(url);
        }
      }
    }
    break;
  }
  return results;
})();
