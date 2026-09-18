function starsHtml(rating) {
  if (rating == null) return '';
  const full = Math.floor(rating);
  const half = (rating - full) >= 0.25 && (rating - full) < 0.75;
  const empty = 5 - full - (half ? 1 : 0);
  return '★'.repeat(full) + (half ? '½' : '') + '☆'.repeat(empty);
}

function slugToName(slug) {
  return slug.replace(/-\d+$/, '').split('-').map(w => w[0].toUpperCase() + w.slice(1)).join(' ');
}

const observer = new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (e.isIntersecting) {
      e.target.src = e.target.dataset.src;
      observer.unobserve(e.target);
    }
  }
}, { rootMargin: '200px' });

function renderCards(items) {
  const grid = document.getElementById('grid');
  grid.innerHTML = items.map(m => {
    const img = m.posterUrl || m.backdropUrl || '';
    const thumb = img
      ? `<img class="card-img" data-src="${img}" alt="">`
      : `<div class="card-img card-img-empty"></div>`;
    const ratingHtml = m.rating != null
      ? `<span class="card-rating">${starsHtml(m.rating)}</span>`
      : '';
    const directors = m.directors.map(slugToName).join(', ');
    return `<a class="card" href="../${m.slug}/">
      ${thumb}
      <div class="card-body">
        <div class="card-title">${m.title}</div>
        <div class="card-meta">${m.year ? m.year + ' · ' : ''}${directors}</div>
        ${ratingHtml}
      </div>
    </a>`;
  }).join('');
  for (const img of grid.querySelectorAll('img[data-src]')) observer.observe(img);
}

const q = new URLSearchParams(location.search).get('q') || '';

if (!q) {
  document.getElementById('not-found').style.display = '';
} else {
  const matches = [];
  const roleSet = new Set();

  for (const m of DATA) {
    const roles = [];
    if (m.directors.includes(q)) roles.push('Director');
    if (m.cast.includes(q)) roles.push('Cast');
    for (const c of m.crew) {
      if (c.slug === q) roles.push(c.role);
    }
    if (roles.length) {
      matches.push(m);
      roles.forEach(r => roleSet.add(r));
    }
  }

  const header = document.getElementById('person-header');
  header.style.display = '';
  document.getElementById('person-name').textContent = slugToName(q);
  document.getElementById('person-roles').textContent =
    roleSet.size ? [...roleSet].join(' · ') : '';

  if (matches.length) {
    renderCards(matches);
  } else {
    document.getElementById('not-found').style.display = '';
    header.style.display = 'none';
  }
}
