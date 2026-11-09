package app.cim.family

import android.content.Context
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.concurrent.TimeUnit

/**
 * @brief Keeps the on-device search index (LocalIndex) up to date: embeds new photos
 * on the phone and newly cached library thumbnails. Runs every few hours, only
 * while on-device search is on (and, by default, only while charging).
 */
class EmbedWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val prefs = Prefs.get(applicationContext)
        if (!prefs.onDeviceSearch || !OnDevice.installed(applicationContext)) return@withContext Result.success()
        try {
            LocalIndex.update(applicationContext, limit = 2000, stop = { isStopped })
            Result.success()
        } catch (e: Exception) { Result.retry() }
    }

    companion object {
        private const val PERIODIC = "cim_embed_periodic"

        /** @brief (Re)schedule or cancel the background indexing to match the settings. */
        fun schedule(ctx: Context, prefs: Prefs) {
            val wm = WorkManager.getInstance(ctx)
            if (!prefs.onDeviceSearch) { wm.cancelUniqueWork(PERIODIC); return }
            val c = Constraints.Builder().setRequiresCharging(prefs.embedChargingOnly).setRequiresBatteryNotLow(true).build()
            wm.enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.UPDATE,
                PeriodicWorkRequestBuilder<EmbedWorker>(6, TimeUnit.HOURS).setConstraints(c).build())
        }

        fun runNow(ctx: Context) {
            WorkManager.getInstance(ctx).enqueueUniqueWork("cim_embed_now", ExistingWorkPolicy.KEEP,
                OneTimeWorkRequestBuilder<EmbedWorker>().build())
        }
    }
}