package app.cim.family

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okio.BufferedSink
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.InputStream
import java.util.UUID
import java.util.concurrent.TimeUnit

/** Keys and settings. The private key never leaves the hardware-backed
 *  EncryptedSharedPreferences; a device backup does not carry it. */
class Prefs(ctx: Context) {
    private val p = EncryptedSharedPreferences.create(
        ctx, "cim_family_secure",
        MasterKey.Builder(ctx).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM)

    val privateKey: ByteArray
        get() {
            p.getString("priv", null)?.let { return Crypto.b64d(it) }
            val k = Crypto.generatePrivateKey()
            p.edit().putString("priv", Crypto.b64e(k)).apply()
            return k
        }
    val publicKey: ByteArray get() = Crypto.publicKey(privateKey)

    /** This device's identity as a peer: the name the server knows it by, its
     *  instance id, and the secret the server must present to us. */
    val deviceId: String get() = p.getString("device_id", null) ?: UUID.randomUUID().toString().replace("-", "").also { p.edit().putString("device_id", it).apply() }
    var deviceName: String
        get() = p.getString("device_name", "") ?: ""
        set(v) = p.edit().putString("device_name", v).apply()
    val keyIn: String get() = p.getString("key_in", null) ?: Crypto.b64e(ByteArray(32).also { java.security.SecureRandom().nextBytes(it) }).also { p.edit().putString("key_in", it).apply() }

    // the server, from its pairing code
    var serverName: String get() = p.getString("srv_name", "") ?: ""; set(v) = p.edit().putString("srv_name", v).apply()
    var serverUrl: String get() = p.getString("srv_url", "") ?: ""; set(v) = p.edit().putString("srv_url", v).apply()
    var serverPub: ByteArray? get() = p.getString("srv_pub", null)?.let { Crypto.b64d(it) }; set(v) = p.edit().putString("srv_pub", v?.let { Crypto.b64e(it) }).apply()
    var serverKey: String get() = p.getString("srv_key", "") ?: ""; set(v) = p.edit().putString("srv_key", v).apply()
    var serverId: String get() = p.getString("srv_id", "") ?: ""; set(v) = p.edit().putString("srv_id", v).apply()
    val paired: Boolean get() = serverUrl.isNotEmpty() && serverPub != null && serverKey.isNotEmpty()

    var wifiOnly: Boolean get() = p.getBoolean("wifi_only", true); set(v) = p.edit().putBoolean("wifi_only", v).apply()
    var chargingOnly: Boolean get() = p.getBoolean("charging_only", false); set(v) = p.edit().putBoolean("charging_only", v).apply()
    var uploadVideos: Boolean get() = p.getBoolean("upload_videos", true); set(v) = p.edit().putBoolean("upload_videos", v).apply()
    var paused: Boolean get() = p.getBoolean("paused", false); set(v) = p.edit().putBoolean("paused", v).apply()
    var lastRun: Long get() = p.getLong("last_run", 0); set(v) = p.edit().putLong("last_run", v).apply()
    var lastError: String get() = p.getString("last_error", "") ?: ""; set(v) = p.edit().putString("last_error", v).apply()

    fun applyPairing(code: String) {
        val d = Crypto.parsePairingCode(code)
        if (d.pub.contentEquals(publicKey)) throw IllegalArgumentException("that is this phone's own pairing code")
        serverName = d.name; serverUrl = d.url; serverPub = d.pub; serverKey = d.key; serverId = d.id
    }

    fun myPairingCode(): String = Crypto.makePairingCode(deviceName, "", publicKey, keyIn, deviceId)

    fun unpair() { p.edit().remove("srv_name").remove("srv_url").remove("srv_pub").remove("srv_key").remove("srv_id").apply() }
}

/** Local bookkeeping: which MediaStore items are uploaded, and per-bucket policy. */
class Db(ctx: Context) : SQLiteOpenHelper(ctx, "cim_family.db", null, 1) {
    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("""CREATE TABLE uploaded (
            media_id INTEGER PRIMARY KEY, sha TEXT, bucket TEXT, name TEXT, size INTEGER,
            rel_path TEXT DEFAULT '', uploaded_at INTEGER, purged INTEGER DEFAULT 0)""")
        db.execSQL("CREATE INDEX idx_up_sha ON uploaded(sha)")
        db.execSQL("CREATE TABLE folders (bucket TEXT PRIMARY KEY, policy TEXT NOT NULL DEFAULT 'off', display TEXT)")
        db.execSQL("CREATE TABLE failures (media_id INTEGER PRIMARY KEY, attempts INTEGER, error TEXT, at INTEGER)")
    }
    override fun onUpgrade(db: SQLiteDatabase, o: Int, n: Int) {}

    fun policy(bucket: String): String = readableDatabase.rawQuery("SELECT policy FROM folders WHERE bucket=?", arrayOf(bucket)).use {
        if (it.moveToFirst()) it.getString(0) else "off"
    }
    fun policies(): Map<String, String> = readableDatabase.rawQuery("SELECT bucket, policy FROM folders", null).use { c ->
        val m = HashMap<String, String>(); while (c.moveToNext()) m[c.getString(0)] = c.getString(1); m
    }
    fun setPolicy(bucket: String, display: String, policy: String) {
        writableDatabase.execSQL("INSERT OR REPLACE INTO folders(bucket, policy, display) VALUES (?,?,?)", arrayOf(bucket, policy, display))
    }
    fun isUploaded(mediaId: Long): Boolean = readableDatabase.rawQuery("SELECT 1 FROM uploaded WHERE media_id=?", arrayOf(mediaId.toString())).use { it.moveToFirst() }
    fun markUploaded(mediaId: Long, sha: String, bucket: String, name: String, size: Long, relPath: String) {
        val v = ContentValues().apply {
            put("media_id", mediaId); put("sha", sha); put("bucket", bucket); put("name", name); put("size", size)
            put("rel_path", relPath); put("uploaded_at", System.currentTimeMillis())
        }
        writableDatabase.insertWithOnConflict("uploaded", null, v, SQLiteDatabase.CONFLICT_REPLACE)
        writableDatabase.delete("failures", "media_id=?", arrayOf(mediaId.toString()))
    }
    fun markFailed(mediaId: Long, error: String) {
        writableDatabase.execSQL("INSERT INTO failures(media_id, attempts, error, at) VALUES (?,1,?,?) " +
                "ON CONFLICT(media_id) DO UPDATE SET attempts=attempts+1, error=excluded.error, at=excluded.at",
            arrayOf(mediaId, error.take(300), System.currentTimeMillis()))
    }
    fun failureBackoffOk(mediaId: Long): Boolean = readableDatabase.rawQuery("SELECT attempts, at FROM failures WHERE media_id=?", arrayOf(mediaId.toString())).use {
        if (!it.moveToFirst()) true else {
            val wait = minOf(6 * 3600_000L, 60_000L shl minOf(it.getInt(0), 8))
            System.currentTimeMillis() - it.getLong(1) > wait
        }
    }
    /** Uploaded items in "upload & purge" buckets still on the phone. */
    fun purgeCandidates(): List<Long> = readableDatabase.rawQuery(
        "SELECT u.media_id FROM uploaded u JOIN folders f ON f.bucket=u.bucket WHERE f.policy='purge' AND u.purged=0", null).use { c ->
        val out = ArrayList<Long>(); while (c.moveToNext()) out.add(c.getLong(0)); out
    }
    fun markPurged(ids: Collection<Long>) {
        if (ids.isEmpty()) return
        writableDatabase.execSQL("UPDATE uploaded SET purged=1 WHERE media_id IN (${ids.joinToString(",")})")
    }
    fun counts(): Pair<Int, Int> {
        val up = readableDatabase.rawQuery("SELECT COUNT(*) FROM uploaded", null).use { it.moveToFirst(); it.getInt(0) }
        val fail = readableDatabase.rawQuery("SELECT COUNT(*) FROM failures", null).use { it.moveToFirst(); it.getInt(0) }
        return up to fail
    }
    fun failures(limit: Int = 50): List<Triple<Long, Int, String>> = readableDatabase.rawQuery(
        "SELECT media_id, attempts, error FROM failures ORDER BY at DESC LIMIT $limit", null).use { c ->
        val out = ArrayList<Triple<Long, Int, String>>(); while (c.moveToNext()) out.add(Triple(c.getLong(0), c.getInt(1), c.getString(2))); out
    }
}

class ApiException(msg: String) : Exception(msg)

/** Talks to the paired server. Every push is sealed to the server's pinned
 *  key; every read comes back sealed to ours. */
class Api(private val prefs: Prefs) {
    private val http = OkHttpClient.Builder()
        .connectTimeout(20, TimeUnit.SECONDS).readTimeout(10, TimeUnit.MINUTES).writeTimeout(10, TimeUnit.MINUTES).build()
    private val base get() = prefs.serverUrl.trimEnd('/') + "/api/family_share/inbound"

    private fun req(path: String) = Request.Builder().url(base + path)
        .header("X-Family-Peer", prefs.deviceName).header("X-Family-Key", prefs.serverKey)

    private fun sealer() = Crypto.Sealer(prefs.privateKey, prefs.serverPub ?: throw ApiException("not paired"))

    private fun inner(extra: JSONObject): JSONObject = extra
        .put("ts", System.currentTimeMillis() / 1000.0).put("to", prefs.serverId).put("from", prefs.deviceId)

    private fun check(r: okhttp3.Response): okhttp3.Response {
        if (r.code == 401) throw ApiException("server rejected our key — re-pair")
        if (r.code == 404) throw ApiException("server has no family_share endpoint (module off?)")
        if (r.code >= 400) {
            val body = try { JSONObject(r.body?.string() ?: "").optString("error") } catch (e: Exception) { "" }
            throw ApiException("server answered ${r.code}: $body")
        }
        return r
    }

    private fun openSealed(r: okhttp3.Response): Pair<ByteArray, String> {
        val env = r.header("X-Family-Env") ?: throw ApiException("unsealed response from server")
        val opener = Crypto.Opener(prefs.privateKey, prefs.serverPub!!, JSONObject(env))
        val data = r.body?.bytes() ?: ByteArray(0)
        return opener.openBytes(data) to (r.header("X-Family-Mime") ?: "application/octet-stream")
    }

    fun ping(): JSONObject = http.newCall(req("/ping").get().build()).execute().use { r ->
        JSONObject(check(r).body!!.string())
    }

    data class PushResult(val stored: Boolean, val updated: Boolean, val duplicate: Boolean, val declined: Boolean,
                          val queued: Boolean, val needFile: Boolean, val filename: String)

    /** file == null: metadata only (the server answers need_file if it lacks the bytes). */
    fun push(sha: String, bucket: String, name: String, metadata: JSONObject, file: (() -> InputStream)?, size: Long): PushResult {
        val s = sealer()
        val meta = inner(JSONObject().put("origin_sha", sha).put("origin_id", prefs.deviceId)
            .put("folder", bucket).put("orig_name", name).put("metadata", metadata))
        val mp = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("env", s.header.toString())
            .addFormDataPart("meta", s.sealMeta(meta))
        if (file != null) {
            val body = object : RequestBody() {
                override fun contentType() = "application/octet-stream".toMediaType()
                override fun writeTo(sink: BufferedSink) { file().use { s.sealStream(it, size, sink.outputStream()) } }
            }
            mp.addFormDataPart("file", "payload.bin", body)
        }
        return http.newCall(req("/push").post(mp.build()).build()).execute().use { r ->
            val j = JSONObject(check(r).body!!.string())
            if (!j.optBoolean("ok")) throw ApiException(j.optString("error", "refused"))
            PushResult(j.optBoolean("stored"), j.optBoolean("updated"), j.optBoolean("duplicate"), j.optBoolean("declined"),
                j.optBoolean("queued"), j.optBoolean("need_file"), j.optString("filename"))
        }
    }

    /** Which of these originals the server already has (batched, sealed both ways). */
    fun have(shas: List<String>): Set<String> {
        val s = sealer()
        val body = JSONObject().put("env", s.header).put("meta", s.sealMeta(inner(JSONObject().put("shas", JSONArray(shas)))))
        return http.newCall(req("/have").post(body.toString().toRequestBody("application/json".toMediaType())).build()).execute().use { r ->
            val (pt, _) = openSealed(check(r))
            val arr = JSONObject(String(pt)).optJSONArray("have") ?: JSONArray()
            (0 until arr.length()).map { arr.getString(it) }.toSet()
        }
    }

    data class Item(val path: String, val w: Int, val h: Int, val t: Double, val video: Boolean)

    fun timeline(offset: Int, limit: Int): Pair<List<Item>, Int> =
        http.newCall(req("/timeline?offset=$offset&limit=$limit").get().build()).execute().use { r ->
            val (pt, _) = openSealed(check(r))
            val j = JSONObject(String(pt))
            val arr = j.getJSONArray("files")
            (0 until arr.length()).map { i -> arr.getJSONObject(i).let { Item(it.getString("p"), it.optInt("w"), it.optInt("h"), it.optDouble("t"), it.optBoolean("v")) } } to j.optInt("total")
        }

    fun thumb(path: String): ByteArray = http.newCall(req("/thumb?p=" + java.net.URLEncoder.encode(path, "UTF-8")).get().build()).execute().use { r ->
        openSealed(check(r)).first
    }

    /** Full file, decrypted straight to disk (videos never fit in RAM). */
    fun media(path: String, dst: File): String = http.newCall(req("/media?p=" + java.net.URLEncoder.encode(path, "UTF-8")).get().build()).execute().use { r ->
        check(r)
        val env = r.header("X-Family-Env") ?: throw ApiException("unsealed response from server")
        val opener = Crypto.Opener(prefs.privateKey, prefs.serverPub!!, JSONObject(env))
        dst.outputStream().use { out -> opener.openStream(r.body!!.byteStream(), out) }
        r.header("X-Family-Mime") ?: "application/octet-stream"
    }
}
