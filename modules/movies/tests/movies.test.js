// The Movies tab: the declared tab and Reindex button reach the page, the pane
// lists /api/movies, and picking a movie plays it through the core's selectFile.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("movies");

const MOVIES = { success: true, total: 2, page: 0, page_size: 120, movies: [
  { rel_path: "movies/Heat (1995)/Heat (1995).mkv", title: "Heat", year: 1995, edition: "", poster_rel: "movies/Heat (1995)/poster.jpg",
    genres: ["Crime"], plot: "Cops.", parts: [{ rel_path: "movies/Heat (1995)/Heat (1995).mkv", part: null }],
    subtitles: [{ rel: "movies/Heat (1995)/Heat (1995).en.srt", lang: "en", forced: false, sdh: false }] },
  { rel_path: "movies/Alien (1979).mkv", title: "Alien", year: 1979, edition: "", poster_rel: null, genres: [], plot: "",
    parts: [{ rel_path: "movies/Alien (1979).mkv", part: null }], subtitles: [] }] };

async function boot() {
  const b = page({ modules: ["movies"] });
  b.api.on("/api/movies/status", { success: true, movies: 2, extras: 0, state: { indexing: false } });
  b.api.on("/api/movies/genres", { success: true, genres: [{ name: "Crime", count: 1 }] });
  b.api.on("/api/movies/item", { success: true, movie: Object.assign({ extras: [] }, MOVIES.movies[0]) });
  b.api.on("/api/movies", MOVIES);
  await b.tick(10);
  return b;
}

test("the tab is declared, the pane lists the movies and one plays in the viewer", { skip }, async () => {
  const b = await boot();
  assert.deepEqual(b.errors, []);
  assert.ok(b.document.querySelector('[data-ltab="movies"]'), "the Movies tab button comes from the declaration");
  assert.ok(b.document.getElementById("movies_pane"), "the pane is rendered");
  b.run(`window.__played = null; window.selectFile = f => { window.__played = f; return Promise.resolve(); };`);
  b.run(`setPane('movies')`);
  await b.tick(30);
  assert.ok(b.api.last("/api/movies", "GET"), "showing the tab loads the list");
  const tiles = [...b.document.querySelectorAll("#movies_list .movie-tile")];
  assert.equal(tiles.length, 2);
  assert.match(tiles[0].textContent, /Heat/);
  assert.ok(tiles[0].querySelector("img"), "a poster shows as its thumbnail");
  assert.match(b.document.getElementById("movies_stat").textContent, /2 movies/);
  assert.ok(b.document.querySelector('#movies_pane [data-ext-area="movies_tools"] button'),
    "the declared Reindex button is drawn into the pane");
  tiles[0].click();
  await b.tick(30);
  assert.equal(b.val("window.__played"), "movies/Heat (1995)/Heat (1995).mkv");
  assert.match(b.document.getElementById("movies_detail").textContent, /Heat.*1995/s);
  const tracks = [...b.document.querySelectorAll("#media_video track[data-cim-sub]")];
  assert.equal(tracks.length, 1, "the movie's subtitle rides along as a track");
  assert.match(tracks[0].getAttribute("src"), /\/api\/movies\/subtitle\//);
});
