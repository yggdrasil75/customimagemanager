@file:OptIn(ExperimentalMaterial3Api::class)

package app.cim.family.ui

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
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Download
import androidx.compose.material.icons.filled.MusicNote
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.SkipNext
import androidx.compose.material.icons.filled.SkipPrevious
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
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
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import app.cim.family.formatDuration
import app.cim.family.media.Downloads
import app.cim.family.media.Items
import app.cim.family.media.PlayerConnection
import app.cim.family.media.Radio
import kotlinx.coroutines.delay

/** @brief The bar above the bottom navigation while something is loaded in the player. */
@Composable
fun MiniPlayer(onOpen: () -> Unit) {
    val s by PlayerConnection.state.collectAsState()
    if (!s.hasItem) return
    var pos by remember { mutableStateOf(0L) }
    LaunchedEffect(s.mediaId, s.isPlaying) {
        while (true) { pos = PlayerConnection.position(); delay(1000L) }
    }
    Column(Modifier.fillMaxWidth().background(CardBg)) {
        val frac = if (s.durationMs > 0) (pos.toFloat() / s.durationMs).coerceIn(0f, 1f) else 0f
        LinearProgressIndicator(progress = { frac }, modifier = Modifier.fillMaxWidth())
        Row(Modifier.fillMaxWidth().clickable(onClick = onOpen).padding(8.dp, 6.dp), verticalAlignment = Alignment.CenterVertically) {
            Cover(s.artKey, Modifier.size(42.dp).clip(RoundedCornerShape(4.dp)), Icons.Default.MusicNote)
            Column(Modifier.weight(1f).padding(horizontal = 10.dp)) {
                Text(s.title, maxLines = 1, overflow = TextOverflow.Ellipsis, fontSize = 14.sp)
                val sub = if (s.error.isNotEmpty()) s.error else s.artist
                Text(sub, maxLines = 1, overflow = TextOverflow.Ellipsis, fontSize = 12.sp,
                    color = if (s.error.isNotEmpty()) MaterialTheme.colorScheme.error else Color.Gray)
            }
            IconButton(onClick = { PlayerConnection.toggle() }) {
                Icon(if (s.isPlaying) Icons.Default.Pause else Icons.Default.PlayArrow, if (s.isPlaying) "pause" else "play")
            }
            IconButton(onClick = { PlayerConnection.next() }) { Icon(Icons.Default.SkipNext, "next") }
        }
    }
}

/** @brief Full-screen player: cover, title, seek bar, transport and the queue. */
@Composable
fun NowPlayingScreen(onBack: () -> Unit) {
    val ctx = LocalContext.current
    BackHandler { onBack() }
    val s by PlayerConnection.state.collectAsState()
    val rs by Radio.state.collectAsState()
    var pos by remember { mutableStateOf(0L) }
    var dragging by remember { mutableStateOf(false) }
    var dragPos by remember { mutableStateOf(0f) }
    LaunchedEffect(Unit) {
        while (true) { if (!dragging) pos = PlayerConnection.position(); delay(500L) }
    }
    val dur = maxOf(s.durationMs, 1L).toFloat()
    LazyColumn(Modifier.fillMaxSize()) {
        item { BackHeader("Now playing", if (s.radio) "Radio" + (if (rs.round > 0) " | round ${rs.round}" else "") else "${s.index + 1} of ${s.count}", onBack) }
        item {
            Box(Modifier.fillMaxWidth().padding(24.dp, 8.dp), contentAlignment = Alignment.Center) {
                Cover(s.artKey, Modifier.widthIn(max = 360.dp).fillMaxWidth().aspectRatio(1f).clip(RoundedCornerShape(10.dp)), Icons.Default.MusicNote)
            }
        }
        item {
            Column(Modifier.fillMaxWidth().padding(horizontal = 24.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                Text(if (s.hasItem) s.title else "Nothing playing", fontSize = 20.sp, fontWeight = FontWeight.Bold, textAlign = TextAlign.Center,
                    maxLines = 2, overflow = TextOverflow.Ellipsis)
                Text(listOf(s.artist, s.album).filter { it.isNotEmpty() }.joinToString(" | "), color = Color.Gray,
                    textAlign = TextAlign.Center, maxLines = 1, overflow = TextOverflow.Ellipsis)
                ErrorText(s.error)
                ErrorText(rs.error)
                Slider(
                    value = (if (dragging) dragPos else pos.toFloat()).coerceIn(0f, dur),
                    onValueChange = { dragging = true; dragPos = it },
                    onValueChangeFinished = { PlayerConnection.seekTo(dragPos.toLong()); pos = dragPos.toLong(); dragging = false },
                    valueRange = 0f..dur,
                    modifier = Modifier.fillMaxWidth(),
                )
                Row(Modifier.fillMaxWidth()) {
                    Text(formatDuration((if (dragging) dragPos.toLong() else pos) / 1000.0).ifEmpty { "0:00" }, fontSize = 12.sp, color = Color.Gray)
                    Box(Modifier.weight(1f))
                    Text(formatDuration(s.durationMs / 1000.0), fontSize = 12.sp, color = Color.Gray)
                }
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                    IconButton(onClick = { PlayerConnection.previous() }) { Icon(Icons.Default.SkipPrevious, "previous", Modifier.size(36.dp)) }
                    IconButton(onClick = { PlayerConnection.toggle() }, modifier = Modifier.size(72.dp)) {
                        Icon(if (s.isPlaying) Icons.Default.Pause else Icons.Default.PlayArrow, if (s.isPlaying) "pause" else "play", Modifier.size(56.dp))
                    }
                    IconButton(onClick = { PlayerConnection.next() }) { Icon(Icons.Default.SkipNext, "next", Modifier.size(36.dp)) }
                }
                if (s.radio) {
                    TextButton(onClick = {
                        val songs = PlayerConnection.queueItems().filter { Items.isRadio(it) }.mapNotNull { Items.songOf(it) }
                        if (songs.isNotEmpty()) Downloads.add(ctx, "radio:" + System.currentTimeMillis(), "Radio round ${rs.round}", songs)
                    }) { Icon(Icons.Default.Download, null); Text(" Download this round") }
                }
            }
        }
        item { Text("Queue", Modifier.padding(16.dp, 12.dp, 16.dp, 4.dp), fontWeight = FontWeight.SemiBold) }
        itemsIndexed(s.queue) { i, q ->
            val cur = i == s.index
            Row(Modifier.fillMaxWidth().clickable { PlayerConnection.skipTo(i) }.padding(16.dp, 6.dp, 4.dp, 6.dp),
                verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text(q.title, maxLines = 1, overflow = TextOverflow.Ellipsis,
                        color = if (cur) MaterialTheme.colorScheme.primary else Color.Unspecified,
                        fontWeight = if (cur) FontWeight.SemiBold else FontWeight.Normal)
                    if (q.artist.isNotEmpty()) Text(q.artist, fontSize = 12.sp, color = Color.Gray, maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
                if (!cur) IconButton(onClick = { PlayerConnection.remove(i) }) { Icon(Icons.Default.Close, "remove from queue", tint = Color.Gray) }
            }
        }
    }
}
