package app.cim.family

import android.content.Context
import android.graphics.Bitmap
import android.os.Build
import android.provider.MediaStore
import android.util.LruCache
import android.util.Size
import app.cim.family.ui.ThumbCache
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * @brief What the Photos tab shows when the server can't be reached: the timeline
 * as it was last seen (only the entries whose thumbnail is cached on the phone)
 * merged with the photos and videos on the phone itself, newest first.
 *
 * Items from the phone carry a path of "local:<MediaStore id>" so the grid and
 * the viewer can tell them apart from library paths.
 */
object OfflinePhotos {
    private const val CACHE_FILE = "timeline_cache.json"
    private const val MAX_CACHED = 20_000
    const val LOCAL = "local:"

    private val localThumbs = object : LruCache<Long, Bitmap>(24 * 1024 * 1024) {
        override fun sizeOf(key: Long, value: Bitmap): Int = value.byteCount
    }

    private fun file(ctx: Context) = File(ctx.filesDir, CACHE_FILE)

    /** @brief Remember the timeline pages loaded so far (called after every page). */
    fun save(ctx: Context, items: List<Api.Item>, total: Int) {
        try {
            val arr = JSONArray()
            for (it in items.take(MAX_CACHED)) {
                arr.put(JSONObject().put("p", it.path).put("w", it.w).put("h", it.h).put("t", it.t).put("v", it.video))
            }
            val tmp = File(ctx.filesDir, "$CACHE_FILE.tmp")
            tmp.writeText(JSONObject().put("total", total).put("at", System.currentTimeMillis()).put("items", arr).toString())
            tmp.renameTo(file(ctx))
        } catch (e: Exception) { }
    }

    /** @brief The cached timeline, every entry (thumbnail cached or not). */
    fun cached(ctx: Context): List<Api.Item> = try {
        val j = JSONObject(file(ctx).readText())
        val a = j.getJSONArray("items")
        (0 until a.length()).map { i ->
            val o = a.getJSONObject(i)
            Api.Item(o.getString("p"), o.optInt("w"), o.optInt("h"), o.optDouble("t"), o.optBoolean("v"))
        }
    } catch (e: Exception) { emptyList() }

    /** @brief When the cached timeline was saved (0 = never). */
    fun cachedAt(ctx: Context): Long = try { JSONObject(file(ctx).readText()).optLong("at") } catch (e: Exception) { 0L }

    /** @brief The phone's own photos / videos as timeline items (needs media permission). */
    fun local(ctx: Context, includeVideo: Boolean = true): List<Api.Item> = try {
        Scanner.items(ctx, includeVideo).map { m -> Api.Item(LOCAL + m.id, 0, 0, m.dateTaken / 1000.0, m.video) }
    } catch (e: Exception) { emptyList() }

    /** @brief Offline view: cached server items whose thumbnail is on the phone, plus the
     *  phone's own media, newest first. */
    fun offlineItems(ctx: Context, includeLocal: Boolean): List<Api.Item> {
        val server = cached(ctx).filter { ThumbCache.hasCached(ctx, it.path) }
        val phone = if (includeLocal) local(ctx) else emptyList()
        return (server + phone).sortedByDescending { it.t }
    }

    fun isLocal(item: Api.Item) = item.path.startsWith(LOCAL)

    fun localId(item: Api.Item): Long = item.path.removePrefix(LOCAL).toLongOrNull() ?: -1L

    fun localUri(item: Api.Item): android.net.Uri {
        // the Images / Video tables, not Files: MediaProvider only makes thumbnails (and
        // other apps only open ACTION_VIEW) for those
        val base = if (item.video) MediaStore.Video.Media.EXTERNAL_CONTENT_URI else MediaStore.Images.Media.EXTERNAL_CONTENT_URI
        return android.content.ContentUris.withAppendedId(base, localId(item))
    }

    /** @brief A thumbnail of a phone item from MediaStore (blocking). */
    fun localThumb(ctx: Context, item: Api.Item): Bitmap? {
        val id = localId(item)
        localThumbs.get(id)?.let { return it }
        val bmp = try {
            if (Build.VERSION.SDK_INT >= 29) {
                ctx.contentResolver.loadThumbnail(localUri(item), Size(256, 256), null)
            } else if (item.video) {
                @Suppress("DEPRECATION")
                MediaStore.Video.Thumbnails.getThumbnail(ctx.contentResolver, id, MediaStore.Video.Thumbnails.MINI_KIND, null)
            } else {
                @Suppress("DEPRECATION")
                MediaStore.Images.Thumbnails.getThumbnail(ctx.contentResolver, id, MediaStore.Images.Thumbnails.MINI_KIND, null)
            }
        } catch (e: Exception) { null }
        if (bmp != null) localThumbs.put(id, bmp)
        return bmp
    }
}

/**
 * @brief Photos search: the server's semantic search ("sem:" on /api/list, through the
 * sealed proxy) when it answers, else the on-device index (ondevice.kt) when the
 * user turned it on. @return (items, where the answer came from).
 */
object PhotoSearch {
    data class Result(val items: List<Api.Item>, val source: String)

    fun search(ctx: Context, prefs: Prefs, q: String, preferPhone: Boolean = false): Result {
        val query = q.trim()
        if (query.isEmpty()) return Result(emptyList(), "")
        var serverErr: String? = null
        if (!preferPhone) {
            try {
                val j = ApiV2(prefs).getJson("list", mapOf("q" to "sem:$query", "page" to "0"))
                val files = j.optJSONArray("files") ?: JSONArray()
                val items = (0 until files.length()).map { i ->
                    val o = files.getJSONObject(i)
                    val rel = o.optString("filename")
                    // semantic entries don't say the kind: tell videos by extension
                    val ext = rel.substringAfterLast('.', "").lowercase()
                    Api.Item(rel, o.optInt("width"), o.optInt("height"), 0.0,
                             o.optString("type") == "video" || o.optString("media_kind") == "video" ||
                             ext in setOf("mp4", "mkv", "webm", "mov", "m4v", "avi", "3gp"))
                }.filter { it.path.isNotEmpty() }
                return Result(items, "server")
            } catch (e: Exception) {
                val m = e.message ?: e.toString()
                if (m.contains("No matches")) return Result(emptyList(), "server")   // answered: nothing found
                serverErr = m
            }
        }
        if (prefs.onDeviceSearch && OnDevice.installed(ctx)) {
            val v = OnDevice.embedText(ctx, query)
            val meta = OfflinePhotos.cached(ctx).associateBy { it.path }
            val items = LocalIndex.search(ctx, v, 300).map { (k, _) ->
                meta[k] ?: Api.Item(k, 0, 0, 0.0, false)
            }
            return Result(items, "phone")
        }
        throw ApiException(serverErr?.let { "search needs the server ($it) or on-device search (Settings)" }
                           ?: "on-device search is off or its model isn't downloaded (Settings -> On-device search)")
    }
}