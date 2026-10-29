// 26.10, Sid - page "Photos" : la file de révision au fil de l'eau (Toutes/
// Reolink/Proies suspectées/un chat), chaque sous-page sa vraie URL
// (/photos, /photos/reolink, /photos/proies, /photos/<chat>) - lue depuis
// location.pathname au chargement, donc un F5 ou un lien partagé reste sur
// le même filtre, plus de retour forcé à "Toutes".

let cats = [];
let currentQueue = [];
let currentIndex = 0;
let lastStats = {};
// 29.10, Sid ("comment je rajoute les 84 patoune sans proie") - les
// candidates pour le lot identité du futur Modèle 2 sont ici, dans la
// vraie file "jamais encore revue" (Archives ne contient QUE du déjà
// traité - confirmé, ça ne peut jamais aider à combler ce manque).
let excludePreyFilter = false;

const SOURCE_SUBPAGES = [
  { slug: 'reolink', label: 'Reolink', source: 'reolink_or_manual', statKey: 'reolink_or_manual_pending' },
];
const PREY_SLUG = 'proies';

function currentFilter() {
  // "/photos" -> [''], "/photos/" -> [''], "/photos/reolink" -> ['reolink']
  const rest = location.pathname.replace(/^\/photos\/?/, '');
  return rest || null; // null = Toutes
}

function slugify(name) { return name.toLowerCase(); }

async function init() {
  const [catsResp, statsResp] = await Promise.all([
    api('/app-api/cats'),
    api('/app-api/stats'),
  ]);
  cats = catsResp;
  lastStats = statsResp;
  renderSubNav();
  renderStats(statsResp);
  await loadQueue();
}

function renderStats(stats) {
  document.getElementById('stats').innerHTML = `
    <span><b>${stats.pending}</b> à réviser</span>
    <span><b>${stats.prey_pending}</b> proies suspectées</span>
    <span><b>${stats.model_disagreements}</b> désaccords modèle</span>
    <span><b>${stats.deduped}</b> dédoublonnées</span>
  `;
}

function renderSubNav() {
  const filter = currentFilter();
  const el = document.getElementById('sub-nav');
  const allTab = `<a class="tab ${filter === null ? 'active' : ''}" style="${filter === null ? 'background:var(--accent)' : ''}" href="/photos">Toutes</a>`;
  const catTabs = cats.map(c => {
    const slug = slugify(c.name);
    const active = filter === slug;
    return `<a class="tab ${active ? 'active' : ''}"
         style="${active ? `background:${catColor(c.name)}` : `border-color:${catColor(c.name)}44`}"
         href="/photos/${slug}">${c.name}<span class="count">${c.pending}</span></a>`;
  }).join('');
  const sourceTabs = SOURCE_SUBPAGES.map(t => {
    const active = filter === t.slug;
    return `<a class="tab ${active ? 'active' : ''}"
         style="${active ? 'background:var(--accent)' : 'border-color:var(--accent)44'}"
         href="/photos/${t.slug}">${t.label}<span class="count">${lastStats[t.statKey] ?? 0}</span></a>`;
  }).join('');
  const preyTab = `<a class="tab ${filter === PREY_SLUG ? 'active' : ''}"
       style="${filter === PREY_SLUG ? 'background:var(--danger)' : 'border-color:var(--danger)'}"
       href="/photos/${PREY_SLUG}">Proies suspectées<span class="count">${lastStats.prey_pending ?? 0}</span></a>`;
  const excludePreyCheckbox = `<label style="margin-left:8px; white-space:nowrap"
       title="Exclut les captures où une proie coexiste - pour trouver des captures 'chat seul' pour le lot identité">
       <input type="checkbox" ${excludePreyFilter ? 'checked' : ''} onchange="setExcludePrey(this.checked)">
       Exclure les proies (lot identité)</label>`;
  el.innerHTML = allTab + catTabs + sourceTabs + preyTab + excludePreyCheckbox;
}

function setExcludePrey(checked) {
  excludePreyFilter = checked;
  // 29.10 - même bug que archives.js/revision.js : vider la sélection au
  // changement de filtre, sinon une action en lot peut viser des captures
  // qui ne sont plus affichées à l'écran.
  selectedIds.clear();
  loadQueue();
}

async function loadQueue() {
  const filter = currentFilter();
  // 25.10, Sid ("je sois pas limitée à 200") - relevée largement au-dessus
  // de n'importe quelle combinaison réaliste plutôt que supprimée.
  const params = new URLSearchParams({ limit: 10000 });
  const sourceTab = SOURCE_SUBPAGES.find(t => t.slug === filter);
  if (filter === PREY_SLUG) {
    params.set('prey_only', '1');
  } else if (sourceTab) {
    params.set('source', sourceTab.source);
  } else if (filter) {
    const cat = cats.find(c => slugify(c.name) === filter);
    if (cat) params.set('cat_name', cat.name);
  }
  if (excludePreyFilter) params.set('exclude_prey', '1');
  currentQueue = await api(`/app-api/queue?${params}`);
  renderQueue();
}

function renderQueue() {
  const grid = document.getElementById('queue');
  const empty = document.getElementById('empty');
  if (currentQueue.length === 0) {
    grid.innerHTML = '';
    empty.hidden = false;
    renderBulkBar();
    return;
  }
  empty.hidden = true;
  grid.innerHTML = currentQueue.map((c, i) => `
    <div class="queue-tile ${c.is_prey_candidate ? 'prey' : ''} ${c.model_disagreement ? 'disagreement' : ''} ${selectedIds.has(c.id) ? 'selected' : ''}"
         onclick="openDetail(${i})">
      <img src="/app-api/captures/${c.id}/image?size=thumbnail" loading="lazy">
      <input type="checkbox" class="tile-check" ${selectedIds.has(c.id) ? 'checked' : ''}
             onclick="event.stopPropagation(); toggleSelect(${c.id}, renderQueue)">
      ${c.is_prey_candidate ? '<div class="flag">proie ?</div>' : ''}
      ${c.model_disagreement && !c.is_prey_candidate ? '<div class="flag">modèle ?</div>' : ''}
    </div>
  `).join('');
  renderBulkBar();
}

function renderBulkBar() {
  const bar = document.getElementById('bulk-bar');
  const count = selectedIds.size;
  bar.hidden = count === 0;
  if (count === 0) return;
  const label = `${count} sélectionnée${count > 1 ? 's' : ''}`;
  bar.innerHTML = `
    <span>${label}</span>
    <button onclick="selectAllVisible(currentQueue, renderQueue)">Tout sélectionner</button>
    <button onclick="clearSelection(renderQueue)">Annuler</button>
    <button class="btn-reject" onclick="bulkReview('rejected')">Écarter la sélection</button>`;
}

async function bulkReview(status) {
  const ids = [...selectedIds];
  if (ids.length === 0) return;
  await api('/app-api/captures/bulk-review', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids, status }),
  });
  selectedIds.clear();
  await loadQueue();
  const statsResp = await api('/app-api/stats');
  lastStats = statsResp;
  renderStats(statsResp);
  const catsResp = await api('/app-api/cats');
  cats = catsResp;
  renderSubNav();
}

async function openDetail(index) {
  currentIndex = index;
  await renderDetail(currentQueue[currentIndex].id, null, () => renderDetail(currentQueue[currentIndex].id, null, arguments.callee));
  document.getElementById('detail').classList.add('open');
}

async function navigateDetail(delta) {
  const newIndex = currentIndex + delta;
  if (newIndex < 0 || newIndex >= currentQueue.length) return;
  currentIndex = newIndex;
  await renderCurrentDetail();
}

async function renderCurrentDetail() {
  const capture = currentQueue[currentIndex];
  await renderDetail(capture.id, null, renderCurrentDetail);
}

let reviewInFlight = false;

async function submitReview(status) {
  // 18.10, Sid ("ça m'arrive 3x de suite, une boîte que j'ai pas eu le
  // temps de corriger n'est plus dans la liste") - verrou simple contre un
  // double-clic pendant que le premier submitReview() attend encore.
  if (reviewInFlight) return;
  reviewInFlight = true;
  const actionButtons = document.querySelectorAll('.actions button');
  actionButtons.forEach(b => b.disabled = true);
  try {
    const capture = currentQueue[currentIndex];
    await api(`/app-api/captures/${capture.id}/review?status=${status}`, { method: 'POST' });
    currentQueue.splice(currentIndex, 1);
    renderQueue();
    const statsResp = await api('/app-api/stats');
    lastStats = statsResp;
    renderStats(statsResp);
    const catsResp = await api('/app-api/cats');
    cats = catsResp;
    renderSubNav();

    // 18.10, Sid ("ça pourrait passer direct à la suivante ?") - reste
    // ouvert, affiche directement la suivante (même emplacement, on vient
    // de spliced l'actuelle).
    if (currentQueue.length === 0) {
      // 18.10, Sid ("pourquoi je vois pas les 4700 photos ?") - un lot
      // peut être bien plus gros que ce qui a été chargé d'un coup ;
      // retente un chargement avant de conclure que c'est vraiment vide.
      await loadQueue();
      if (currentQueue.length === 0) {
        closeDetail();
        return;
      }
      currentIndex = 0;
      await renderCurrentDetail();
      return;
    }
    if (currentIndex >= currentQueue.length) {
      currentIndex = currentQueue.length - 1;
    }
    await renderCurrentDetail();
  } finally {
    reviewInFlight = false;
    actionButtons.forEach(b => b.disabled = false);
  }
}

init();

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}
