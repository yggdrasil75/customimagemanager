/* Personal IQA settings pane: status, validation metrics, Retrain. */
(function () {
  let timer = null;

  async function refresh(pane) {
    const s = await fetch("/api/personal_iqa/status").then(r => r.json()).catch(() => null);
    const out = pane.querySelector("#piqa_status");
    if (!s || !s.success) { out.textContent = "status unavailable"; return; }
    const m = s.metrics;
    const f = x => (x == null ? "–" : (+x).toFixed(3));
    out.textContent =
      `user ratings: ${s.ratings} (min ${s.min_ratings})   tier: D=${s.tier.d} depth=${s.tier.depth}\n` +
      (m ? `model: D=${m.d} depth=${m.depth}   last train: ${new Date(m.trained_at * 1000).toLocaleString()}\n` +
           `validation (${m.n_val} imgs): spearman ${f(m.val_spearman)} / base ${f(m.base_spearman)}` +
           `   mse ${f(m.val_mse)} / base ${f(m.base_mse)}\n`
         : "model: not trained yet\n") +
      `provider: ${s.available ? "ENABLED (beats base IQA on validation)" : "disabled"}\n` +
      (s.torch ? "" : "torch not installed\n") + (s.text || "");
    pane.querySelector("#piqa_train").disabled = !!s.busy || !s.torch;
    if (s.busy && !timer) timer = setInterval(() => refresh(pane), 2000);
    if (!s.busy && timer) { clearInterval(timer); timer = null; }
  }

  function render() {
    const pane = document.getElementById("settings_pane_module_personal_iqa");
    if (!pane) return;
    pane.innerHTML =
      '<p class="text-[11px] text-gray-500 mb-3">Learns your taste from your star ratings. ' +
      'Rate images, retrain, then pick "Personal" as the IQA model. Base model, encoder and grow/rebuild ' +
      'are set in the Models tab under the Personal provider.</p>' +
      '<button id="piqa_train" class="px-3 py-1.5 rounded bg-indigo-600 text-white text-sm">Retrain</button>' +
      '<pre id="piqa_status" class="mt-3 text-[11px] text-gray-300 whitespace-pre-wrap"></pre>';
    pane.querySelector("#piqa_train").addEventListener("click", async () => {
      await fetch("/api/personal_iqa/train", { method: "POST" });
      refresh(pane);
    });
    refresh(pane);
  }

  document.addEventListener("module-settings-tab", ev => {
    if (ev.detail === "personal_iqa") render();
  });
})();