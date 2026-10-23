@file:OptIn(ExperimentalMaterial3Api::class)

package app.cim.family.ui

import android.content.Context
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Theaters
import androidx.compose.material.icons.filled.Tv
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.ui.PlayerView
import app.cim.family.ApiV2
import app.cim.family.Caps
import app.cim.family.Episode
import app.cim.family.Movie
import app.cim.family.Prefs
import app.cim.family.Season
import app.cim.family.Show
import app.cim.family.media.Items
import app.cim.family.media.PlayerConnection
import app.cim.family.media.Prefetcher
import app.cim.family.media.ResumeStore
import app.cim.family.media.VideoPlayers
import app.cim.family.str
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import java.io.File

private fun vapi(ctx: Context) = ApiV2(Prefs.get(ctx))

fun episodeSpec(show: Show, e: Episode): VideoSpec = VideoSpec(e.relPath,
    listOf(e.code, e.displayTitle).filter { it.isNotEmpty() }.joinToString("  "), show.title, e.thumbKey ?: show.posterKey,
    e.subtitles, "tv/subtitle")

fun movieSpec(m: Movie, partRel: String): VideoSpec = VideoSpec(partRel,
    m.displayTitle + (if (m.year.isNotEmpty()) " (${m.year})" else ""), m.edition, m.posterKey,
    if (partRel == m.relPath) m.subtitles else emptyList(), "movies/subtitle")

// -- the Video tab ------------------------------------------------------------------
@Composable
fun VideoTab(caps: Caps, push: (Route) -> Unit) {
    val opts = ArrayList<Pair<String, String>>()
    if (caps.has("tv")) opts.add("tv" to "TV shows")
    if (caps.has("movies")) opts.add("movies" to "Movies")
    if (opts.isEmpty()) { Text("No video modules on this server.", Modifier.padding(16.dp)); return }
    var section by remember { mutableStateOf(opts.first().first) }
    Column(Modifier.fillMaxSize()) {
        ChipRow(opts, section) { section = it }
        Box(Modifier.weight(1f)) {
            if (section == "tv") TvSection(push) else MoviesSection(push)
        }
    }
}

@Composable
private fun PosterGrid(content: androidx.compose.foundation.lazy.grid.LazyGridScope.() -> Unit) {
    LazyVerticalGrid(GridCells.Adaptive(118.dp), Modifier.fillMaxSize().padding(horizontal = 6.dp),
        horizontalArrangement = Arrangement.spacedBy(6.dp), verticalArrangement = Arrangement.spacedBy(10.dp), content = content)
}

@Composable
private fun Poster(key: String?, title: String, sub: String, onClick: () -> Unit) {
    Column(Modifier.clickable(onClick = onClick)) {
        Cover(key, Modifier.fillMaxWidth().aspectRatio(2f / 3f).clip(RoundedCornerShape(6.dp)), Icons.Default.Theaters)
        Text(title, maxLines = 1, overflow = TextOverflow.Ellipsis, fontSize = 12.sp)
        if (sub.isNotEmpty()) Text(sub, maxLines = 1, overflow = TextOverflow.Ellipsis, fontSize = 11.sp, color = Color.Gray)
    }
}

@Composable
fun TvSection(push: (Route) -> Unit) {
    val ctx = LocalContext.current
    Loader(key = "tv", load = { Show.list(vapi(ctx).getJson("tv/shows")) }) { shows ->
        if (shows.isEmpty()) Text("No shows on the server.", Modifier.padding(16.dp), color = Color.Gray)
        PosterGrid {
            items(shows, key = { it.relPath }) { s ->
                Poster(s.posterKey, s.title, listOf(s.year, "${s.seasons} seasons").filter { it.isNotEmpty() }.joinToString(" | ")) {
                    push(Route.ShowR(s))
                }
            }
        }
    }
}

@Composable
fun ShowScreen(show: Show, push: (Route) -> Unit, onBack: () -> Unit) {
    val ctx = LocalContext.current
    Column(Modifier.fillMaxSize()) {
        BackHeader(show.title, listOf(show.year, show.genres.joinToString(", ")).filter { it.isNotEmpty() }.joinToString(" | "), onBack)
        Loader(key = show.relPath, load = { Season.list(vapi(ctx).getJson("tv/seasons", mapOf("show" to show.relPath))) }) { seasons ->
            LazyColumn(Modifier.fillMaxSize()) {
                if (show.plot.isNotEmpty()) item { Text(show.plot, Modifier.padding(16.dp, 4.dp), fontSize = 13.sp, color = Color.LightGray) }
                items(seasons) { se ->
                    Row2(se.label, "${se.episodes} episodes", onClick = { push(Route.SeasonR(show, se)) },
                        leading = { Cover(se.posterKey ?: show.posterKey, Modifier.width(48.dp).aspectRatio(2f / 3f).clip(RoundedCornerShape(4.dp)), Icons.Default.Tv) })
                }
            }
        }
    }
}

@Composable
fun SeasonScreen(show: Show, season: Season, push: (Route) -> Unit, onBack: () -> Unit) {
    val ctx = LocalContext.current
    Column(Modifier.fillMaxSize()) {
        BackHeader(season.label, show.title, onBack)
        Loader(key = show.relPath + "|" + season.season, load = {
            val q = HashMap<String, String>()
            q["show"] = show.relPath
            val sn = season.season
            if (sn != null) q["season"] = sn.toString()
            Episode.list(vapi(ctx).getJson("tv/episodes", q)).filter { it.season == season.season }
        }) { eps ->
            LazyColumn(Modifier.fillMaxSize()) {
                items(eps, key = { it.relPath }) { e ->
                    val resumeMs = ResumeStore.get(ctx, e.relPath)
                    Row2(listOf(e.code, e.displayTitle).filter { it.isNotEmpty() }.joinToString("  "),
                        listOf(e.aired, if (resumeMs > 0) "resume at ${app.cim.family.formatDuration(resumeMs / 1000.0)}" else "",
                            if (e.subtitles.isNotEmpty()) "CC" else "").filter { it.isNotEmpty() }.joinToString(" | "),
                        onClick = { push(Route.Video(episodeSpec(show, e))) },
                        leading = { Cover(e.thumbKey, Modifier.width(96.dp).aspectRatio(16f / 9f).clip(RoundedCornerShape(4.dp)), Icons.Default.Tv) })
                }
            }
        }
    }
}

@Composable
fun MoviesSection(push: (Route) -> Unit) {
    val ctx = LocalContext.current
    var q by remember { mutableStateOf("") }
    var applied by remember { mutableStateOf("") }
    var genre by remember { mutableStateOf("") }
    var genres by remember { mutableStateOf<List<String>>(emptyList()) }
    LaunchedEffect(q) { delay(500L); applied = q.trim() }
    LaunchedEffect(Unit) {
        genres = withContext(Dispatchers.IO) {
            try {
                val j = vapi(ctx).getJson("movies/genres")
                val a = j.optJSONArray("genres")
                val out = ArrayList<String>()
                if (a != null) for (i in 0 until a.length()) { val g = a.optJSONObject(i)?.str("name").orEmpty(); if (g.isNotEmpty()) out.add(g) }
                out
            } catch (e: Exception) { emptyList<String>() }
        }
    }
    Column(Modifier.fillMaxSize()) {
        OutlinedTextField(q, { q = it }, label = { Text("Search movies") }, singleLine = true,
            modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp))
        if (genres.isNotEmpty()) ChipRow(listOf("" to "All") + genres.map { it to it }, genre) { genre = it }
        Box(Modifier.weight(1f)) {
            Loader(key = applied + "|" + genre, load = {
                Movie.list(vapi(ctx).getJson("movies", mapOf("q" to applied, "genre" to genre, "per" to "1000")))
            }) { movies ->
                if (movies.isEmpty()) Text("No movies.", Modifier.padding(16.dp), color = Color.Gray)
                PosterGrid {
                    items(movies, key = { it.relPath }) { m ->
                        Poster(m.posterKey, m.displayTitle, m.year) { push(Route.MovieR(m)) }
                    }
                }
            }
        }
    }
}

@Composable
fun MovieScreen(movie: Movie, push: (Route) -> Unit, onBack: () -> Unit) {
    val ctx = LocalContext.current
    Column(Modifier.fillMaxSize()) {
        BackHeader(movie.displayTitle, listOf(movie.year, movie.edition).filter { it.isNotEmpty() }.joinToString(" | "), onBack)
        LazyColumn(Modifier.fillMaxSize()) {
            item {
                Row(Modifier.padding(12.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    Cover(movie.posterKey, Modifier.width(130.dp).aspectRatio(2f / 3f).clip(RoundedCornerShape(6.dp)), Icons.Default.Theaters)
                    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        if (movie.genres.isNotEmpty()) Text(movie.genres.joinToString(", "), fontSize = 12.sp, color = Color.Gray)
                        if (movie.subtitles.isNotEmpty()) Text("Subtitles: " + movie.subtitles.joinToString(", ") { it.label }, fontSize = 12.sp, color = Color.Gray)
                        val resumeMs = ResumeStore.get(ctx, movie.parts.first().relPath)
                        if (resumeMs > 0) Text("Resume at " + app.cim.family.formatDuration(resumeMs / 1000.0), fontSize = 12.sp)
                        for (p in movie.parts) {
                            val label = if (movie.parts.size > 1) (p.label.ifEmpty { "Part ${p.part ?: ""}" }) else "Play"
                            Button(onClick = { push(Route.Video(movieSpec(movie, p.relPath))) }) { Icon(Icons.Default.PlayArrow, null); Text(" $label") }
                        }
                    }
                }
            }
            if (movie.plot.isNotEmpty()) item { Text(movie.plot, Modifier.padding(16.dp, 4.dp), fontSize = 13.sp, color = Color.LightGray) }
        }
    }
}

// -- the player -----------------------------------------------------------------------
/**
 * @brief Full-screen in-app video: media3 PlayerView over a local ExoPlayer that reads
 * through the decrypting cache stack, subtitles side-loaded from the server, the
 * position remembered per file. Music is paused while it plays.
 */
@Composable
fun VideoPlayerScreen(spec: VideoSpec, onBack: () -> Unit) {
    val ctx = LocalContext.current
    BackHandler { onBack() }
    var subs by remember(spec.rel) { mutableStateOf<List<Pair<File, String>>?>(null) }
    LaunchedEffect(spec.rel) {
        PlayerConnection.pause()
        subs = withContext(Dispatchers.IO) { VideoPlayers.fetchSubtitles(ctx, spec.subRoute, spec.subs) }
    }
    Box(Modifier.fillMaxSize().background(Color.Black)) {
        val ready = subs
        if (ready == null) {
            CircularProgressIndicator(Modifier.align(Alignment.Center))
        } else {
            VideoSurface(spec, ready)
        }
        IconButton(onClick = onBack, modifier = Modifier.align(Alignment.TopStart).padding(8.dp)) {
            Icon(Icons.Default.ArrowBack, "back", tint = Color.White)
        }
    }
}

@Composable
private fun VideoSurface(spec: VideoSpec, subs: List<Pair<File, String>>) {
    val ctx = LocalContext.current
    val exo = remember(spec.rel) { VideoPlayers.build(ctx) }
    val prefetcher = remember(spec.rel) { Prefetcher(ctx) }
    var err by remember(spec.rel) { mutableStateOf("") }
    DisposableEffect(spec.rel) {
        val listener = object : Player.Listener {
            override fun onPlayerError(error: PlaybackException) {
                err = error.cause?.message ?: error.message ?: "playback failed"
            }
            override fun onIsPlayingChanged(isPlaying: Boolean) {
                if (isPlaying) err = ""
            }
        }
        exo.addListener(listener)
        exo.setMediaItem(Items.video(spec.rel, spec.title, spec.subtitle, spec.artKey, subs), ResumeStore.get(ctx, spec.rel))
        exo.prepare()
        exo.playWhenReady = true
        onDispose {
            ResumeStore.put(ctx, spec.rel, exo.currentPosition, exo.duration)
            exo.removeListener(listener)
            prefetcher.release()
            exo.release()
        }
    }
    LaunchedEffect(spec.rel) {
        while (true) {
            delay(15_000L)
            val d = exo.duration
            if (d > 0) {
                ResumeStore.put(ctx, spec.rel, exo.currentPosition, d)
                VideoPlayers.prefetch(ctx, prefetcher, spec.rel, exo.currentPosition, d)
            }
        }
    }
    Box(Modifier.fillMaxSize()) {
        AndroidView(
            factory = { c ->
                PlayerView(c).apply {
                    this.player = exo
                    keepScreenOn = true
                    setShowSubtitleButton(true)
                }
            },
            update = { v -> if (v.player !== exo) v.player = exo },
            modifier = Modifier.fillMaxSize(),
        )
        if (err.isNotEmpty()) {
            Text(err, Modifier.align(Alignment.Center).background(Color(0xCC000000)).padding(16.dp), color = Color.White)
        }
        Text(spec.title, Modifier.align(Alignment.TopCenter).padding(top = 18.dp, start = 56.dp, end = 16.dp),
            color = Color.White, fontWeight = FontWeight.SemiBold, maxLines = 1, overflow = TextOverflow.Ellipsis)
    }
}
