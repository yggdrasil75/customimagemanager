/* Barcodes module front-end: the Scan barcodes button (controls panel). */
(function () {
  async function runBarcodes() {
    if (!window.currentFile) { alert("Select an image first."); return; }
    const btn = document.getElementById("btn_barcodes"); const og = btn.innerText;
    btn.innerText = "Scanning..."; btn.disabled = true;
    try {
      const d = await fetch("/api/barcodes", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: window.currentFile }),
      }).then((r) => r.json());
      if (d.success) {
        const regs = d.regions || [];
        if (!regs.length) { showToast(d.note || "No barcodes found."); }
        else {
          // Server already shaped these as regions (type BarCode, payload in
          // barcode_value); push them through unchanged.
          regs.forEach((r) => currentRegions.push(r));
          if (d.summary) {
            const ta = document.getElementById("meta_desc");
            ta.value = (ta.value ? ta.value.trim() + "\n\n" : "") + "Barcodes:\n" + d.summary;
          }
          drawCanvas(); if (typeof popoutOpen !== "undefined" && popoutOpen) drawPopout();
          renderRegionsList(); triggerAutosave();
          const undec = d.detected - d.decoded;
          showToast(`Barcodes: ${d.detected} found, ${d.decoded} decoded`
            + (undec ? ` (${undec} not readable)` : "")
            + (d.detector ? ` | ${d.detector}` : "") + ".");
        }
        if (d.note) console.info("barcodes:", d.note);
      } else alert("Barcode scan failed: " + (d.error || ""));
    } catch (e) { alert("Network error during barcode scan."); }
    btn.innerText = og; btn.disabled = false;
  }
  window.runBarcodes = runBarcodes;

  function buildButtons() {
    if (!window.registerControlButton) return;
    registerControlButton("ai_tools", {label: "Scan barcodes", onclick: "runBarcodes()", id: "btn_barcodes",
      feature: "ai.barcodes", variant: "warn",
      title: "Detect barcodes and QR codes with the configured YOLO model, then decode each one. " +
             "Codes that are found but can't be read are still marked, so you keep a note of where they are."});
  }
  if (document.readyState === "loading") window.addEventListener("DOMContentLoaded", buildButtons);
  else buildButtons();
})();