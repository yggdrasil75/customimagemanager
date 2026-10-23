package app.cim.family

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.net.URLEncoder

/** @brief An HTTP error from the server, with its status and (parsed) body. */
class HttpApiException(val code: Int, msg: String, val body: JSONObject?) : ApiException(msg)

/** @brief A slice of a file from inbound/frames, already decrypted and checked. */
class FramesResult(val size: Long, val totalFrames: Long, val first: Long, val count: Int, val data: ByteArray, val mime: String)

/**
 * @brief The v2 phone API (modules/family_share/PHONE_API.md): capabilities, the
 * sealed proxy to the server's web API, and ranged sealed media. Same auth
 * headers, sealing and base URL (direct or the remote link) as [Api].
 */
class ApiV2(private val prefs: Prefs) {
    private val v1 = Api(prefs)

    // -- caps ---------------------------------------------------------------
    /** @brief GET inbound/caps. A 404 without an envelope is a v1 server: gallery only. */
    fun fetchCaps(): Caps {
        v1.http.newCall(v1.req("/caps").get().build()).execute().use { r ->
            if (r.code == 404 && r.header("X-Family-Env") == null) return Caps.V1
            val j = openJson(r)
            return Caps.parse(j)
        }
    }

    // -- proxy --------------------------------------------------------------
    /** @brief GET inbound/api/<path>?<query> -> the view's JSON. A `{"success": false}`
     *  answer throws its error. */
    fun getJson(path: String, query: Map<String, String> = emptyMap()): JSONObject {
        v1.http.newCall(v1.req("/api/" + path + queryString(query)).get().build()).execute().use { r ->
            return checkSuccess(openJson(r))
        }
    }

    /** @brief POST inbound/api/<path> with a sealed {"body": body}. */
    fun postJson(path: String, body: JSONObject): JSONObject {
        val s = v1.sealer()
        val req = JSONObject().put("env", s.header).put("meta", s.sealMeta(v1.inner(JSONObject().put("body", body))))
        val rb = req.toString().toRequestBody("application/json".toMediaType())
        v1.http.newCall(v1.req("/api/" + path).post(rb).build()).execute().use { r ->
            return checkSuccess(openJson(r))
        }
    }

    /** @brief GET inbound/api/<path> for a binary answer (covers, subtitles) -> (bytes, mime). */
    fun getBytes(path: String, query: Map<String, String> = emptyMap()): Pair<ByteArray, String> {
        v1.http.newCall(v1.req("/api/" + path + queryString(query)).get().build()).execute().use { r ->
            if (r.code >= 400) fail(r)
            return v1.openSealed(r)
        }
    }

    /** @brief A proxied route ending in a library path (music/cover/<rel>, ...), with the
     *  path's segments URL-encoded the way the browser does. */
    fun pathRoute(prefix: String, rel: String): String =
        prefix.trimEnd('/') + "/" + rel.split('/').joinToString("/") { enc(it) }

    // -- frames -------------------------------------------------------------
    /**
     * @brief Frames first .. first+count-1 of a library file, decrypted. Checks: exactly
     * min(count, frames - first) frames, the last flag only on the file's final
     * frame, nothing trailing, and the server's X-Family-First matches. A first
     * past the end answers 416 with {"size", "frames"}: thrown as HttpApiException.
     */
    fun frames(rel: String, first: Long, count: Int): FramesResult {
        val n = count.coerceIn(1, FrameMath.MAX_BATCH)
        val path = "/frames?p=" + enc(rel) + "&first=" + first + "&count=" + n
        v1.http.newCall(v1.req(path).get().build()).execute().use { r ->
            if (r.code >= 400) fail(r)
            val env = r.header("X-Family-Env") ?: throw ApiException("unsealed response from server")
            val size = r.header("X-Family-Size")?.trim()?.toLongOrNull() ?: throw ApiException("frames: no X-Family-Size")
            val total = r.header("X-Family-Frames")?.trim()?.toLongOrNull() ?: throw ApiException("frames: no X-Family-Frames")
            val gotFirst = r.header("X-Family-First")?.trim()?.toLongOrNull() ?: first
            if (gotFirst != first) throw ApiException("frames: asked for frame $first, got $gotFirst")
            if (size < 0 || total < 1) throw ApiException("frames: bad size headers")
            val expected = FrameMath.expected(first, n, total)
            if (expected < 1) throw ApiException("frames: frame $first is past the end ($total frames)")
            val opener = Crypto.Opener(prefs.privateKey, prefs.serverPub ?: throw ApiException("not paired"), JSONObject(env))
            val out = ByteArrayOutputStream(expected * Caps.DEFAULT_CHUNK.coerceAtMost(1 shl 20))
            val body = r.body ?: throw ApiException("frames: empty body")
            body.byteStream().use { input: InputStream -> opener.openFrames(input, out, first, expected, total) }
            return FramesResult(size, total, first, expected, out.toByteArray(),
                r.header("X-Family-Mime") ?: "application/octet-stream")
        }
    }

    // -- helpers ------------------------------------------------------------
    private fun openJson(r: Response): JSONObject {
        if (r.code >= 400) fail(r)
        val (pt, _) = v1.openSealed(r)
        return try { JSONObject(String(pt, Charsets.UTF_8)) } catch (e: Exception) { throw ApiException("server sent a non-JSON answer") }
    }

    private fun checkSuccess(j: JSONObject): JSONObject {
        if (j.has("success") && !j.optBoolean("success", true)) throw ApiException(j.str("error").ifEmpty { "the server refused" })
        if (j.has("ok") && !j.optBoolean("ok", true)) throw ApiException(j.str("error").ifEmpty { "the server refused" })
        return j
    }

    /** @brief Refusals before any view runs are plain JSON; a view's own errors are sealed.
     *  Tell them apart by X-Family-Env. */
    private fun fail(r: Response): Nothing {
        val env = r.header("X-Family-Env")
        val text: String = try {
            if (env != null) String(v1.openSealed(r).first, Charsets.UTF_8) else (r.body?.string() ?: "")
        } catch (e: Exception) { "" }
        val j: JSONObject? = try { JSONObject(text) } catch (e: Exception) { null }
        val msg = j?.str("error").orEmpty()
        if (r.code == 401) throw ApiException("server rejected us as '${prefs.deviceName}': that name must match the peer row on the server and the key must be from the server's current pairing code - re-paste it")
        if (r.code == 403 && j != null && j.str("error_code") in ACCOUNT_CODES) throw AccountException(msg.ifEmpty { "refused by the server" })
        throw HttpApiException(r.code, msg.ifEmpty { "server answered ${r.code}" }, j)
    }

    companion object {
        fun enc(s: String): String = URLEncoder.encode(s, "UTF-8").replace("+", "%20")

        fun queryString(q: Map<String, String>): String {
            val parts = q.entries.filter { it.value.isNotEmpty() }.map { enc(it.key) + "=" + enc(it.value) }
            return if (parts.isEmpty()) "" else "?" + parts.joinToString("&")
        }
    }
}
