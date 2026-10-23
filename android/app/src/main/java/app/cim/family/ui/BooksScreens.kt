@file:OptIn(ExperimentalMaterial3Api::class)

package app.cim.family.ui

import android.content.ActivityNotFoundException
import android.content.Intent
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
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.AutoStories
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Button
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.FileProvider
import app.cim.family.ApiV2
import app.cim.family.Book
import app.cim.family.FileFetch
import app.cim.family.Prefs
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

@Composable
fun BooksTab(push: (Route) -> Unit) {
    val ctx = LocalContext.current
    var q by remember { mutableStateOf("") }
    var applied by remember { mutableStateOf("") }
    LaunchedEffect(q) { delay(500L); applied = q.trim() }
    Column(Modifier.fillMaxSize()) {
        OutlinedTextField(q, { q = it }, label = { Text("Search books") }, singleLine = true,
            modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp))
        Box(Modifier.weight(1f)) {
            Loader(key = applied, load = {
                Book.list(ApiV2(Prefs.get(ctx)).getJson("books/list", mapOf("q" to applied, "per" to "300", "sort" to "title")))
            }) { books ->
                LazyColumn(Modifier.fillMaxSize()) {
                    if (books.isEmpty()) item { Text("No books.", Modifier.padding(16.dp), color = Color.Gray) }
                    items(books, key = { it.relPath }) { b ->
                        Row2(b.displayTitle,
                            listOf(b.authors.joinToString(", "), if (b.series.isNotEmpty()) b.series + (if (b.seriesIndex.isNotEmpty()) " #" + b.seriesIndex else "") else "",
                                b.fmt.uppercase()).filter { it.isNotEmpty() }.joinToString(" | "),
                            onClick = { push(Route.BookR(b)) },
                            leading = { Cover(b.coverKey, Modifier.width(44.dp).aspectRatio(2f / 3f).clip(RoundedCornerShape(3.dp)), Icons.Default.AutoStories) })
                    }
                }
            }
        }
    }
}

/** @brief A book's details; Open downloads it (decrypted, to the app cache) and hands it to a reader app. */
@Composable
fun BookScreen(book: Book, onBack: () -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var busy by remember { mutableStateOf(false) }
    var progress by remember { mutableStateOf(0f) }
    var err by remember { mutableStateOf("") }
    fun open() {
        busy = true; err = ""; progress = 0f
        scope.launch {
            try {
                val dir = File(ctx.cacheDir, "media/books").also { it.mkdirs() }
                val name = book.relPath.substringAfterLast('/').replace(Regex("[^A-Za-z0-9._ -]"), "_").ifEmpty { "book" }
                val f = File(dir, name)
                val mime = withContext(Dispatchers.IO) {
                    FileFetch.toFile(Prefs.get(ctx), book.relPath, f) { done, total ->
                        if (total > 0) progress = (done.toFloat() / total).coerceIn(0f, 1f)
                    }
                }
                val uri = FileProvider.getUriForFile(ctx, "app.cim.family.files", f)
                val intent = Intent(Intent.ACTION_VIEW).setDataAndType(uri, FileFetch.bookMime(book.fmt, mime))
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
                try {
                    ctx.startActivity(intent)
                } catch (e: ActivityNotFoundException) {
                    err = "No app on this phone opens ${book.fmt.uppercase()} files. Install a reader app."
                }
            } catch (e: Exception) {
                err = e.message ?: e.toString()
            } finally {
                busy = false
            }
        }
    }
    Column(Modifier.fillMaxSize()) {
        BackHeader(book.displayTitle, book.authors.joinToString(", "), onBack)
        LazyColumn(Modifier.fillMaxSize()) {
            item {
                Row(Modifier.padding(12.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    Cover(book.coverKey, Modifier.width(130.dp).aspectRatio(2f / 3f).clip(RoundedCornerShape(4.dp)), Icons.Default.AutoStories)
                    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        if (book.series.isNotEmpty()) Text(book.series + (if (book.seriesIndex.isNotEmpty()) " #" + book.seriesIndex else ""), fontSize = 13.sp)
                        Text(book.fmt.uppercase() + (if (book.size > 0) " | " + formatBytes(book.size) else ""), fontSize = 12.sp, color = Color.Gray)
                        Button(onClick = { open() }, enabled = !busy) { Text(if (busy) "Downloading..." else "Open") }
                        if (busy) LinearProgressIndicator(progress = { progress }, modifier = Modifier.width(140.dp))
                        ErrorText(err)
                    }
                }
            }
            if (book.description.isNotEmpty()) item {
                Text(book.description.replace(Regex("<[^>]+>"), " "), Modifier.padding(16.dp, 4.dp), fontSize = 13.sp, color = Color.LightGray)
            }
            item {
                Text("Books open in another app (an e-book or PDF reader). The decrypted copy stays in this app's cache until Settings → Clear cache.",
                    Modifier.padding(16.dp), fontSize = 11.sp, color = Color.Gray)
            }
        }
    }
}
