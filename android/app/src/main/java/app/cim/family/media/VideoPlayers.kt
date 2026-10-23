package app.cim.family.media

import android.content.Context
import androidx.annotation.OptIn
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import app.cim.family.ApiV2
import app.cim.family.PrefetchEntry
import app.cim.family.PrefetchPlan
import app.cim.family.Prefs
import app.cim.family.SubtitleRef
import java.io.File
import java.security.MessageDigest

/** @brief Helpers for the in-app video player (the screen owns the ExoPlayer). */
@OptIn(markerClass = [UnstableApi::class])
object VideoPlayers {
    /** @brief A local ExoPlayer over the same cache / decrypting stack as audio, with a
     *  video-sized memory buffer; the disk read-ahead is [prefetch]. */
    fun build(ctx: Context): ExoPlayer {
        val load = DefaultLoadControl.Builder()
            .setBufferDurationsMs(15_000, 90_000, 2_500, 5_000)
            .build()
        val attrs = AudioAttributes.Builder().setUsage(C.USAGE_MEDIA).setContentType(C.AUDIO_CONTENT_TYPE_MOVIE).build()
        return ExoPlayer.Builder(ctx)
            .setMediaSourceFactory(DefaultMediaSourceFactory(MediaCache.playbackFactory(ctx)))
            .setLoadControl(load)
            .setAudioAttributes(attrs, true)
            .setHandleAudioBecomingNoisy(true)
            .setWakeMode(C.WAKE_MODE_NETWORK)
            .build()
    }

    /** @brief Read ahead the video's next minutes (Settings: video read-ahead) into the cache. */
    fun prefetch(ctx: Context, pf: Prefetcher, rel: String, positionMs: Long, durationMs: Long) {
        if (durationMs <= 0) return
        val size = SealedFrameDataSource.SIZES[rel] ?: return
        val budget = Prefs.get(ctx).videoAheadMin.coerceIn(0, 120) * 60.0
        pf.update(PrefetchPlan.plan(listOf(PrefetchEntry(rel, durationMs / 1000.0, size)), 0, positionMs / 1000.0, budget))
    }

    /**
     * @brief The subtitle files (WebVTT from the server's subtitle route) saved to the
     * app cache, as (file, language label). Ones that fail are left out.
     */
    fun fetchSubtitles(ctx: Context, route: String, subs: List<SubtitleRef>): List<Pair<File, String>> {
        if (route.isEmpty() || subs.isEmpty()) return emptyList()
        val dir = File(ctx.cacheDir, "subs").also { it.mkdirs() }
        val api = ApiV2(Prefs.get(ctx))
        val out = ArrayList<Pair<File, String>>()
        for (s in subs) {
            val h = MessageDigest.getInstance("SHA-1").digest(s.rel.toByteArray()).joinToString("") { "%02x".format(it) }
            val f = File(dir, "$h.vtt")
            try {
                if (!f.exists() || f.length() == 0L) f.writeBytes(api.getBytes(api.pathRoute(route, s.rel)).first)
                out.add(f to s.label)
            } catch (e: Exception) {
                f.delete()
            }
        }
        return out
    }
}
