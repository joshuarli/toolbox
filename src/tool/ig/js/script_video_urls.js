(function () {
  const scripts = document.querySelectorAll("script:not([src])");
  const seen = {};
  const results = [];
  function decodeUrl(raw) {
    return raw
      .replace(/\\\//g, "/")
      .replace(/\\u0025/g, "%")
      .replace(/\\u0026/g, "&")
      .replace(/\\u003[dD]/g, "=");
  }
  for (let i = 0; i < scripts.length; i++) {
    const txt = scripts[i].textContent || "";
    if (txt.indexOf("PolarisPostRootQuery") === -1) continue;
    if (txt.indexOf("video_versions") === -1 && txt.indexOf(".mp4") === -1) continue;
    let start = 0;
    while (true) {
      const mp4Idx = txt.indexOf(".mp4", start);
      if (mp4Idx === -1) break;
      const qStart = txt.lastIndexOf('"', mp4Idx);
      if (qStart === -1) {
        start = mp4Idx + 4;
        continue;
      }
      const qEnd = txt.indexOf('"', mp4Idx);
      if (qEnd === -1) {
        start = mp4Idx + 4;
        continue;
      }
      const url = decodeUrl(txt.substring(qStart + 1, qEnd));
      if (url.indexOf("http") === 0 && url.indexOf(".mp4") !== -1 && !seen[url]) {
        seen[url] = true;
        let decoded = "";
        try {
          const m = url.match(/[?&]efg=([^&]+)/);
          if (m) decoded = atob(decodeURIComponent(m[1]).replace(/-/g, "+").replace(/_/g, "/"));
        } catch {}
        results.push({ url, decoded: decoded.toLowerCase() });
      }
      start = qEnd + 1;
    }
    if (results.length > 0) break;
  }
  results.sort((a, b) => {
    function score(x) {
      var d = x.decoded;
      var s = 0;
      if (d.indexOf("vp9") !== -1 || d.indexOf("av1") !== -1) s -= 100;
      if (d.indexOf("progressive") !== -1) s += 50;
      if (d.indexOf("dash_baseline") !== -1) s += 30;
      if (d.indexOf("720") !== -1) s += 7;
      if (d.indexOf("540") !== -1) s += 5;
      return s;
    }
    return score(b) - score(a);
  });
  return results.map((x) => {
    return x.url;
  });
})();
