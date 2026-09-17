// Person editor (controls pane "Person" tab): bio / body fields, appearances,
// relationships, tag suggestions, and the T-pose / mesh estimate buttons.
// ── Person editor: unified body/bio fields + T-pose/mesh estimation ──────────
// Bio fields that render as a specific input type; everything else is short text.
const _DATE_FIELDS = ['birthday', 'death_date'];
const _MULTILINE_FIELDS = ['notes'];
const _CHOICE_FIELDS = { gender: ['', 'male', 'female'] };
let _peopleDirectory = [];   // {uuid,name,cluster_id}, loaded once for typeahead

async function _loadDirectory() {
  if (_peopleDirectory.length) return _peopleDirectory;
  const d = await (await fetch('/api/persons/directory')).json();
  _peopleDirectory = d.people || [];
  return _peopleDirectory;
}

// Which cluster's editor is currently shown in the right-pane Person tab.
let _openPersonCid = null;

// Open a person: take over the centre pane with their body mesh (+ appearance
// scrub bar) and fill the right-pane "Person" tab with the editor. Clicking the
// same person again while it's open closes back to the image view.
async function openPerson(cid) {
  if (_openPersonCid === cid && typeof mediaMode !== 'undefined' && mediaMode === 'person') {
    if (typeof setMediaMode === 'function') setMediaMode('image');
    _openPersonCid = null;
    return;
  }
  const body = document.getElementById('person_editor_body');
  if (body) body.innerHTML = '<div class="text-xs text-gray-500">Loading…</div>';
  const [d] = await Promise.all([
    (await fetch('/api/persons/' + cid)).json(), _loadDirectory()]);
  if (!d.success) {
    if (body) body.innerHTML = '<div class="text-xs text-red-400">' + (d.error || 'Failed.') + '</div>';
    return;
  }
  _openPersonCid = cid;
  // Centre pane: 3D mesh + scrub bar. This also switches the right pane to the
  // Person tab via setMediaMode('person').
  if (window.personView) window.personView.open(cid, d.person);
  // Right pane: the editor.
  _renderPersonEditor(cid, d);
}

// Build the person editor markup into the right-pane Person tab body.
function _renderPersonEditor(cid, d) {
  const el = document.getElementById('person_editor_body');
  if (!el) return;
  const p = d.person;
  const esc = v => (v || '').replace(/"/g, '&quot;');

  // Typed person-level bio field.
  const bioField = k => {
    const label = `<span class="text-[10px] text-gray-400">${k.replace(/_/g, ' ')}</span>`;
    if (_DATE_FIELDS.includes(k))
      return `<label class="flex flex-col gap-0.5">${label}
        <input type="date" value="${esc(p.bio[k])}"
               onchange="savePersonField(${cid},'bio','${k}',this.value,null)"
               class="p-1 bg-gray-700 rounded border border-gray-600 text-xs text-white"></label>`;
    if (_MULTILINE_FIELDS.includes(k))
      return `<label class="flex flex-col gap-0.5 col-span-2">${label}
        <textarea rows="3" onchange="savePersonField(${cid},'bio','${k}',this.value,null)"
               class="p-1 bg-gray-700 rounded border border-gray-600 text-xs text-white">${esc(p.bio[k])}</textarea></label>`;
    if (_CHOICE_FIELDS[k]) {
      const cur = p.bio[k] || '';
      const opts = _CHOICE_FIELDS[k].map(o =>
        `<option value="${o}"${o === cur ? ' selected' : ''}>${o || '—'}</option>`).join('');
      return `<label class="flex flex-col gap-0.5">${label}
        <select onchange="savePersonField(${cid},'bio','${k}',this.value,null)"
                class="p-1 bg-gray-700 rounded border border-gray-600 text-xs text-white">${opts}</select></label>`;
    }
    return `<label class="flex flex-col gap-0.5">${label}
      <input value="${esc(p.bio[k])}"
             onchange="savePersonField(${cid},'bio','${k}',this.value,null)"
             class="p-1 bg-gray-700 rounded border border-gray-600 text-xs text-white"></label>`;
  };
  const bioRows = d.bio_fields.map(bioField).join('');

  // Hold each list (aliases, tags) in memory so chip add/remove mutate state
  // directly, then persist the whole list — the same pattern relationships use.
  _personLists[cid] = {};
  (d.list_fields || []).forEach(k => { _personLists[cid][k] = (p.lists[k] || []).slice(); });

  // List fields (aliases, tags) as chip editors that match the gallery Tags box:
  // one chip per entry with an inline-editable name and an × to remove, plus an
  // adder that splits on comma/Enter. tags additionally gets a suggestions panel.
  const listRows = (d.list_fields || []).map(k => {
    const suggest = (k === 'tags')
      ? `<div id="person_tagsuggest_${cid}" class="mt-1"></div>` : '';
    return `<div class="col-span-2">
        <span class="text-[10px] text-gray-400">${k}</span>
        <div id="person_list_${cid}_${k}" class="mt-0.5"></div>
        <input placeholder="+ add ${k} (comma to add several)"
               onkeydown="if(event.key==='Enter'){event.preventDefault();addPersonListItems(${cid},'${k}',this.value);this.value='';}"
               onblur="if(this.value.trim()){addPersonListItems(${cid},'${k}',this.value);this.value='';}"
               class="mt-0.5 w-full p-1 bg-gray-800 rounded border border-gray-600 text-xs text-white">
        ${suggest}
      </div>`;
  }).join('');

  // Hold this person's relationships in memory so add/remove mutate state
  // directly instead of scraping it back off the DOM.
  _relState[cid] = p.relationships || {};
  const singles = new Set(d.single_relations || []);
  const relTree = (d.relation_lines || []).map(line =>
    _renderRelationLine(cid, line, _relState[cid][line] || [], singles.has(line))).join('');

  const flagBanner = (d.date_flags && d.date_flags.length)
    ? `<div class="mt-2 p-1.5 bg-amber-900/40 border border-amber-700 rounded text-[10px] text-amber-200">
         ⚠ ${d.date_flags.length} photo(s) have a date that disagrees with their look —
         likely a scan date. Review before trusting; nothing was changed automatically.
       </div>` : '';

  const eras = (p.appearances || []).map(a => {
    const bodyRows = d.body_fields.map(k =>
      `<label class="flex flex-col gap-0.5">
         <span class="text-[10px] text-gray-400">${k.replace(/_/g, ' ')}</span>
         <input value="${esc(a.body[k])}"
                onchange="savePersonField(${cid},'body','${k}','${a.id}')"
                class="p-1 bg-gray-700 rounded border border-gray-600 text-xs text-white"></label>`).join('');
    return `<div class="mt-2 pt-2 border-t border-gray-700">
        <div class="text-[11px] text-blue-300 font-bold mb-1">${a.label || a.id}
          <span class="text-gray-500 font-normal">· ${a.rel_paths.length} photo(s)</span></div>
        <div class="grid grid-cols-2 gap-1.5">${bodyRows}</div>
        <div class="flex items-center gap-2 mt-2">
          <button onclick="estimatePose(${cid},'${a.id}')"
            class="text-xs bg-teal-700 hover:bg-teal-600 px-2 py-1 rounded font-bold"
            title="Fuses this appearance's pose skeletons into one canonical T-pose. Needs the pose stage to have run and at least 2 full-torso views (both shoulders + hips visible).">
            ${a.has_tpose ? 'Re-estimate T-pose' : 'Estimate T-pose'}</button>
          <button onclick="estimateMesh(${cid},'${a.id}')" ${d.mesh_estimator ? '' : 'disabled'}
            class="text-xs bg-teal-700 hover:bg-teal-600 disabled:opacity-40 px-2 py-1 rounded font-bold"
            title="${d.mesh_estimator ? '' : 'shape estimator not installed'}">
            ${a.has_mesh ? 'Re-estimate mesh' : 'Estimate mesh'}</button>
          <button onclick="estimateFaceMesh(${cid},'${a.id}')"
            class="text-xs bg-indigo-700 hover:bg-indigo-600 px-2 py-1 rounded font-bold"
            title="${d.face_estimator ? ('3D face via ' + (d.face_estimator_name || 'estimator')) : 'Fits a sparse 3D face from this appearance photos using landmarks already produced by the face model. No extra download.'}">
            ${a.has_face_mesh ? 'Re-estimate face' : 'Estimate face'}</button>
          <span id="person_status_${cid}_${a.id}" class="text-[10px] text-gray-400"></span>
        </div>
      </div>`;
  }).join('');

  el.innerHTML = `
    <div class="grid grid-cols-2 gap-1.5">${bioRows}${listRows}</div>
    <div class="mt-2 pt-2 border-t border-gray-700">
      <div class="text-[11px] text-blue-300 font-bold mb-1">Relationships</div>
      <datalist id="peopledir_${cid}">
        ${_peopleDirectory.map(pp => `<option value="${esc(pp.name)}">`).join('')}
      </datalist>
      ${relTree}
    </div>
    ${flagBanner}
    ${eras || '<div class="text-[10px] text-gray-500 mt-2">No appearances yet.</div>'}`;

  // Paint the chip lists now the containers exist, then load tag suggestions.
  (d.list_fields || []).forEach(k => _renderPersonList(cid, k));
  _loadTagSuggestions(cid);
}

// In-memory list fields (aliases, tags) per open person.
let _personLists = {};
// Chosen threshold per person for the tag-suggestions panel; persisted only in
// memory for the session. {mode:'count'|'frac', value:number}.
let _tagSuggestState = {};

// Render one list field as gallery-style tag chips (blue dot, inline-edit, ×).
function _renderPersonList(cid, key) {
  const box = document.getElementById('person_list_' + cid + '_' + key);
  if (!box) return;
  const items = (_personLists[cid] && _personLists[cid][key]) || [];
  const esc = v => (v || '').replace(/"/g, '&quot;');
  if (!items.length) {
    box.innerHTML = `<div class="text-[11px] text-gray-600 italic px-1 py-0.5">No ${key}</div>`;
    return;
  }
  box.innerHTML = items.map((t, i) =>
    `<div class="rrow tag-row flex items-center gap-1">
       <span class="inline-block w-2 h-2 rounded-full flex-shrink-0" style="background:#3B82F6"></span>
       <input class="tag-edit flex-1 min-w-0 bg-transparent border-b border-transparent focus:border-gray-500 focus:outline-none"
              value="${esc(t)}" onchange="renamePersonListItem(${cid},'${key}',${i},this.value)">
       <span class="tag-x flex-shrink-0" onclick="removePersonListItem(${cid},'${key}',${i})" title="Remove">✕</span>
     </div>`).join('');
}

async function _savePersonList(cid, key) {
  const value = (_personLists[cid][key] || []).slice();
  await fetch('/api/persons/' + cid + '/field', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ section: 'list', key, value })
  });
  _renderPersonList(cid, key);
  if (key === 'tags') _paintTagSuggestions(cid);   // grey out newly-added ones
}

// Add one or more items (split on comma), case-insensitive dedupe, then persist.
function addPersonListItems(cid, key, raw) {
  const cur = _personLists[cid][key] || (_personLists[cid][key] = []);
  const have = new Set(cur.map(t => t.toLowerCase()));
  let changed = false;
  (raw || '').split(',').map(s => s.trim()).filter(Boolean).forEach(name => {
    if (!have.has(name.toLowerCase())) { cur.push(name); have.add(name.toLowerCase()); changed = true; }
  });
  if (changed) _savePersonList(cid, key);
}

function renamePersonListItem(cid, key, i, name) {
  const cur = _personLists[cid][key] || [];
  name = (name || '').trim();
  if (i < 0 || i >= cur.length) return;
  if (!name) { cur.splice(i, 1); _savePersonList(cid, key); return; }
  // Merge on collision with a different entry.
  const other = cur.findIndex((t, j) => j !== i && t.toLowerCase() === name.toLowerCase());
  if (other >= 0) cur.splice(i, 1);
  else cur[i] = name;
  _savePersonList(cid, key);
}

function removePersonListItem(cid, key, i) {
  const cur = _personLists[cid][key] || [];
  cur.splice(i, 1);
  _savePersonList(cid, key);
}

// ── Tag suggestions ─────────────────────────────────────────────────────────
// Suggestions come from the tags on every image this person appears in. The
// server returns each tag with its occurrence count and total tagged images;
// the threshold (an absolute count, or a fraction of those images) is applied
// here so moving the slider is instant and needs no round-trip.
async function _loadTagSuggestions(cid) {
  const wrap = document.getElementById('person_tagsuggest_' + cid);
  if (!wrap) return;
  wrap.innerHTML = '<div class="text-[10px] text-gray-500">Loading tag suggestions…</div>';
  let d;
  try { d = await (await fetch('/api/persons/' + cid + '/tag_suggestions')).json(); }
  catch (e) { wrap.innerHTML = ''; return; }
  if (!d.success) { wrap.innerHTML = ''; return; }
  _tagSuggestData = _tagSuggestData || {};
  _tagSuggestData[cid] = d;
  if (!_tagSuggestState[cid]) _tagSuggestState[cid] = { mode: 'count', value: 2 };
  _paintTagSuggestions(cid);
}
let _tagSuggestData = {};

function _suggestPasses(s, st, imageTotal) {
  if (st.mode === 'frac') return imageTotal > 0 && (s.count / imageTotal) >= st.value;
  return s.count >= st.value;
}

function _paintTagSuggestions(cid) {
  const wrap = document.getElementById('person_tagsuggest_' + cid);
  const d = _tagSuggestData[cid];
  if (!wrap || !d) return;
  const st = _tagSuggestState[cid];
  const all = d.suggestions || [];
  const imgTotal = d.image_total || 0;
  // Recompute "present" live against in-memory tags so chips grey out on add.
  const have = new Set((_personLists[cid] && _personLists[cid].tags || []).map(t => t.toLowerCase()));
  const maxCount = all.reduce((m, s) => Math.max(m, s.count), 0);

  if (!all.length) {
    wrap.innerHTML = `<div class="text-[10px] text-gray-600 italic">No tags on this person's images yet.</div>`;
    return;
  }

  const passing = all.filter(s => _suggestPasses(s, st, imgTotal));
  const addable = passing.filter(s => !have.has(s.tag.toLowerCase()));

  const esc = v => (v || '').replace(/"/g, '&quot;').replace(/</g, '&lt;');
  const chip = s => {
    const present = have.has(s.tag.toLowerCase());
    return `<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] ${present ? 'bg-gray-800 text-gray-500' : 'bg-gray-700 text-gray-200 cursor-pointer hover:bg-blue-700'}"
        ${present ? 'title="already added"' : `onclick="addPersonListItems(${cid},'tags','${esc(s.tag)}')" title="click to add"`}>
        ${present ? '' : '<span class="text-blue-300">+</span>'}${esc(s.tag)}
        <span class="text-gray-500">${s.count}</span></span>`;
  };

  // Slider: count mode steps 1..max; fraction mode 0..1. A toggle switches which.
  const sliderMax = st.mode === 'frac' ? 100 : Math.max(1, maxCount);
  const sliderVal = st.mode === 'frac' ? Math.round(st.value * 100) : st.value;
  const thLabel = st.mode === 'frac'
    ? `in ≥ ${Math.round(st.value * 100)}% of images`
    : `in ≥ ${st.value} image(s)`;

  wrap.innerHTML = `
    <div class="mt-1 pt-1 border-t border-gray-700">
      <div class="flex items-center justify-between mb-1">
        <span class="text-[10px] text-gray-400">Suggested from ${imgTotal} tagged image(s)</span>
        <button onclick="toggleTagSuggestMode(${cid})"
          class="text-[9px] px-1 py-0.5 bg-gray-700 hover:bg-gray-600 rounded">
          ${st.mode === 'frac' ? '% mode' : 'count mode'}</button>
      </div>
      <div class="flex items-center gap-2 mb-1">
        <input type="range" min="${st.mode === 'frac' ? 1 : 1}" max="${sliderMax}" value="${sliderVal}"
          oninput="setTagSuggestThreshold(${cid}, this.value)"
          class="flex-1 accent-blue-500">
        <span class="text-[10px] text-gray-400 w-28 text-right">${thLabel}</span>
      </div>
      <div class="flex flex-wrap gap-1">${passing.map(chip).join('') || '<span class="text-[10px] text-gray-600 italic">none above threshold</span>'}</div>
      ${addable.length ? `<button onclick="addAllSuggestedTags(${cid})"
          class="mt-1 text-[10px] px-2 py-0.5 bg-teal-700 hover:bg-teal-600 rounded font-bold">
          + Add all ${addable.length} above threshold</button>` : ''}
    </div>`;
}

function setTagSuggestThreshold(cid, v) {
  const st = _tagSuggestState[cid];
  v = parseInt(v, 10) || 0;
  st.value = st.mode === 'frac' ? (v / 100) : v;
  _paintTagSuggestions(cid);
}

function toggleTagSuggestMode(cid) {
  const st = _tagSuggestState[cid];
  st.mode = st.mode === 'frac' ? 'count' : 'frac';
  st.value = st.mode === 'frac' ? 0.5 : 2;   // sensible default per mode
  _paintTagSuggestions(cid);
}

function addAllSuggestedTags(cid) {
  const d = _tagSuggestData[cid], st = _tagSuggestState[cid];
  if (!d) return;
  const have = new Set((_personLists[cid].tags || []).map(t => t.toLowerCase()));
  const toAdd = (d.suggestions || [])
    .filter(s => _suggestPasses(s, st, d.image_total || 0))
    .filter(s => !have.has(s.tag.toLowerCase()))
    .map(s => s.tag);
  if (toAdd.length) addPersonListItems(cid, 'tags', toAdd.join(','));
}

// In-memory relationships per open person, so add/remove mutate state directly.
let _relState = {};

// One relationship line. Single lines (mother/father/spouse) show a single slot:
// a filled chip that can only be cleared, or one adder. Multi lines (siblings,
// children, ex-spouses, step-family) show all chips plus an always-present adder.
function _renderRelationLine(cid, line, edges, single) {
  const label = line.replace(/_/g, ' ');
  const chip = (e, i) =>
    `<span class="inline-flex items-center gap-1 px-1.5 py-0.5 bg-gray-700 rounded text-[10px]">
       ${e.uuid ? '' : '<span class="text-gray-500" title="external, no photos">◇</span>'}
       ${(e.name || '?').replace(/</g, '&lt;')}
       <button onclick="removeRelation(${cid},'${line}',${i})"
               class="text-gray-500 hover:text-red-400">×</button>
     </span>`;
  const adder =
    `<input list="peopledir_${cid}" placeholder="+ add"
            onkeydown="if(event.key==='Enter'){addRelation(${cid},'${line}',this.value);this.value='';}"
            class="px-1 py-0.5 bg-gray-800 rounded border border-gray-600 text-[10px] text-white w-24">`;
  // A single line shows its one chip OR the adder; multi shows all chips AND the adder.
  const body = single
    ? (edges.length ? chip(edges[0], 0) : adder)
    : edges.map(chip).join('') + adder;
  return `<div class="mb-1.5">
      <div class="flex items-center gap-1 flex-wrap">
        <span class="text-[10px] text-gray-400 w-20">${label}</span>${body}
      </div>
    </div>`;
}

// Add an edge: match the typed name to a known person, else store as external.
async function addRelation(cid, line, name) {
  name = (name || '').trim();
  if (!name) return;
  const match = _peopleDirectory.find(p => p.name.toLowerCase() === name.toLowerCase());
  const edge = { uuid: match ? match.uuid : null, name: match ? match.name : name };
  const edges = (_relState[cid][line] || []).slice();
  if (!edges.some(e => e.name.toLowerCase() === edge.name.toLowerCase())) edges.push(edge);
  await _saveRelation(cid, line, edges);
}

async function removeRelation(cid, line, idx) {
  const edges = (_relState[cid][line] || []).slice();
  edges.splice(idx, 1);
  await _saveRelation(cid, line, edges);
}

async function _saveRelation(cid, line, edges) {
  _relState[cid][line] = edges;
  await fetch('/api/persons/' + cid + '/relationship', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ line, edges })
  });
  // Repaint chips and reflect any reciprocal edges written server-side by
  // reloading the record into the right-pane editor (no centre-pane reset).
  const dd = await (await fetch('/api/persons/' + cid)).json();
  if (dd.success) _renderPersonEditor(cid, dd);
}

async function saveListField(cid, key, raw) {
  const value = raw.split(',').map(s => s.trim()).filter(Boolean);
  await fetch('/api/persons/' + cid + '/field', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ section: 'list', key, value })
  });
}

async function savePersonField(cid, section, key, value, appearance_id) {
  if (section === 'body') { appearance_id = value; value = event.target.value; }
  await fetch('/api/persons/' + cid + '/field', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ section, key, value, appearance_id })
  });
}

async function _personTask(cid, appearanceId, path, label) {
  const s = document.getElementById('person_status_' + cid + '_' + appearanceId);
  if (s) s.textContent = label + '…';
  const d = await (await fetch('/api/persons/' + cid + path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ appearance_id: appearanceId })
  })).json();
  if (s) {
    if (d.success) {
      s.textContent = label + ' done.';
      s.className = 'text-[10px] text-green-400';
      s.title = '';
    } else {
      const why = d.reason || (label + ' unavailable.');
      s.textContent = why;
      s.className = 'text-[10px] text-amber-400';
      s.title = why;
    }
  }
  // Re-pull the record so the mesh viewer + editor reflect the new tpose/mesh.
  if (d.success) {
    const dd = await (await fetch('/api/persons/' + cid)).json();
    if (dd.success) {
      if (window.personView) window.personView.open(cid, dd.person);
      _renderPersonEditor(cid, dd);
    }
  }
}
const estimatePose = (cid, aid) => _personTask(cid, aid, '/tpose', 'T-pose');
const estimateMesh = (cid, aid) => _personTask(cid, aid, '/mesh', 'Mesh');
// After estimating a face mesh, flip the viewer to Face mode so the result shows
// without the user having to hit the toggle.
async function estimateFaceMesh(cid, aid) {
  const s = document.getElementById('person_status_' + cid + '_' + aid);
  if (s) { s.className = 'text-[10px] text-gray-400'; s.textContent = 'Face mesh…'; }
  await _personTask(cid, aid, '/face_mesh', 'Face mesh');
  if (window.personView && window.personView.setView) window.personView.setView('face');
}