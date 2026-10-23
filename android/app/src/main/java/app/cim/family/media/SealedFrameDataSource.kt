package app.cim.family.media

import android.content.Context
import android.net.ConnectivityManager
import android.net.Uri
import androidx.annotation.OptIn
import androidx.media3.common.C
import androidx.media3.common.PlaybackException
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.BaseDataSource
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSourceException
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.DefaultDataSource
import androidx.media3.datasource.TransferListener
import app.cim.family.ApiV2
import app.cim.family.Caps
import app.cim.family.CapsStore
import app.cim.family.CimUri
import app.cim.family.FrameMath
import app.cim.family.FramesResult
import app.cim.family.HttpApiException
import app.cim.family.Prefs
import java.io.IOException
import java.util.concurrent.ConcurrentHashMap

/** @brief Is the active network metered (mobile data, a metered hotspot)? */
fun isMetered(ctx: Context): Boolean {
    val cm = ctx.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager ?: return false
    return cm.isActiveNetworkMetered
}

/**
 * @brief A media3 DataSource for `cim://media/<urlencoded rel_path>`: reads the file
 * through inbound/frames a few 1 MiB frames at a time, decrypting and checking
 * every slice (Crypto.Opener.openFrames). A seek is a new open() at the new
 * position: the frame holding it is fetched and served from its offset.
 */
@OptIn(markerClass = [UnstableApi::class])
class SealedFrameDataSource(private val ctx: Context, private val mode: Int) : BaseDataSource(true) {
    private var uri: Uri? = null
    private var rel: String = ""
    private var chunk: Int = Caps.DEFAULT_CHUNK
    private var size: Long = -1
    private var position: Long = 0
    private var bytesRemaining: Long = 0
    private var buf: ByteArray = ByteArray(0)
    private var bufStart: Long = 0
    private var opened = false

    override fun open(dataSpec: DataSpec): Long {
        val r = CimUri.rel(dataSpec.uri.toString())
            ?: throw DataSourceException("not a cim:// uri: ${dataSpec.uri}", PlaybackException.ERROR_CODE_IO_UNSPECIFIED)
        val prefs = Prefs.get(ctx)
        val allowed = when (mode) {
            MODE_PREFETCH -> prefs.cellularPrefetch
            MODE_DOWNLOAD -> !prefs.downloadsWifiOnly
            else -> prefs.cellularStreaming
        }
        if (!allowed && isMetered(ctx))
            throw DataSourceException("mobile data is off for " + (when (mode) {
                MODE_PREFETCH -> "read-ahead"; MODE_DOWNLOAD -> "downloads"; else -> "streaming" }) +
                " (Settings)", PlaybackException.ERROR_CODE_IO_NETWORK_CONNECTION_FAILED)
        uri = dataSpec.uri
        rel = r
        chunk = CapsStore.chunk(ctx)
        position = dataSpec.position
        buf = ByteArray(0); bufStart = 0
        transferInitializing(dataSpec)
        size = SIZES[rel] ?: -1L
        if (size < 0 || position < size) {
            try {
                fill(FrameMath.OPEN_BATCH)
            } catch (e: HttpApiException) {
                // 416: the position is at/after the end; the body tells the size
                val s = e.body?.optLong("size", -1L) ?: -1L
                if (e.code != 416 || s < 0) throw DataSourceException(e.message, e, PlaybackException.ERROR_CODE_IO_BAD_HTTP_STATUS)
                size = s; SIZES[rel] = s
            } catch (e: IOException) {
                throw e
            } catch (e: Exception) {
                throw DataSourceException(e.message, e, PlaybackException.ERROR_CODE_IO_UNSPECIFIED)
            }
        }
        val left = FrameMath.remaining(position, size, if (dataSpec.length == C.LENGTH_UNSET.toLong()) -1L else dataSpec.length)
        if (left < 0) throw DataSourceException(PlaybackException.ERROR_CODE_IO_READ_POSITION_OUT_OF_RANGE)
        bytesRemaining = left
        opened = true
        transferStarted(dataSpec)
        return bytesRemaining
    }

    /** @brief Fetch the frames holding `position` (and a few after it). */
    private fun fill(batch: Int) {
        val first = FrameMath.frameOf(position, chunk)
        val res: FramesResult = ApiV2(Prefs.get(ctx)).frames(rel, first, batch)
        size = res.size
        SIZES[rel] = res.size
        buf = res.data
        bufStart = FrameMath.frameStart(first, chunk)
    }

    override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
        if (length == 0) return 0
        if (bytesRemaining == 0L) return C.RESULT_END_OF_INPUT
        if (position < bufStart || position >= bufStart + buf.size) {
            try {
                fill(FrameMath.READ_BATCH)
            } catch (e: IOException) {
                throw e
            } catch (e: Exception) {
                throw DataSourceException(e.message, e, PlaybackException.ERROR_CODE_IO_UNSPECIFIED)
            }
            if (position < bufStart || position >= bufStart + buf.size) {
                // the server's file is shorter than it said: never loop forever
                throw DataSourceException("file ended early", PlaybackException.ERROR_CODE_IO_UNSPECIFIED)
            }
        }
        val inBuf = (bufStart + buf.size - position).toInt()
        val n = minOf(length.toLong(), bytesRemaining, inBuf.toLong()).toInt()
        System.arraycopy(buf, (position - bufStart).toInt(), buffer, offset, n)
        position += n
        bytesRemaining -= n
        bytesTransferred(n)
        return n
    }

    override fun getUri(): Uri? = uri

    override fun close() {
        uri = null
        buf = ByteArray(0)
        if (opened) {
            opened = false
            transferEnded()
        }
    }

    class Factory(private val ctx: Context, private val mode: Int = MODE_STREAM) : DataSource.Factory {
        override fun createDataSource(): DataSource = SealedFrameDataSource(ctx.applicationContext, mode)
    }

    companion object {
        const val MODE_STREAM = 0
        const val MODE_PREFETCH = 1
        const val MODE_DOWNLOAD = 2

        /** Plaintext sizes seen per rel_path (they only change when the file does). */
        val SIZES = ConcurrentHashMap<String, Long>()
    }
}

/**
 * @brief Routes cim:// to the sealed stack and everything else (file:// subtitles,
 * content://) to DefaultDataSource, so one factory serves the whole player.
 */
@OptIn(markerClass = [UnstableApi::class])
class SchemeRoutingDataSource(private val cim: DataSource, private val other: DataSource) : DataSource {
    private var active: DataSource? = null

    override fun addTransferListener(transferListener: TransferListener) {
        cim.addTransferListener(transferListener)
        other.addTransferListener(transferListener)
    }

    override fun open(dataSpec: DataSpec): Long {
        val ds = if (dataSpec.uri.scheme == CimUri.SCHEME) cim else other
        active = ds
        return ds.open(dataSpec)
    }

    override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
        val ds = active ?: throw IOException("read before open")
        return ds.read(buffer, offset, length)
    }

    override fun getUri(): Uri? = active?.uri

    override fun getResponseHeaders(): Map<String, List<String>> = active?.responseHeaders ?: emptyMap()

    override fun close() {
        val ds = active
        active = null
        ds?.close()
    }

    class Factory(private val ctx: Context, private val cim: DataSource.Factory) : DataSource.Factory {
        private val other = DefaultDataSource.Factory(ctx.applicationContext)
        override fun createDataSource(): DataSource = SchemeRoutingDataSource(cim.createDataSource(), other.createDataSource())
    }
}
