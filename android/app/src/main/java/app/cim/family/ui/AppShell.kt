@file:OptIn(ExperimentalMaterial3Api::class)

package app.cim.family.ui

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.AutoStories
import androidx.compose.material.icons.filled.LibraryMusic
import androidx.compose.material.icons.filled.PhotoLibrary
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Theaters
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.core.content.ContextCompat
import app.cim.family.Api
import app.cim.family.Caps
import app.cim.family.CapsStore
import app.cim.family.Db
import app.cim.family.Prefs
import app.cim.family.UploadWorker
import app.cim.family.media.PlayerConnection
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

private data class TabDef(val id: String, val label: String, val icon: ImageVector)

private fun tabsFor(caps: Caps): List<TabDef> {
    val out = ArrayList<TabDef>()
    out.add(TabDef("photos", "Photos", Icons.Default.PhotoLibrary))
    if (caps.v2 && caps.has("music")) out.add(TabDef("music", "Music", Icons.Default.LibraryMusic))
    if (caps.v2 && (caps.has("tv") || caps.has("movies"))) out.add(TabDef("video", "Video", Icons.Default.Theaters))
    if (caps.v2 && caps.has("books")) out.add(TabDef("books", "Books", Icons.Default.AutoStories))
    out.add(TabDef("settings", "Settings", Icons.Default.Settings))
    return out
}

/** @brief Pairing, then the tabbed app: one tab per server module the account may use. */
@Composable
fun App(prefs: Prefs) {
    var paired by remember { mutableStateOf(prefs.paired) }
    if (!paired) { SetupScreen(prefs) { paired = true }; return }
    val ctx = LocalContext.current
    val db = remember { Db(ctx) }
    val api = remember { Api(prefs) }
    val initialCaps = remember { CapsStore.current(ctx) }
    val capsState by CapsStore.flow.collectAsState()
    val caps: Caps = capsState ?: initialCaps
    val stack = remember { mutableStateListOf<Route>() }
    var tab by remember { mutableStateOf("photos") }
    var askedNotify by remember { mutableStateOf(false) }
    val notifyLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { }

    LaunchedEffect(Unit) {
        UploadWorker.schedule(ctx, prefs)
        withContext(Dispatchers.IO) { try { CapsStore.refresh(ctx) } catch (e: Exception) { } }
    }
    LaunchedEffect(caps.v2) {
        if (caps.v2) {
            PlayerConnection.connect(ctx)
            if (Build.VERSION.SDK_INT >= 33 && !askedNotify &&
                ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                askedNotify = true
                notifyLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
            }
        }
    }

    val tabs = tabsFor(caps)
    val current = if (tabs.any { it.id == tab }) tab else tabs.first().id
    fun push(r: Route) { stack.add(r) }
    fun pop() { if (stack.isNotEmpty()) stack.removeAt(stack.size - 1) }
    // Android convention: back pops a pushed screen, then returns to the first tab, then exits.
    BackHandler(enabled = stack.isNotEmpty()) { pop() }
    BackHandler(enabled = stack.isEmpty() && current != tabs.first().id) { tab = tabs.first().id }

    val top: Route? = stack.lastOrNull()
    if (top != null && top.fullScreen) {
        when (top) {
            is Route.Viewer -> ViewerScreen(api, top.item) { pop() }
            is Route.Video -> VideoPlayerScreen(top.spec) { pop() }
            is Route.NowPlaying -> NowPlayingScreen { pop() }
            else -> {}
        }
        return
    }
    val pushFn: (Route) -> Unit = { r -> push(r) }
    val popFn: () -> Unit = { pop() }
    Scaffold(bottomBar = {
        Column {
            if (caps.v2) MiniPlayer { push(Route.NowPlaying) }
            NavigationBar {
                for (t in tabs) {
                    NavigationBarItem(
                        selected = current == t.id,
                        onClick = { tab = t.id; stack.clear() },
                        icon = { Icon(t.icon, contentDescription = null) },
                        label = { Text(t.label) },
                    )
                }
            }
        }
    }) { pad ->
        Box(Modifier.padding(pad).fillMaxSize()) {
            if (top != null) {
                RouteScreen(top, pushFn, popFn)
            } else {
                when (current) {
                    "photos" -> PhotosTab(prefs, db, api, caps.has("gallery") || !caps.v2) { item ->
                        if (item.video && caps.v2) push(Route.Video(VideoSpec(item.path, item.path.substringAfterLast('/'), "", null, emptyList(), "")))
                        else push(Route.Viewer(item))
                    }
                    "music" -> MusicTab(caps, pushFn)
                    "video" -> VideoTab(caps, pushFn)
                    "books" -> BooksTab(pushFn)
                    else -> SettingsScreen(prefs, db) {
                        PlayerConnection.clear()
                        prefs.unpair(); CapsStore.clear(ctx); stack.clear(); paired = false
                    }
                }
            }
        }
    }
}

/** @brief The pushed (non-full-screen) screens. */
@Composable
private fun RouteScreen(r: Route, push: (Route) -> Unit, pop: () -> Unit) {
    when (r) {
        is Route.AlbumR -> AlbumScreen(r.album, pop)
        is Route.ArtistR -> ArtistScreen(r.name, push, pop)
        is Route.GenreR -> GenreScreen(r.name, pop)
        is Route.PlaylistR -> PlaylistScreen(r.id, r.name, pop)
        is Route.OfflineR -> OfflineGroupScreen(r.id, r.name, pop)
        is Route.ShowR -> ShowScreen(r.show, push, pop)
        is Route.SeasonR -> SeasonScreen(r.show, r.season, push, pop)
        is Route.MovieR -> MovieScreen(r.movie, push, pop)
        is Route.BookR -> BookScreen(r.book, pop)
        else -> {}
    }
}
