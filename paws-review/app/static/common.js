// 26.10, Sid ("l'idée serait d'avoir des vraies pages distinctes... quand je
// rafraîchis ça me remet tout à zéro de mes filtres") - app.js (une seule
// page, tout en mémoire JS) éclaté en 4 vraies pages (photos/entrainement/
// revision/archives.html, chacune son .js), chacune avec ses propres URLs
// de sous-page (ex: /photos/nala, /revision/comparaison) - un rafraîchissement
// ou un lien partagé restaure exactement le même état, plus besoin de l'onglet
// "Toutes" à chaque F5. Ce fichier = tout ce qui était identique partout :
// l'éditeur de boîtes (dessiner/corriger/supprimer), la vue détail générique,
// la sélection multiple, api()/CAT_COLORS. Chargé par chaque page AVANT son
// propre .js.

const CAT_COLORS = {
  Pookie: 'var(--cat-pookie)',
  Patoune: 'var(--cat-patoune)',
  Nala: 'var(--cat-nala)',
  Boubou: 'var(--cat-boubou)',
};
function catColor(name) { return CAT_COLORS[name] || 'var(--cat-unknown)'; }

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  const ct = res.headers.get('content-type') || '';
  return ct.includes('application/json') ? res.json() : res;
}

// 26.10, Sid ("un petit bouton pour que je puisse rester directement la
// comparaison ou autre chose si ça plante en cours de route") - remet un état
// JSON bloqué sur "running" à "idle" côté serveur, pour les 4 barres de
// progression (export/train/model_review/compare). Partagé : chaque page qui
// a sa propre barre de ce genre l'utilise.
async function resetStuckState(which, reloadFn) {
  if (!confirm("Réinitialiser : à utiliser seulement si ça semble bloqué (après un redéploiement, par exemple). Si quelque chose tournait vraiment, sa progression sera perdue. Continuer ?")) return;
  await api(`/app-api/admin/reset-stuck-state/${which}`, { method: 'POST' });
  await reloadFn();
}

// --- Sélection multiple (grille de vignettes) ---------------------------
// 18.10, Sid ("un moyen d'en sélectionner plusieurs d'un coup pour les
// écarter en une fois ?") - Set d'ids, survit à un renderQueue() (un
// changement de page/sous-page le vide explicitement, voir chaque page).
let selectedIds = new Set();

function toggleSelect(id, onChange) {
  if (selectedIds.has(id)) selectedIds.delete(id); else selectedIds.add(id);
  onChange();
}
function selectAllVisible(items, onChange) {
  items.forEach(c => selectedIds.add(c.id));
  onChange();
}
function clearSelection(onChange) {
  selectedIds.clear();
  onChange();
}

// --- Éditeur de boîtes (vue détail) --------------------------------------
// Générique : chaque page fournit sa propre queue + son propre point
// d'entrée (openDetail(queue, index, opts)), mais le rendu de l'image, le
// glisser/déposer des boîtes et le tracé d'une nouvelle boîte sont partagés
// à l'identique partout (Photos, Validées, Équilibrage, Écartées, Archivées).

const KNOWN_LABELS = ['Pookie', 'Patoune', 'Nala', 'Boubou', 'Proie', 'Autre'];
// La brute du modèle peut dire "Prey"/"Maus"/"mouse" (anglais/allemand, selon
// la génération du modèle) - sans ça, aucune option ne matchait "Prey" et le
// <select> retombait silencieusement sur la première option (Pookie),
// donnant l'impression fausse que la boîte était déjà étiquetée "Pookie".
const LABEL_SYNONYMS = { prey: 'Proie', maus: 'Proie', mouse: 'Proie' };
function canonicalLabel(raw) {
  const lower = (raw || '').toLowerCase();
  if (LABEL_SYNONYMS[lower]) return LABEL_SYNONYMS[lower];
  const known = KNOWN_LABELS.find(l => l.toLowerCase() === lower);
  return known || raw;
}

// 18.10, Sid ("j'ai 2x Patoune, je sais pas laquelle je supprime") -
// identifiant visuel partagé entre une boîte sur l'image et sa ligne dans
// la liste en dessous (object_index, stable, jamais réutilisé pour deux
// boîtes différentes d'une même capture).
const INDEX_COLORS = ['#e8a33d', '#5fb87a', '#5a9fd4', '#c77dd4', '#e35d5d', '#4fc4c4'];
function indexColor(i) { return INDEX_COLORS[i % INDEX_COLORS.length]; }

// detailCtx: { captureId } posé par renderDetail() ci-dessous, lu par
// correctBox/discardBox/wireDrawing pour savoir quelle vue redessiner après
// une mutation - chaque page doit fournir son propre re-rendu via
// detailCtx.onChanged (ex: renderDetail() pour Photos).
let detailCtx = null;

function renderBox(wrap, box) {
  const el = document.createElement('div');
  el.className = 'box' + (box.confirmed ? '' : ' unconfirmed') +
    (/prey|proie|maus|mouse/i.test(box.label) ? ' prey' : '');
  el.style.left = box.x + '%';
  el.style.top = box.y + '%';
  el.style.width = box.width + '%';
  el.style.height = box.height + '%';
  const probTxt = box.probability != null ? ` ${box.probability.toFixed(0)}%` : '';
  el.innerHTML = `<div class="box-label">${box.label}${probTxt}</div><div class="box-handle"></div>` +
    `<div class="index-badge" style="background:${indexColor(box.object_index)}">${box.object_index}</div>`;
  wrap.appendChild(el);
  wireBoxResize(wrap, el, box);
}

// 18.10, Sid ("un moyen pour que je puisse reproportionner une boîte
// existante ?") : glisser le corps de la boîte la déplace, glisser la
// poignée (coin bas-droit) la redimensionne.
function wireBoxResize(wrap, el, box) {
  function toPercent(clientX, clientY) {
    const rect = wrap.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(100, ((clientX - rect.left) / rect.width) * 100)),
      y: Math.max(0, Math.min(100, ((clientY - rect.top) / rect.height) * 100)),
    };
  }
  function applyGeom(geom) {
    el.style.left = geom.x + '%'; el.style.top = geom.y + '%';
    el.style.width = geom.w + '%'; el.style.height = geom.h + '%';
  }

  el.onpointerdown = (e) => {
    if (e.target.closest('.box-handle')) return;
    e.stopPropagation();
    e.preventDefault();
    const start = toPercent(e.clientX, e.clientY);
    const orig = { x: box.x, y: box.y, w: box.width, h: box.height };
    let geom = orig;
    el.setPointerCapture(e.pointerId);
    el.onpointermove = (ev) => {
      const p = toPercent(ev.clientX, ev.clientY);
      geom = {
        x: Math.max(0, Math.min(100 - orig.w, orig.x + (p.x - start.x))),
        y: Math.max(0, Math.min(100 - orig.h, orig.y + (p.y - start.y))),
        w: orig.w, h: orig.h,
      };
      applyGeom(geom);
    };
    el.onpointerup = () => {
      el.onpointermove = null; el.onpointerup = null;
      if (geom.x !== orig.x || geom.y !== orig.y) {
        correctBox(box.object_index, box.label, geom.x, geom.y, geom.w, geom.h, box.source);
      }
    };
  };

  const handle = el.querySelector('.box-handle');
  handle.onpointerdown = (e) => {
    e.stopPropagation();
    e.preventDefault();
    const orig = { x: box.x, y: box.y, w: box.width, h: box.height };
    let geom = orig;
    handle.setPointerCapture(e.pointerId);
    handle.onpointermove = (ev) => {
      const p = toPercent(ev.clientX, ev.clientY);
      geom = {
        x: orig.x, y: orig.y,
        w: Math.max(2, Math.min(100 - orig.x, p.x - orig.x)),
        h: Math.max(2, Math.min(100 - orig.y, p.y - orig.y)),
      };
      applyGeom(geom);
    };
    handle.onpointerup = () => {
      handle.onpointermove = null; handle.onpointerup = null;
      if (geom.w !== orig.w || geom.h !== orig.h) {
        correctBox(box.object_index, box.label, geom.x, geom.y, geom.w, geom.h, box.source);
      }
    };
  };
}

function renderBoxList(full) {
  const list = document.getElementById('box-list');
  list.innerHTML = full.boxes.map(box => {
    const canon = canonicalLabel(box.label);
    const options = KNOWN_LABELS.includes(canon) ? KNOWN_LABELS : [canon, ...KNOWN_LABELS];
    return `
    <div class="box-row">
      <div class="index-badge" style="background:${indexColor(box.object_index)}">${box.object_index}</div>
      <select onchange="correctBox(${box.object_index}, this.value, ${box.x}, ${box.y}, ${box.width}, ${box.height}, '${box.source}')">
        ${options.map(l => `<option value="${l}" ${l === canon ? 'selected' : ''}>${l}</option>`).join('')}
      </select>
      <span class="prob">${box.source === 'manual' ? 'ajoutée' : (box.probability != null ? box.probability.toFixed(0) + '%' : '')}</span>
      <button onclick="discardBox(${box.object_index})">supprimer</button>
    </div>
  `;
  }).join('') || '<div class="hint">Aucune boîte détectée - trace un rectangle sur l\'image si quelque chose a été manqué.</div>';
}

// Point d'entrée commun : affiche la vue #detail pour une capture donnée.
// `onChanged` est rappelé après toute mutation de boîte (correction, ajout,
// suppression) pour redessiner - chaque page passe sa propre fonction
// (généralement : se recharger soi-même avec le même id).
async function renderDetail(captureId, metaExtra, onChanged) {
  detailCtx = { captureId, onChanged };
  const full = await api(`/app-api/captures/${captureId}`);
  document.getElementById('detail-meta').textContent =
    `#${full.id} · ${full.created_at} · chat: ${full.cat_name || '?'} (${full.cat_source || '?'}) · proie: ${full.prey_probability ?? '?'}%` +
    (metaExtra ? ` · ${metaExtra}` : '');

  const img = document.getElementById('detail-img');
  img.src = `/app-api/captures/${captureId}/image?size=original`;

  const wrap = document.getElementById('image-wrap');
  wrap.querySelectorAll('.box').forEach(b => b.remove());

  // 18.10, Sid ("j'arrive pas à sélectionner la petite boîte") - une boîte
  // plus grande dessinée après recouvre tout ce qu'elle contient. Dessiner
  // les plus petites en dernier (donc au-dessus dans la pile) : une boîte
  // contenue dans une autre gagne toujours le clic.
  const byAreaDesc = [...full.boxes].sort((a, b) => (b.width * b.height) - (a.width * a.height));
  byAreaDesc.forEach(box => renderBox(wrap, box));
  renderBoxList(full);
  wireDrawing(wrap, full.boxes.length);
  return full;
}

async function correctBox(objectIndex, label, x, y, width, height, source = 'model') {
  await api(`/app-api/captures/${detailCtx.captureId}/boxes`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ object_index: objectIndex, label, x, y, width, height, source }),
  });
  await detailCtx.onChanged();
}

async function discardBox(objectIndex) {
  await api(`/app-api/captures/${detailCtx.captureId}/boxes/${objectIndex}`, { method: 'DELETE' });
  await detailCtx.onChanged();
}

// Petit <select> flottant posé sur la boîte qu'on vient de tracer à la main.
function pickLabel(wrap, boxGeom) {
  return new Promise((resolve) => {
    const sel = document.createElement('select');
    sel.className = 'label-picker';
    sel.style.left = boxGeom.x + '%';
    sel.style.top = boxGeom.y + '%';
    sel.innerHTML =
      `<option value="" disabled selected>Chat ou proie ?</option>` +
      KNOWN_LABELS.map(l => `<option value="${l}">${l}</option>`).join('');
    wrap.appendChild(sel);

    let settled = false;
    function finish(value) {
      if (settled) return;
      settled = true;
      sel.remove();
      resolve(value || null);
    }
    sel.onchange = () => finish(sel.value);
    sel.onblur = () => setTimeout(() => finish(null), 150);

    sel.focus();
    if (sel.showPicker) {
      try { sel.showPicker(); } catch (e) { /* pas supporté partout */ }
    }
  });
}

function wireDrawing(wrap, nextIndex) {
  let startX, startY, tempEl, current;

  function toPercent(clientX, clientY) {
    const rect = wrap.getBoundingClientRect();
    const x = Math.max(0, Math.min(100, ((clientX - rect.left) / rect.width) * 100));
    const y = Math.max(0, Math.min(100, ((clientY - rect.top) / rect.height) * 100));
    return { x, y };
  }

  wrap.onpointerdown = (e) => {
    if (e.target.closest('.box')) return;
    const p = toPercent(e.clientX, e.clientY);
    startX = p.x; startY = p.y;
    current = { x: startX, y: startY, w: 0, h: 0 };
    tempEl = document.createElement('div');
    tempEl.className = 'box drawing';
    wrap.appendChild(tempEl);
    wrap.setPointerCapture(e.pointerId);
  };
  wrap.onpointermove = (e) => {
    if (!tempEl) return;
    const p = toPercent(e.clientX, e.clientY);
    current = {
      x: Math.min(startX, p.x), y: Math.min(startY, p.y),
      w: Math.abs(p.x - startX), h: Math.abs(p.y - startY),
    };
    tempEl.style.left = current.x + '%'; tempEl.style.top = current.y + '%';
    tempEl.style.width = current.w + '%'; tempEl.style.height = current.h + '%';
  };
  wrap.onpointerup = async () => {
    if (!tempEl) return;
    tempEl.remove(); tempEl = null;
    if (!current || current.w < 2 || current.h < 2) return;
    const boxGeom = { x: current.x, y: current.y, w: current.w, h: current.h };
    const label = await pickLabel(wrap, boxGeom);
    if (!label) return;
    await api(`/app-api/captures/${detailCtx.captureId}/boxes`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        object_index: nextIndex, label,
        x: boxGeom.x, y: boxGeom.y, width: boxGeom.w, height: boxGeom.h,
        source: 'manual',
      }),
    });
    await detailCtx.onChanged();
  };
}

function closeDetail() {
  document.getElementById('detail').classList.remove('open');
}
