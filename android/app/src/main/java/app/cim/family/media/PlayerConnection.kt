package app.cim.family.media

import android.content.ComponentName
import android.content.Context
import android.content.SharedPreferences
import androidx.core.content.ContextCompat
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.session.MediaController
import androidx.media3.session.SessionToken
import com.google.common.util.concurrent.ListenableFuture
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/**
 * @brief The UI's handle on PlaybackService: one MediaController per process,
 * connected lazily; actions issued before it connects run once it does.
 * [state] is a snapshot refreshed on every player event (positions are read
 * on demand with [position]). Main thread only.
 */
object PlayerConnection {
    data class QueueEntry(val mediaId: String, val title: String, val artist: String)

    data class Snapshot(
        val connected: Boolean = false,
        val hasItem: Boolean = false,
        val isPlaying: Boolean = false,
        val buffering: Boolean = false,
        val mediaId: String = "",
        val title: String = "",
        val artist: String = "",
        val album: String = "",
        val artKey: String? = null,
        val index: Int = -1,
        val count: Int = 0,
        val durationMs: Long = 0L,
        val queue: List<QueueEntry> = emptyList(),
        val radio: Boolean = false,
        val error: String = "",
    )

    private val st = MutableStateFlow(Snapshot())
    val state: StateFlow<Snapshot> get() = st

    private var future: ListenableFuture<MediaController>? = null
    private var controller: MediaController? = null
    private val pending = ArrayList<(MediaController) -> Unit>()

    private val listener = object : Player.Listener {
        override fun onEvents(player: Player, events: Player.Events) { refresh() }
    }

    fun connect(ctx: Context) {
        if (future != null) return
        val app = ctx.applicationContext
        val token = SessionToken(app, ComponentName(app, PlaybackService::class.java))
        val f = MediaController.Builder(app, token).buildAsync()
        future = f
        f.addListener({
            val c: MediaController? = try { f.get() } catch (e: Exception) { null }
            if (c == null) {
                future = null
                st.value = st.value.copy(connected = false, error = "player service unavailable")
                return@addListener
            }
            controller = c
            c.addListener(listener)
            refresh()
            val todo = ArrayList(pending)
            pending.clear()
            for (a in todo) a(c)
        }, ContextCompat.getMainExecutor(app))
    }

    fun release() {
        controller?.removeListener(listener)
        controller = null
        future?.let { MediaController.releaseFuture(it) }
        future = null
        pending.clear()
        st.value = Snapshot()
    }

    /** @brief Run on the controller now, or as soon as it connects. */
    fun run(ctx: Context, action: (MediaController) -> Unit) {
        val c = controller
        if (c != null) { action(c); return }
        pending.add(action)
        connect(ctx)
    }

    fun play(ctx: Context, items: List<MediaItem>, start: Int = 0) {
        if (items.isEmpty()) return
        run(ctx) { c ->
            c.setMediaItems(items, start.coerceIn(0, items.size - 1), 0L)
            c.prepare()
            c.play()
        }
    }

    fun enqueue(ctx: Context, items: List<MediaItem>) {
        if (items.isEmpty()) return
        run(ctx) { c ->
            val empty = c.mediaItemCount == 0
            c.addMediaItems(items)
            if (empty) { c.prepare(); c.play() }
        }
    }

    fun playNext(ctx: Context, items: List<MediaItem>) {
        if (items.isEmpty()) return
        run(ctx) { c ->
            if (c.mediaItemCount == 0) { c.setMediaItems(items); c.prepare(); c.play() }
            else c.addMediaItems(c.currentMediaItemIndex + 1, items)
        }
    }

    fun toggle() {
        val c = controller ?: return
        if (c.isPlaying) c.pause() else {
            if (c.playbackState == Player.STATE_IDLE) c.prepare()
            c.play()
        }
    }

    fun pause() { controller?.pause() }
    fun next() { controller?.seekToNextMediaItem() }
    fun previous() {
        val c = controller ?: return
        if (c.currentPosition > 5_000L || !c.hasPreviousMediaItem()) c.seekTo(0L) else c.seekToPreviousMediaItem()
    }
    fun seekTo(ms: Long) { controller?.seekTo(ms) }
    fun skipTo(index: Int) {
        val c = controller ?: return
        if (index in 0 until c.mediaItemCount) { c.seekToDefaultPosition(index); c.play() }
    }
    fun remove(index: Int) {
        val c = controller ?: return
        if (index in 0 until c.mediaItemCount) c.removeMediaItem(index)
    }
    fun clear() { controller?.let { it.stop(); it.clearMediaItems() } }

    fun position(): Long = controller?.currentPosition ?: 0L
    fun duration(): Long {
        val d = controller?.duration ?: 0L
        return if (d < 0) 0L else d
    }

    /** @brief The songs in the queue (for "download the radio round"). */
    fun queueItems(): List<MediaItem> {
        val c = controller ?: return emptyList()
        return (0 until c.mediaItemCount).map { c.getMediaItemAt(it) }
    }

    private fun refresh() {
        val c = controller ?: return
        val item = c.currentMediaItem
        val md = item?.mediaMetadata
        val q = ArrayList<QueueEntry>(c.mediaItemCount)
        for (i in 0 until c.mediaItemCount) {
            val m = c.getMediaItemAt(i)
            q.add(QueueEntry(m.mediaId, m.mediaMetadata.title?.toString() ?: m.mediaId.substringAfterLast('/'),
                m.mediaMetadata.artist?.toString() ?: ""))
        }
        val err = c.playerError
        val d = c.duration
        st.value = Snapshot(
            connected = true,
            hasItem = item != null,
            isPlaying = c.isPlaying,
            buffering = c.playbackState == Player.STATE_BUFFERING,
            mediaId = item?.mediaId ?: "",
            title = md?.title?.toString() ?: (item?.mediaId?.substringAfterLast('/') ?: ""),
            artist = md?.artist?.toString() ?: "",
            album = md?.albumTitle?.toString() ?: "",
            artKey = Items.artKeyOf(item),
            index = c.currentMediaItemIndex,
            count = c.mediaItemCount,
            durationMs = if (d < 0) 0L else d,
            queue = q,
            radio = Items.isRadio(item),
            error = if (err != null) (err.cause?.message ?: err.message ?: "playback failed") else "",
        )
    }
}

/** @brief Video resume positions, per rel_path (plain prefs: not secret). */
object ResumeStore {
    private fun p(ctx: Context): SharedPreferences = ctx.applicationContext.getSharedPreferences("cim_resume", Context.MODE_PRIVATE)

    fun get(ctx: Context, rel: String): Long = p(ctx).getLong(rel, 0L)

    /** Near the end counts as watched: the next open starts from the beginning. */
    fun put(ctx: Context, rel: String, posMs: Long, durMs: Long) {
        val e = p(ctx).edit()
        if (posMs < 10_000L || (durMs > 0 && posMs > durMs * 95 / 100)) e.remove(rel) else e.putLong(rel, posMs)
        e.apply()
    }
}
