package app.cim.family

import java.io.File

/** @brief Whole files through inbound/frames (books for an external reader). */
object FileFetch {
    private const val BATCH = 8

    /**
     * @brief Download and decrypt `rel` into `dst` (via a temp file; every slice checked
     * by openFrames). Returns the server's mime. `progress(done, total)` after each batch.
     */
    fun toFile(prefs: Prefs, rel: String, dst: File, progress: (Long, Long) -> Unit = { _, _ -> }): String {
        val api = ApiV2(prefs)
        val tmp = File(dst.path + ".part")
        var mime = "application/octet-stream"
        try {
            tmp.outputStream().use { out ->
                var first = 0L
                var total = 1L
                var size = 0L
                var done = 0L
                while (first < total) {
                    val r = api.frames(rel, first, BATCH)
                    total = r.totalFrames; size = r.size; mime = r.mime
                    out.write(r.data)
                    done += r.data.size
                    first += r.count
                    progress(done, size)
                }
                if (done != size) throw ApiException("download ended at $done of $size bytes")
            }
            if (dst.exists()) dst.delete()
            if (!tmp.renameTo(dst)) throw ApiException("cannot store ${dst.name}")
        } finally {
            tmp.delete()
        }
        return mime
    }

    /** @brief A mime for a book format when the server's is generic. */
    fun bookMime(fmt: String, serverMime: String): String {
        if (serverMime.isNotEmpty() && serverMime != "application/octet-stream") return serverMime
        return when (fmt.lowercase()) {
            "epub" -> "application/epub+zip"
            "pdf" -> "application/pdf"
            "mobi", "prc", "palmdoc" -> "application/x-mobipocket-ebook"
            "azw", "azw3" -> "application/vnd.amazon.ebook"
            "cbz" -> "application/vnd.comicbook+zip"
            "cbr" -> "application/vnd.comicbook-rar"
            "fb2" -> "application/x-fictionbook+xml"
            "djvu" -> "image/vnd.djvu"
            "txt" -> "text/plain"
            "html", "htm" -> "text/html"
            else -> "application/octet-stream"
        }
    }
}
