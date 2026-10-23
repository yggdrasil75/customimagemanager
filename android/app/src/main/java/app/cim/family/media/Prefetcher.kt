package app.cim.family.media

import android.content.Context
import android.net.Uri
import androidx.annotation.OptIn
import androidx.media3.common.C
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.cache.CacheWriter
import app.cim.family.CimUri
import app.cim.family.PrefetchRange
import app.cim.family.Prefs
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * @brief Read-ahead: pulls the planned byte ranges (PrefetchPlan) into the lru cache
 * with CacheWriter, one at a time, skipping what either cache already holds.
 * A new plan for the same files keeps the running job; a different one
 * replaces it.
 */
@OptIn(markerClass = [UnstableApi::class])
class Prefetcher(ctx: Context) {
    private val app = ctx.applicationContext
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var job: Job? = null
    private var lastKey = ""
    @Volatile private var writer: CacheWriter? = null

    @Synchronized fun update(ranges: List<PrefetchRange>) {
        val key = ranges.joinToString("\n") { it.rel }
        val running = job
        if (key == lastKey && running != null && running.isActive) return
        lastKey = key
        stopJob()
        if (ranges.isEmpty()) return
        job = scope.launch {
            for (r in ranges) {
                if (!isActive) break
                val prefs = Prefs.get(app)
                if (!prefs.paired) break
                if (!prefs.cellularPrefetch && isMetered(app)) break
                try {
                    if (MediaCache.isOffline(app, r.rel)) continue
                    val spec = DataSpec.Builder()
                        .setUri(Uri.parse(CimUri.of(r.rel)))
                        .setPosition(r.start)
                        .setLength(if (r.length < 0) C.LENGTH_UNSET.toLong() else r.length)
                        .build()
                    val w = CacheWriter(MediaCache.prefetchDataSource(app), spec, null, null)
                    writer = w
                    w.cache()
                } catch (e: Exception) {
                    break        // canceled, offline, or the server failed: try again on the next update
                } finally {
                    writer = null
                }
            }
        }
    }

    @Synchronized private fun stopJob() {
        writer?.cancel()
        job?.cancel()
        job = null
    }

    @Synchronized fun stop() { lastKey = ""; stopJob() }

    fun release() {
        stop()
        scope.cancel()
    }
}
