@file:OptIn(ExperimentalMaterial3Api::class)

package app.cim.family.ui

import android.content.Context
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Download
import androidx.compose.material.icons.filled.DownloadDone
import androidx.compose.material.icons.filled.LibraryMusic
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.MusicNote
import androidx.compose.material.icons.filled.Person
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.QueueMusic
import androidx.compose.material.icons.filled.Radio
import androidx.compose.material.icons.filled.Shuffle
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
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
import app.cim.family.Album
import app.cim.family.ApiV2
import app.cim.family.Artist
import app.cim.family.Caps
import app.cim.family.Genre
import app.cim.family.Playlist
import app.cim.family.Prefs
import app.cim.family.Song
import app.cim.family.formatDuration
import app.cim.family.media.Downloads
import app.cim.family.media.Items
import app.cim.family.media.OfflineDb
import app.cim.family.media.PlayerConnection
import app.cim.family.media.Radio
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

// -- actions ----------------------------------------------------------------------
fun playSongs(ctx: Context, songs: List<Song>, start: Int = 0, radio: Boolean = false) {
    PlayerConnection.play(ctx, songs.map { Items.song(it, radio) }, start)
}

private fun api(ctx: Context) = ApiV2(Prefs.get(ctx))

private fun songsQuery(ctx: Context, q: Map<String, String>): List<Song> =
    Song.list(api(ctx).getJson("music/songs", q))

// -- the Music tab ----------------------------------------------------------------
@Composable
fun MusicTab(caps: Caps, push: (Route) -> Unit) {
    val opts = ArrayList<Pair<String, String>>()
    if (caps.has("radio")) opts.add("radio" to "Radio")
    opts.add("offline" to "Offline")
    if (caps.has("playlists")) opts.add("playlists" to "Playlists")
    opts.add("albums" to "Albums"); opts.add("artists" to "Artists"); opts.add("songs" to "Songs"); opts.add("genres" to "Genres")
    var section by remember { mutableStateOf(opts.first().first) }
    Column(Modifier.fillMaxSize()) {
        ChipRow(opts, section) { section = it }
        Box(Modifier.weight(1f)) {
            when (section) {
                "radio" -> RadioSection(push)
                "offline" -> OfflineSection(push)
                "playlists" -> PlaylistsSection(push)
                "albums" -> AlbumsSection(push)
                "artists" -> ArtistsSection(push)
                "songs" -> SongsSection()
                else -> GenresSection(push)
            }
        }
    }
}

// -- radio ------------------------------------------------------------------------
@Composable
fun RadioSection(push: (Route) -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    val rs by Radio.state.collectAsState()
    val ps by PlayerConnection.state.collectAsState()
    var resume by remember { mutableStateOf<Pair<Int, List<Song>>?>(null) }
    LaunchedEffect(Unit) {
        resume = withContext(Dispatchers.IO) { try { Radio.status(ctx) } catch (e: Exception) { null } }
    }
    fun start(songs: List<Song>) { if (songs.isNotEmpty()) { playSongs(ctx, songs, 0, radio = true); push(Route.NowPlaying) } }
    LazyColumn(Modifier.fillMaxSize()) {
        item {
            Card(Modifier.fillMaxWidth().padding(12.dp), colors = CardDefaults.cardColors(containerColor = CardBg)) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Default.Radio, contentDescription = null, modifier = Modifier.size(28.dp))
                        Text("  Radio", fontSize = 20.sp, fontWeight = FontWeight.Bold)
                    }
                    Text("An endless mix from your library, routed track to track by sound. A new round is fetched when the queue runs low.",
                        color = Color.Gray, fontSize = 13.sp)
                    if (rs.round > 0) Text("Round ${rs.round}: ${rs.tracks} tracks, ${"%.1f".format(rs.hours)} h", fontSize = 13.sp)
                    ErrorText(rs.error)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Button(onClick = {
                            scope.launch {
                                val songs = withContext(Dispatchers.IO) { try { Radio.nextRound(ctx) } catch (e: Exception) { emptyList<Song>() } }
                                start(songs)
                            }
                        }, enabled = !rs.loading) { Text(if (rs.loading) "Loading..." else "Start radio") }
                        val r = resume
                        if (r != null && r.second.isNotEmpty()) {
                            OutlinedButton(onClick = { start(r.second) }) { Text("Resume round ${r.first}") }
                        }
                    }
                    if (ps.radio) {
                        TextButton(onClick = {
                            val songs = PlayerConnection.queueItems().filter { Items.isRadio(it) }.mapNotNull { Items.songOf(it) }
                            if (songs.isNotEmpty()) Downloads.add(ctx, "radio:" + System.currentTimeMillis(), "Radio round ${rs.round}", songs)
                        }) { Icon(Icons.Default.Download, null); Text(" Download this round") }
                    }
                }
            }
        }
    }
}

// -- offline ----------------------------------------------------------------------
@Composable
fun OfflineSection(push: (Route) -> Unit) {
    val ctx = LocalContext.current
    var groups by remember { mutableStateOf<List<OfflineDb.Group>>(emptyList()) }
    var tick by remember { mutableStateOf(0) }
    LaunchedEffect(tick) {
        while (true) {
            groups = withContext(Dispatchers.IO) { OfflineDb.get(ctx).groups() }
            delay(1500L)
        }
    }
    LazyColumn(Modifier.fillMaxSize()) {
        item {
            Text("Downloaded for offline listening: plays with no connection to the server." +
                (if (Prefs.get(ctx).downloadsWifiOnly) " Downloads run on Wi-Fi only (Settings)." else ""),
                Modifier.padding(16.dp, 8.dp), color = Color.Gray, fontSize = 13.sp)
        }
        if (groups.isEmpty()) item {
            Text("Nothing downloaded yet. Use Download on a playlist, an album or the radio round.", Modifier.padding(16.dp), color = Color.Gray)
        }
        if (groups.any { it.failed > 0 }) item {
            TextButton(onClick = { Downloads.retry(ctx); tick++ }, modifier = Modifier.padding(horizontal = 8.dp)) { Text("Retry failed downloads") }
        }
        items(groups, key = { it.id }) { g ->
            Column(Modifier.fillMaxWidth().clickable { push(Route.OfflineR(g.id, g.name)) }.padding(16.dp, 8.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(if (g.done == g.total && g.total > 0) Icons.Default.DownloadDone else Icons.Default.Download, null, tint = Color.Gray)
                    Column(Modifier.weight(1f).padding(start = 12.dp)) {
                        Text(g.name, maxLines = 1, overflow = TextOverflow.Ellipsis)
                        Text("${g.done}/${g.total} tracks" + (if (g.failed > 0) ", ${g.failed} failed" else "") +
                            (if (g.bytes > 0) " | ${formatBytes(g.bytes)}" else ""), fontSize = 12.sp, color = Color.Gray)
                    }
                    IconButton(onClick = { Downloads.delete(ctx, g.id); tick++ }) { Icon(Icons.Default.Delete, "delete download") }
                }
                if (g.done < g.total && g.total > 0) {
                    val frac = if (g.bytes > 0) (g.doneBytes.toFloat() / g.bytes).coerceIn(0f, 1f) else g.done.toFloat() / g.total
                    LinearProgressIndicator(progress = { frac }, modifier = Modifier.fillMaxWidth().padding(top = 4.dp))
                }
            }
        }
    }
}

@Composable
fun OfflineGroupScreen(id: String, name: String, onBack: () -> Unit) {
    val ctx = LocalContext.current
    var tracks by remember { mutableStateOf<List<OfflineDb.Track>>(emptyList()) }
    LaunchedEffect(id) {
        while (true) {
            tracks = withContext(Dispatchers.IO) { OfflineDb.get(ctx).groupTracks(id) }
            delay(2000L)
        }
    }
    val songs = tracks.map { it.song }
    Column(Modifier.fillMaxSize()) {
        BackHeader(name, "${tracks.count { it.state == "done" }}/${tracks.size} downloaded", onBack)
        Row(Modifier.padding(horizontal = 12.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = { playSongs(ctx, songs) }, enabled = songs.isNotEmpty()) { Icon(Icons.Default.PlayArrow, null); Text(" Play") }
            OutlinedButton(onClick = { playSongs(ctx, songs.shuffled()) }, enabled = songs.isNotEmpty()) { Icon(Icons.Default.Shuffle, null) }
            OutlinedButton(onClick = { Downloads.delete(ctx, id); onBack() }) { Icon(Icons.Default.Delete, null); Text(" Delete") }
        }
        LazyColumn(Modifier.weight(1f)) {
            itemsIndexed(tracks) { i, t ->
                val status = when (t.state) {
                    "done" -> formatDuration(t.song.durationSec)
                    "failed" -> "failed: " + t.error
                    else -> if (t.size > 0) "downloading ${(t.done * 100 / t.size)}%" else "queued"
                }
                SongRow(t.song, status, onClick = { playSongs(ctx, songs, i) })
            }
        }
    }
}

// -- playlists --------------------------------------------------------------------
@Composable
fun PlaylistsSection(push: (Route) -> Unit) {
    val ctx = LocalContext.current
    Loader(key = "playlists", load = { Playlist.list(api(ctx).getJson("music/playlists")) }) { lists ->
        LazyColumn(Modifier.fillMaxSize()) {
            if (lists.isEmpty()) item { Text("No playlists. Create them in the Music tab on the server.", Modifier.padding(16.dp), color = Color.Gray) }
            items(lists, key = { it.id }) { p ->
                Row2(p.name, "${p.count} tracks" + (if (p.seconds > 0) " | ${formatDuration(p.seconds)}" else ""),
                    onClick = { push(Route.PlaylistR(p.id, p.name)) },
                    leading = { Icon(Icons.Default.QueueMusic, null, Modifier.size(40.dp), tint = Color.Gray) })
            }
        }
    }
}

@Composable
fun PlaylistScreen(id: Long, name: String, onBack: () -> Unit) {
    val ctx = LocalContext.current
    Column(Modifier.fillMaxSize()) {
        BackHeader(name, "Playlist", onBack)
        Loader(key = id, load = { Song.list(api(ctx).getJson("music/playlist", mapOf("id" to id.toString()))) }) { songs ->
            SongListWithActions(songs, downloadId = "playlist:$id", downloadName = name)
        }
    }
}

// -- albums / artists / genres / songs ---------------------------------------------
@Composable
fun AlbumsSection(push: (Route) -> Unit) {
    val ctx = LocalContext.current
    Loader(key = "albums", load = { Album.list(api(ctx).getJson("music/albums", mapOf("sort" to "album"))) }) { albums ->
        AlbumGrid(albums, push)
    }
}

@Composable
fun AlbumGrid(albums: List<Album>, push: (Route) -> Unit) {
    LazyVerticalGrid(GridCells.Adaptive(140.dp), Modifier.fillMaxSize(), horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)) {
        items(albums, key = { it.artist + "\u0000" + it.album }) { a ->
            Column(Modifier.clickable { push(Route.AlbumR(a)) }.padding(6.dp)) {
                Cover(a.coverKey, Modifier.fillMaxWidth().aspectRatio(1f).clip(RoundedCornerShape(6.dp)), Icons.Default.LibraryMusic)
                Text(a.album, maxLines = 1, overflow = TextOverflow.Ellipsis, fontSize = 13.sp)
                Text(a.artist + (if (a.year.isNotEmpty()) " | ${a.year}" else ""), maxLines = 1, overflow = TextOverflow.Ellipsis,
                    fontSize = 11.sp, color = Color.Gray)
            }
        }
    }
}

@Composable
fun AlbumScreen(album: Album, onBack: () -> Unit) {
    val ctx = LocalContext.current
    Column(Modifier.fillMaxSize()) {
        BackHeader(album.album, album.artist, onBack)
        Loader(key = album, load = {
            songsQuery(ctx, mapOf("album" to album.album, "artist" to album.artist, "per" to "500"))
        }) { songs ->
            SongListWithActions(songs, downloadId = "album:${album.artist}|${album.album}", downloadName = album.album,
                header = { Cover(album.coverKey, Modifier.padding(12.dp).size(160.dp).clip(RoundedCornerShape(8.dp)), Icons.Default.LibraryMusic) })
        }
    }
}

@Composable
fun ArtistsSection(push: (Route) -> Unit) {
    val ctx = LocalContext.current
    Loader(key = "artists", load = { Artist.list(api(ctx).getJson("music/artists", mapOf("sort" to "name"))) }) { artists ->
        LazyColumn(Modifier.fillMaxSize()) {
            items(artists, key = { it.name }) { a ->
                Row2(a.name, "${a.albums} albums | ${a.tracks} tracks", onClick = { push(Route.ArtistR(a.name)) },
                    leading = { Cover(a.coverKey, Modifier.size(44.dp).clip(RoundedCornerShape(22.dp)), Icons.Default.Person) })
            }
        }
    }
}

@Composable
fun ArtistScreen(name: String, push: (Route) -> Unit, onBack: () -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var err by remember { mutableStateOf("") }
    Column(Modifier.fillMaxSize()) {
        BackHeader(name, "Artist", onBack)
        Row(Modifier.padding(horizontal = 12.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = {
                scope.launch {
                    try {
                        val songs = withContext(Dispatchers.IO) { songsQuery(ctx, mapOf("artist" to name, "per" to "2000")) }
                        playSongs(ctx, songs); err = ""
                    } catch (e: Exception) { err = e.message ?: e.toString() }
                }
            }) { Icon(Icons.Default.PlayArrow, null); Text(" Play all") }
            OutlinedButton(onClick = {
                scope.launch {
                    try {
                        val songs = withContext(Dispatchers.IO) { songsQuery(ctx, mapOf("artist" to name, "per" to "2000")) }
                        playSongs(ctx, songs.shuffled()); err = ""
                    } catch (e: Exception) { err = e.message ?: e.toString() }
                }
            }) { Icon(Icons.Default.Shuffle, null) }
        }
        ErrorText(err)
        Box(Modifier.weight(1f)) {
            Loader(key = name, load = { Album.list(api(ctx).getJson("music/albums", mapOf("artist" to name, "sort" to "year"))) }) { albums ->
                AlbumGrid(albums, push)
            }
        }
    }
}

@Composable
fun GenresSection(push: (Route) -> Unit) {
    val ctx = LocalContext.current
    Loader(key = "genres", load = { Genre.list(api(ctx).getJson("music/genres", mapOf("sort" to "name"))) }) { genres ->
        LazyColumn(Modifier.fillMaxSize()) {
            items(genres, key = { it.name }) { g ->
                Row2(g.name, "${g.artists} artists | ${g.tracks} tracks", onClick = { push(Route.GenreR(g.name)) },
                    leading = { Cover(g.coverKey, Modifier.size(44.dp).clip(RoundedCornerShape(6.dp)), Icons.Default.MusicNote) })
            }
        }
    }
}

@Composable
fun GenreScreen(name: String, onBack: () -> Unit) {
    val ctx = LocalContext.current
    Column(Modifier.fillMaxSize()) {
        BackHeader(name, "Genre", onBack)
        Loader(key = name, load = { songsQuery(ctx, mapOf("genre" to name, "per" to "1000")) }) { songs ->
            SongListWithActions(songs, downloadId = null, downloadName = name)
        }
    }
}

@Composable
fun SongsSection() {
    val ctx = LocalContext.current
    var q by remember { mutableStateOf("") }
    var applied by remember { mutableStateOf("") }
    LaunchedEffect(q) { delay(500L); applied = q.trim() }
    Column(Modifier.fillMaxSize()) {
        OutlinedTextField(q, { q = it }, label = { Text("Search songs (sem:mood for sound search)") }, singleLine = true,
            modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp))
        Loader(key = applied, load = { songsQuery(ctx, mapOf("q" to applied, "per" to "500")) }) { songs ->
            LazyColumn(Modifier.fillMaxSize()) {
                if (songs.isEmpty()) item { Text("No songs.", Modifier.padding(16.dp), color = Color.Gray) }
                itemsIndexed(songs) { i, s ->
                    SongRow(s, formatDuration(s.durationSec), onClick = { playSongs(ctx, songs, i) }, menu = true)
                }
            }
        }
    }
}

/** @brief Play / shuffle / download buttons over a song list. */
@Composable
fun SongListWithActions(songs: List<Song>, downloadId: String?, downloadName: String, header: @Composable () -> Unit = {}) {
    val ctx = LocalContext.current
    var downloaded by remember(downloadId) { mutableStateOf(downloadId != null && OfflineDb.get(ctx).hasGroup(downloadId)) }
    LazyColumn(Modifier.fillMaxSize()) {
        item {
            header()
            Row(Modifier.padding(horizontal = 12.dp, vertical = 4.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = { playSongs(ctx, songs) }, enabled = songs.isNotEmpty()) { Icon(Icons.Default.PlayArrow, null); Text(" Play") }
                OutlinedButton(onClick = { playSongs(ctx, songs.shuffled()) }, enabled = songs.isNotEmpty()) { Icon(Icons.Default.Shuffle, null) }
                if (downloadId != null) {
                    OutlinedButton(onClick = { Downloads.add(ctx, downloadId, downloadName, songs); downloaded = true },
                        enabled = songs.isNotEmpty()) {
                        Icon(if (downloaded) Icons.Default.DownloadDone else Icons.Default.Download, null)
                        Text(if (downloaded) " Update download" else " Download")
                    }
                }
            }
            Text("${songs.size} tracks | ${formatDuration(songs.sumOf { it.durationSec })}", Modifier.padding(16.dp, 2.dp),
                fontSize = 12.sp, color = Color.Gray)
        }
        itemsIndexed(songs) { i, s ->
            SongRow(s, formatDuration(s.durationSec), onClick = { playSongs(ctx, songs, i) }, menu = true)
        }
    }
}

@Composable
fun SongRow(song: Song, status: String, onClick: () -> Unit, menu: Boolean = false) {
    val ctx = LocalContext.current
    val ps by PlayerConnection.state.collectAsState()
    val current = ps.mediaId == song.relPath
    var open by remember { mutableStateOf(false) }
    Row2(song.displayTitle, listOf(song.displayArtist, status).filter { it.isNotEmpty() }.joinToString(" | "), onClick = onClick,
        leading = { Cover(song.coverKey, Modifier.size(44.dp).clip(RoundedCornerShape(4.dp)), Icons.Default.MusicNote) },
        trailing = {
            if (current) Icon(Icons.Default.PlayArrow, "playing", tint = MaterialTheme.colorScheme.primary)
            if (menu) {
                Box {
                    IconButton(onClick = { open = true }) { Icon(Icons.Default.MoreVert, "more") }
                    DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
                        DropdownMenuItem(text = { Text("Play next") }, onClick = { open = false; PlayerConnection.playNext(ctx, listOf(Items.song(song))) })
                        DropdownMenuItem(text = { Text("Add to queue") }, onClick = { open = false; PlayerConnection.enqueue(ctx, listOf(Items.song(song))) })
                    }
                }
            }
        })
}
