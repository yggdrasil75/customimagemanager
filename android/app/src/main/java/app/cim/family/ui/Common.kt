@file:OptIn(ExperimentalMaterial3Api::class)

package app.cim.family.ui

import android.graphics.Bitmap
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.Photo
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import app.cim.family.Images
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

val CardBg = Color(0xFF1C1C24)
val TileBg = Color(0xFF202028)
val Ok = Color(0xFF7BD88F)
val Warn = Color(0xFFE0B060)

@Composable
fun ToggleRow(label: String, value: Boolean, onChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Text(label, Modifier.weight(1f)); Switch(checked = value, onCheckedChange = onChange)
    }
}

@Composable
fun SectionTitle(text: String, modifier: Modifier = Modifier) {
    Text(text, modifier.padding(16.dp, 14.dp, 16.dp, 6.dp), fontSize = 18.sp, fontWeight = FontWeight.Bold)
}

@Composable
fun ErrorText(msg: String, modifier: Modifier = Modifier) {
    if (msg.isNotEmpty()) Text(msg, modifier.padding(12.dp), color = MaterialTheme.colorScheme.error, fontSize = 13.sp)
}

@Composable
fun Spinner(modifier: Modifier = Modifier) {
    Box(modifier.fillMaxWidth().height(64.dp), contentAlignment = Alignment.Center) {
        CircularProgressIndicator(Modifier.size(28.dp))
    }
}

/** @brief A header with a back arrow, for pushed screens. */
@Composable
fun BackHeader(title: String, subtitle: String = "", onBack: () -> Unit) {
    Row(Modifier.fillMaxWidth().padding(4.dp, 4.dp, 12.dp, 4.dp), verticalAlignment = Alignment.CenterVertically) {
        IconButton(onClick = onBack) { Icon(Icons.Default.ArrowBack, contentDescription = "back") }
        Column(Modifier.weight(1f)) {
            Text(title, fontSize = 18.sp, fontWeight = FontWeight.Bold, maxLines = 1, overflow = TextOverflow.Ellipsis)
            if (subtitle.isNotEmpty()) Text(subtitle, fontSize = 12.sp, color = Color.Gray, maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
    }
}

/** @brief A horizontal row of single-choice chips. */
@Composable
fun ChipRow(options: List<Pair<String, String>>, selected: String, onSelect: (String) -> Unit) {
    Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()).padding(horizontal = 12.dp, vertical = 4.dp),
        horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        for ((value, label) in options) {
            FilterChip(selected = selected == value, onClick = { onSelect(value) }, label = { Text(label) })
        }
    }
}

/** @brief A decrypted cover / poster from [Images], or a placeholder icon. */
@Composable
fun Cover(key: String?, modifier: Modifier = Modifier, placeholder: ImageVector = Icons.Default.Photo) {
    val ctx = LocalContext.current
    val bmp: Bitmap? by produceState<Bitmap?>(initialValue = if (key == null) null else Images.cached(key), key) {
        value = if (key == null) null else Images.load(ctx, key)
    }
    Box(modifier.background(TileBg), contentAlignment = Alignment.Center) {
        val b = bmp
        if (b != null) Image(b.asImageBitmap(), contentDescription = null, modifier = Modifier.fillMaxSize(), contentScale = ContentScale.Crop)
        else Icon(placeholder, contentDescription = null, tint = Color(0xFF55555F))
    }
}

/** @brief A clickable list row: leading content, two lines, trailing content. */
@Composable
fun Row2(title: String, subtitle: String, onClick: () -> Unit,
         leading: @Composable () -> Unit = {}, trailing: @Composable () -> Unit = {}) {
    Row(Modifier.fillMaxWidth().clickable(onClick = onClick).padding(horizontal = 12.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        leading()
        Column(Modifier.weight(1f)) {
            Text(title, maxLines = 1, overflow = TextOverflow.Ellipsis)
            if (subtitle.isNotEmpty()) Text(subtitle, fontSize = 12.sp, color = Color.Gray, maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
        trailing()
    }
}

/**
 * @brief Runs `load` off the main thread (again when `key` changes or on Retry) and
 * shows a spinner, the error with a Retry button, or `content(result)`.
 */
@Composable
fun <T> Loader(key: Any?, load: suspend () -> T, content: @Composable (T) -> Unit) {
    var attempt by remember(key) { mutableStateOf(0) }
    var result by remember(key) { mutableStateOf<Result<T>?>(null) }
    LaunchedEffect(key, attempt) {
        result = null
        result = try {
            Result.success(withContext(Dispatchers.IO) { load() })
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            Result.failure(e)
        }
    }
    val r = result
    when {
        r == null -> Spinner()
        r.isFailure -> Column(Modifier.fillMaxWidth().padding(12.dp)) {
            val e = r.exceptionOrNull()
            ErrorText(e?.message ?: e?.toString() ?: "failed")
            Button(onClick = { attempt++ }) { Text("Retry") }
        }
        else -> {
            @Suppress("UNCHECKED_CAST")
            content(r.getOrNull() as T)
        }
    }
}

fun formatBytes(n: Long): String = when {
    n >= 1L shl 30 -> "%.1f GB".format(n / (1024.0 * 1024 * 1024))
    n >= 1L shl 20 -> "%.0f MB".format(n / (1024.0 * 1024))
    n >= 1L shl 10 -> "%.0f KB".format(n / 1024.0)
    else -> "$n B"
}
