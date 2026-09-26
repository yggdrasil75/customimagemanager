package app.cim.family

import android.app.PendingIntent
import android.content.ContentUris
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.MediaStore
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

data class MediaItem(val id: Long, val uri: Uri, val name: String, val bucketId: String, val bucket: String,
                     val size: Long, val dateTaken: Long, val video: Boolean, val mime: String)

data class Bucket(val id: String, val name: String, val count: Int, val uploaded: Int)

/** Reads the phone's photo library through MediaStore (no raw file paths needed). */
object Scanner {
    // DATE_TAKEN / BUCKET_* on the Files table are API 29+; older phones get DATE_ADDED twice.
    private val PROJ = arrayOf(MediaStore.Files.FileColumns._ID, MediaStore.Files.FileColumns.DISPLAY_NAME,
        if (Build.VERSION.SDK_INT >= 29) MediaStore.Files.FileColumns.BUCKET_ID else MediaStore.Images.ImageColumns.BUCKET_ID,
        if (Build.VERSION.SDK_INT >= 29) MediaStore.Files.FileColumns.BUCKET_DISPLAY_NAME else MediaStore.Images.ImageColumns.BUCKET_DISPLAY_NAME,
        MediaStore.Files.FileColumns.SIZE,
        if (Build.VERSION.SDK_INT >= 29) MediaStore.Files.FileColumns.DATE_TAKEN else MediaStore.Files.FileColumns.DATE_ADDED,
        MediaStore.Files.FileColumns.DATE_ADDED, MediaStore.Files.FileColumns.MEDIA_TYPE, MediaStore.Files.FileColumns.MIME_TYPE)

    private fun contentUri(): Uri = if (Build.VERSION.SDK_INT >= 29) MediaStore.Files.getContentUri(MediaStore.VOLUME_EXTERNAL) else MediaStore.Files.getContentUri("external")

    fun items(ctx: Context, includeVideo: Boolean, buckets: Set<String>? = null): List<MediaItem> {
        val types = if (includeVideo) "(${MediaStore.Files.FileColumns.MEDIA_TYPE} IN (1,3))" else "(${MediaStore.Files.FileColumns.MEDIA_TYPE}=1)"
        val out = ArrayList<MediaItem>()
        ctx.contentResolver.query(contentUri(), PROJ, types, null, "${MediaStore.Files.FileColumns.DATE_ADDED} DESC")?.use { c ->
            while (c.moveToNext()) {
                val bid = c.getString(2) ?: continue
                if (buckets != null && bid !in buckets) continue
                val id = c.getLong(0)
                val video = c.getInt(7) == 3
                val taken = (if (Build.VERSION.SDK_INT >= 29) c.getLong(5) else c.getLong(5) * 1000).takeIf { it > 0 } ?: (c.getLong(6) * 1000)
                out.add(MediaItem(id, ContentUris.withAppendedId(contentUri(), id), c.getString(1) ?: "media",
                    bid, c.getString(3) ?: "Unknown", c.getLong(4), taken, video, c.getString(8) ?: ""))
            }
        }
        return out
    }

    fun buckets(ctx: Context, db: Db, includeVideo: Boolean): List<Bucket> {
        val all = items(ctx, includeVideo)
        return all.groupBy { it.bucketId }.map { (id, items) ->
            Bucket(id, items.first().bucket, items.size, items.count { db.isUploaded(it.id) })
        }.sortedByDescending { it.count }
    }
}

/**
 * The auto-upload job. Runs on a content-change trigger (a new photo lands),
 * a 15-minute periodic fallback, and on demand from the UI. For every item in
 * a bucket whose policy is keep/purge: hash it, ask the server which hashes it
 * already holds (batched), push the rest sealed, record success. Purging is
 * NOT done here — Android requires the user to confirm deletion of media the
 * app didn't create, so the UI offers "Free up space" instead.
 */
class UploadWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    override suspend fun doWork(): Result {
        val ctx = applicationContext
        val prefs = Prefs(ctx)
        val db = Db(ctx)
        try {
            if (!prefs.paired || prefs.paused) return Result.success()
            val api = Api(prefs)
            val policies = db.policies().filterValues { it != "off" }
            if (policies.isEmpty()) return Result.success()
            val items = Scanner.items(ctx, prefs.uploadVideos, policies.keys)
                .filter { !db.isUploaded(it.id) && it.size > 0 && db.failureBackoffOk(it.id) }
            if (items.isEmpty()) return Result.success()

            // 1) hash everything new, 2) one batched "which do you have", 3) push the rest.
            val hashed = ArrayList<Pair<MediaItem, String>>()
            for (it in items) {
                if (isStopped) return Result.retry()
                try {
                    val sha = ctx.contentResolver.openInputStream(it.uri)?.use { s -> Crypto.sha256Hex(s) } ?: continue
                    hashed.add(it to sha)
                } catch (e: Exception) { db.markFailed(it.id, "unreadable: ${e.message}") }
            }
            val have = HashSet<String>()
            for (chunk in hashed.map { it.second }.chunked(1000)) have.addAll(api.have(chunk))
            var sent = 0
            for ((item, sha) in hashed) {
                if (isStopped) return Result.retry()
                if (sha in have) { db.markUploaded(item.id, sha, item.bucketId, item.name, item.size, ""); continue }
                try {
                    val meta = JSONObject().put("tags", JSONArray()).put("description", "")
                        .put("albums", JSONArray()).put("date_taken", item.dateTaken / 1000.0).put("mime", item.mime)
                    var r = api.push(sha, item.bucket, item.name, meta, null, item.size)
                    if (r.needFile) r = api.push(sha, item.bucket, item.name, meta,
                        { ctx.contentResolver.openInputStream(item.uri) ?: throw ApiException("cannot open ${item.name}") }, item.size)
                    if (r.declined) { db.markUploaded(item.id, sha, item.bucketId, item.name, item.size, ""); continue }
                    db.markUploaded(item.id, sha, item.bucketId, item.name, item.size, r.filename)
                    sent++
                } catch (e: Exception) {
                    db.markFailed(item.id, e.message ?: e.toString())
                    prefs.lastError = "${item.name}: ${e.message}"
                }
            }
            prefs.lastRun = System.currentTimeMillis()
            if (sent > 0) prefs.lastError = ""
            return Result.success()
        } catch (e: Exception) {
            prefs.lastError = e.message ?: e.toString()
            return Result.retry()
        } finally {
            db.close()
            schedule(ctx, prefs)          // re-arm the content trigger (one-shot by nature)
        }
    }

    companion object {
        private const val PERIODIC = "cim_upload_periodic"
        private const val TRIGGER = "cim_upload_trigger"

        private fun constraints(prefs: Prefs) = Constraints.Builder()
            .setRequiredNetworkType(if (prefs.wifiOnly) NetworkType.UNMETERED else NetworkType.CONNECTED)
            .setRequiresCharging(prefs.chargingOnly).setRequiresBatteryNotLow(true).build()

        fun schedule(ctx: Context, prefs: Prefs) {
            val wm = WorkManager.getInstance(ctx)
            wm.enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.UPDATE,
                PeriodicWorkRequestBuilder<UploadWorker>(15, TimeUnit.MINUTES).setConstraints(constraints(prefs))
                    .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 1, TimeUnit.MINUTES).build())
            val trig = OneTimeWorkRequestBuilder<UploadWorker>().setConstraints(
                Constraints.Builder(constraints(prefs))
                    .addContentUriTrigger(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, true)
                    .addContentUriTrigger(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, true)
                    .setTriggerContentUpdateDelay(30, TimeUnit.SECONDS).build()).build()
            wm.enqueueUniqueWork(TRIGGER, ExistingWorkPolicy.KEEP, trig)
        }

        fun runNow(ctx: Context) {
            WorkManager.getInstance(ctx).enqueueUniqueWork("cim_upload_now", ExistingWorkPolicy.KEEP,
                OneTimeWorkRequestBuilder<UploadWorker>().build())
        }

        /** Android 11+: the system asks the user once for the whole batch. Older: direct delete. */
        fun purgeIntent(ctx: Context, ids: List<Long>): PendingIntent? {
            if (ids.isEmpty()) return null
            val uris = ids.map { ContentUris.withAppendedId(MediaStore.Files.getContentUri("external"), it) }
            return if (Build.VERSION.SDK_INT >= 30) MediaStore.createDeleteRequest(ctx.contentResolver, uris)
            else { uris.forEach { runCatching { ctx.contentResolver.delete(it, null, null) } }; null }
        }

        fun viewIntent(uri: Uri, mime: String): Intent = Intent(Intent.ACTION_VIEW).setDataAndType(uri, mime)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
    }
}
