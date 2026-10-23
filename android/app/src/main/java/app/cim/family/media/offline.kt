package app.cim.family.media

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import android.net.Uri
import androidx.annotation.OptIn
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.cache.CacheWriter
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import app.cim.family.CimUri
import app.cim.family.Images
import app.cim.family.Prefs
import app.cim.family.Song
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject

/**
 * @brief What is downloaded for offline use. The bytes live in MediaCache.offline
 * (a never-evicted SimpleCache in files/), so they play through the same
 * DataSource stack; this table keeps the song metadata and the collections
 * (playlist / album / radio round) so the Offline section works without the server.
 */
class OfflineDb private constructor(ctx: Context) : SQLiteOpenHelper(ctx, "cim_offline.db", null, 1) {
    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("""CREATE TABLE tracks (rel_path TEXT PRIMARY KEY, song TEXT NOT NULL, size INTEGER DEFAULT 0,
            done INTEGER DEFAULT 0, state TEXT NOT NULL DEFAULT 'queued', error TEXT DEFAULT '', added_at INTEGER)""")
        db.execSQL("CREATE TABLE groups (id TEXT PRIMARY KEY, name TEXT NOT NULL, added_at INTEGER)")
        db.execSQL("CREATE TABLE group_items (group_id TEXT NOT NULL, pos INTEGER NOT NULL, rel_path TEXT NOT NULL)")
        db.execSQL("CREATE INDEX idx_gi ON group_items(group_id, pos)")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {}

    data class Group(val id: String, val name: String, val total: Int, val done: Int, val failed: Int, val bytes: Long, val doneBytes: Long)
    data class Track(val song: Song, val size: Long, val done: Long, val state: String, val error: String)

    /** @brief Add (or refresh) a collection and queue its tracks that are not downloaded yet. */
    @Synchronized fun addGroup(id: String, name: String, songs: List<Song>) {
        val db = writableDatabase
        db.beginTransaction()
        try {
            db.execSQL("INSERT OR REPLACE INTO groups(id, name, added_at) VALUES (?,?,?)", arrayOf<Any>(id, name, System.currentTimeMillis()))
            db.delete("group_items", "group_id=?", arrayOf(id))
            songs.forEachIndexed { i, s ->
                db.execSQL("INSERT INTO group_items(group_id, pos, rel_path) VALUES (?,?,?)", arrayOf<Any>(id, i, s.relPath))
                val v = ContentValues()
                v.put("rel_path", s.relPath); v.put("song", s.toJson().toString()); v.put("size", s.size)
                v.put("added_at", System.currentTimeMillis())
                db.insertWithOnConflict("tracks", null, v, SQLiteDatabase.CONFLICT_IGNORE)
                db.execSQL("UPDATE tracks SET state='queued', error='' WHERE rel_path=? AND state='failed'", arrayOf<Any>(s.relPath))
            }
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    /** @brief Remove a collection; tracks no other collection holds go too. Returns their rel_paths. */
    @Synchronized fun deleteGroup(id: String): List<String> {
        val db = writableDatabase
        db.delete("group_items", "group_id=?", arrayOf(id))
        db.delete("groups", "id=?", arrayOf(id))
        val orphans = ArrayList<String>()
        db.rawQuery("SELECT rel_path FROM tracks WHERE rel_path NOT IN (SELECT rel_path FROM group_items)", null).use { c ->
            while (c.moveToNext()) orphans.add(c.getString(0))
        }
        for (r in orphans) db.delete("tracks", "rel_path=?", arrayOf(r))
        return orphans
    }

    fun groups(): List<Group> = readableDatabase.rawQuery(
        """SELECT g.id, g.name, COUNT(t.rel_path), SUM(CASE WHEN t.state='done' THEN 1 ELSE 0 END),
           SUM(CASE WHEN t.state='failed' THEN 1 ELSE 0 END), COALESCE(SUM(t.size),0), COALESCE(SUM(t.done),0)
           FROM groups g LEFT JOIN group_items gi ON gi.group_id=g.id LEFT JOIN tracks t ON t.rel_path=gi.rel_path
           GROUP BY g.id ORDER BY g.added_at DESC""", null).use { c ->
        val out = ArrayList<Group>()
        while (c.moveToNext()) out.add(Group(c.getString(0), c.getString(1), c.getInt(2), c.getInt(3), c.getInt(4), c.getLong(5), c.getLong(6)))
        out
    }

    fun groupTracks(id: String): List<Track> = readableDatabase.rawQuery(
        "SELECT t.song, t.size, t.done, t.state, t.error FROM group_items gi JOIN tracks t ON t.rel_path=gi.rel_path " +
            "WHERE gi.group_id=? ORDER BY gi.pos", arrayOf(id)).use { c ->
        val out = ArrayList<Track>()
        while (c.moveToNext()) {
            val s = try { Song.parse(JSONObject(c.getString(0))) } catch (e: Exception) { null } ?: continue
            out.add(Track(s, c.getLong(1), c.getLong(2), c.getString(3), c.getString(4) ?: ""))
        }
        out
    }

    fun hasGroup(id: String): Boolean = readableDatabase.rawQuery("SELECT 1 FROM groups WHERE id=?", arrayOf(id)).use { it.moveToFirst() }

    fun nextQueued(): Song? = readableDatabase.rawQuery(
        "SELECT song FROM tracks WHERE state='queued' ORDER BY added_at LIMIT 1", null).use { c ->
        if (!c.moveToFirst()) null else try { Song.parse(JSONObject(c.getString(0))) } catch (e: Exception) { null }
    }

    fun queuedCount(): Int = readableDatabase.rawQuery("SELECT COUNT(*) FROM tracks WHERE state='queued'", null).use { it.moveToFirst(); it.getInt(0) }

    fun isTracked(rel: String): Boolean = readableDatabase.rawQuery("SELECT 1 FROM tracks WHERE rel_path=?", arrayOf(rel)).use { it.moveToFirst() }

    @Synchronized fun progress(rel: String, done: Long, size: Long) {
        writableDatabase.execSQL("UPDATE tracks SET done=?, size=CASE WHEN ?>0 THEN ? ELSE size END WHERE rel_path=?",
            arrayOf<Any>(done, size, size, rel))
    }

    @Synchronized fun finish(rel: String, ok: Boolean, error: String) {
        writableDatabase.execSQL("UPDATE tracks SET state=?, error=? WHERE rel_path=?",
            arrayOf<Any>(if (ok) "done" else "failed", error.take(300), rel))
    }

    @Synchronized fun retryFailed() { writableDatabase.execSQL("UPDATE tracks SET state='queued', error='' WHERE state='failed'") }

    companion object {
        @Volatile private var shared: OfflineDb? = null
        fun get(ctx: Context): OfflineDb = shared ?: synchronized(this) {
            shared ?: OfflineDb(ctx.applicationContext).also { shared = it }
        }
    }
}

/** @brief Downloads collections and removes them. */
object Downloads {
    private const val WORK = "cim_downloads"

    fun add(ctx: Context, id: String, name: String, songs: List<Song>) {
        OfflineDb.get(ctx).addGroup(id, name, songs)
        start(ctx)
    }

    /** @brief Run the download worker. replace = the network rule changed: restart it
     *  (a half-done track resumes from what is already in the store). */
    fun start(ctx: Context, replace: Boolean = false) {
        val net = if (Prefs.get(ctx).downloadsWifiOnly) NetworkType.UNMETERED else NetworkType.CONNECTED
        val req = OneTimeWorkRequestBuilder<DownloadWorker>()
            .setConstraints(Constraints.Builder().setRequiredNetworkType(net).build())
            .build()
        WorkManager.getInstance(ctx).enqueueUniqueWork(WORK,
            if (replace) ExistingWorkPolicy.REPLACE else ExistingWorkPolicy.APPEND_OR_REPLACE, req)
    }

    fun delete(ctx: Context, id: String) {
        val gone = OfflineDb.get(ctx).deleteGroup(id)
        for (rel in gone) {
            try { MediaCache.removeOffline(ctx, rel) } catch (e: Exception) { }
            Images.deleteOffline(ctx, app.cim.family.ImageKey.musicCover(rel))
        }
    }

    fun retry(ctx: Context) {
        OfflineDb.get(ctx).retryFailed()
        start(ctx)
    }
}

/**
 * @brief Pulls every queued track fully into the offline store through frames
 * (CacheWriter over MediaCache.downloadDataSource), with its cover.
 */
@OptIn(markerClass = [UnstableApi::class])
class DownloadWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val ctx = applicationContext
        val db = OfflineDb.get(ctx)
        if (!Prefs.get(ctx).paired) return@withContext Result.success()
        val tried = HashSet<String>()
        while (!isStopped) {
            val s = db.nextQueued() ?: break
            if (!tried.add(s.relPath)) break          // finish() didn't stick: never spin
            val rel = s.relPath
            try {
                val ds = MediaCache.downloadDataSource(ctx)
                val spec = DataSpec.Builder().setUri(Uri.parse(CimUri.of(rel))).build()
                var lastWrite = 0L
                val w = CacheWriter(ds, spec, null) { requestLength, bytesCached, _ ->
                    val now = System.currentTimeMillis()
                    if (now - lastWrite > 1000L) { lastWrite = now; db.progress(rel, bytesCached, requestLength) }
                }
                w.cache()
                val ok = MediaCache.isFullyCached(MediaCache.offline(ctx), rel)
                if (ok) {
                    val len = SealedFrameDataSource.SIZES[rel] ?: s.size
                    db.progress(rel, len, len)
                    s.coverKey?.let { Images.saveOffline(ctx, it) }
                }
                db.finish(rel, ok, if (ok) "" else "incomplete")
            } catch (e: Exception) {
                if (isStopped) return@withContext Result.retry()
                db.finish(rel, false, e.message ?: e.toString())
            }
        }
        if (isStopped) Result.retry() else Result.success()
    }
}
