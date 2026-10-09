// Filter builder in the gallery search box (search_filter.js): the box is the
// single source of truth - opening the form parses it, applying writes it back,
// and tokens the form does not own survive untouched.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("search_sort");

const INFO = { success: true, sections: [{ id: "search", title: "Search filters", rows: [
  { token: "date:<YYYY[-MM[-DD]]>" }, { token: "tags:..." }, { token: "name:..." }, { token: "kind:..." },
  { token: "fav:..." }, { token: "exif:..." }, { token: "ratio:..." }, { token: "near:..." },
  { token: "rating:..." }] }] };

function boot() {
  const b = page({ modules: ["search_sort"] });
  b.api.on("/api/info", INFO);
  b.api.on("/api/search_sort/values", { success: true, values: [{ value: "Canon EOS R5", count: 2 }] });
  return b;
}

const QUERY = "beach sort:-date tag:cat date:2024-01-01..2024-02-01 tags:+sun,-rain name:alice " +
  "rating:>=4 kind:video exif:Model:Canon%EOS fav:yes ratio:portrait min>=512 path:x width>100";

test("opening the builder fills the fields from the box", { skip }, async () => {
  const b = boot();
  assert.deepEqual(b.errors, []);
  const $ = id => b.document.getElementById(id);
  assert.ok($("sf_btn"), "funnel icon in the search box");
  $("search_input").value = QUERY;
  $("sf_btn").click();
  assert.ok(!$("sf_pop").classList.contains("hidden"));
  assert.equal($("sf_text").value, "beach");
  assert.equal($("sf_date_field").value, "date");
  assert.equal($("sf_from").value, "2024-01-01");
  assert.equal($("sf_to").value, "2024-02-01");
  assert.equal($("sf_tags_in").value, "sun");
  assert.equal($("sf_tags_out").value, "rain");
  assert.equal($("sf_people").value, "alice");
  assert.equal($("sf_rating").value, "4");
  assert.equal($("sf_kind").value, "video");
  assert.equal($("sf_camera").value, "Canon EOS");
  assert.equal($("sf_fav").checked, true);
  assert.equal($("sf_orient").value, "portrait");
  assert.equal($("sf_min").value, "512");
  await b.tick(20);
  // rows follow /api/info: near: is registered, location: is not; rating needs the rating module's JS
  const row = id => $(id).closest("[data-sf-need]");
  assert.ok(!row("sf_people").classList.contains("hidden"));
  assert.ok(!row("sf_near").classList.contains("hidden"));
  assert.ok(row("sf_location").classList.contains("hidden"));
  assert.ok(row("sf_rating").classList.contains("hidden"));
  assert.ok(b.api.last("/api/search_sort/values"), "camera suggestions requested");
  assert.equal($("sf_camera_list").querySelectorAll("option").length, 1);
});

test("apply round-trips the query and keeps unknown tokens", { skip }, async () => {
  const b = boot();
  const $ = id => b.document.getElementById(id);
  $("search_input").value = QUERY;
  $("sf_btn").click();
  $("sf_apply").click();
  assert.equal($("search_input").value,
    "beach sort:-date tag:cat path:x width>100 date:2024-01-01..2024-02-01 name:alice tags:+sun,-rain " +
    "rating:>=4 kind:video exif:Model:Canon%EOS fav:yes ratio:portrait min>=512");
  assert.ok($("sf_pop").classList.contains("hidden"));
  assert.ok($("sf_btn").classList.contains("sf-on"));

  // edit: open-ended date, a name with a space, drop a tag and the favorites filter
  $("sf_btn").click();
  $("sf_from").value = "";
  $("sf_people").value = "alice, Bob Smith";
  $("sf_tags_out").value = "";
  $("sf_fav").checked = false;
  $("sf_kind").value = "";
  $("sf_apply").click();
  const v = $("search_input").value;
  assert.match(v, /(^| )date:<=2024-02-01( |$)/);
  assert.match(v, /(^| )name:alice,Bob\*Smith( |$)/);
  assert.match(v, /(^| )tags:\+sun( |$)/);
  assert.doesNotMatch(v, /fav:|kind:|-rain/);
  assert.match(v, /^beach sort:-date tag:cat path:x width>100 /);

  // reopen: the edited box parses back to the same fields
  $("sf_btn").click();
  assert.equal($("sf_people").value, "alice, Bob*Smith");
  assert.equal($("sf_from").value, "");
  assert.equal($("sf_to").value, "2024-02-01");

  // clear drops only the builder's tokens
  $("sf_clear").click();
  assert.equal($("search_input").value, "sort:-date tag:cat path:x width>100");
  assert.ok(!$("sf_btn").classList.contains("sf-on"));
});

test("a semantic query is kept whole until the form is filled", { skip }, async () => {
  const b = boot();
  const $ = id => b.document.getElementById(id);
  $("search_input").value = "sem:dog on a beach";
  $("sf_btn").click();
  assert.ok(!$("sf_sem_note").classList.contains("hidden"));
  assert.equal($("sf_text").value, "");
  $("sf_apply").click();
  assert.equal($("search_input").value, "sem:dog on a beach");
  $("sf_btn").click();
  $("sf_orient").value = "square";
  $("sf_apply").click();
  assert.equal($("search_input").value, "ratio:square");
});
