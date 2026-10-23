package app.cim.family

import org.json.JSONArray
import org.json.JSONObject

/*
 * Plain data + defensive JSON parsing for the server's web API, as the phone
 * sees it through inbound/api/<path>. No android.* / androidx.* imports, so
 * this file compiles (and is testable) on a plain JVM.
 *
 * Shapes follow modules/music, tv, movies and books on the server; every field
 * is read with a fallback because the server may add, drop or null fields.
 */

internal fun JSONObject.str(key: String, def: String = ""): String =
    if (!has(key) || isNull(key)) def else optString(key, def)

internal fun JSONObject.intOrNull(key: String): Int? =
    if (!has(key) || isNull(key)) null else {
        val v = opt(key)
        when (v) {
            is Number -> v.toInt()
            is String -> v.trim().toDoubleOrNull()?.toInt()
            else -> null
        }
    }

internal fun JSONObject.longOr(key: String, def: Long = 0L): Long =
    if (!has(key) || isNull(key)) def else {
        val v = opt(key)
        when (v) {
            is Number -> v.toLong()
            is String -> v.trim().toDoubleOrNull()?.toLong() ?: def
            else -> def
        }
    }

internal fun JSONObject.dbl(key: String, def: Double = 0.0): Double =
    if (!has(key) || isNull(key)) def else {
        val v = opt(key)
        when (v) {
            is Number -> v.toDouble()
            is String -> v.trim().toDoubleOrNull() ?: def
            else -> def
        }
    }

internal fun JSONObject.boolOrNull(key: String): Boolean? =
    if (!has(key) || isNull(key)) null else {
        val v = opt(key)
        when (v) {
            is Boolean -> v
            is Number -> v.toInt() != 0
            is String -> v == "1" || v.equals("true", ignoreCase = true)
            else -> null
        }
    }

internal fun JSONObject.strList(key: String): List<String> {
    val a = optJSONArray(key) ?: return emptyList()
    val out = ArrayList<String>(a.length())
    for (i in 0 until a.length()) {
        if (a.isNull(i)) continue
        val s = a.optString(i, "")
        if (s.isNotEmpty()) out.add(s)
    }
    return out
}

internal fun <T> JSONObject.objList(key: String, parse: (JSONObject) -> T?): List<T> {
    val a: JSONArray = optJSONArray(key) ?: return emptyList()
    val out = ArrayList<T>(a.length())
    for (i in 0 until a.length()) {
        val o = a.optJSONObject(i) ?: continue
        val v = try { parse(o) } catch (e: Exception) { null }
        if (v != null) out.add(v)
    }
    return out
}

/** @brief Image sources for [Images]: a proxied API route, or a library file
 *  (thumbnail first, the file itself through frames as a fallback). */
object ImageKey {
    /** `path` is the proxied route with its library-path part already URL-encoded. */
    fun api(path: String): String = "api:$path"
    fun file(rel: String): String = "file:$rel"
    fun musicCover(rel: String): String = api("music/cover/" + encPath(rel))
    fun bookCover(rel: String): String = api("books/cover/" + encPath(rel))

    /** Each segment percent-encoded, "/" kept (encodeURIComponent per segment). */
    fun encPath(rel: String): String =
        rel.split('/').joinToString("/") { java.net.URLEncoder.encode(it, "UTF-8").replace("+", "%20") }
}

// -- capabilities -----------------------------------------------------------
data class Caps(
    val api: Int,
    val chunk: Int,
    val modules: Map<String, Boolean>,
    val username: String,
    val displayName: String,
) {
    fun has(module: String): Boolean = modules[module] == true
    val v2: Boolean get() = api >= 2

    fun toJson(): JSONObject {
        val m = JSONObject()
        for ((k, v) in modules) m.put(k, v)
        return JSONObject().put("ok", true).put("api", api).put("chunk", chunk).put("modules", m)
            .put("account", JSONObject().put("username", username).put("display_name", displayName))
    }

    companion object {
        const val DEFAULT_CHUNK = 1 shl 20

        /** A server without inbound/caps: the v1 photo gallery only. */
        val V1 = Caps(1, DEFAULT_CHUNK, mapOf("gallery" to true), "", "")

        fun parse(j: JSONObject): Caps {
            val mods = HashMap<String, Boolean>()
            val m = j.optJSONObject("modules")
            if (m != null) {
                val it = m.keys()
                while (it.hasNext()) {
                    val k = it.next()
                    mods[k] = m.optBoolean(k, false)
                }
            }
            val acct = j.optJSONObject("account") ?: JSONObject()
            val chunk = j.optInt("chunk", DEFAULT_CHUNK).let { if (it <= 0) DEFAULT_CHUNK else it }
            return Caps(j.optInt("api", 2), chunk, mods, acct.str("username"), acct.str("display_name"))
        }
    }
}

// -- music --------------------------------------------------------------------
data class Song(
    val relPath: String,
    val title: String,
    val artist: String,
    val album: String,
    val albumArtist: String,
    val durationSec: Double,
    val track: Int?,
    val year: String,
    val genre: String,
    val hasCover: Boolean?,
    val size: Long,
) {
    val displayTitle: String get() = title.ifEmpty { relPath.substringAfterLast('/').substringBeforeLast('.') }
    val displayArtist: String get() = artist.ifEmpty { albumArtist }
    /** null when the server knows the track has no art. */
    val coverKey: String? get() = if (hasCover == false) null else ImageKey.musicCover(relPath)

    fun toJson(): JSONObject = JSONObject().put("rel_path", relPath).put("title", title).put("artist", artist)
        .put("album", album).put("albumartist", albumArtist).put("duration", durationSec)
        .put("track", track ?: JSONObject.NULL).put("year", year).put("genre", genre)
        .put("has_cover", hasCover ?: JSONObject.NULL).put("size", size)

    companion object {
        fun parse(j: JSONObject): Song? {
            val rel = j.str("rel_path")
            if (rel.isEmpty()) return null
            return Song(rel, j.str("title"), j.str("artist"), j.str("album"), j.str("albumartist"),
                j.dbl("duration"), j.intOrNull("track"), j.str("year"), j.str("genre"),
                j.boolOrNull("has_cover"), j.longOr("size"))
        }
        fun list(j: JSONObject, key: String = "songs"): List<Song> = j.objList(key) { parse(it) }
    }
}

data class Album(
    val album: String,
    val artist: String,
    val tracks: Int,
    val year: String,
    val genre: String,
    val durationSec: Double,
    val coverRel: String,
    val artRel: String,
) {
    val coverKey: String?
        get() = when {
            coverRel.isNotEmpty() -> ImageKey.musicCover(coverRel)
            artRel.isNotEmpty() -> ImageKey.file(artRel)
            else -> null
        }

    companion object {
        fun parse(j: JSONObject): Album? = Album(j.str("album", "(unknown)"), j.str("artist"), j.optInt("tracks", 0),
            j.str("year"), j.str("genre"), j.dbl("duration"), j.str("cover_rel"), j.str("art_rel"))
        fun list(j: JSONObject): List<Album> = j.objList("albums") { parse(it) }
    }
}

data class Artist(
    val name: String,
    val tracks: Int,
    val albums: Int,
    val durationSec: Double,
    val coverRel: String,
    val pictureRel: String,
) {
    val coverKey: String?
        get() = when {
            pictureRel.isNotEmpty() -> ImageKey.file(pictureRel)
            coverRel.isNotEmpty() -> ImageKey.musicCover(coverRel)
            else -> null
        }

    companion object {
        fun parse(j: JSONObject): Artist? {
            val n = j.str("name")
            if (n.isEmpty()) return null
            return Artist(n, j.optInt("tracks", 0), j.optInt("albums", 0), j.dbl("duration"),
                j.str("cover_rel"), j.str("picture_rel"))
        }
        fun list(j: JSONObject): List<Artist> = j.objList("artists") { parse(it) }
    }
}

data class Genre(val name: String, val tracks: Int, val albums: Int, val artists: Int, val coverRel: String) {
    val coverKey: String? get() = if (coverRel.isNotEmpty()) ImageKey.musicCover(coverRel) else null

    companion object {
        fun parse(j: JSONObject): Genre? {
            val n = j.str("name")
            if (n.isEmpty()) return null
            return Genre(n, j.optInt("tracks", 0), j.optInt("albums", 0), j.optInt("artists", 0), j.str("cover_rel"))
        }
        fun list(j: JSONObject): List<Genre> = j.objList("genres") { parse(it) }
    }
}

data class Playlist(val id: Long, val name: String, val count: Int, val seconds: Double, val updatedAt: Double) {
    companion object {
        fun parse(j: JSONObject): Playlist? {
            val id = j.longOr("id", -1L)
            if (id < 0) return null
            return Playlist(id, j.str("name", "Playlist $id"), j.optInt("count", 0), j.dbl("seconds"), j.dbl("updated_at"))
        }
        fun list(j: JSONObject): List<Playlist> = j.objList("playlists") { parse(it) }
    }
}

// -- tv / movies ----------------------------------------------------------------
data class SubtitleRef(val rel: String, val lang: String, val forced: Boolean, val sdh: Boolean) {
    val label: String
        get() = listOf(lang.ifEmpty { "subtitles" }, if (forced) "forced" else "", if (sdh) "SDH" else "")
            .filter { it.isNotEmpty() }.joinToString(" ")

    companion object {
        fun parse(j: JSONObject): SubtitleRef? {
            val rel = j.str("rel")
            if (rel.isEmpty()) return null
            return SubtitleRef(rel, j.str("lang"), j.boolOrNull("forced") ?: false, j.boolOrNull("sdh") ?: false)
        }
        fun list(j: JSONObject): List<SubtitleRef> = j.objList("subtitles") { parse(it) }
    }
}

data class Show(
    val relPath: String,
    val title: String,
    val year: String,
    val plot: String,
    val genres: List<String>,
    val posterRel: String,
    val seasons: Int,
    val episodes: Int,
) {
    val posterKey: String? get() = if (posterRel.isNotEmpty()) ImageKey.file(posterRel) else null

    companion object {
        fun parse(j: JSONObject): Show? {
            val rel = j.str("rel_path")
            if (rel.isEmpty()) return null
            return Show(rel, j.str("title", rel.substringAfterLast('/')), j.str("year"), j.str("plot"),
                j.strList("genres"), j.str("poster_rel"), j.optInt("seasons", 0), j.optInt("episodes", 0))
        }
        fun list(j: JSONObject): List<Show> = j.objList("shows") { parse(it) }
    }
}

data class Season(val season: Int?, val episodes: Int, val posterRel: String, val label: String) {
    val posterKey: String? get() = if (posterRel.isNotEmpty()) ImageKey.file(posterRel) else null

    companion object {
        fun parse(j: JSONObject): Season? {
            val s = j.intOrNull("season")
            val label = j.str("label").ifEmpty { if (s == null) "Unknown season" else if (s == 0) "Specials" else "Season $s" }
            return Season(s, j.optInt("episodes", 0), j.str("poster_rel"), label)
        }
        fun list(j: JSONObject): List<Season> = j.objList("seasons") { parse(it) }
    }
}

data class Episode(
    val relPath: String,
    val showRel: String,
    val season: Int?,
    val episode: Int?,
    val episodeEnd: Int?,
    val title: String,
    val plot: String,
    val aired: String,
    val thumbRel: String,
    val seasonPosterRel: String,
    val subtitles: List<SubtitleRef>,
) {
    val code: String
        get() {
            val s = season; val e = episode
            if (s == null && e == null) return ""
            val sb = StringBuilder()
            if (s != null) sb.append("S").append(s.toString().padStart(2, '0'))
            if (e != null) sb.append("E").append(e.toString().padStart(2, '0'))
            val end = episodeEnd
            if (end != null && e != null && end != e) sb.append("-E").append(end.toString().padStart(2, '0'))
            return sb.toString()
        }
    val displayTitle: String get() = title.ifEmpty { relPath.substringAfterLast('/').substringBeforeLast('.') }
    val thumbKey: String?
        get() = when {
            thumbRel.isNotEmpty() -> ImageKey.file(thumbRel)
            seasonPosterRel.isNotEmpty() -> ImageKey.file(seasonPosterRel)
            else -> null
        }

    companion object {
        fun parse(j: JSONObject): Episode? {
            val rel = j.str("rel_path")
            if (rel.isEmpty()) return null
            return Episode(rel, j.str("show_rel"), j.intOrNull("season"), j.intOrNull("episode"), j.intOrNull("episode_end"),
                j.str("title"), j.str("plot"), j.str("aired"), j.str("thumb_rel"), j.str("season_poster_rel"),
                SubtitleRef.list(j))
        }
        fun list(j: JSONObject): List<Episode> = j.objList("episodes") { parse(it) }
    }
}

data class MoviePart(val relPath: String, val part: Int?, val label: String)

data class Movie(
    val relPath: String,
    val title: String,
    val year: String,
    val edition: String,
    val plot: String,
    val genres: List<String>,
    val posterRel: String,
    val subtitles: List<SubtitleRef>,
    val parts: List<MoviePart>,
) {
    val posterKey: String? get() = if (posterRel.isNotEmpty()) ImageKey.file(posterRel) else null
    val displayTitle: String get() = title.ifEmpty { relPath.substringAfterLast('/').substringBeforeLast('.') }

    companion object {
        fun parse(j: JSONObject): Movie? {
            val rel = j.str("rel_path")
            if (rel.isEmpty()) return null
            val parts = j.objList("parts") { p ->
                val r = p.str("rel_path")
                if (r.isEmpty()) null else MoviePart(r, p.intOrNull("part"), p.str("label"))
            }
            return Movie(rel, j.str("title"), j.str("year"), j.str("edition"), j.str("plot"), j.strList("genres"),
                j.str("poster_rel"), SubtitleRef.list(j), parts.ifEmpty { listOf(MoviePart(rel, null, "")) })
        }
        fun list(j: JSONObject): List<Movie> = j.objList("movies") { parse(it) }
    }
}

// -- books ----------------------------------------------------------------------
data class Book(
    val relPath: String,
    val fmt: String,
    val kind: String,
    val title: String,
    val authors: List<String>,
    val series: String,
    val seriesIndex: String,
    val description: String,
    val hasCover: Boolean,
    val size: Long,
) {
    val coverKey: String? get() = if (hasCover) ImageKey.bookCover(relPath) else null
    val displayTitle: String get() = title.ifEmpty { relPath.substringAfterLast('/').substringBeforeLast('.') }

    companion object {
        fun parse(j: JSONObject): Book? {
            val rel = j.str("rel_path")
            if (rel.isEmpty()) return null
            return Book(rel, j.str("fmt"), j.str("kind"), j.str("title"), j.strList("authors"), j.str("series"),
                j.str("series_index"), j.str("description"), j.boolOrNull("has_cover") ?: false, j.longOr("size"))
        }
        fun list(j: JSONObject): List<Book> = j.objList("books") { parse(it) }
    }
}

/** @brief "3:07" / "1:02:03" from seconds. */
fun formatDuration(sec: Double): String {
    if (sec.isNaN() || sec <= 0) return ""
    val t = sec.toLong()
    val h = t / 3600; val m = (t % 3600) / 60; val s = t % 60
    return if (h > 0) "%d:%02d:%02d".format(h, m, s) else "%d:%02d".format(m, s)
}
