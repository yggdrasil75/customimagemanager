package app.cim.family

import java.net.URLDecoder
import java.net.URLEncoder

/*
 * Pure range math shared by the decrypting DataSource, the prefetcher and the
 * downloader. No android.* / androidx.* imports: compiles on a plain JVM.
 */

/** @brief `cim://media/<urlencoded rel_path>`: the URI (and cache key) of a server file. */
object CimUri {
    const val SCHEME = "cim"
    private const val PREFIX = "cim://media/"

    fun of(rel: String): String = PREFIX + URLEncoder.encode(rel, "UTF-8")

    /** @brief The rel_path of a cim:// URI string, or null for anything else. */
    fun rel(uri: String): String? {
        if (!uri.startsWith(PREFIX)) return null
        val enc = uri.substring(PREFIX.length)
        if (enc.isEmpty()) return null
        return try { URLDecoder.decode(enc, "UTF-8") } catch (e: Exception) { null }
    }
}

/** @brief Byte offset <-> frame index of the framed stream (chunk = Caps.chunk). */
object FrameMath {
    /** Frames fetched per request while playing (2-4: small enough to start fast). */
    const val OPEN_BATCH = 2
    const val READ_BATCH = 4
    const val MAX_BATCH = 64

    fun frameOf(position: Long, chunk: Int): Long {
        require(chunk > 0 && position >= 0)
        return position / chunk
    }

    fun frameStart(frame: Long, chunk: Int): Long = frame * chunk

    /** max(1, ceil(size / chunk)) - the server's frame_count. */
    fun frameCount(size: Long, chunk: Int): Long {
        require(chunk > 0 && size >= 0)
        if (size == 0L) return 1
        return (size + chunk - 1) / chunk
    }

    /** How many frames a response must hold: min(requested, total - first). */
    fun expected(first: Long, requested: Int, totalFrames: Long): Int {
        if (first < 0 || first >= totalFrames || requested < 1) return 0
        return minOf(requested.toLong(), totalFrames - first).toInt()
    }

    /** Bytes left to read from position (size known), capped by a requested length (-1 = unset). */
    fun remaining(position: Long, size: Long, requestedLength: Long): Long {
        if (position > size) return -1
        val left = size - position
        return if (requestedLength < 0) left else minOf(left, requestedLength)
    }
}

/** @brief One file to pull into the cache: bytes [start, start+length) (length -1 = to the end). */
data class PrefetchRange(val rel: String, val start: Long, val length: Long)

/** @brief A queue entry as the prefetcher sees it. */
data class PrefetchEntry(val rel: String, val durationSec: Double, val size: Long)

object PrefetchPlan {
    /**
     * @brief The byte ranges that put `budgetSec` seconds of forward playback in the
     * cache: the rest of the current entry from `positionSec`, then the following
     * entries, the last one partially (bytes proportional to time - exact enough
     * for constant- and variable-bitrate files alike over minutes).
     * Entries with an unknown duration or size are fetched whole and count as
     * `fallbackSec` seconds.
     */
    fun plan(entries: List<PrefetchEntry>, currentIndex: Int, positionSec: Double, budgetSec: Double,
             fallbackSec: Double = 240.0): List<PrefetchRange> {
        val out = ArrayList<PrefetchRange>()
        if (currentIndex < 0 || currentIndex >= entries.size || budgetSec <= 0) return out
        var left = budgetSec
        for (i in currentIndex until entries.size) {
            if (left <= 0) break
            val e = entries[i]
            val known = e.durationSec > 0 && e.size > 0
            val dur = if (e.durationSec > 0) e.durationSec else fallbackSec
            val from = if (i == currentIndex) positionSec.coerceIn(0.0, dur) else 0.0
            val span = dur - from
            if (!known) {
                out.add(PrefetchRange(e.rel, 0, -1))
                left -= span
                continue
            }
            val startByte = ((from / dur) * e.size).toLong().coerceIn(0L, e.size)
            if (span <= left) {
                out.add(PrefetchRange(e.rel, startByte, -1))
                left -= span
            } else {
                val bytes = ((left / dur) * e.size).toLong().coerceAtLeast(1L)
                out.add(PrefetchRange(e.rel, startByte, minOf(bytes, e.size - startByte)))
                left = 0.0
            }
        }
        return out.filter { it.length != 0L }
    }
}
