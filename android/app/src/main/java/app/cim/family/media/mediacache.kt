package app.cim.family.media

import android.content.Context
import androidx.annotation.OptIn
import androidx.media3.common.util.UnstableApi
import androidx.media3.database.StandaloneDatabaseProvider
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.cache.CacheDataSource
import androidx.media3.datasource.cache.ContentMetadata
import androidx.media3.datasource.cache.LeastRecentlyUsedCacheEvictor
import androidx.media3.datasource.cache.NoOpCacheEvictor
import androidx.media3.datasource.cache.SimpleCache
import app.cim.family.CimUri
import app.cim.family.Prefs
import java.io.File

/**
 * @brief The two media caches (one SimpleCache instance per folder per process):
 *  - lru: the streaming / read-ahead cache in the app's cache dir, capped
 *    (Settings, applied at the next start), least recently used goes first;
 *  - offline: downloads in the app's files dir, never evicted.
 * Playback reads offline -> lru -> server (SealedFrameDataSource), so a
 * downloaded track never touches the network. Cache keys are the cim:// URIs.
 */
@OptIn(markerClass = [UnstableApi::class])
object MediaCache {
    private var db: StandaloneDatabaseProvider? = null
    private var lruCache: SimpleCache? = null
    private var offlineCache: SimpleCache? = null

    @Synchronized private fun database(ctx: Context): StandaloneDatabaseProvider =
        db ?: StandaloneDatabaseProvider(ctx.applicationContext).also { db = it }

    @Synchronized fun lru(ctx: Context): SimpleCache {
        lruCache?.let { return it }
        val mb = Prefs.get(ctx).cacheMb.coerceIn(64, 64 * 1024).toLong()
        val c = SimpleCache(File(ctx.applicationContext.cacheDir, "media3"),
            LeastRecentlyUsedCacheEvictor(mb * 1024L * 1024L), database(ctx))
        lruCache = c
        return c
    }

    @Synchronized fun offline(ctx: Context): SimpleCache {
        offlineCache?.let { return it }
        val c = SimpleCache(File(ctx.applicationContext.filesDir, "offline_media"), NoOpCacheEvictor(), database(ctx))
        offlineCache = c
        return c
    }

    fun key(rel: String): String = CimUri.of(rel)

    /** @brief Everything the player reads: offline (read-only) -> lru (read/write) -> server,
     *  and non-cim URIs (subtitle files) through DefaultDataSource. */
    fun playbackFactory(ctx: Context): DataSource.Factory {
        val app = ctx.applicationContext
        val lruLayer = CacheDataSource.Factory()
            .setCache(lru(app))
            .setUpstreamDataSourceFactory(SealedFrameDataSource.Factory(app, SealedFrameDataSource.MODE_STREAM))
            .setFlags(CacheDataSource.FLAG_IGNORE_CACHE_ON_ERROR)
        val offlineLayer = CacheDataSource.Factory()
            .setCache(offline(app))
            .setUpstreamDataSourceFactory(lruLayer)
            .setCacheWriteDataSinkFactory(null)
            .setFlags(CacheDataSource.FLAG_IGNORE_CACHE_ON_ERROR)
        return SchemeRoutingDataSource.Factory(app, offlineLayer)
    }

    /** @brief For CacheWriter read-ahead into the lru cache. */
    fun prefetchDataSource(ctx: Context): CacheDataSource {
        val app = ctx.applicationContext
        return CacheDataSource.Factory()
            .setCache(lru(app))
            .setUpstreamDataSourceFactory(SealedFrameDataSource.Factory(app, SealedFrameDataSource.MODE_PREFETCH))
            .createDataSourceForDownloading()
    }

    /** @brief For CacheWriter downloads into the offline store. */
    fun downloadDataSource(ctx: Context): CacheDataSource {
        val app = ctx.applicationContext
        return CacheDataSource.Factory()
            .setCache(offline(app))
            .setUpstreamDataSourceFactory(SealedFrameDataSource.Factory(app, SealedFrameDataSource.MODE_DOWNLOAD))
            .createDataSourceForDownloading()
    }

    /** @brief The whole file is in this cache (its length known and every byte present). */
    fun isFullyCached(cache: SimpleCache, rel: String): Boolean {
        val k = key(rel)
        val len = ContentMetadata.getContentLength(cache.getContentMetadata(k))
        if (len < 0) return false
        return cache.isCached(k, 0, len)
    }

    fun isOffline(ctx: Context, rel: String): Boolean = isFullyCached(offline(ctx), rel)

    /** @brief Bytes of [start, start+length) already in either cache (length -1 = to the end). */
    fun cachedBytes(ctx: Context, rel: String, start: Long, length: Long): Long {
        val k = key(rel)
        return maxOf(lru(ctx).getCachedBytes(k, start, length), offline(ctx).getCachedBytes(k, start, length))
    }

    fun removeOffline(ctx: Context, rel: String) { offline(ctx).removeResource(key(rel)) }

    /** @brief Empty the streaming cache (downloads stay). */
    fun clearLru(ctx: Context) {
        val c = lru(ctx)
        for (k in HashSet(c.keys)) c.removeResource(k)
    }

    fun lruBytes(ctx: Context): Long = lru(ctx).cacheSpace
    fun offlineBytes(ctx: Context): Long = offline(ctx).cacheSpace
}
