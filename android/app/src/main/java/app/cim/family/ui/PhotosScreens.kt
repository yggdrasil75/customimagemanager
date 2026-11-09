@file:OptIn(ExperimentalMaterial3Api::class)

package app.cim.family.ui

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.os.Build
import android.util.LruCache
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.PhoneAndroid
import androidx.compose.material.icons.filled.PlayCircle
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import app.cim.family.Api
import app.cim.family.Bucket
import app.cim.family.Crypto
import app.cim.family.Db
import app.cim.family.OfflinePhotos
import app.cim.family.PhotoSearch
import app.cim.family.Prefs
import app.cim.family.Scanner
import app.cim.family.UploadWorker
import com.google.zxing.BarcodeFormat
import com.google.zxing.qrcode.QRCodeWriter
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import java.security.MessageDigest
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

// -- thumbnails: decrypted once, cached in app-private storage ----------------
object ThumbCache {
    private val mem = object : LruCache<String, Bitmap>(64 * 1024 * 1024) {
        override fun sizeOf(key: String, value: Bitmap): Int = value.byteCount
    }
    private fun file(ctx: Context, path: String): File {
        val h = MessageDigest.getInstance("SHA-1").digest(path.toByteArray()).joinToString("") { "%02x".format(it) }
        return File(ctx.cacheDir, "thumbs").also { it.mkdirs() }.resolve("$h.jpg")
    }
    suspend fun get(ctx: Context, api: Api, path: String, offline: Boolean = false): Bitmap? = withContext(Dispatchers.IO) {
        mem.get(path)?.let { return@withContext it }
        val f = file(ctx, path)
        val bytes = if (f.exists()) f.readBytes()
                    else if (offline) return@withContext null          // no server: only what is cached
                    else runCatching { api.thumb(path) }.getOrNull()?.also { f.writeBytes(it) } ?: return@withContext null
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size)?.also { mem.put(path, it) }
    }
    /** @brief The cached thumbnail only, never the network (blocking; any thread but main). */
    fun cachedBlocking(ctx: Context, path: String): Bitmap? {
        mem.get(path)?.let { return it }
        val f = file(ctx, path)
        if (!f.exists()) return null
        val b = runCatching { f.readBytes() }.getOrNull() ?: return null
        return BitmapFactory.decodeByteArray(b, 0, b.size)
    }
    /** @brief True when this library path's thumbnail is on the phone (memory or disk). */
    fun hasCached(ctx: Context, path: String): Boolean = mem.get(path) != null || file(ctx, path).exists()
    fun clear(ctx: Context) { mem.evictAll(); File(ctx.cacheDir, "thumbs").deleteRecursively(); File(ctx.cacheDir, "media").deleteRecursively() }
}

// -- setup: paste the server's pairing code, hand over ours ------------------
@Composable
fun SetupScreen(prefs: Prefs, onPaired: () -> Unit) {
    var code by remember { mutableStateOf("") }
    var name by remember { mutableStateOf(prefs.deviceName) }
    var err by remember { mutableStateOf("") }
    var mine by remember { mutableStateOf<String?>(null) }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text("CIM Family", fontSize = 26.sp, fontWeight = FontWeight.Bold)
        Text("End-to-end encrypted backup of this phone's photos to your own image manager, and your library - photos, music, video and books - on the phone.", color = Color.Gray)
        OutlinedTextField(name, { name = it.lowercase().replace(Regex("[^a-z0-9._-]"), "-") }, label = { Text("This phone's name (the server's pairing code overrides it)") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        Text("1. On the server, signed in as yourself: Settings → My devices → add this phone, then click \"Code for the app\" and paste it here. The phone acts as that account. The code carries the name the server gave this phone.", color = Color.LightGray)
        OutlinedTextField(code, { code = it }, label = { Text("Server pairing code (fs1....)") }, modifier = Modifier.fillMaxWidth(), minLines = 3)
        if (err.isNotEmpty()) Text(err, color = MaterialTheme.colorScheme.error)
        Button(onClick = {
            try { prefs.deviceName = name; prefs.applyPairing(code); name = prefs.deviceName; mine = prefs.myPairingCode(); err = "" }
            catch (e: Exception) { err = e.message ?: "bad code" }
        }, enabled = name.isNotEmpty() && code.isNotEmpty()) { Text("Pair with server") }
        val m = mine
        if (m != null) {
            BackHandler { mine = null }
            HorizontalDivider()
            Text("2. Paste THIS phone's pairing code into that peer's row on the server (the pairing box), then tap Done.", color = Color.LightGray)
            Text("Fingerprint ${Crypto.fingerprint(prefs.publicKey)} - the server shows the same after pasting.", fontFamily = FontFamily.Monospace, fontSize = 12.sp)
            QrImage(m)
            SelectionContainer { Text(m, fontFamily = FontFamily.Monospace, fontSize = 10.sp) }
            val ctx = LocalContext.current
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = { ctx.startActivity(Intent.createChooser(Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, m), "Send pairing code")) }) { Text("Share code") }
                Button(onClick = { onPaired() }) { Text("Done") }
            }
        }
    }
}

@Composable
fun QrImage(text: String) {
    val bmp = remember(text) {
        val m = QRCodeWriter().encode(text, BarcodeFormat.QR_CODE, 512, 512)
        Bitmap.createBitmap(512, 512, Bitmap.Config.RGB_565).also { b ->
            for (x in 0 until 512) for (y in 0 until 512) b.setPixel(x, y, if (m[x, y]) android.graphics.Color.BLACK else android.graphics.Color.WHITE)
        }
    }
    Image(bmp.asImageBitmap(), "pairing QR", Modifier.fillMaxWidth().height(220.dp), alignment = Alignment.Center)
}

/** @brief The Photos tab: the server's library (when the account may browse it) and backup folders. */
@Composable
fun PhotosTab(prefs: Prefs, db: Db, api: Api, galleryOn: Boolean, onOpen: (Api.Item) -> Unit) {
    var sub by remember { mutableStateOf(if (galleryOn) "library" else "backup") }
    val ctx = LocalContext.current
    // a phone item opens in the system viewer; library items in ours
    val open: (Api.Item) -> Unit = { item ->
        if (OfflinePhotos.isLocal(item)) {
            runCatching { ctx.startActivity(UploadWorker.viewIntent(OfflinePhotos.localUri(item), if (item.video) "video/*" else "image/*")) }
        } else onOpen(item)
    }
    Column(Modifier.fillMaxSize()) {
        val opts = if (galleryOn) listOf("library" to "Library", "backup" to "Backup") else listOf("backup" to "Backup")
        ChipRow(opts, sub) { sub = it }
        Box(Modifier.weight(1f)) {
            if (sub == "library" && galleryOn) TimelineScreen(api, open, prefs) else FoldersScreen(prefs, db)
        }
    }
}

// -- timeline: the server's library, newest first, grouped by day ------------
@Composable
fun TimelineScreen(api: Api, onOpen: (Api.Item) -> Unit, prefs: Prefs? = null) {
    val ctx = LocalContext.current
    // search: the server's semantic search, else the on-device index
    var query by remember { mutableStateOf("") }
    var results by remember { mutableStateOf<List<Api.Item>?>(null) }
    var searchNote by remember { mutableStateOf("") }
    var searching by remember { mutableStateOf(false) }
    var items by remember { mutableStateOf<List<Api.Item>>(emptyList()) }
    var total by remember { mutableStateOf(0) }
    var err by remember { mutableStateOf("") }
    var loading by remember { mutableStateOf(false) }
    // offline: the server didn't answer; show cached thumbnails + the phone's own media
    var offline by remember { mutableStateOf(false) }
    var attempt by remember { mutableStateOf(0) }
    val scope = rememberCoroutineScope()
    fun goOffline(why: String) {
        offline = true; err = why
        scope.launch {
            items = withContext(Dispatchers.IO) { OfflinePhotos.offlineItems(ctx, hasMediaPermission(ctx)) }
            total = items.size
        }
    }
    fun loadMore() {
        if (offline || loading || (items.isNotEmpty() && items.size >= total)) return
        loading = true
        scope.launch {
            try {
                val (page, n) = withContext(Dispatchers.IO) { api.timeline(items.size, 300) }
                items = items + page; total = n; err = ""
                val snap = items
                withContext(Dispatchers.IO) { OfflinePhotos.save(ctx, snap, n) }
            } catch (e: Exception) {
                val msg = e.message ?: e.toString()
                if (items.isEmpty()) goOffline(msg) else err = msg
            } finally { loading = false }
        }
    }
    LaunchedEffect(attempt) { loadMore() }
    val fmt = remember { SimpleDateFormat("EEEE, d MMMM yyyy", Locale.getDefault()) }
    val groups = remember(items) { items.groupBy { fmt.format(Date((it.t * 1000).toLong())) } }
    val scopeS = rememberCoroutineScope()
    fun runSearch() {
        val p = prefs ?: return
        val q = query.trim()
        if (q.isEmpty()) { results = null; searchNote = ""; return }
        searching = true
        scopeS.launch {
            try {
                val r = withContext(Dispatchers.IO) { PhotoSearch.search(ctx, p, q, preferPhone = offline) }
                results = r.items
                searchNote = "${r.items.size} match${if (r.items.size == 1) "" else "es"} " +
                             (if (r.source == "phone") "on this phone (offline search)" else "on the server")
            } catch (e: Exception) { results = emptyList(); searchNote = e.message ?: e.toString() }
            finally { searching = false }
        }
    }
    Column {
        if (prefs != null) {
            Row(Modifier.fillMaxWidth().padding(8.dp, 4.dp), verticalAlignment = Alignment.CenterVertically) {
                OutlinedTextField(query, { query = it; if (it.isEmpty()) { results = null; searchNote = "" } },
                    label = { Text("Search photos (what's in them)") }, singleLine = true, modifier = Modifier.weight(1f),
                    keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(imeAction = androidx.compose.ui.text.input.ImeAction.Search),
                    keyboardActions = androidx.compose.foundation.text.KeyboardActions(onSearch = { runSearch() }))
                if (searching) CircularProgressIndicator(Modifier.padding(start = 8.dp).size(20.dp))
            }
            if (searchNote.isNotEmpty()) Text(searchNote, Modifier.padding(horizontal = 10.dp), color = Color.Gray, fontSize = 12.sp)
        }
        val res = results
        if (res != null) {
            LazyVerticalGrid(GridCells.Adaptive(110.dp), Modifier.fillMaxSize(), horizontalArrangement = Arrangement.spacedBy(2.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                items(res, key = { "s:" + it.path }) { it -> Tile(api, it, offline) { onOpen(it) } }
            }
            return@Column
        }
        if (offline) {
            Row(Modifier.fillMaxWidth().padding(8.dp), verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("Offline - the server can't be reached", fontWeight = FontWeight.SemiBold)
                    Text("Showing cached thumbnails and the photos on this phone. ($err)", color = Color.Gray, fontSize = 12.sp)
                }
                OutlinedButton(onClick = { offline = false; err = ""; items = emptyList(); total = 0; attempt++ }) { Text("Retry") }
            }
        } else if (err.isNotEmpty()) Text(err, color = MaterialTheme.colorScheme.error, modifier = Modifier.padding(8.dp))
        LazyVerticalGrid(GridCells.Adaptive(110.dp), Modifier.fillMaxSize(), horizontalArrangement = Arrangement.spacedBy(2.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            groups.forEach { (day, its) ->
                item(span = { GridItemSpan(maxLineSpan) }) { Text(day, Modifier.padding(10.dp, 12.dp, 10.dp, 4.dp), fontWeight = FontWeight.SemiBold) }
                items(its, key = { it.path }) { it ->
                    Tile(api, it, offline) { onOpen(it) }
                }
            }
            item(span = { GridItemSpan(maxLineSpan) }) {
                LaunchedEffect(items.size) { loadMore() }
                Box(Modifier.fillMaxWidth().height(48.dp), contentAlignment = Alignment.Center) {
                    if (loading) CircularProgressIndicator(Modifier.size(24.dp))
                    else if (items.isEmpty()) Text(if (offline) "Nothing cached and no photos on this phone" else "Nothing on the server yet", color = Color.Gray)
                }
            }
        }
    }
}

@Composable
fun Tile(api: Api, item: Api.Item, offline: Boolean = false, onClick: () -> Unit) {
    val ctx = LocalContext.current
    val bmp by produceState<Bitmap?>(null, item.path) {
        value = if (OfflinePhotos.isLocal(item)) withContext(Dispatchers.IO) { OfflinePhotos.localThumb(ctx, item) }
                else ThumbCache.get(ctx, api, item.path, offline)
    }
    Box(Modifier.aspectRatio(1f).background(TileBg).clickable(onClick = onClick)) {
        val b = bmp
        if (b != null) Image(b.asImageBitmap(), null, Modifier.fillMaxSize(), contentScale = ContentScale.Crop)
        if (item.video) Icon(Icons.Default.PlayCircle, null, Modifier.align(Alignment.TopEnd).padding(4.dp).size(20.dp), tint = Color.White)
        if (OfflinePhotos.isLocal(item)) Icon(Icons.Default.PhoneAndroid, "on this phone", Modifier.align(Alignment.BottomEnd).padding(4.dp).size(16.dp), tint = Color.White)
    }
}

// -- viewer: images inline; videos in the in-app player (v2) or the system one (v1)
@Composable
fun ViewerScreen(api: Api, item: Api.Item, onBack: () -> Unit) {
    val ctx = LocalContext.current
    BackHandler { onBack() }                       // system back = close the viewer, not the app
    var bmp by remember { mutableStateOf<Bitmap?>(null) }
    var err by remember { mutableStateOf("") }
    LaunchedEffect(item.path) {
        withContext(Dispatchers.IO) {
            try {
                val dir = File(ctx.cacheDir, "media").also { it.mkdirs() }
                val f = File(dir, MessageDigest.getInstance("SHA-1").digest(item.path.toByteArray()).joinToString("") { "%02x".format(it) } + "." + item.path.substringAfterLast('.', "bin"))
                val mime = if (f.exists()) null else api.media(item.path, f)
                if (item.video) {
                    val uri = FileProvider.getUriForFile(ctx, "app.cim.family.files", f)
                    ctx.startActivity(UploadWorker.viewIntent(uri, mime ?: "video/*"))
                } else {
                    bmp = BitmapFactory.decodeFile(f.path) ?: ThumbCache.get(ctx, api, item.path)
                    if (bmp == null) err = "This format can't be decoded on the phone; showing the thumbnail failed too."
                }
            } catch (e: Exception) {
                // offline: show the cached thumbnail rather than an error
                bmp = ThumbCache.get(ctx, api, item.path, offline = true)
                if (bmp == null) err = e.message ?: e.toString()
            }
        }
    }
    Box(Modifier.fillMaxSize().background(Color.Black)) {
        val b = bmp
        if (b != null) Image(b.asImageBitmap(), null, Modifier.fillMaxSize(), contentScale = ContentScale.Fit)
        if (b == null && err.isEmpty()) CircularProgressIndicator(Modifier.align(Alignment.Center))
        if (err.isNotEmpty()) Text(err, Modifier.align(Alignment.Center).padding(24.dp), color = Color.White)
        IconButton(onClick = onBack, modifier = Modifier.align(Alignment.TopStart).padding(8.dp)) { Icon(Icons.Default.ArrowBack, "back", tint = Color.White) }
        Text(item.path, Modifier.align(Alignment.BottomStart).padding(12.dp), color = Color.LightGray, fontSize = 11.sp)
    }
}

// -- backup folders: per bucket, off / keep / upload & purge -----------------
@Composable
fun FoldersScreen(prefs: Prefs, db: Db) {
    val ctx = LocalContext.current
    var buckets by remember { mutableStateOf<List<Bucket>>(emptyList()) }
    var policies by remember { mutableStateOf(db.policies()) }
    var granted by remember { mutableStateOf(hasMediaPermission(ctx)) }
    val ask = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { granted = hasMediaPermission(ctx) }
    LaunchedEffect(granted) { if (granted) buckets = withContext(Dispatchers.IO) { Scanner.buckets(ctx, db, prefs.uploadVideos) } }
    if (!granted) {
        Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("The app needs to read your photos, including their location data, to back up the originals. " +
                 "Without location access Android only hands out altered copies: GPS is stripped and raw (DNG) files break.")
            Button(onClick = { ask.launch(mediaPermissions()) }) { Text("Allow photo access") }
        }
        return
    }
    LazyColumn(Modifier.fillMaxSize()) {
        item {
            Text("Backup folders", Modifier.padding(16.dp, 16.dp, 16.dp, 4.dp), fontSize = 20.sp, fontWeight = FontWeight.Bold)
            Text("Keep: upload and leave the copy on the phone.  Upload & purge: once the server has it, it can be freed from the phone (Settings → Free up space).",
                Modifier.padding(16.dp, 0.dp, 16.dp, 8.dp), color = Color.Gray, fontSize = 13.sp)
        }
        items(buckets, key = { it.id }) { b ->
            val pol = policies[b.id] ?: "off"
            Column(Modifier.fillMaxWidth().padding(16.dp, 8.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text(b.name, fontWeight = FontWeight.SemiBold)
                        Text("${b.count} items | ${b.uploaded} backed up", color = Color.Gray, fontSize = 12.sp)
                    }
                }
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp), modifier = Modifier.padding(top = 4.dp)) {
                    for ((v, label) in listOf("off" to "Off", "keep" to "Keep", "purge" to "Upload & purge")) {
                        FilterChip(selected = pol == v, onClick = { db.setPolicy(b.id, b.name, v); policies = db.policies(); UploadWorker.runNow(ctx) }, label = { Text(label) })
                    }
                }
            }
            HorizontalDivider(color = Color(0xFF26262E))
        }
    }
}

fun mediaPermissions(): Array<String> = when {
    Build.VERSION.SDK_INT >= 33 -> arrayOf(Manifest.permission.READ_MEDIA_IMAGES, Manifest.permission.READ_MEDIA_VIDEO,
                                           Manifest.permission.ACCESS_MEDIA_LOCATION)
    Build.VERSION.SDK_INT >= 29 -> arrayOf(Manifest.permission.READ_EXTERNAL_STORAGE, Manifest.permission.ACCESS_MEDIA_LOCATION)
    else -> arrayOf(Manifest.permission.READ_EXTERNAL_STORAGE)
}

fun hasMediaPermission(ctx: Context): Boolean = mediaPermissions().all {
    ContextCompat.checkSelfPermission(ctx, it) == PackageManager.PERMISSION_GRANTED
}