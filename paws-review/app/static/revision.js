// 26.10, Sid - page "Révision" : 3 vraies sous-pages (/revision = Revérif-
// ication par défaut, /revision/comparaison, /revision/equilibrage), chacune
// son URL propre - seule la section active est initialisée/pollée.

const SUBPAGES = [
  { slug: '', label: 'Revérification', href: '/revision' },
  { slug: 'comparaison', label: 'Comparaison', href: '/revision/comparaison' },
  { slug: 'equilibrage', label: 'Équilibrage', href: '/revision/equilibrage' },
];

function currentSub() {
  const rest = location.pathname.replace(/^\/revision\/?/, '');
  return rest || '';
}

let cats = [];

function renderSubNav() {
  const sub = currentSub();
  document.getElementById('sub-nav').innerHTML = SUBPAGES.map(s => {
    const active = s.slug === sub;
    return `<a class="tab ${active ? 'active' : ''}" style="${active ? 'background:var(--accent)' : 'border-color:var(--accent)44'}" href="${s.href}">${s.label}</a>`;
  }).join('');
}

async function init() {
  cats = await api('/app-api/cats');
  renderSubNav();
  const sub = currentSub();
  document.getElementById('model-review-bar').hidden = sub !== '';
  document.getElementById('compare-bar').hidden = sub !== 'comparaison';
  document.getElementById('rebalance-bar').hidden = sub !== 'equilibrage';
  if (sub === 'comparaison') {
    document.getElementById('queue').innerHTML = '';
    document.getElementById('empty').hidden = true;
    await checkCompareStatus();
  } else if (sub === 'equilibrage') {
    await loadRebalanceCounts();
    await loadRebalanceQueue();
  } else {
    await checkModelReviewStatus();
    await loadModelReviewQueue();
  }
}

/* ======================= Revérification ======================= */
let mrQueue = [];
let mrPollTimer = null;
let mrModels = [];
let mrIndex = 0;

async function checkModelReviewStatus() {
  const state = await api('/app-api/model-review/status');
  renderModelReviewBar(state);
  if (state.status === 'running') {
    if (!mrPollTimer) mrPollTimer = setInterval(checkModelReviewStatus, 3000);
  } else if (mrPollTimer) {
    clearInterval(mrPollTimer);
    mrPollTimer = null;
    await loadModelReviewQueue();
  }
}

async function startModelReview() {
  mrModels = await api('/app-api/model-review/models');
  if (mrModels.length === 0) return;
  try {
    // Le plus récent (list_model_dirs() trie déjà du plus récent au plus
    // ancien) - celui qu'on vient d'entraîner, le cas d'usage courant.
    await api('/app-api/model-review/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model_version: mrModels[0], num_captures: 300 }),
    });
  } catch (e) { /* 409 = déjà en cours - pas une vraie erreur */ }
  await checkModelReviewStatus();
}

const MR_STEP_LABELS = { loading_model: 'Chargement du modèle', scanning: 'Analyse des captures' };

function renderModelReviewBar(state) {
  const bar = document.getElementById('model-review-bar');
  if (state.status === 'running') {
    const label = MR_STEP_LABELS[state.step] || 'En cours';
    const progress = state.total ? ` (${state.scanned ?? 0}/${state.total})` : '';
    bar.innerHTML = `<span class="export-msg">Revérification : ${label}${progress}...</span>
      <button onclick="resetStuckState('model_review', checkModelReviewStatus)" title="Si ça semble bloqué après un redéploiement">Réinitialiser</button>`;
  } else if (state.status === 'success') {
    const when = state.finished_at ? new Date(state.finished_at * 1000).toLocaleString('fr-CH') : '';
    bar.innerHTML = `
      <span class="export-msg">✓ ${state.flagged ?? 0} désaccord(s) à trancher (${state.scanned ?? 0} captures scannées, ${when})</span>
      <button onclick="startModelReview()">Relancer le scan</button>
      <button onclick="resetModelReview()" title="Vide la file de désaccords en attente, pour repartir d'une file propre après un nouvel entraînement">Réinitialiser</button>`;
  } else if (state.status === 'error') {
    bar.innerHTML = `
      <span class="export-msg export-error">Échec : ${state.message || 'erreur inconnue'}</span>
      <button onclick="startModelReview()">Réessayer</button>`;
  } else {
    bar.innerHTML = `
      <button onclick="startModelReview()">Lancer le scan (dernier modèle)</button>
      <button onclick="resetModelReview()" title="Vide la file de désaccords en attente, pour repartir d'une file propre après un nouvel entraînement">Réinitialiser</button>`;
  }
}

async function resetModelReview() {
  if (!confirm("Réinitialiser la revérification : vide la file de désaccords en attente (non tranchés = perdus). Continuer ?")) return;
  await api('/app-api/model-review/reset', { method: 'POST' });
  await checkModelReviewStatus();
  await loadModelReviewQueue();
}

async function loadModelReviewQueue() {
  mrQueue = await api('/app-api/model-review/queue');
  renderMrQueue();
}

function renderMrQueue() {
  const grid = document.getElementById('queue');
  const empty = document.getElementById('empty');
  if (mrQueue.length === 0) {
    grid.innerHTML = '';
    empty.hidden = false;
    renderBulkBar();
    return;
  }
  empty.hidden = true;
  grid.innerHTML = mrQueue.map((q, i) => `
    <div class="queue-tile ${q.new_label === 'Prey' || q.old_label === 'Prey' ? 'prey' : 'disagreement'} ${selectedIds.has(q.id) ? 'selected' : ''}"
         onclick="openMrDetail(${i})">
      <img src="/app-api/captures/${q.capture_id}/image?size=thumbnail" loading="lazy">
      <input type="checkbox" class="tile-check" ${selectedIds.has(q.id) ? 'checked' : ''}
             onclick="event.stopPropagation(); toggleSelect(${q.id}, renderMrQueue)">
      <div class="flag">${q.old_label || '?'} → ${q.new_label}</div>
    </div>
  `).join('');
  renderBulkBar();
}

async function bulkResolveMr(action) {
  const ids = [...selectedIds];
  if (ids.length === 0) return;
  await api('/app-api/model-review/resolve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids, action }),
  });
  selectedIds.clear();
  await loadModelReviewQueue();
}

async function openMrDetail(index) {
  mrIndex = index;
  await renderMrDetailView();
  document.getElementById('mr-detail').classList.add('open');
}

function closeMrDetail() {
  document.getElementById('mr-detail').classList.remove('open');
}

async function navigateMrDetail(delta) {
  const newIndex = mrIndex + delta;
  if (newIndex < 0 || newIndex >= mrQueue.length) return;
  mrIndex = newIndex;
  await renderMrDetailView();
}

function toggleMrZoom() {
  document.getElementById('mr-image-wrap').classList.toggle('zoomed');
}

async function renderMrDetailView() {
  document.getElementById('mr-image-wrap').classList.remove('zoomed');
  const q = mrQueue[mrIndex];
  document.getElementById('mr-detail-meta').textContent =
    `capture #${q.capture_id} · chat : ${q.cat_name || '?'} (${q.cat_source || '?'})`;
  const img = document.getElementById('mr-detail-img');
  img.src = `/app-api/captures/${q.capture_id}/image?size=original`;
  const wrap = document.getElementById('mr-image-wrap');
  wrap.querySelectorAll('.box').forEach(b => b.remove());
  // Boîte ancienne : pas de géométrie stockée sur la ligne de groupe elle-
  // même (chaque frame a la sienne) - cherchée sur la capture représentante.
  const full = await api(`/app-api/captures/${q.capture_id}`);
  const oldBox = full.boxes.find(b => (b.label || '').toLowerCase() === (q.old_label || '').toLowerCase());
  if (oldBox) {
    const el = document.createElement('div');
    el.className = 'box mr-old';
    el.style.left = oldBox.x + '%'; el.style.top = oldBox.y + '%';
    el.style.width = oldBox.width + '%'; el.style.height = oldBox.height + '%';
    el.innerHTML = `<div class="box-label">ancien : ${q.old_label}</div>`;
    wrap.appendChild(el);
  }
  const newEl = document.createElement('div');
  newEl.className = 'box mr-new';
  newEl.style.left = q.new_x + '%'; newEl.style.top = q.new_y + '%';
  newEl.style.width = q.new_width + '%'; newEl.style.height = q.new_height + '%';
  newEl.innerHTML = `<div class="box-label">nouveau : ${q.new_label} ${q.new_probability.toFixed(0)}%</div>`;
  wrap.appendChild(newEl);

  document.getElementById('mr-old-chip').textContent = `ancien : ${q.old_label || '(rien)'}`;
  document.getElementById('mr-new-chip').textContent = `nouveau : ${q.new_label} (${q.new_probability.toFixed(0)}%)`;
}

async function resolveMrCurrent(action) {
  const q = mrQueue[mrIndex];
  await api('/app-api/model-review/resolve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids: [q.id], action }),
  });
  mrQueue.splice(mrIndex, 1);
  if (mrQueue.length === 0) {
    closeMrDetail();
    renderMrQueue();
    return;
  }
  if (mrIndex >= mrQueue.length) mrIndex = mrQueue.length - 1;
  await renderMrDetailView();
}

/* ======================= Comparaison ======================= */
const COMPARE_BATCH = 50;
let compareResult = null;
let comparePollTimer = null;
let compareClassFilter = null;

function renderCompareBar(state) {
  const bar = document.getElementById('compare-bar');
  // 26.10, Sid ("un filtre sur négatif... on était souvent tagué comme
  // boubou au lieu de rien") - "Negatif" est une pseudo-classe gérée côté
  // serveur, pas une vraie classe du modèle.
  const options = ['', 'Prey', 'Negatif', ...Object.keys(CAT_COLORS)]
    .map(c => `<option value="${c}" ${compareClassFilter === (c || null) ? 'selected' : ''}>${c === '' ? 'Tous' : (c === 'Prey' ? 'Proies' : c)}</option>`)
    .join('');
  const filterSelect = `<select onchange="setCompareClassFilter(this.value)" style="margin-right:8px">${options}</select>`;
  if (state && state.status === 'running') {
    const label = state.step === 'loading_model' ? 'Chargement des modèles' : 'Comparaison';
    const progress = state.total ? ` (${state.scanned ?? 0}/${state.total})` : '';
    bar.innerHTML = filterSelect + `<span class="export-msg">${label}${progress}...</span>
      <button onclick="resetStuckState('compare', checkCompareStatus)" title="Si ça semble bloqué après un redéploiement">Réinitialiser</button>`;
    return;
  }
  if (state && state.status === 'error') {
    bar.innerHTML = filterSelect + `
      <span class="export-msg export-error">Échec : ${state.message || 'erreur inconnue'}</span>
      <button onclick="startCompareRecent()">Réessayer</button>`;
    return;
  }
  if (!compareResult) {
    bar.innerHTML = filterSelect + `<button onclick="startCompareRecent()">Comparer les ${COMPARE_BATCH} derniers événements</button>`;
    return;
  }
  const r = compareResult;
  const hasMore = r.total_events > 0;
  bar.innerHTML = filterSelect + `
    <span class="export-msg">✓ ${r.total_shown} événement(s) accumulé(s) dans la galerie${r.class_filter ? ` (filtre : ${r.class_filter})` : ''}${hasMore ? `, ${r.total_events} restant(s)` : ', plus aucun restant'}</span>
    <a href="${r.url}" target="_blank" onclick="markCompareGalleryOpened()"><button>Ouvrir la galerie</button></a>
    ${hasMore ? `<button onclick="compareRecentMore()">${COMPARE_BATCH} événements précédents</button>` : ''}
    <button onclick="startCompareRecent()">Recommencer (nouvelle galerie)</button>`;
}

function markCompareGalleryOpened() {
  compareResult = null;
  renderCompareBar();
}

function setCompareClassFilter(value) {
  compareClassFilter = value || null;
  compareResult = null; // nouveau filtre = nouvelle galerie, pas de mélange
  renderCompareBar();
}

async function checkCompareStatus() {
  const state = await api('/app-api/model-compare/status');
  renderCompareBar(state);
  if (state.status === 'running') {
    if (!comparePollTimer) comparePollTimer = setInterval(checkCompareStatus, 2000);
  } else {
    if (comparePollTimer) { clearInterval(comparePollTimer); comparePollTimer = null; }
    if (state.status === 'success') compareResult = state;
  }
}

async function startCompareRecent() {
  try {
    await api('/app-api/model-compare/recent', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ limit: COMPARE_BATCH, class_filter: compareClassFilter }),
    });
  } catch (e) { /* 409 = déjà en cours */ }
  await checkCompareStatus();
}

async function compareRecentMore() {
  const sessionDir = compareResult ? compareResult.out_dir.split('/').pop() : null;
  try {
    await api('/app-api/model-compare/recent', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ limit: COMPARE_BATCH, class_filter: compareClassFilter, session_dir: sessionDir }),
    });
  } catch (e) { /* 409 = déjà en cours */ }
  await checkCompareStatus();
}

/* ======================= Équilibrage ======================= */
const REBALANCE_TARGET_PER_SOURCE = 200;
let rebalanceFilters = { cat: null, source: null, prey: false, negative: false, excludePrey: false };
let rebalanceCounts = null;
let currentQueue = [];
let currentIndex = 0;

async function loadRebalanceCounts() {
  rebalanceCounts = await api('/app-api/rebalance/counts');
  renderRebalanceBar();
}

function rebalanceSetFilter(key, value) {
  rebalanceFilters[key] = value;
  // 29.10 - même bug que archives.js : une sélection faite avant de
  // changer de filtre restait active, une action en lot pouvait alors
  // s'appliquer à des captures qui n'étaient plus affichées à l'écran.
  selectedIds.clear();
  loadRebalanceQueue();
  renderRebalanceBar();
}

async function rebalanceKeepSelection() {
  const ids = [...selectedIds];
  if (ids.length === 0) return;
  await api('/app-api/rebalance/keep', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids }),
  });
  selectedIds.clear();
  await loadRebalanceQueue();
  await loadRebalanceCounts();
}

async function rebalancePromote() {
  if (!confirm("Envoyer tout le lot 'gardé' vers le vrai statut validé (export-éligible) ? Elles sortiront de cet onglet et redeviendront des captures normales.")) return;
  const result = await api('/app-api/rebalance/promote', { method: 'POST' });
  alert(`${result.count} capture(s) envoyée(s) vers le statut validé.`);
  await loadRebalanceQueue();
  await loadRebalanceCounts();
}

function renderRebalanceBar() {
  const bar = document.getElementById('rebalance-bar');
  const catOptions = ['', ...cats.map(c => c.name), 'Prey'].map(n =>
    `<option value="${n}" ${rebalanceFilters.cat === (n || null) ? 'selected' : ''}>${n || 'Tous les chats'}</option>`
  ).join('');
  const controls = `
    <select onchange="rebalanceSetFilter('cat', this.value || null)">${catOptions}</select>
    <select onchange="rebalanceSetFilter('source', this.value || null)">
      <option value="" ${!rebalanceFilters.source ? 'selected' : ''}>Toute source</option>
      <option value="kittyflap" ${rebalanceFilters.source === 'kittyflap' ? 'selected' : ''}>HK</option>
      <option value="reolink_manual" ${rebalanceFilters.source === 'reolink_manual' ? 'selected' : ''}>Reolink+Manuel</option>
    </select>
    <label style="margin-left:8px"><input type="checkbox" ${rebalanceFilters.prey ? 'checked' : ''}
      onchange="rebalanceSetFilter('prey', this.checked)"> Proies suspectées seulement</label>
    <label style="margin-left:8px"><input type="checkbox" ${rebalanceFilters.negative ? 'checked' : ''}
      onchange="rebalanceSetFilter('negative', this.checked)"> Négatif seulement (ni chat ni proie)</label>
    <label style="margin-left:8px" title="Exclut toute capture où une proie coexiste avec le chat - pour constituer le lot propre du futur modèle d'identité">
      <input type="checkbox" ${rebalanceFilters.excludePrey ? 'checked' : ''}
      onchange="rebalanceSetFilter('excludePrey', this.checked)"> Exclure les proies (lot identité)</label>
    <button style="margin-left:8px" class="btn-confirm" onclick="rebalancePromote()" title="Envoie tout ce qui a été gardé ('Garder la sélection') vers le vrai statut validé, export-éligible">
      Envoyer le lot gardé vers Validé
    </button>
  `;
  let table = '';
  if (rebalanceCounts) {
    // 29.10, Sid ("comment je rajoute les 84 patoune sans proie") - colonnes
    // "sans proie" ajoutées : une capture Patoune+Prey compte dans HK/Reolink
    // (utile au Modèle 1) mais jamais dans ces deux-là (lot propre destiné au
    // futur modèle d'identité). Identiques aux colonnes normales pour les
    // classes sans co-occurrence proie - pas de régression visuelle pour elles.
    const rows = Object.entries(rebalanceCounts).map(([cls, g]) => `<tr>
        <td>${cls}</td>
        <td class="${g.kittyflap >= REBALANCE_TARGET_PER_SOURCE ? '' : 'export-error'}">${g.kittyflap} / ${REBALANCE_TARGET_PER_SOURCE}</td>
        <td class="${g.reolink_manual >= REBALANCE_TARGET_PER_SOURCE ? '' : 'export-error'}">${g.reolink_manual} / ${REBALANCE_TARGET_PER_SOURCE}</td>
        <td class="${g.kittyflap_no_prey >= REBALANCE_TARGET_PER_SOURCE ? '' : 'export-error'}">${g.kittyflap_no_prey} / ${REBALANCE_TARGET_PER_SOURCE}</td>
        <td class="${g.reolink_manual_no_prey >= REBALANCE_TARGET_PER_SOURCE ? '' : 'export-error'}">${g.reolink_manual_no_prey} / ${REBALANCE_TARGET_PER_SOURCE}</td>
        <td>${g.kittyflap + g.reolink_manual}</td>
      </tr>`).join('');
    table = `
      <table class="stats-table" style="margin-top:8px">
        <thead><tr><th>Classe</th><th>HK gardées</th><th>Reolink+Manuel gardées</th>
          <th>HK sans proie</th><th>Reolink+Manuel sans proie</th><th>Total</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }
  bar.innerHTML = controls + table;
}

async function loadRebalanceQueue() {
  const params = new URLSearchParams({ limit: 10000, rebalance: '1' });
  if (rebalanceFilters.cat) params.set('cat_name', rebalanceFilters.cat);
  if (rebalanceFilters.source) params.set('source', rebalanceFilters.source);
  if (rebalanceFilters.prey) params.set('prey_only', '1');
  if (rebalanceFilters.negative) params.set('negative_only', '1');
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
  if (sub === '') {
    bar.innerHTML = `
      <span>${label}</span>
      <button onclick="selectAllVisible(mrQueue, renderMrQueue)">Tout sélectionner</button>
      <button onclick="clearSelection(renderMrQueue)">Annuler</button>
      <button class="btn-reject" onclick="bulkResolveMr('dismiss')">Garder l'ancien (sélection)</button>
      <button class="btn-confirm" onclick="bulkResolveMr('apply')">Appliquer le nouveau (sélection)</button>`;
  } else if (sub === 'equilibrage') {
    bar.innerHTML = `
      <span>${label}</span>
      <button onclick="selectAllVisible(currentQueue, renderQueue)">Tout sélectionner</button>
      <button onclick="clearSelection(renderQueue)">Annuler</button>
      <button class="btn-reject" onclick="bulkReview('rejected')">Écarter la sélection</button>
      <button class="btn-confirm" onclick="rebalanceKeepSelection()">Garder la sélection</button>`;
  }
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
  await loadRebalanceQueue();
  await loadRebalanceCounts();
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
    // 22.10, Sid ("j'en ai validé 3 de boubou... les trois se sont mises
    // automatiquement dans validé, le validé habituel") - dans Équilibrage,
    // "Valider" doit passer par le même mécanisme que "Garder la
    // sélection" (bulk), pas confirmer directement (ignorerait
    // rebalance_bucket et ferait sortir la photo du lot en cours).
    if (status === 'confirmed') {
      await api('/app-api/rebalance/keep', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: [capture.id] }),
      });
    } else {
      await api(`/app-api/captures/${capture.id}/review?status=${status}`, { method: 'POST' });
    }
    currentQueue.splice(currentIndex, 1);
    renderQueue();
    await loadRebalanceCounts();
    if (currentQueue.length === 0) {
      await loadRebalanceQueue();
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
