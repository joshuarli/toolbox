(function () {
  const scripts = document.querySelectorAll('script[type="application/ld+json"]');
  for (let i = 0; i < scripts.length; i++) {
    try {
      const d = JSON.parse(scripts[i].textContent);
      if (d["@type"] === "VideoObject") {
        let videoUrl = d.contentUrl || null;
        if (!videoUrl) {
          const v = document.querySelector("video");
          if (v) videoUrl = v.src || v.getAttribute("src") || null;
        }
        return {
          type: "video",
          username: (d.author && d.author.identifier) || null,
          caption: d.description || null,
          mediaUrls: videoUrl ? [videoUrl] : [],
        };
      }
      if (d["@type"] === "ImageObject") {
        const imgs = Array.isArray(d.image) ? d.image : d.image ? [d.image] : [];
        return {
          type: "image",
          username: (d.author && d.author.identifier) || null,
          caption: d.description || null,
          mediaUrls: imgs,
        };
      }
    } catch {}
  }
  function meta(prop) {
    const el = document.querySelector('meta[property="' + prop + '"]');
    return el ? el.getAttribute("content") : null;
  }
  let username = null;
  const links = document.querySelectorAll('article header a[href], header a[role="link"][href]');
  for (let j = 0; j < links.length; j++) {
    const m = (links[j].getAttribute("href") || "").match(/^\/([A-Za-z0-9._]+)\/?$/);
    if (m) {
      username = m[1];
      break;
    }
  }
  return {
    type: "image",
    username,
    caption: meta("og:description"),
    mediaUrls: [meta("og:image")].filter(Boolean),
  };
})();
