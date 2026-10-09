/* theme_intermediate.js - activates the Simple theme's shared viewer for the
 * "intermediate" layout: Meta shows the controls pane (Editor +
 * EXIF / IPTC / XMP), no albums strip, Timeline / Albums / People / Music.
 *
 * People in the picture: the shared viewer already lists them in its side
 * panel (#sv_people, one chip per person built from the picture's regions).
 * This layout turns those chips into avatar chips - the face cropped from the
 * thumbnail - and, with the People module on, a click shows every photo of
 * that person (person:<id> search) and a small button changes who it is.
 * The person ids come from window.CIMPeople (the people module); without it
 * the chips keep a plain initial and are not links.
 */
(function () {
  "use strict";
  const LAYOUT = "intermediate";
  let observer = null;

  function sync() {
    if (!window.CIMTheme || !window.CIMSimpleViewer) return;
    if (window.CIMTheme.layout === LAYOUT) {
      CIMSimpleViewer.activate(LAYOUT, { metaMode: "controls", albumsStrip: false,
                                         panes: ["gallery", "albums", "faces", "music"] });
      watchPeople();
      decorate();
    } else CIMSimpleViewer.release(LAYOUT);
  }

  /** @brief Re-decorate whenever the shared viewer re-renders its people chips. */
  function watchPeople() {
    const box = document.getElementById("sv_people");
    if (!box || observer) return;
    observer = new MutationObserver(() => decorate());
    observer.observe(box, { childList: true });
  }

  const regionsOfCurrent = () => (typeof currentRegions !== "undefined" && typeof currentRegionsFile !== "undefined"
    && currentRegionsFile === window.currentFile) ? currentRegions : [];

  /** @brief Avatar + link on each not-yet-decorated chip of #sv_people. */
  function decorate() {
    if (!window.CIMTheme || window.CIMTheme.layout !== LAYOUT) return;
    const box = document.getElementById("sv_people");
    if (!box) return;
    const chips = [...box.querySelectorAll(".sv-person[data-ridx]")].filter(c => !c.dataset.ivDone);
    if (!chips.length) return;
    const fn = window.currentFile, regions = regionsOfCurrent(), P = window.CIMPeople;
    chips.forEach(c => {
      c.dataset.ivDone = "1";
      c.classList.add("iv-person");
      const r = regions[+c.dataset.ridx];
      const av = document.createElement("span");
      av.className = "iv-avatar";
      if (r && P && fn) {
        av.setAttribute("style", P.cropStyle(r, 1.4));
        av.style.backgroundImage = `url('/api/thumb/${encodeURIComponent(fn)}')`;
      } else av.textContent = (c.textContent.trim()[0] || "?").toUpperCase();
      c.insertBefore(av, c.firstChild);
    });
    if (!P || !fn) return;
    P.facesIn(fn).then(faces => {
      if (window.currentFile !== fn) return;
      chips.forEach(c => {
        const i = +c.dataset.ridx, r = regions[i], f = P.matchFace(faces, r);
        if (f && f.cluster_id >= 0) {
          c.dataset.cluster = f.cluster_id;
          c.classList.add("iv-link");
          c.title = `Show every photo of ${(r && r.region_name) || f.name || "this person"}`;
          c.addEventListener("click", () => P.search(f.cluster_id));
        }
        if (P.isFace(r) && !c.querySelector(".iv-change")) {
          const b = document.createElement("button");
          b.type = "button";
          b.className = "iv-change";
          b.dataset.feature = "tab.faces";
          b.setAttribute("data-write-gate", "tab.faces");
          b.title = "Change person";
          b.textContent = "✎";
          b.addEventListener("click", e => { e.stopPropagation(); P.changeRegion(i); });
          c.appendChild(b);
        }
      });
      if (window.CIMFeatures) window.CIMFeatures.apply(box);
    });
  }

  // a face changed person: the shared viewer re-renders its chips (and we decorate them)
  window.addEventListener("cim:people-changed", e => {
    if (window.CIMTheme && window.CIMTheme.layout === LAYOUT && window.CIMSimpleViewer
        && e.detail && e.detail.filename === window.currentFile) window.CIMSimpleViewer.refreshPanels();
  });
  window.addEventListener("cim:theme", sync);
  if (window.CIMTheme && window.CIMTheme.loaded) sync();
})();
