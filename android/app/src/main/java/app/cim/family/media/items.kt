package app.cim.family.media

import android.net.Uri
import android.os.Bundle
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.MimeTypes
import app.cim.family.CimUri
import app.cim.family.Song
import org.json.JSONObject
import java.io.File
import java.net.URLDecoder
import java.net.URLEncoder

/** @brief MediaItems for the player. mediaId = rel_path; the URI is cim://media/<rel>. */
object Items {
    const val EXTRA_DUR = "cim_dur"          // seconds (double)
    const val EXTRA_SIZE = "cim_size"        // plaintext bytes (long), 0 = unknown
    const val EXTRA_KIND = "cim_kind"        // "audio" | "video"
    const val EXTRA_ART = "cim_art"          // Images key of the cover
    const val EXTRA_SONG = "cim_song"        // the Song as JSON (downloads, queue rebuild)
    const val EXTRA_RADIO = "cim_radio"      // 1 = queued by the radio

    private const val ART_PREFIX = "cimart://img/"

    fun artUri(key: String): Uri = Uri.parse(ART_PREFIX + URLEncoder.encode(key, "UTF-8"))

    fun artKey(uri: Uri?): String? {
        val s = uri?.toString() ?: return null
        if (!s.startsWith(ART_PREFIX)) return null
        return try { URLDecoder.decode(s.substring(ART_PREFIX.length), "UTF-8") } catch (e: Exception) { null }
    }

    fun song(s: Song, radio: Boolean = false): MediaItem {
        val extras = Bundle()
        extras.putDouble(EXTRA_DUR, s.durationSec)
        extras.putLong(EXTRA_SIZE, s.size)
        extras.putString(EXTRA_KIND, "audio")
        extras.putString(EXTRA_SONG, s.toJson().toString())
        if (radio) extras.putInt(EXTRA_RADIO, 1)
        val md = MediaMetadata.Builder()
            .setTitle(s.displayTitle)
            .setArtist(s.displayArtist)
            .setAlbumTitle(s.album)
            .setExtras(extras)
        val art = s.coverKey
        if (art != null) {
            extras.putString(EXTRA_ART, art)
            md.setArtworkUri(artUri(art))
        }
        return MediaItem.Builder()
            .setMediaId(s.relPath)
            .setUri(Uri.parse(CimUri.of(s.relPath)))
            .setMediaMetadata(md.build())
            .build()
    }

    /** @brief A video (episode, movie, library clip) with optional side-loaded WebVTT files (file, label). */
    fun video(rel: String, title: String, subtitle: String, artKey: String?,
              subs: List<Pair<File, String>>): MediaItem {
        val extras = Bundle()
        extras.putString(EXTRA_KIND, "video")
        val md = MediaMetadata.Builder().setTitle(title).setArtist(subtitle).setExtras(extras)
        if (artKey != null) {
            extras.putString(EXTRA_ART, artKey)
            md.setArtworkUri(artUri(artKey))
        }
        val configs = ArrayList<MediaItem.SubtitleConfiguration>()
        for ((f, label) in subs) {
            configs.add(MediaItem.SubtitleConfiguration.Builder(Uri.fromFile(f))
                .setMimeType(MimeTypes.TEXT_VTT)
                .setLabel(label.ifEmpty { "subtitles" })
                .build())
        }
        return MediaItem.Builder()
            .setMediaId(rel)
            .setUri(Uri.parse(CimUri.of(rel)))
            .setMediaMetadata(md.build())
            .setSubtitleConfigurations(configs)
            .build()
    }

    /** @brief An item that crossed a process/binder boundary without its URI: rebuild it. */
    fun withUri(item: MediaItem): MediaItem {
        if (item.localConfiguration != null || item.mediaId.isEmpty()) return item
        return item.buildUpon().setUri(Uri.parse(CimUri.of(item.mediaId))).build()
    }

    fun songOf(item: MediaItem?): Song? {
        val raw = item?.mediaMetadata?.extras?.getString(EXTRA_SONG) ?: return null
        return try { Song.parse(JSONObject(raw)) } catch (e: Exception) { null }
    }

    fun isRadio(item: MediaItem?): Boolean = (item?.mediaMetadata?.extras?.getInt(EXTRA_RADIO, 0) ?: 0) == 1
    fun durationSec(item: MediaItem?): Double = item?.mediaMetadata?.extras?.getDouble(EXTRA_DUR, 0.0) ?: 0.0
    fun sizeOf(item: MediaItem?): Long = item?.mediaMetadata?.extras?.getLong(EXTRA_SIZE, 0L) ?: 0L
    fun artKeyOf(item: MediaItem?): String? = item?.mediaMetadata?.extras?.getString(EXTRA_ART)
}
