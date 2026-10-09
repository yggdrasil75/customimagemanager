// Settings -> Media (modules/encoding/static/media_settings.js) against the real /api/encoding/schema
// snapshot: the Simple / Expert switch, fields hidden when they don't apply to the
// chosen output, the thumbnail-size warning, the simple slider and the save payload.
const { page, test, assert } = require("cim");

async function open() {
  const b = page({ modules: ["encoding"] });
  assert.deepEqual(b.errors, []);
  b.api.on("POST /api/encoding/simple", c => ({ success: true,
    values: { enc_image_mode: "lossy", enc_image_quality: c.body.quality, enc_jxl_distance: 0 } }));
  await b.tick(20);
  b.run("window._mediaLoaded = false");
  await b.run("loadMediaSettings()");
  await b.tick(10);
  // start every test from the simple view with images converted and videos converted
  b.run("CIMMediaSettings.view = 'simple'; CIMMediaSettings.refresh()");
  return b;
}
const keys = b => b.val("CIMMediaSettings.visibleKeys()");
const row = (b, key) => b.document.querySelector(`#media_settings_root .media-field[data-key="${key}"]`);
const shown = (b, key) => { const r = row(b, key); return !!r && !r.classList.contains("hidden"); };
const set = (b, key, v) => b.run(`CIMMediaSettings.setValue(${JSON.stringify(key)}, ${JSON.stringify(v)})`);

test("simple view shows output formats, one quality slider and thumbnails", async () => {
  const b = await open();
  set(b, "media_storage.image.mode", "all");
  set(b, "media_storage.image.target", ".jxl");
  const k = keys(b);
  for (const want of ["media_storage.image.mode", "media_storage.image.target", "enc_anim_target", "thumb_size"])
    assert.ok(k.includes(want), want);
  for (const hidden of ["enc_image_effort", "enc_jpeg_transcode", "thumb_quality", "thumb_format", "enc_video_preset"])
    assert.ok(!k.includes(hidden), hidden);
  assert.ok(shown(b, "thumb_size") && !shown(b, "enc_image_effort"));
  const slider = b.document.querySelector('.media-simple[data-group="image"]');
  assert.ok(slider && !slider.classList.contains("hidden"), "image quality slider in the simple view");
  // every field carries a hover hint
  for (const r of b.document.querySelectorAll("#media_settings_root .media-field")) assert.ok(r.title, r.dataset.key);
});

test("expert toggle reveals every option and is remembered per user", async () => {
  const b = await open();
  set(b, "media_storage.image.mode", "all");
  set(b, "media_storage.image.target", ".jxl");
  b.document.querySelector('.media-view-btn[data-view="expert"]').click();
  await b.tick(5);
  assert.equal(b.run("CIMMediaSettings.view"), "expert");
  assert.ok(shown(b, "enc_image_effort") && shown(b, "thumb_quality") && shown(b, "thumb_format"));
  assert.ok(b.document.querySelector('.media-simple[data-group="image"]').classList.contains("hidden"));
  const call = b.api.last("/api/user/settings", "POST");
  assert.deepEqual(JSON.parse(JSON.stringify(call.body)), { media_settings_view: "expert" });
  b.document.querySelector('.media-view-btn[data-view="simple"]').click();
  assert.ok(!shown(b, "enc_image_effort"));
});

test("fields follow the chosen output type", async () => {
  const b = await open();
  b.run("CIMMediaSettings.view = 'expert'");
  set(b, "media_storage.image.mode", "all");
  set(b, "media_storage.image.target", ".png");
  assert.ok(shown(b, "enc_png_level"));
  for (const k of ["enc_image_quality", "enc_image_mode", "enc_image_effort", "enc_jxl_distance", "enc_webp_method"])
    assert.ok(!shown(b, k), k);
  set(b, "media_storage.image.target", ".jxl");
  set(b, "enc_image_mode", "lossless");
  assert.ok(shown(b, "enc_jpeg_transcode") && !shown(b, "enc_jxl_distance"));   // no distance when lossless
  set(b, "enc_image_mode", "lossy");
  assert.ok(shown(b, "enc_jxl_distance") && !shown(b, "enc_jpeg_transcode"));
  set(b, "media_storage.image.mode", "none");
  assert.ok(!shown(b, "media_storage.image.target") && !shown(b, "enc_image_metadata"));

  // video: remux (copy) hides the encoder knobs and says so
  set(b, "media_storage.video.mode", "all");
  set(b, "media_storage.video.target", ".mp4");
  set(b, "enc_video_codec", "h264");
  assert.ok(shown(b, "enc_video_crf") && shown(b, "enc_video_preset") && shown(b, "enc_video_faststart"));
  set(b, "enc_video_codec", "copy");
  for (const k of ["enc_video_crf", "enc_video_preset", "enc_video_hw", "enc_video_bits", "enc_video_rc"])
    assert.ok(!shown(b, k), k);
  assert.ok(shown(b, "enc_video_audio") && shown(b, "enc_video_subs"));
  assert.ok(!row(b, "enc_video_codec").querySelector(".media-remux-note").classList.contains("hidden"));
  // WebM: no H.264 / AAC choices, no fast start; a codec it can't hold is replaced
  set(b, "enc_video_codec", "h264");
  set(b, "media_storage.video.target", ".webm");
  const codecs = [...row(b, "enc_video_codec").querySelectorAll("option")].map(o => o.value);
  assert.ok(!codecs.includes("h264") && !codecs.includes("h265") && codecs.includes("copy"), codecs.join());
  assert.notEqual(b.run("CIMMediaSettings.values.enc_video_codec"), "h264");
  const audio = [...row(b, "enc_video_audio").querySelectorAll("option")].map(o => o.value);
  assert.ok(!audio.includes("aac"), audio.join());
  assert.ok(!shown(b, "enc_video_faststart"));
  set(b, "enc_video_audio", "none");
  assert.ok(!shown(b, "enc_video_audio_bitrate"));
});

test("thumbnail size warns above 512 px", async () => {
  const b = await open();
  const warn = () => !row(b, "thumb_size").querySelector(".media-warn").classList.contains("hidden");
  set(b, "thumb_size", 256);
  assert.ok(!warn());
  set(b, "thumb_size", 768);
  assert.ok(warn());
  assert.match(row(b, "thumb_size").querySelector(".media-warn").textContent, /more disk and memory/);
});

test("simple slider maps through the server and the save payload is complete", async () => {
  const b = await open();
  set(b, "media_storage.image.mode", "all");
  const r = b.document.querySelector('.media-simple[data-group="image"] input');
  r.value = "70";
  r.dispatchEvent(new b.window.Event("change"));
  await b.tick(10);
  const call = b.api.last("/api/encoding/simple", "POST");
  assert.equal(call.body.group, "image");
  assert.equal(call.body.quality, 70);
  assert.equal(b.run("CIMMediaSettings.values.enc_image_quality"), 70);
  set(b, "thumb_size", 384);
  set(b, "keep_raws", true);
  const p = b.val("CIMMediaSettings.payload()");
  assert.equal(p.body.enc_image_mode, "lossy");
  assert.equal(p.body.thumb_size, 384);
  assert.equal(p.keepRaws, true);
  assert.equal(p.body.media_storage.image.mode, "all");
  assert.ok(p.body.media_storage.video && p.body.media_storage.book, "every kind sent");
  assert.ok(!("keep_raws" in p.body));
});
