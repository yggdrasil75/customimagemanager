// The TV tab: the declared tab and Reindex button reach the page, the pane
// browses shows > seasons > episodes, and an episode plays through selectFile.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("tv");

const SHOW = { rel_path: "tv/The Show (2001)", title: "The Show", year: 2001, plot: "About.", genres: ["Drama"],
  poster_rel: "tv/The Show (2001)/poster.jpg", seasons: 2, episodes: 3 };
const EP = { rel_path: "tv/The Show (2001)/Season 01/The Show - s01e01 - Pilot.mkv", season: 1, episode: 1,
  episode_end: null, title: "Pilot", extra: null, plot: "Starts.", aired: "2001-05-01",
  subtitles: [{ rel: "tv/The Show (2001)/Season 01/The Show - s01e01 - Pilot.en.srt", lang: "en", forced: false, sdh: false }] };

async function boot() {
  const b = page({ modules: ["tv"] });
  b.api.on("/api/tv/status", { success: true, shows: 1, episodes: 3, state: { indexing: false } });
  b.api.on("/api/tv/shows", { success: true, total: 1, shows: [SHOW] });
  b.api.on("/api/tv/seasons", { success: true, show: SHOW, seasons: [
    { season: 0, episodes: 1, label: "Specials", poster_rel: null },
    { season: 1, episodes: 2, label: "Season 1", poster_rel: "tv/The Show (2001)/season01-poster.jpg" }] });
  b.api.on("/api/tv/episodes", c => ({ success: true, show: SHOW,
    episodes: c.query.season === "1" ? [EP, Object.assign({}, EP, { episode: 2, episode_end: 3, title: "Two", subtitles: [] })] : [] }));
  await b.tick(10);
  return b;
}

test("shows > seasons > episodes, and an episode plays in the viewer", { skip }, async () => {
  const b = await boot();
  assert.deepEqual(b.errors, []);
  assert.ok(b.document.querySelector('[data-ltab="tv"]'), "the TV tab button comes from the declaration");
  b.run(`window.__played = null; window.selectFile = f => { window.__played = f; return Promise.resolve(); };`);
  b.run(`setPane('tv')`);
  await b.tick(30);
  assert.ok(b.api.last("/api/tv/shows", "GET"));
  assert.ok(b.document.querySelector('#tv_pane [data-ext-area="tv_tools"] button'), "the declared Reindex button");
  assert.match(b.document.getElementById("tv_stat").textContent, /1 shows, 3 episodes/);
  const shows = [...b.document.querySelectorAll("#tv_list .tv-show")];
  assert.equal(shows.length, 1);
  shows[0].click();
  await b.tick(30);
  const seasons = [...b.document.querySelectorAll("#tv_list .tv-season")];
  assert.deepEqual(seasons.map(s => s.querySelector(".truncate").textContent), ["Specials", "Season 1"]);
  assert.match(b.document.getElementById("tv_detail").textContent, /The Show.*About\./s);
  seasons[1].click();
  await b.tick(30);
  assert.equal(b.api.last("/api/tv/episodes", "GET").query.season, "1");
  const eps = [...b.document.querySelectorAll("#tv_list .tv-episode")];
  assert.equal(eps.length, 2);
  assert.match(eps[0].textContent, /s01e01/);
  assert.match(eps[1].textContent, /s01e02-e03/);
  assert.match(b.document.getElementById("tv_crumbs").textContent, /Shows.*The Show.*Season 1/s);
  eps[0].click();
  await b.tick(30);
  assert.equal(b.val("window.__played"), EP.rel_path);
  const tracks = [...b.document.querySelectorAll("#media_video track[data-cim-sub]")];
  assert.equal(tracks.length, 1, "the episode's subtitle rides along as a track");
  assert.match(tracks[0].getAttribute("src"), /\/api\/tv\/subtitle\//);
  // back up the crumbs
  b.document.querySelector('#tv_crumbs [data-crumb="0"]').click();
  await b.tick(30);
  assert.equal(b.document.querySelectorAll("#tv_list .tv-show").length, 1);
});
