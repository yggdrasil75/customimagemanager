@file:OptIn(ExperimentalMaterial3Api::class)

package app.cim.family.ui

import android.app.Activity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.IntentSenderRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Slider
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import app.cim.family.AccountException
import app.cim.family.Api
import app.cim.family.CapsStore
import app.cim.family.Crypto
import app.cim.family.Db
import app.cim.family.Images
import app.cim.family.Prefs
import app.cim.family.Remote
import app.cim.family.UploadWorker
import app.cim.family.media.Downloads
import app.cim.family.media.MediaCache
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

// -- settings / status -------------------------------------------------------
@Composable
fun SettingsScreen(prefs: Prefs, db: Db, onUnpair: () -> Unit) {
    val ctx = LocalContext.current
    var wifi by remember { mutableStateOf(prefs.wifiOnly) }
    var charging by remember { mutableStateOf(prefs.chargingOnly) }
    var videos by remember { mutableStateOf(prefs.uploadVideos) }
    var paused by remember { mutableStateOf(prefs.paused) }
    var tick by remember { mutableStateOf(0) }
    var pingMsg by remember { mutableStateOf("") }
    var account by remember { mutableStateOf(prefs.accountLabel()) }
    val caps by CapsStore.flow.collectAsState()
    val scope = rememberCoroutineScope()
    LaunchedEffect(Unit) {
        val err = withContext(Dispatchers.IO) { runCatching { Api(prefs).ping() }.exceptionOrNull() }
        account = prefs.accountLabel()
        if (err is AccountException) pingMsg = "Failed: ${err.message}"
    }
    val counts = remember(tick) { db.counts() }
    val purgeable = remember(tick) { db.purgeCandidates() }
    var pendingPurge by remember { mutableStateOf<List<Long>>(emptyList()) }
    val purgeLauncher = rememberLauncherForActivityResult(ActivityResultContracts.StartIntentSenderForResult()) { res ->
        if (res.resultCode == Activity.RESULT_OK) { db.markPurged(pendingPurge); tick++ }
        pendingPurge = emptyList()
    }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Text("Server", fontSize = 18.sp, fontWeight = FontWeight.Bold)
        Text("${prefs.serverName} | ${prefs.serverUrl}\nThis phone is peer \"${prefs.deviceName}\" there", color = Color.LightGray)
        if (account.isNotEmpty()) Text(account, color = Color.LightGray)
        val c = caps
        if (c != null) {
            val mods = c.modules.filterValues { it }.keys.sorted().joinToString(", ")
            Text(if (c.v2) "Phone API v${c.api}: $mods" else "Older server (photos only) - update it for music, video and books.",
                color = Color.Gray, fontSize = 12.sp)
        }
        Text("Server key ${Crypto.fingerprint(prefs.serverPub ?: ByteArray(32))}\nThis phone ${Crypto.fingerprint(prefs.publicKey)}", fontFamily = FontFamily.Monospace, fontSize = 11.sp, color = Color.Gray)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(onClick = { scope.launch { pingMsg = withContext(Dispatchers.IO) { runCatching {
                val j = Api(prefs).ping()
                runCatching { CapsStore.refresh(ctx) }
                val pub = j.optString("pub_key")
                if (pub.isNotEmpty() && !Crypto.b64d(pub).contentEquals(prefs.serverPub ?: ByteArray(0))) "KEY MISMATCH: server at that URL has ${j.optString("fingerprint")}. Re-pair before trusting it."
                else "Reached ${j.optString("name")} | key matches"
            }.getOrElse { "Failed: ${it.message}" } }; account = prefs.accountLabel() } }) { Text("Test connection") }
            OutlinedButton(onClick = onUnpair, colors = ButtonDefaults.outlinedButtonColors(contentColor = MaterialTheme.colorScheme.error)) { Text("Unpair") }
        }
        if (pingMsg.isNotEmpty()) Text(pingMsg, color = if (pingMsg.startsWith("KEY") || pingMsg.startsWith("Failed")) MaterialTheme.colorScheme.error else Ok, fontSize = 13.sp)

        if (c != null && c.v2) {
            HorizontalDivider()
            MediaSettings(prefs)
        }

        HorizontalDivider()
        Text("Backup", fontSize = 18.sp, fontWeight = FontWeight.Bold)
        val reverify = remember(tick) { db.unverifiedCount() }
        if (reverify > 0) Text("$reverify earlier upload(s) queued to be re-checked against the original " +
            "(older versions of this app could upload an altered copy; mismatches are re-sent and replace the server's copy).",
            color = Warn, fontSize = 12.sp)
        Text("${counts.first} uploaded | ${counts.second} failing | last run ${if (prefs.lastRun > 0) SimpleDateFormat("d MMM HH:mm", Locale.getDefault()).format(Date(prefs.lastRun)) else "never"}", color = Color.LightGray, fontSize = 13.sp)
        if (prefs.lastError.isNotEmpty()) Text(prefs.lastError, color = MaterialTheme.colorScheme.error, fontSize = 12.sp)
        ToggleRow("Only on Wi-Fi", wifi) { wifi = it; prefs.wifiOnly = it; UploadWorker.schedule(ctx, prefs) }
        ToggleRow("Only while charging", charging) { charging = it; prefs.chargingOnly = it; UploadWorker.schedule(ctx, prefs) }
        ToggleRow("Include videos", videos) { videos = it; prefs.uploadVideos = it }
        ToggleRow("Pause backup", paused) { paused = it; prefs.paused = it }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = { UploadWorker.runNow(ctx); tick++ }) { Text("Back up now") }
            Button(onClick = {
                pendingPurge = purgeable
                val pi = UploadWorker.purgeIntent(ctx, purgeable)
                if (pi != null) purgeLauncher.launch(IntentSenderRequest.Builder(pi.intentSender).build()) else { db.markPurged(purgeable); tick++ }
            }, enabled = purgeable.isNotEmpty()) { Text("Free up space (${purgeable.size})") }
        }
        Text("Free up space removes photos from THIS PHONE that are already safely on the server, from folders set to \"Upload & purge\". Android asks you to confirm the batch.", color = Color.Gray, fontSize = 12.sp)
        val fails = remember(tick) { db.failures() }
        if (fails.isNotEmpty()) {
            Text("Failing items", fontWeight = FontWeight.SemiBold)
            fails.forEach { (id, n, e) -> Text("#$id | $n attempts | $e", fontSize = 11.sp, color = Color.Gray) }
        }

        HorizontalDivider()
        Text("Away from home", fontSize = 18.sp, fontWeight = FontWeight.Bold)
        var remote by remember { mutableStateOf(prefs.remoteEnabled) }
        var linkMsg by remember { mutableStateOf(prefs.linkStatus) }
        Text("When ${prefs.serverUrl} doesn't answer, the app finds the server through its gateways and the BitTorrent DHT " +
             "and connects to it directly, end-to-end encrypted. Nothing passes through anyone else's server. " +
             "Needs the Remote access module on the server; turn off \"Only on Wi-Fi\" to back up over mobile data.",
            color = Color.Gray, fontSize = 12.sp)
        ToggleRow("Reach the server from anywhere", remote) { remote = it; prefs.remoteEnabled = it; Remote.recheck() }
        if (linkMsg.isNotEmpty()) Text(linkMsg, color = if (linkMsg.startsWith("No link") || linkMsg.startsWith("Failed")) MaterialTheme.colorScheme.error else Color.LightGray, fontSize = 12.sp)
        OutlinedButton(onClick = { scope.launch { linkMsg = "Connecting..."; linkMsg = withContext(Dispatchers.IO) {
            runCatching { Remote.test(prefs) }.getOrElse { "Failed: ${it.message}" } } } }, enabled = remote) { Text("Test remote link") }

        HorizontalDivider()
        Text("This phone's pairing code", fontSize = 18.sp, fontWeight = FontWeight.Bold)
        Text("Paste on the server if you ever need to re-pair (rotating the server's key, a restored server).", color = Color.Gray, fontSize = 12.sp)
        QrImage(prefs.myPairingCode())
        SelectionContainer { Text(prefs.myPairingCode(), fontFamily = FontFamily.Monospace, fontSize = 9.sp) }
        OutlinedButton(onClick = { ThumbCache.clear(ctx); Images.clear(ctx) }) { Text("Clear cached thumbnails / covers / media") }
        Spacer(Modifier.height(24.dp))
    }
}

/** @brief Read-ahead, cache and download settings (v2 servers). */
@Composable
private fun MediaSettings(prefs: Prefs) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var audioMin by remember { mutableStateOf(prefs.audioAheadMin.toFloat()) }
    var videoMin by remember { mutableStateOf(prefs.videoAheadMin.toFloat()) }
    var cacheMb by remember { mutableStateOf(prefs.cacheMb) }
    var cellStream by remember { mutableStateOf(prefs.cellularStreaming) }
    var cellPrefetch by remember { mutableStateOf(prefs.cellularPrefetch) }
    var dlWifi by remember { mutableStateOf(prefs.downloadsWifiOnly) }
    var usage by remember { mutableStateOf("") }
    var tick by remember { mutableStateOf(0) }
    LaunchedEffect(tick) {
        usage = withContext(Dispatchers.IO) {
            try { "Streaming cache ${formatBytes(MediaCache.lruBytes(ctx))} | downloads ${formatBytes(MediaCache.offlineBytes(ctx))}" }
            catch (e: Exception) { "" }
        }
    }
    Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("Music & video", fontSize = 18.sp, fontWeight = FontWeight.Bold)
        Text("Read ahead: music ${audioMin.toInt()} min", fontSize = 14.sp)
        Slider(value = audioMin, onValueChange = { audioMin = it }, valueRange = 0f..120f, steps = 23,
            onValueChangeFinished = { prefs.audioAheadMin = audioMin.toInt() })
        Text("Read ahead: video ${videoMin.toInt()} min", fontSize = 14.sp)
        Slider(value = videoMin, onValueChange = { videoMin = it }, valueRange = 0f..60f, steps = 11,
            onValueChangeFinished = { prefs.videoAheadMin = videoMin.toInt() })
        Text("The queue (music) or the rest of the video is fetched ahead into the cache, so playback rides out " +
            "gaps in the connection.", color = Color.Gray, fontSize = 12.sp)
        Text("Cache size (applies after restarting the app)", fontSize = 14.sp)
        ChipRow(listOf(256, 512, 1024, 2048, 4096).map { it.toString() to (if (it >= 1024) "${it / 1024} GB" else "$it MB") },
            cacheMb.toString()) { v -> val n = v.toIntOrNull() ?: 1024; cacheMb = n; prefs.cacheMb = n }
        ToggleRow("Stream over mobile data", cellStream) { cellStream = it; prefs.cellularStreaming = it }
        ToggleRow("Read ahead over mobile data", cellPrefetch) { cellPrefetch = it; prefs.cellularPrefetch = it }
        ToggleRow("Downloads only on Wi-Fi", dlWifi) { dlWifi = it; prefs.downloadsWifiOnly = it; Downloads.start(ctx, replace = true) }
        if (usage.isNotEmpty()) Text(usage, color = Color.LightGray, fontSize = 12.sp)
        OutlinedButton(onClick = {
            scope.launch {
                withContext(Dispatchers.IO) { try { MediaCache.clearLru(ctx) } catch (e: Exception) { } }
                Images.clear(ctx)
                tick++
            }
        }) { Text("Clear cache (downloads stay)") }
    }
}
