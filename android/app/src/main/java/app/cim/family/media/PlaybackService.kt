package app.cim.family.media

import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.os.SystemClock
import androidx.annotation.OptIn
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.Timeline
import androidx.media3.common.util.BitmapLoader
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSourceBitmapLoader
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService
import app.cim.family.Images
import app.cim.family.MainActivity
import app.cim.family.PrefetchEntry
import app.cim.family.PrefetchPlan
import app.cim.family.Prefs
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture
import com.google.common.util.concurrent.ListeningExecutorService
import com.google.common.util.concurrent.MoreExecutors
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.IOException
import java.util.concurrent.Callable
import java.util.concurrent.Executors

/**
 * @brief Background audio: an ExoPlayer in a MediaSessionService, so music keeps
 * playing with the screen off and shows the media notification, lock-screen and
 * Bluetooth controls. The UI talks to it through a MediaController
 * (PlayerConnection). Also runs the radio's round refill / played reports and
 * the read-ahead of the queue into the cache.
 */
@OptIn(markerClass = [UnstableApi::class])
class PlaybackService : MediaSessionService() {
    private var session: MediaSession? = null
    private var player: ExoPlayer? = null
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var prefetcher: Prefetcher? = null
    private var refilling = false

    // listened time of the current item, for radio/played
    private var curItem: MediaItem? = null
    private var listenedMs = 0L
    private var playingSince = -1L

    override fun onCreate() {
        super.onCreate()
        val prefs = Prefs.get(this)
        val aheadMs = (prefs.audioAheadMin.coerceIn(1, 240) * 60_000L).coerceAtMost(Int.MAX_VALUE.toLong()).toInt()
        // In-memory buffer: up to the read-ahead time but capped at 32 MB; the disk
        // cache (Prefetcher) holds the rest of the read-ahead.
        val load = DefaultLoadControl.Builder()
            .setBufferDurationsMs(30_000, maxOf(60_000, aheadMs), 2_500, 5_000)
            .setTargetBufferBytes(32 * 1024 * 1024)
            .setPrioritizeTimeOverSizeThresholds(false)
            .build()
        val attrs = AudioAttributes.Builder().setUsage(C.USAGE_MEDIA).setContentType(C.AUDIO_CONTENT_TYPE_MUSIC).build()
        val p = ExoPlayer.Builder(this)
            .setMediaSourceFactory(DefaultMediaSourceFactory(MediaCache.playbackFactory(this)))
            .setLoadControl(load)
            .setAudioAttributes(attrs, true)
            .setHandleAudioBecomingNoisy(true)
            .setWakeMode(C.WAKE_MODE_NETWORK)
            .build()
        p.addListener(listener)
        player = p
        prefetcher = Prefetcher(this)
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        session = MediaSession.Builder(this, p)
            .setCallback(callback)
            .setSessionActivity(open)
            .setBitmapLoader(ArtBitmapLoader(this))
            .build()
        scope.launch {
            while (isActive) {
                delay(30_000L)
                if (player?.isPlaying == true) prefetchNow()
            }
        }
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo): MediaSession? = session

    override fun onTaskRemoved(rootIntent: Intent?) {
        val p = player
        if (p == null || !p.playWhenReady || p.mediaItemCount == 0) stopSelf()
    }

    override fun onDestroy() {
        accountListening()
        prefetcher?.release()
        prefetcher = null
        scope.cancel()
        session?.release()
        session = null
        player?.removeListener(listener)
        player?.release()
        player = null
        super.onDestroy()
    }

    private val callback = object : MediaSession.Callback {
        override fun onAddMediaItems(mediaSession: MediaSession, controller: MediaSession.ControllerInfo,
                                     mediaItems: MutableList<MediaItem>): ListenableFuture<MutableList<MediaItem>> {
            val out: MutableList<MediaItem> = ArrayList(mediaItems.size)
            for (m in mediaItems) out.add(Items.withUri(m))
            return Futures.immediateFuture(out)
        }
    }

    private val listener = object : Player.Listener {
        override fun onMediaItemTransition(mediaItem: MediaItem?, reason: Int) {
            accountListening()
            curItem = mediaItem
            listenedMs = 0L
            playingSince = if (player?.isPlaying == true) SystemClock.elapsedRealtime() else -1L
            maybeRefillRadio()
            prefetchNow()
        }

        override fun onIsPlayingChanged(isPlaying: Boolean) {
            val now = SystemClock.elapsedRealtime()
            if (isPlaying) {
                if (playingSince < 0) playingSince = now
                prefetchNow()
            } else if (playingSince >= 0) {
                listenedMs += now - playingSince
                playingSince = -1L
            }
        }

        override fun onTimelineChanged(timeline: Timeline, reason: Int) {
            if (curItem == null) curItem = player?.currentMediaItem
            prefetchNow()
        }

        override fun onPlayerError(error: PlaybackException) {
            prefetcher?.stop()
        }
    }

    /** @brief The track that just ended or was skipped: report it to the radio if it was
     *  really listened to (over 30 s or over half its length). */
    private fun accountListening() {
        val item = curItem ?: return
        if (playingSince >= 0) {
            listenedMs += SystemClock.elapsedRealtime() - playingSince
            playingSince = SystemClock.elapsedRealtime()
        }
        val heard = listenedMs
        listenedMs = 0L
        if (!Items.isRadio(item)) return
        val durMs = (Items.durationSec(item) * 1000).toLong()
        if (heard < 30_000L && !(durMs > 0 && heard >= durMs / 2)) return
        val rel = item.mediaId
        val ctx: Context = applicationContext
        scope.launch {
            withContext(Dispatchers.IO) { try { Radio.played(ctx, rel) } catch (e: Exception) { } }
        }
    }

    /** @brief Radio: fewer than 3 tracks after the current one -> fetch and append the next round. */
    private fun maybeRefillRadio() {
        val p = player ?: return
        if (refilling || !Items.isRadio(p.currentMediaItem)) return
        val left = p.mediaItemCount - p.currentMediaItemIndex - 1
        if (left >= 3) return
        refilling = true
        val ctx: Context = applicationContext
        scope.launch {
            try {
                val songs = withContext(Dispatchers.IO) { Radio.nextRound(ctx) }
                player?.addMediaItems(songs.map { Items.song(it, radio = true) })
            } catch (e: Exception) {
                // Radio.state carries the error for the UI
            } finally {
                refilling = false
            }
        }
    }

    /** @brief Read ahead: the current track from the play position and the next ones,
     *  until the configured minutes of audio are in the cache. */
    private fun prefetchNow() {
        val p = player ?: return
        val pf = prefetcher ?: return
        val n = p.mediaItemCount
        val cur = p.currentMediaItemIndex
        if (n == 0 || cur < 0 || cur >= n) { pf.stop(); return }
        val entries = ArrayList<PrefetchEntry>()
        val end = minOf(n, cur + 200)
        for (i in cur until end) {
            val m = p.getMediaItemAt(i)
            if (m.mediaId.isEmpty()) continue
            entries.add(PrefetchEntry(m.mediaId, Items.durationSec(m), Items.sizeOf(m)))
        }
        if (entries.isEmpty()) { pf.stop(); return }
        val budget = Prefs.get(this).audioAheadMin.coerceIn(0, 240) * 60.0
        pf.update(PrefetchPlan.plan(entries, 0, p.currentPosition / 1000.0, budget))
    }
}

/**
 * @brief Notification / lock-screen artwork: cimart:// URIs (Items.artUri) come from
 * Images (decrypted covers); anything else goes to media3's own loader.
 */
@OptIn(markerClass = [UnstableApi::class])
class ArtBitmapLoader(ctx: Context) : BitmapLoader {
    private val app = ctx.applicationContext
    private val exec: ListeningExecutorService = MoreExecutors.listeningDecorator(Executors.newSingleThreadExecutor())
    private val fallback = DataSourceBitmapLoader(app)

    override fun supportsMimeType(mimeType: String): Boolean = fallback.supportsMimeType(mimeType)

    override fun decodeBitmap(data: ByteArray): ListenableFuture<Bitmap> = fallback.decodeBitmap(data)

    override fun loadBitmap(uri: Uri): ListenableFuture<Bitmap> {
        val key = Items.artKey(uri) ?: return fallback.loadBitmap(uri)
        return exec.submit(Callable<Bitmap> { Images.loadBlocking(app, key) ?: throw IOException("no artwork") })
    }
}
