package app.cim.family

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.LruCache
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withContext
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.ConcurrentHashMap

/**
 * @brief Covers and posters, decrypted: an in-memory LRU of decoded bitmaps over a
 * small disk cache (app cache dir) of the decrypted bytes. Offline downloads
 * keep their covers in files/ (offlineDir) so they survive cache clears.
 *
 * Keys come from [ImageKey]: "api:<route>" through the sealed API proxy, or
 * "file:<rel>" (a library image: the v1 thumbnail, else the file via frames).
 */
object Images {
    private const val MAX_PX = 512
    private const val DISK_CAP = 64L * 1024 * 1024
    private const val FAIL_RETRY_MS = 10 * 60_000L

    private val mem = object : LruCache<String, Bitmap>(32 * 1024 * 1024) {
        override fun sizeOf(key: String, value: Bitmap): Int = value.byteCount
    }
    private val failed = ConcurrentHashMap<String, Long>()
    private val gate = Semaphore(4)
    @Volatile private var trimmedAt = 0L

    private fun hash(key: String): String =
        MessageDigest.getInstance("SHA-1").digest(key.toByteArray()).joinToString("") { "%02x".format(it) }

    private fun diskDir(ctx: Context): File = File(ctx.cacheDir, "covers").also { it.mkdirs() }
    fun offlineDir(ctx: Context): File = File(ctx.filesDir, "offline_covers").also { it.mkdirs() }

    fun cached(key: String): Bitmap? = mem.get(key)

    suspend fun load(ctx: Context, key: String): Bitmap? {
        mem.get(key)?.let { return it }
        return withContext(Dispatchers.IO) { gate.withPermit { loadBlocking(ctx, key) } }
    }

    /** @brief Blocking load (any thread but main). Null when there is no image or it fails. */
    fun loadBlocking(ctx: Context, key: String): Bitmap? {
        mem.get(key)?.let { return it }
        val bytes = bytesBlocking(ctx, key) ?: return null
        val bmp = decode(bytes) ?: return null
        mem.put(key, bmp)
        return bmp
    }

    /** @brief The decrypted bytes (disk cache, offline copy, or the server). */
    fun bytesBlocking(ctx: Context, key: String): ByteArray? {
        val h = hash(key)
        val off = File(offlineDir(ctx), h)
        if (off.exists()) return try { off.readBytes() } catch (e: Exception) { null }
        val f = File(diskDir(ctx), h)
        if (f.exists()) {
            f.setLastModified(System.currentTimeMillis())
            return try { f.readBytes() } catch (e: Exception) { null }
        }
        val t = failed[key]
        if (t != null && System.currentTimeMillis() - t < FAIL_RETRY_MS) return null
        val prefs = Prefs.get(ctx)
        if (!prefs.paired) return null
        val got: ByteArray? = try { fetch(prefs, key) } catch (e: Exception) { null }
        if (got == null || got.isEmpty()) { failed[key] = System.currentTimeMillis(); return null }
        try { f.writeBytes(got) } catch (e: Exception) { }
        trim(ctx)
        return got
    }

    /** @brief Copy an image into the offline store (downloads), fetching it if needed. */
    fun saveOffline(ctx: Context, key: String) {
        val off = File(offlineDir(ctx), hash(key))
        if (off.exists()) return
        val b = bytesBlocking(ctx, key) ?: return
        try { off.writeBytes(b) } catch (e: Exception) { }
    }

    fun deleteOffline(ctx: Context, key: String) { File(offlineDir(ctx), hash(key)).delete() }

    fun clear(ctx: Context) {
        mem.evictAll(); failed.clear()
        diskDir(ctx).deleteRecursively()
    }

    private fun fetch(prefs: Prefs, key: String): ByteArray? = when {
        key.startsWith("api:") -> ApiV2(prefs).getBytes(key.substring(4)).first
        key.startsWith("file:") -> {
            val rel = key.substring(5)
            val thumb: ByteArray? = try { Api(prefs).thumb(rel) } catch (e: Exception) { null }
            thumb ?: wholeFile(prefs, rel)
        }
        else -> null
    }

    /** @brief A small library image through frames (posters when the account lacks the
     *  gallery feature that inbound/thumb needs). Gives up past 16 MiB. */
    private fun wholeFile(prefs: Prefs, rel: String): ByteArray? {
        val api = ApiV2(prefs)
        val first = api.frames(rel, 0, 16)
        if (first.totalFrames > 16) return null
        return first.data
    }

    private fun decode(bytes: ByteArray): Bitmap? {
        val o = BitmapFactory.Options()
        o.inJustDecodeBounds = true
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, o)
        if (o.outWidth <= 0 || o.outHeight <= 0) return null
        var sample = 1
        while (o.outWidth / (sample * 2) >= MAX_PX && o.outHeight / (sample * 2) >= MAX_PX) sample *= 2
        val d = BitmapFactory.Options()
        d.inSampleSize = sample
        return BitmapFactory.decodeByteArray(bytes, 0, bytes.size, d)
    }

    /** @brief Keep the disk cache under DISK_CAP, oldest first (at most once a minute). */
    private fun trim(ctx: Context) {
        val now = System.currentTimeMillis()
        if (now - trimmedAt < 60_000L) return
        trimmedAt = now
        val files = diskDir(ctx).listFiles() ?: return
        var total = files.sumOf { it.length() }
        if (total <= DISK_CAP) return
        for (f in files.sortedBy { it.lastModified() }) {
            if (total <= DISK_CAP * 3 / 4) break
            total -= f.length(); f.delete()
        }
    }
}
