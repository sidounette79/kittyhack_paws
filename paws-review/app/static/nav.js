// 26.10, Sid - navigation de premier niveau entre les 4 vraies pages,
// identique partout (liens <a> classiques = vraie navigation, pas de JS
// qui réécrit le contenu en place). Chargé par chaque page après common.js.
const PAGE_NAV_ITEMS = [
  { href: '/photos', label: 'Photos', match: '/photos' },
  { href: '/entrainement', label: 'Entraînement', match: '/entrainement' },
  { href: '/revision', label: 'Révision', match: '/revision' },
  { href: '/archives', label: 'Archives', match: '/archives' },
];

function renderPageNav() {
  const el = document.getElementById('page-nav');
  if (!el) return;
  el.innerHTML = PAGE_NAV_ITEMS.map(item => {
    const active = location.pathname === item.match || location.pathname.startsWith(item.match + '/');
    return `<a class="tab ${active ? 'active' : ''}" style="${active ? 'background:var(--accent)' : 'border-color:var(--accent)44'}" href="${item.href}">${item.label}</a>`;
  }).join('');
}

renderPageNav();
