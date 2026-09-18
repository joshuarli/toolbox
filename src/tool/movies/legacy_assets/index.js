const PAGE = 80;

const ms = new MiniSearch({
  fields: ['title', 'altTitle', 'year', 'directors'],
  storeFields: ['title', 'altTitle', 'year', 'directors', 'rating', 'backdropUrl', 'posterUrl'],
  searchOptions: { prefix: true, fuzzy: 0.2, boost: { title: 3, year: 2 } },
});

const grid = document.getElementById('grid');
const sentinel = document.getElementById('sentinel');
const input = document.getElementById('search');

let allData = [];
let displayed = 0;
let searchActive = false;

const imgObserver = new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (e.isIntersecting) {
      e.target.src = e.target.dataset.src;
      imgObserver.unobserve(e.target);
    }
  }
}, { rootMargin: '200px' });

const pageObserver = new IntersectionObserver((entries) => {
  if (entries[0].isIntersecting && !searchActive) appendPage();
}, { rootMargin: '400px' });

function starsHtml(rating) {
  if (rating == null) return '';
  const full = Math.floor(rating);
  const half = (rating - full) >= 0.25 && (rating - full) < 0.75;
  const empty = 5 - full - (half ? 1 : 0);
  return '★'.repeat(full) + (half ? '½' : '') + '☆'.repeat(empty);
}

function cardHtml(m) {
  const img = m.posterUrl || m.backdropUrl || '';
  const thumb = img
    ? `<img class="card-img" data-src="${img}" alt="">`
    : `<div class="card-img card-img-empty"></div>`;
  const ratingHtml = m.rating != null
    ? `<span class="card-rating" title="${m.rating.toFixed(2)}">${starsHtml(m.rating)}</span>`
    : '';
  return `<a class="card" href="${m.id}/">
    ${thumb}
    <div class="card-body">
      <div class="card-title">${m.title}</div>
      <div class="card-meta">${m.year ? m.year + ' · ' : ''}${m.directors}</div>
      ${ratingHtml}
    </div>
  </a>`;
}

function appendCards(items) {
  const tmpl = document.createElement('template');
  tmpl.innerHTML = items.map(cardHtml).join('');
  for (const img of tmpl.content.querySelectorAll('img[data-src]')) imgObserver.observe(img);
  grid.appendChild(tmpl.content);
}

function appendPage() {
  if (displayed >= allData.length) return;
  const batch = allData.slice(displayed, displayed + PAGE);
  appendCards(batch);
  displayed += batch.length;
}

function renderSearch(items) {
  grid.innerHTML = '';
  appendCards(items);
}

fetch('data.json')
  .then(r => r.json())
  .then(data => {
    allData = data;
    ms.addAll(data);
    appendPage();
    pageObserver.observe(sentinel);
  });

let debounce;
input.addEventListener('input', () => {
  clearTimeout(debounce);
  debounce = setTimeout(() => {
    const q = input.value.trim();
    if (!q) {
      searchActive = false;
      grid.innerHTML = '';
      displayed = 0;
      appendPage();
      pageObserver.observe(sentinel);
    } else {
      searchActive = true;
      pageObserver.unobserve(sentinel);
      renderSearch(ms.search(q));
    }
  }, 120);
});
