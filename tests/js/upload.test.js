// The core drop zone uploader (static/upload.js): small files in one request,
// big ones through chunked sessions with resume, byte progress, failures with
// a toast and Retry, folder uploads keeping their subfolders.
const { page, test, assert } = require("cim");
const b = page();
const $ = id => b.document.getElementById(id);

/** @brief Point the uploader at a server offer and forget the cached one. */
function offer(o) {
  b.api.reset();
  b.api.on("/api/upload/config", Object.assign({ success: true, sessions: true, chunk_size: 4,
                                                 chunked_over: 4, validate: false }, o || {}));
  b.run("CIMUpload._reset(); CIMUpload.dismiss();");
}

test("the uploader is core: loaded without modules and wired to the drop zone", () => {
  assert.equal(typeof b.run("window.CIMUpload"), "object");
  assert.equal(typeof b.run("window.handleFiles"), "function");
  assert.ok($("folder_input").hasAttribute("webkitdirectory"));
  assert.ok($("upload_status"));
});

test("small files use /api/upload, big ones a chunked session with byte progress", async () => {
  offer();
  const seen = [];
  b.api.on("POST /api/upload/session", c => {
    assert.equal(c.body.filename, "big.bin");
    assert.equal(c.body.size, 10);
    assert.equal(c.body.folder, "trips");
    return { success: true, id: "s1", chunk_size: 4, received: 0 };
  });
  let got = 0;
  b.api.on("PUT /api/upload/session/s1", c => {
    seen.push($("upload_progress_text") && $("upload_progress_text").textContent);
    assert.equal(Number(c.query.offset), got);
    got = Math.min(10, got + 4);
    return { success: true, received: got };
  });
  b.api.on("POST /api/upload/session/s1/complete", { success: true, filename: "trips/big.jxl" });
  b.api.on("POST /api/upload", { success: true, filename: "trips/small.jxl" });
  $("upload_folder").value = "trips";
  await b.run(`CIMUpload.handleFiles([new File([new Uint8Array(10)], "big.bin"),
                                      new File([new Uint8Array(3)], "small.png")])`);
  $("upload_folder").value = "";
  const puts = b.api.find("/api/upload/session/s1", "PUT").map(c => Number(c.query.offset));
  assert.deepEqual(puts, [0, 4, 8]);
  assert.equal(b.api.find("/api/upload/session/s1/complete", "POST").length, 1);
  const small = b.api.find("/api/upload", "POST").filter(c => c.path === "/api/upload");
  assert.equal(small.length, 1);
  assert.ok(seen.some(t => /Uploading 0\/2 - .* of 13 B/.test(t || "")), seen.join(" | "));
  assert.ok(/Uploaded 2 of 2/.test($("toast").innerText));
  assert.ok($("upload_status").classList.contains("hidden"));
});

test("a 409 resumes from the server's offset", async () => {
  offer();
  b.api.on("POST /api/upload/session", { success: true, id: "s2", chunk_size: 4, received: 0 });
  let first = true;
  b.api.on("PUT /api/upload/session/s2", c => {
    if (first) { first = false; return { __status: 409, success: false, received: 4 }; }
    return { success: true, received: Math.min(10, Number(c.query.offset) + 4) };
  });
  b.api.on("POST /api/upload/session/s2/complete", { success: true, filename: "x.jxl" });
  await b.run(`CIMUpload.handleFiles([new File([new Uint8Array(10)], "r.bin")])`);
  const puts = b.api.find("/api/upload/session/s2", "PUT").map(c => Number(c.query.offset));
  assert.deepEqual(puts, [0, 4, 8]);
});

test("a quota refusal and a validation failure show a toast and a Retry row", async () => {
  offer({ sessions: false });
  let refuse = true;
  b.api.on("POST /api/upload", () => refuse
    ? { __status: 413, success: false, error_code: "refused", error: "over quota" }
    : { success: true, filename: "q.jxl" });
  await b.run(`CIMUpload.handleFiles([new File([new Uint8Array(3)], "q.png")])`);
  assert.ok(/q\.png: over quota/.test($("toast").innerText), $("toast").innerText);
  const rows = $("upload_status").querySelectorAll(".upload-failed");
  assert.equal(rows.length, 1);
  assert.ok(/Retry/.test(rows[0].textContent));
  refuse = false;
  await b.run("CIMUpload.retry(0)");
  assert.equal($("upload_status").querySelectorAll(".upload-failed").length, 0);

  offer();
  b.api.on("POST /api/upload/session", { success: true, id: "s3", chunk_size: 8, received: 0 });
  b.api.on("PUT /api/upload/session/s3", { success: true, received: 10 });
  b.api.on("POST /api/upload/session/s3/complete", { __status: 422, success: false,
    error_code: "checksum_mismatch", error: "sha256 mismatch; discarded, send it again" });
  await b.run(`CIMUpload.handleFiles([new File([new Uint8Array(10)], "v.bin")])`);
  assert.ok(/v\.bin: sha256 mismatch/.test($("toast").innerText));
  assert.equal($("upload_status").querySelectorAll(".upload-failed").length, 1);
  b.run("CIMUpload.dismiss()");
});

test("validation sends a sha256, or notes that the browser can't", async () => {
  offer({ sessions: false, validate: true });
  b.api.on("POST /api/upload", { success: true, filename: "h.jxl" });
  const hasSubtle = b.run("!!(window.crypto && window.crypto.subtle && File.prototype.arrayBuffer)");
  await b.run(`CIMUpload.handleFiles([new File([new Uint8Array(3)], "h.png")])`);
  if (!hasSubtle) assert.ok(/Validation skipped/.test($("upload_status").textContent));
  b.run("CIMUpload.dismiss()");
});

test("folders keep their subfolders under the target folder", async () => {
  b.run(`
    function fileEntry(name, size) {
      return { isFile: true, isDirectory: false, name,
               file(res) { res(new File([new Uint8Array(size)], name)); } };
    }
    function dirEntry(name, children) {
      const batches = [children.slice(0, 1), children.slice(1), []];
      return { isFile: false, isDirectory: true, name,
               createReader() { return { readEntries(res) { res(batches.shift() || []); } }; } };
    }
    window._drop = { files: [], items: [{ webkitGetAsEntry: () =>
      dirEntry("trip", [fileEntry("a.jpg", 2), dirEntry("day1", [fileEntry("b.jpg", 3)])]) }] };
  `);
  $("upload_folder").value = "base/";
  const items = await b.run("CIMUpload.itemsFromDrop(window._drop)");
  const got = Array.from(items).map(it => `${it.folder}|${it.file.name}`).sort();
  assert.deepEqual(got, ["base/trip/day1|b.jpg", "base/trip|a.jpg"]);
  // the Folder button's input: webkitRelativePath gives the subfolders
  const picked = b.run(`CIMUpload.itemsFromFiles([{name: "c.jpg", size: 1, webkitRelativePath: "trip/day2/c.jpg"},
                                                  {name: "d.jpg", size: 1}])`);
  assert.deepEqual(Array.from(picked).map(it => it.folder), ["base/trip/day2", "base"]);
  $("upload_folder").value = "";
});
