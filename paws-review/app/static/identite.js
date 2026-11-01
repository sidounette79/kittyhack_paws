// 01.11, Sid ("le but c'est vraiment de la correction d'apprentissage,
// lui dire là tu as mis ça, tu t'es trompé, apprends ça en plus") - Phase
// 5 du plan de scission détection/identité. File des boîtes "Cat" déjà
// résolues par le Modèle 2, triées par score croissant (les moins sûres
// d'abord). Un tap sur le nom déjà prédit = confirmation ; un tap sur un
// autre nom = correction - les deux comptent comme "revu" et sortent de
// la file. Une correction est reprise au prochain entraînement
// (export_identity_training.py préfère identity_corrections au label brut).

const ID_CATS = ['Pookie', 'Patoune', 'Nala', 'Boubou'];

let idQueue = [];

async function init() {
  await loadQueue();
}

async function loadQueue() {
  const res = await api('/app-api/identity/review-queue?limit=150');
  idQueue = res.items;
  renderBar(res.pending_corrections);
  renderGrid();
}

function renderBar(pendingCorrections) {
  const bar = document.getElementById('identity-bar');
  bar.innerHTML = `<span class="export-msg">${idQueue.length} à revoir · ${pendingCorrections} correction(s) en attente du prochain entraînement</span>`;
}

function renderGrid() {
  const grid = document.getElementById('id-grid');
  const empty = document.getElementById('id-empty');
  if (idQueue.length === 0) {
    grid.innerHTML = '';
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  // Clé stable (capture_id/object_index), jamais l'index du tableau -
  // idQueue se vide au fur et à mesure (voir correctIdentity), un index
  // de tableau se décalerait sous les tuiles déjà rendues/cliquables.
  grid.innerHTML = idQueue.map((item) => {
    const key = `${item.capture_id}-${item.object_index}`;
    const scoreClass = item.predicted_score < 70 ? 'id-score-low' : '';
    return `
    <div class="id-tile" id="id-tile-${key}">
      <img src="/app-api/captures/${item.capture_id}/image?size=thumbnail" loading="lazy">
      <div class="id-meta">
        <span style="color:${catColor(item.predicted_name)}">${item.predicted_name}</span>
        <span class="${scoreClass}">${item.predicted_score.toFixed(0)}%</span>
      </div>
      <div class="id-buttons">
        ${ID_CATS.map(cat => `
          <button class="${cat === item.predicted_name ? 'predicted' : ''}"
                  onclick="correctIdentity(${item.capture_id}, ${item.object_index}, '${cat}')">${cat}</button>
        `).join('')}
      </div>
    </div>`;
  }).join('');
}

async function correctIdentity(captureId, objectIndex, correctedName) {
  const key = `${captureId}-${objectIndex}`;
  const item = idQueue.find(q => q.capture_id === captureId && q.object_index === objectIndex);
  const tile = document.getElementById(`id-tile-${key}`);
  if (tile) tile.querySelectorAll('button').forEach(b => b.disabled = true);
  await api(`/app-api/identity/${captureId}/${objectIndex}/correct`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      corrected_name: correctedName,
      predicted_name: item ? item.predicted_name : null,
      predicted_score: item ? item.predicted_score : null,
    }),
  });
  if (tile) {
    tile.querySelector('.id-buttons').innerHTML = `<button class="done" disabled>✓ ${correctedName}</button>`;
  }
  idQueue = idQueue.filter(q => !(q.capture_id === captureId && q.object_index === objectIndex));
}

init();
