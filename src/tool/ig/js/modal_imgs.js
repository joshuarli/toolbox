(function () {
  const article = document.querySelector("article");
  if (!article) return [];
  const CDN_RE = /\/t\d+\.\d+-(?:15|6)\//;
  function isCDN(u) {
    return Boolean(u) && CDN_RE.test(u);
  }
  function bestSrc(img) {
    const ss = (img.getAttribute("srcset") || "")
      .split(",")
      .map(function (p) {
        p = p.trim().split(/\s+/);
        return { url: p[0] || "", w: parseInt(p[1]) || 0 };
      })
      .filter(function (x) {
        return isCDN(x.url);
      });
    if (ss.length > 0) {
      ss.sort((left, right) => {
        return right.w - left.w;
      });
      return ss[0].url;
    }
    const src = img.getAttribute("src") || "";
    return isCDN(src) ? src : "";
  }
  function visibleEnough(img) {
    if (img.closest("header")) return false;
    const r = img.getBoundingClientRect();
    if (!r.width || !r.height) return false;
    if (r.bottom <= 0 || r.right <= 0 || r.top >= window.innerHeight || r.left >= window.innerWidth)
      return false;
    const visibleW = Math.min(r.right, window.innerWidth) - Math.max(r.left, 0);
    const visibleH = Math.min(r.bottom, window.innerHeight) - Math.max(r.top, 0);
    return visibleW > r.width * 0.45 && visibleH > r.height * 0.45;
  }
  let ul = null;
  const uls = article.querySelectorAll("ul");
  for (let i = 0; i < uls.length; i++) {
    const liImgs = uls[i].querySelectorAll("li img");
    for (let j = 0; j < liImgs.length; j++) {
      if (bestSrc(liImgs[j])) {
        ul = uls[i];
        break;
      }
    }
    if (ul) break;
  }
  if (ul) {
    const visible = Array.from(ul.querySelectorAll("li img")).filter(function (img) {
      return visibleEnough(img) && bestSrc(img);
    });
    if (visible.length > 0) {
      visible.sort((left, right) => {
        return (
          right.getBoundingClientRect().width * right.getBoundingClientRect().height -
          left.getBoundingClientRect().width * left.getBoundingClientRect().height
        );
      });
      return [bestSrc(visible[0])];
    }
  }
  const imgs = Array.from(article.querySelectorAll("img")).filter(function (img) {
    return visibleEnough(img) && bestSrc(img);
  });
  return imgs.length > 0 ? [bestSrc(imgs[0])] : [];
})();
