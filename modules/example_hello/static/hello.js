/* Front-end for the "hello" example module.
 *
 * Injected into the page by the module loader because module.py called
 * host.add_asset("hello.js"). It fills the settings pane the module
 * declared via host.add_settings_tab("example_hello", ...). The core
 * renders the tab button and an empty <div data-settings-pane="module_example_hello">;
 * everything inside is up to the module. */
(function () {
  // The core creates a pane element with this id for each module settings tab:
  //   #settings_pane_module_<module_id>
  function paneFor(id) {
    return document.getElementById("settings_pane_module_" + id);
  }

  async function render() {
    const pane = paneFor("example_hello");
    if (!pane) return;
    pane.innerHTML =
      '<p class="text-[11px] text-gray-500 mb-3">This pane is rendered entirely by a ' +
      'pluggable module (modules/example_hello). It calls the route the module ' +
      'registered.</p>' +
      '<button id="hello_ping" class="px-3 py-1.5 rounded bg-indigo-600 text-white text-sm">' +
      'Ping /api/hello</button>' +
      '<pre id="hello_out" class="mt-3 text-[11px] text-gray-300 whitespace-pre-wrap"></pre>';
    pane.querySelector("#hello_ping").addEventListener("click", async () => {
      const out = pane.querySelector("#hello_out");
      out.textContent = "…";
      try {
        const data = await fetch("/api/hello").then((r) => r.json());
        out.textContent = JSON.stringify(data, null, 2);
      } catch (e) {
        out.textContent = "request failed: " + e;
      }
    });
  }

  // Render when the module's settings tab is opened. The core dispatches a
  // 'module-settings-tab' event with the module id when such a tab is shown.
  document.addEventListener("module-settings-tab", (ev) => {
    if (ev.detail === "example_hello") render();
  });
})();
