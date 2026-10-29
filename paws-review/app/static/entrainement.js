// 26.10, Sid ("Validée ça va avec entrainement") - page "Entraînement" :
// Export + Entraînement (anciennement dans le header de toutes les pages,
// maintenant 100% ici), et 2 vraies sous-pages pour les données
// d'entraînement elles-mêmes : /entrainement = Validées (pas encore
// exportées), /entrainement/exportees = déjà dans un zip (confirmed/
// corrected uniquement - voir status_group="exported" côté serveur,
// distinct d'"archived" qui mélange aussi les écartées). Stats par classe/
// source retiré le même soir ("tu peux enlever la partie des stats").

const SUBPAGES = [
  { slug: '', label: 'Validées', statusGroup: 'validated', href: '/entrainement' },
  { slug: 'exportees', label: 'Exportées (dans le zip)', statusGroup: 'exported', href: '/entrainement/exportees' },
];

function currentSub() {
  const rest = location.pathname.replace(/^\/entrainement\/?/, '');
  return SUBPAGES.find(s => s.slug === rest) || SUBPAGES[0];
}

let cats = [];
let currentQueue = [];
let currentIndex = 0;
let reviewedCatFilter = null;
let reviewedSourceFilter = null;
let reviewedPreyFilter = false;

function renderSubNav() {
  const sub = currentSub();
  document.getElementById('sub-nav').innerHTML = SUBPAGES.map(s => {
    const active = s.slug === sub.slug;
    return `<a class="tab ${active ? 'active' : ''}" style="${active ? 'background:var(--text-dim);color:var(--bg)' : 'border-color:var(--text-dim)'}" href="${s.href}">${s.label}</a>`;
  }).join('');
}

async function init() {
  cats = await api('/app-api/cats');
  renderSubNav();
  renderReviewedFilterBar();
  await loadQueue();
  await checkExportStatus();
  await checkTrainStatus();
}

// --- Export ----------------------------------------------------------
let exportPollTimer = null;

async function checkExportStatus() {
  const state = await api('/app-api/export/status');
  renderExportBar(state);
  if (state.status === 'running') {
    if (!exportPollTimer) exportPollTimer = setInterval(checkExportStatus, 3000);
  } else if (exportPollTimer) {
    clearInterval(exportPollTimer);
    exportPollTimer = null;
  }
}

async function startExport(full = false) {
  await api(`/app-api/export/start?full=${full ? '1' : '0'}`, { method: 'POST' });
  await checkExportStatus();
}

function renderExportBar(state) {
  const bar = document.getElementById('export-bar');
  if (state.status === 'running') {
    const progress = state.total ? ` (${state.done ?? 0}/${state.total})` : '';
    bar.innerHTML = `<span class="export-msg">Export en cours${progress}...</span>
      <button onclick="resetStuckState('export', checkExportStatus)" title="Si ça semble bloqué après un redéploiement">Réinitialiser</button>`;
  } else if (state.status === 'ready') {
    if (state.images === 0) {
      bar.innerHTML = `
        <span class="export-msg">${state.message || 'Rien de nouveau à exporter.'}</span>
        <button onclick="startExport()">Exporter pour entraînement</button>`;
    } else {
      bar.innerHTML = `
        <span class="export-msg">✓ Zip prêt : ${state.images} images (${state.positives} avec boîte, ${state.negatives} négatifs)</span>
        <a href="/app-api/export/download" class="btn-download">Télécharger</a>
        <button onclick="startExport()">Nouvel export</button>`;
    }
  } else if (state.status === 'error') {
    bar.innerHTML = `
      <span class="export-msg export-error">Échec de l'export : ${state.message || 'erreur inconnue'}</span>
      <button onclick="startExport()">Réessayer</button>`;
  } else {
    bar.innerHTML = `<button onclick="startExport()">Exporter pour entraînement</button>`;
  }
}

// --- Entraînement ------------------------------------------------------
let trainPollTimer = null;

async function checkTrainStatus() {
  const state = await api('/app-api/train/status');
  renderTrainBar(state);
  if (state.status === 'running') {
    if (!trainPollTimer) trainPollTimer = setInterval(checkTrainStatus, 10000);
  } else if (trainPollTimer) {
    clearInterval(trainPollTimer);
    trainPollTimer = null;
  }
}

async function startTraining(collapseIdentity = false) {
  try {
    await api(`/app-api/train/start?collapse_identity=${collapseIdentity ? '1' : '0'}`, { method: 'POST' });
  } catch (e) {
    // 409 = déjà en cours - pas une vraie erreur, juste resynchroniser l'affichage
  }
  await checkTrainStatus();
}

const TRAIN_STEP_LABELS = {
  export: 'Préparation des données',
  searching_offer: 'Recherche d\'une machine GPU',
  health_check: 'Vérification de la machine',
  training: 'Entraînement en cours',
};

// 29.10, plan de scission détection/identité - deux modes d'entraînement
// possibles côté train_vastai.py (collapse_identity=0|1), même verrou/
// état partagé (un seul run à la fois, peu importe le mode - même budget
// Vast.ai). Deux boutons distincts tant qu'il n'y a pas de modèle 2 à
// proprement parler : le modèle combiné actuel (5 classes, inchangé) et
// le nouveau modèle 1 (détection pure Cat+Prey, voir export_training.py).
function renderTrainBar(state) {
  const bar = document.getElementById('train-bar');
  const bothButtons = `
    <button onclick="startTraining(false)">Lancer un entraînement (modèle actuel, 5 classes)</button>
    <button onclick="startTraining(true)" title="Classes réduites à Cat+Prey - voir le plan de scission détection/identité">Lancer un entraînement Modèle 1 (détection Cat+Prey)</button>`;
  if (state.status === 'running') {
    const label = TRAIN_STEP_LABELS[state.step] || 'En cours';
    const price = state.price ? ` - ${state.price.toFixed(3)}$/h` : '';
    bar.innerHTML = `<span class="export-msg">🧠 ${label}${price}...</span>
      <button onclick="resetStuckState('train', checkTrainStatus)" title="Si ça semble bloqué après un redéploiement">Réinitialiser</button>`;
  } else if (state.status === 'success') {
    const when = state.finished_at ? new Date(state.finished_at * 1000).toLocaleString('fr-CH') : '';
    bar.innerHTML = `
      <span class="export-msg">✓ Nouveau modèle entraîné (${state.images} images, ${when}) - sélectionne-le dans Kittyhack</span>
      ${bothButtons}`;
  } else if (state.status === 'error') {
    bar.innerHTML = `
      <span class="export-msg export-error">Échec de l'entraînement : ${state.message || 'erreur inconnue'}</span>
      ${bothButtons}`;
  } else {
    bar.innerHTML = bothButtons;
  }
}

// --- Validées (file + filtres) ------------------------------------------
function renderReviewedFilterBar() {
  const bar = document.getElementById('reviewed-filter-bar');
  const catOptions = ['', ...cats.map(c => c.name), 'Prey'].map(n =>
    `<option value="${n}" ${reviewedCatFilter === (n || null) ? 'selected' : ''}>${n || 'Tous les chats'}</option>`
  ).join('');
  bar.innerHTML = `
    <span class="export-msg">${currentSub().label}</span>
    <select onchange="reviewedSetFilter('cat', this.value || null)">${catOptions}</select>
    <select onchange="reviewedSetFilter('source', this.value || null)">
      <option value="" ${!reviewedSourceFilter ? 'selected' : ''}>Toute source</option>
      <option value="kittyflap" ${reviewedSourceFilter === 'kittyflap' ? 'selected' : ''}>HK</option>
      <option value="reolink_or_manual" ${reviewedSourceFilter === 'reolink_or_manual' ? 'selected' : ''}>Reolink+Manuel</option>
    </select>
    <label style="margin-left:8px"><input type="checkbox" ${reviewedPreyFilter ? 'checked' : ''}
      onchange="reviewedSetFilter('prey', this.checked)"> Proies suspectées seulement</label>
  `;
}

function reviewedSetFilter(key, value) {
  if (key === 'cat') reviewedCatFilter = value;
  else if (key === 'source') reviewedSourceFilter = value;
  else if (key === 'prey') reviewedPreyFilter = value;
  loadQueue();
}

async function loadQueue() {
  const params = new URLSearchParams({ limit: 10000, status_group: currentSub().statusGroup });
  if (reviewedCatFilter) params.set('cat_name', reviewedCatFilter);
  if (reviewedSourceFilter) params.set('source', reviewedSourceFilter);
  if (reviewedPreyFilter) params.set('prey_only', '1');
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
