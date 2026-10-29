// 26.10, Sid - page "Archives" : 2 vraies sous-pages (/archives = Écartées
// par défaut, /archives/archivees), chacune son URL + ses filtres chat/
// source/proie dans l'URL en mémoire, mais le choix de sous-page dans l'URL
// réelle - F5 reste sur la bonne liste.

const SUBPAGES = [
  { slug: '', label: 'Écartées', statusGroup: 'rejected', href: '/archives' },
  { slug: 'archivees', label: 'Archivées', statusGroup: 'archived', href: '/archives/archivees' },
];

function currentSub() {
  const rest = location.pathname.replace(/^\/archives\/?/, '');
  return SUBPAGES.find(s => s.slug === rest) || SUBPAGES[0];
}

let cats = [];
let currentQueue = [];
let currentIndex = 0;
let catFilter = null;
let sourceFilter = null;
let preyFilter = false;
let excludePreyFilter = false;
// 29.10, Sid ("je dois mettre un filtre validées/écartées dans Archives")
// - "Archivées" mélange à dessein les deux (export_training.py pose
// exported_at sur les deux à la fois), sans way de les distinguer avant
// ça - voir archived_status côté main.py:review_queue().
let archivedStatusFilter = null;

function renderSubNav() {
  const sub = currentSub();
  document.getElementById('sub-nav').innerHTML = SUBPAGES.map(s => {
    const active = s.slug === sub.slug;
    return `<a class="tab ${active ? 'active' : ''}" style="${active ? 'background:var(--text-dim);color:var(--bg)' : 'border-color:var(--text-dim)'}" href="${s.href}">${s.label}</a>`;
  }).join('');
}

function renderFilterBar() {
  const bar = document.getElementById('reviewed-filter-bar');
  const catOptions = ['', ...cats.map(c => c.name), 'Prey'].map(n =>
    `<option value="${n}" ${catFilter === (n || null) ? 'selected' : ''}>${n || 'Tous les chats'}</option>`
  ).join('');
  const sub = currentSub();
  const archivedStatusSelect = sub.statusGroup === 'archived' ? `
    <select onchange="setFilter('archivedStatus', this.value || null)" title="Archivées mélange validées et écartées au moment de l'export - filtre ici pour les distinguer">
      <option value="" ${!archivedStatusFilter ? 'selected' : ''}>Validées + Écartées</option>
      <option value="validated" ${archivedStatusFilter === 'validated' ? 'selected' : ''}>Validées seulement</option>
      <option value="rejected" ${archivedStatusFilter === 'rejected' ? 'selected' : ''}>Écartées seulement</option>
    </select>` : '';
  bar.innerHTML = `
    <select onchange="setFilter('cat', this.value || null)">${catOptions}</select>
    <select onchange="setFilter('source', this.value || null)">
      <option value="" ${!sourceFilter ? 'selected' : ''}>Toute source</option>
      <option value="kittyflap" ${sourceFilter === 'kittyflap' ? 'selected' : ''}>HK</option>
      <option value="reolink_or_manual" ${sourceFilter === 'reolink_or_manual' ? 'selected' : ''}>Reolink+Manuel</option>
    </select>
    ${archivedStatusSelect}
    <label style="margin-left:8px"><input type="checkbox" ${preyFilter ? 'checked' : ''}
      onchange="setFilter('prey', this.checked)"> Proies suspectées seulement</label>
    <label style="margin-left:8px" title="Exclut toute capture où une proie coexiste avec le chat - pour retrouver, parmi les écartées, des captures 'chat seul' réutilisables pour le lot propre du futur modèle d'identité">
      <input type="checkbox" ${excludePreyFilter ? 'checked' : ''}
      onchange="setFilter('excludePrey', this.checked)"> Exclure les proies (lot identité)</label>
  `;
}

function setFilter(key, value) {
  if (key === 'cat') catFilter = value;
  else if (key === 'source') sourceFilter = value;
  else if (key === 'prey') preyFilter = value;
  else if (key === 'excludePrey') excludePreyFilter = value;
  else if (key === 'archivedStatus') archivedStatusFilter = value;
  loadQueue();
}

async function init() {
  cats = await api('/app-api/cats');
  renderSubNav();
  renderFilterBar();
  await loadQueue();
}

async function loadQueue() {
  const sub = currentSub();
  const params = new URLSearchParams({ limit: 10000, status_group: sub.statusGroup });
  if (catFilter) params.set('cat_name', catFilter);
  if (sourceFilter) params.set('source', sourceFilter);
  if (preyFilter) params.set('prey_only', '1');
  if (excludePreyFilter) params.set('exclude_prey', '1');
  if (archivedStatusFilter) params.set('archived_status', archivedStatusFilter);
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
    <div class="queue-tile ${c.is_prey_candidate ? 'prey' : ''} ${selectedIds.has(c.id) ? 'selected' : ''}"
         onclick="openDetail(${i})">
      <img src="/app-api/captures/${c.id}/image?size=thumbnail" loading="lazy">
      <input type="checkbox" class="tile-check" ${selectedIds.has(c.id) ? 'checked' : ''}
             onclick="event.stopPropagation(); toggleSelect(${c.id}, renderQueue)">
      ${c.is_prey_candidate ? '<div class="flag">proie ?</div>' : ''}
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
  const sub = currentSub();
  // 29.10, Sid ("je dois aller les chercher dans celles que j'avais
  // écartée") - repêcher des captures depuis "Écartées" n'avait qu'un
  // bouton pour les écarter à nouveau, aucun moyen de les confirmer en
  // lot depuis cette vue (seulement une par une via le détail). Même
  // endpoint générique que "Écarter la sélection" ci-dessous
  // (/app-api/captures/bulk-review accepte déjà confirmed/corrected/
  // rejected), juste exposé ici aussi - uniquement utile sur "Écartées".
  const confirmButton = sub.statusGroup === 'rejected'
    ? `<button class="btn-confirm" onclick="bulkReview('confirmed')">Confirmer la sélection</button>`
    : '';
  // 29.10, Sid ("un nouveau bouton pour relancer ces images-là dans le
  // nouveau pipeline") - CORRECTIF du commentaire précédent ("repêcher ne
  // changerait rien" - FAUX, voir relaunchCaptures()) : sur "Archivées",
  // une capture écartée-puis-archivée doit repartir avec exported_at
  // remis à NULL pour redevenir "nouvelle", pas juste review_status.
  const relaunchButton = sub.statusGroup === 'archived'
    ? `<button class="btn-confirm" onclick="relaunchCaptures()" title="Remet ces captures en 'Validées', prêtes pour le prochain export/entraînement">Relancer dans le nouveau pipeline</button>`
    : '';
  bar.innerHTML = `
    <span>${label}</span>
    <button onclick="selectAllVisible(currentQueue, renderQueue)">Tout sélectionner</button>
    <button onclick="clearSelection(renderQueue)">Annuler</button>
    <button class="btn-reject" onclick="bulkReview('rejected')">Écarter la sélection</button>
    ${confirmButton}
    ${relaunchButton}`;
}

async function relaunchCaptures() {
  const ids = [...selectedIds];
  if (ids.length === 0) return;
  await api('/app-api/captures/relaunch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids }),
  });
  selectedIds.clear();
  await loadQueue();
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
}

async function openDetail(index) {
  currentIndex = index;
  await renderCurrentDetail();
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
  if (reviewInFlight) return;
  reviewInFlight = true;
  const actionButtons = document.querySelectorAll('.actions button');
  actionButtons.forEach(b => b.disabled = true);
  try {
    const capture = currentQueue[currentIndex];
    await api(`/app-api/captures/${capture.id}/review?status=${status}`, { method: 'POST' });
    currentQueue.splice(currentIndex, 1);
    renderQueue();
    if (currentQueue.length === 0) {
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
