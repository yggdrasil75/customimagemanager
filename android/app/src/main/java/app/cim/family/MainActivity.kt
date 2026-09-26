package app.cim.family

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.os.Build
import android.os.Bundle
import android.util.LruCache
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.IntentSenderRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
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
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
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
import androidx.core.content.FileProvider
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

// ── thumbnails: decrypted once, cached in app-private storage ────────────────
object ThumbCache {
    private val mem = object : LruCache<String, Bitmap>(64 * 1024 * 1024) {
        override fun sizeOf(key: String, value: Bitmap) = value.byteCount
    }
    private fun file(ctx: android.content.Context, path: String): File {
        val h = MessageDigest.getInstance("SHA-1").digest(path.toByteArray()).joinToString("") { "%02x".format(it) }
        return File(ctx.cacheDir, "thumbs").also { it.mkdirs() }.resolve("$h.jpg")
    }
    suspend fun get(ctx: android.content.Context, api: Api, path: String): Bitmap? = withContext(Dispatchers.IO) {
        mem.get(path)?.let { return@withContext it }
        val f = file(ctx, path)
        val bytes = if (f.exists()) f.readBytes() else runCatching { api.thumb(path) }.getOrNull()?.also { f.writeBytes(it) } ?: return@withContext null
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size)?.also { mem.put(path, it) }
    }
    fun clear(ctx: android.content.Context) { mem.evictAll(); File(ctx.cacheDir, "thumbs").deleteRecursively(); File(ctx.cacheDir, "media").deleteRecursively() }
}

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val prefs = Prefs(this)
        if (prefs.deviceName.isEmpty()) prefs.deviceName = "phone-" + (Build.MODEL ?: "android").lowercase().replace(Regex("[^a-z0-9]+"), "-").trim('-').take(20)
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(primary = Color(0xFF8B8BF5), background = Color(0xFF101014), surface = Color(0xFF17171D))) {
                Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) { App(prefs) }
            }
        }
    }
}

@Composable
fun App(prefs: Prefs) {
    var paired by remember { mutableStateOf(prefs.paired) }
    if (!paired) { SetupScreen(prefs) { paired = true } ; return }
    val ctx = LocalContext.current
    val db = remember { Db(ctx) }
    val api = remember { Api(prefs) }
    var tab by remember { mutableStateOf(0) }
    var viewing by remember { mutableStateOf<Api.Item?>(null) }
    LaunchedEffect(Unit) { UploadWorker.schedule(ctx, prefs) }
    viewing?.let { ViewerScreen(api, it) { viewing = null }; return }
    Scaffold(bottomBar = {
        NavigationBar {
            NavigationBarItem(tab == 0, { tab = 0 }, { Icon(Icons.Default.PhotoLibrary, null) }, label = { Text("Library") })
            NavigationBarItem(tab == 1, { tab = 1 }, { Icon(Icons.Default.Folder, null) }, label = { Text("Backup") })
            NavigationBarItem(tab == 2, { tab = 2 }, { Icon(Icons.Default.Settings, null) }, label = { Text("Settings") })
        }
    }) { pad ->
        Box(Modifier.padding(pad)) {
            when (tab) {
                0 -> TimelineScreen(api) { viewing = it }
                1 -> FoldersScreen(prefs, db)
                else -> SettingsScreen(prefs, db) { prefs.unpair(); paired = false }
            }
        }
    }
}

// ── setup: paste the server's pairing code, hand over ours ──────────────────
@Composable
fun SetupScreen(prefs: Prefs, onPaired: () -> Unit) {
    var code by remember { mutableStateOf("") }
    var name by remember { mutableStateOf(prefs.deviceName) }
    var err by remember { mutableStateOf("") }
    var mine by remember { mutableStateOf<String?>(null) }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text("CIM Family", fontSize = 26.sp, fontWeight = FontWeight.Bold)
        Text("End-to-end encrypted backup of this phone's photos to your own image manager, and a gallery of everything on it.", color = Color.Gray)
        OutlinedTextField(name, { name = it.lowercase().replace(Regex("[^a-z0-9._-]"), "-") }, label = { Text("This phone's name (as the server will know it)") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        Text("1. On the server: Settings → Family share → add a peer of kind \"my phone\" with this name, then click \"Pairing code for them\" and paste it here.", color = Color.LightGray)
        OutlinedTextField(code, { code = it }, label = { Text("Server pairing code (fs1.…)") }, modifier = Modifier.fillMaxWidth(), minLines = 3)
        if (err.isNotEmpty()) Text(err, color = MaterialTheme.colorScheme.error)
        Button({
            try { prefs.deviceName = name; prefs.applyPairing(code); mine = prefs.myPairingCode(); err = "" }
            catch (e: Exception) { err = e.message ?: "bad code" }
        }, enabled = name.isNotEmpty() && code.isNotEmpty()) { Text("Pair with server") }
        mine?.let { m ->
            Divider()
            Text("2. Paste THIS phone's pairing code into that peer's row on the server (the pairing box), then tap Done.", color = Color.LightGray)
            Text("Fingerprint ${Crypto.fingerprint(prefs.publicKey)} — the server shows the same after pasting.", fontFamily = FontFamily.Monospace, fontSize = 12.sp)
            QrImage(m)
            SelectionContainer { Text(m, fontFamily = FontFamily.Monospace, fontSize = 10.sp) }
            val ctx = LocalContext.current
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton({ ctx.startActivity(Intent.createChooser(Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, m), "Send pairing code")) }) { Text("Share code") }
                Button({ onPaired() }) { Text("Done") }
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

// ── timeline: the server's library, newest first, grouped by day ────────────
@Composable
fun TimelineScreen(api: Api, onOpen: (Api.Item) -> Unit) {
    var items by remember { mutableStateOf<List<Api.Item>>(emptyList()) }
    var total by remember { mutableStateOf(0) }
    var err by remember { mutableStateOf("") }
    var loading by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    fun loadMore() {
        if (loading || (items.isNotEmpty() && items.size >= total)) return
        loading = true
        scope.launch {
            try {
                val (page, n) = withContext(Dispatchers.IO) { api.timeline(items.size, 300) }
                items = items + page; total = n; err = ""
            } catch (e: Exception) { err = e.message ?: e.toString() } finally { loading = false }
        }
    }
    LaunchedEffect(Unit) { loadMore() }
    val fmt = remember { SimpleDateFormat("EEEE, d MMMM yyyy", Locale.getDefault()) }
    val groups = remember(items) { items.groupBy { fmt.format(Date((it.t * 1000).toLong())) } }
    Column {
        if (err.isNotEmpty()) Text(err, color = MaterialTheme.colorScheme.error, modifier = Modifier.padding(8.dp))
        LazyVerticalGrid(GridCells.Adaptive(110.dp), Modifier.fillMaxSize(), horizontalArrangement = Arrangement.spacedBy(2.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            groups.forEach { (day, its) ->
                item(span = { GridItemSpan(maxLineSpan) }) { Text(day, Modifier.padding(10.dp, 12.dp, 10.dp, 4.dp), fontWeight = FontWeight.SemiBold) }
                items(its, key = { it.path }) { it ->
                    Tile(api, it) { onOpen(it) }
                }
            }
            item(span = { GridItemSpan(maxLineSpan) }) {
                LaunchedEffect(items.size) { loadMore() }
                Box(Modifier.fillMaxWidth().height(48.dp), contentAlignment = Alignment.Center) {
                    if (loading) CircularProgressIndicator(Modifier.size(24.dp)) else if (items.isEmpty()) Text("Nothing on the server yet", color = Color.Gray)
                }
            }
        }
    }
}

@Composable
fun Tile(api: Api, item: Api.Item, onClick: () -> Unit) {
    val ctx = LocalContext.current
    val bmp by produceState<Bitmap?>(null, item.path) { value = ThumbCache.get(ctx, api, item.path) }
    Box(Modifier.aspectRatio(1f).background(Color(0xFF202028)).clickable(onClick = onClick)) {
        bmp?.let { Image(it.asImageBitmap(), null, Modifier.fillMaxSize(), contentScale = ContentScale.Crop) }
        if (item.video) Icon(Icons.Default.PlayCircle, null, Modifier.align(Alignment.TopEnd).padding(4.dp).size(20.dp), tint = Color.White)
    }
}

// ── viewer: images inline, videos handed to the system player ───────────────
@Composable
fun ViewerScreen(api: Api, item: Api.Item, onBack: () -> Unit) {
    val ctx = LocalContext.current
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
            } catch (e: Exception) { err = e.message ?: e.toString() }
        }
    }
    Box(Modifier.fillMaxSize().background(Color.Black)) {
        bmp?.let { Image(it.asImageBitmap(), null, Modifier.fillMaxSize(), contentScale = ContentScale.Fit) }
        if (bmp == null && err.isEmpty()) CircularProgressIndicator(Modifier.align(Alignment.Center))
        if (err.isNotEmpty()) Text(err, Modifier.align(Alignment.Center).padding(24.dp), color = Color.White)
        IconButton(onBack, Modifier.align(Alignment.TopStart).padding(8.dp)) { Icon(Icons.Default.ArrowBack, "back", tint = Color.White) }
        Text(item.path, Modifier.align(Alignment.BottomStart).padding(12.dp), color = Color.LightGray, fontSize = 11.sp)
    }
}

// ── backup folders: per bucket, off / keep / upload & purge ─────────────────
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
            Text("The app needs to read your photos to back them up.")
            Button({ ask.launch(mediaPermissions()) }) { Text("Allow photo access") }
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
                        Text("${b.count} items · ${b.uploaded} backed up", color = Color.Gray, fontSize = 12.sp)
                    }
                }
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp), modifier = Modifier.padding(top = 4.dp)) {
                    for ((v, label) in listOf("off" to "Off", "keep" to "Keep", "purge" to "Upload & purge")) {
                        FilterChip(pol == v, { db.setPolicy(b.id, b.name, v); policies = db.policies(); UploadWorker.runNow(ctx) }, { Text(label) })
                    }
                }
            }
            Divider(color = Color(0xFF26262E))
        }
    }
}

fun mediaPermissions(): Array<String> = if (Build.VERSION.SDK_INT >= 33) arrayOf(Manifest.permission.READ_MEDIA_IMAGES, Manifest.permission.READ_MEDIA_VIDEO)
    else arrayOf(Manifest.permission.READ_EXTERNAL_STORAGE)

fun hasMediaPermission(ctx: android.content.Context): Boolean = mediaPermissions().all {
    androidx.core.content.ContextCompat.checkSelfPermission(ctx, it) == android.content.pm.PackageManager.PERMISSION_GRANTED
}

// ── settings / status ───────────────────────────────────────────────────────
@Composable
fun SettingsScreen(prefs: Prefs, db: Db, onUnpair: () -> Unit) {
    val ctx = LocalContext.current
    var wifi by remember { mutableStateOf(prefs.wifiOnly) }
    var charging by remember { mutableStateOf(prefs.chargingOnly) }
    var videos by remember { mutableStateOf(prefs.uploadVideos) }
    var paused by remember { mutableStateOf(prefs.paused) }
    var tick by remember { mutableStateOf(0) }
    var pingMsg by remember { mutableStateOf("") }
    val scope = rememberCoroutineScope()
    val counts = remember(tick) { db.counts() }
    val purgeable = remember(tick) { db.purgeCandidates() }
    var pendingPurge by remember { mutableStateOf<List<Long>>(emptyList()) }
    val purgeLauncher = rememberLauncherForActivityResult(ActivityResultContracts.StartIntentSenderForResult()) { res ->
        if (res.resultCode == Activity.RESULT_OK) { db.markPurged(pendingPurge); tick++ }
        pendingPurge = emptyList()
    }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Text("Server", fontSize = 18.sp, fontWeight = FontWeight.Bold)
        Text("${prefs.serverName} · ${prefs.serverUrl}", color = Color.LightGray)
        Text("Server key ${Crypto.fingerprint(prefs.serverPub ?: ByteArray(32))}\nThis phone ${Crypto.fingerprint(prefs.publicKey)}", fontFamily = FontFamily.Monospace, fontSize = 11.sp, color = Color.Gray)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton({ scope.launch { pingMsg = withContext(Dispatchers.IO) { runCatching {
                val j = Api(prefs).ping()
                val pub = j.optString("pub_key")
                if (pub.isNotEmpty() && !Crypto.b64d(pub).contentEquals(prefs.serverPub ?: ByteArray(0))) "KEY MISMATCH: server at that URL has ${j.optString("fingerprint")}. Re-pair before trusting it."
                else "Reached ${j.optString("name")} · key matches"
            }.getOrElse { "Failed: ${it.message}" } } } }) { Text("Test connection") }
            OutlinedButton(onUnpair, colors = ButtonDefaults.outlinedButtonColors(contentColor = MaterialTheme.colorScheme.error)) { Text("Unpair") }
        }
        if (pingMsg.isNotEmpty()) Text(pingMsg, color = if (pingMsg.startsWith("KEY") || pingMsg.startsWith("Failed")) MaterialTheme.colorScheme.error else Color(0xFF7BD88F), fontSize = 13.sp)

        Divider()
        Text("Backup", fontSize = 18.sp, fontWeight = FontWeight.Bold)
        Text("${counts.first} uploaded · ${counts.second} failing · last run ${if (prefs.lastRun > 0) SimpleDateFormat("d MMM HH:mm", Locale.getDefault()).format(Date(prefs.lastRun)) else "never"}", color = Color.LightGray, fontSize = 13.sp)
        if (prefs.lastError.isNotEmpty()) Text(prefs.lastError, color = MaterialTheme.colorScheme.error, fontSize = 12.sp)
        ToggleRow("Only on Wi-Fi", wifi) { wifi = it; prefs.wifiOnly = it; UploadWorker.schedule(ctx, prefs) }
        ToggleRow("Only while charging", charging) { charging = it; prefs.chargingOnly = it; UploadWorker.schedule(ctx, prefs) }
        ToggleRow("Include videos", videos) { videos = it; prefs.uploadVideos = it }
        ToggleRow("Pause backup", paused) { paused = it; prefs.paused = it }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button({ UploadWorker.runNow(ctx); tick++ }) { Text("Back up now") }
            Button({
                pendingPurge = purgeable
                val pi = UploadWorker.purgeIntent(ctx, purgeable)
                if (pi != null) purgeLauncher.launch(IntentSenderRequest.Builder(pi.intentSender).build()) else { db.markPurged(purgeable); tick++ }
            }, enabled = purgeable.isNotEmpty()) { Text("Free up space (${purgeable.size})") }
        }
        Text("Free up space removes photos from THIS PHONE that are already safely on the server, from folders set to \"Upload & purge\". Android asks you to confirm the batch.", color = Color.Gray, fontSize = 12.sp)
        val fails = remember(tick) { db.failures() }
        if (fails.isNotEmpty()) {
            Text("Failing items", fontWeight = FontWeight.SemiBold)
            fails.forEach { (id, n, e) -> Text("#$id · $n attempts · $e", fontSize = 11.sp, color = Color.Gray) }
        }

        Divider()
        Text("This phone's pairing code", fontSize = 18.sp, fontWeight = FontWeight.Bold)
        Text("Paste on the server if you ever need to re-pair (rotating the server's key, a restored server).", color = Color.Gray, fontSize = 12.sp)
        QrImage(prefs.myPairingCode())
        SelectionContainer { Text(prefs.myPairingCode(), fontFamily = FontFamily.Monospace, fontSize = 9.sp) }
        OutlinedButton({ ThumbCache.clear(ctx) }) { Text("Clear cached thumbnails / media") }
        Spacer(Modifier.height(24.dp))
    }
}

@Composable
fun ToggleRow(label: String, value: Boolean, onChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Text(label, Modifier.weight(1f)); Switch(value, onChange)
    }
}
