(function () {
  const seen = {};
  const results = [];
  function decodeEfg(url) {
    try {
      const m = url.match(/[?&]efg=([^&]+)/);
      if (!m) return "";
      return atob(decodeURIComponent(m[1]).replace(/-/g, "+").replace(/_/g, "/")).toLowerCase();
    } catch {
      return "";
    }
  }
  function fullFileUrl(url) {
    try {
      const u = new URL(url);
      u.searchParams.delete("bytestart");
      u.searchParams.delete("byteend");
      return u.toString();
    } catch {
      return url;
    }
  }
  performance.getEntriesByType("resource").forEach((e) => {
    var url = e.name || "";
    if (url.indexOf(".mp4") === -1) return;
    if (url.indexOf("cdninstagram.com") === -1 && url.indexOf("fbcdn.net") === -1) return;
    url = fullFileUrl(url);
    if (seen[url]) return;
    seen[url] = true;
    results.push({ url: url, decoded: decodeEfg(url), startTime: e.startTime || 0 });
  });
  results.sort((a, b) => {
    function score(x) {
      var d = x.decoded;
      var s = x.startTime / 10;
      if (d.indexOf("audio") !== -1) s -= 10000;
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
