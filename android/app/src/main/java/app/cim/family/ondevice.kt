package app.cim.family

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import android.content.Context
import android.graphics.Bitmap
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.File
import java.nio.FloatBuffer
import java.nio.LongBuffer
import java.util.concurrent.TimeUnit
import kotlin.math.sqrt

/**
 * @brief On-device semantic search (optional): a small CLIP-style model (MobileCLIP
 * by default, as ONNX) that embeds text and images on the phone, and an index of
 * the photos on the phone plus the library thumbnails cached here.
 *
 * Nothing is downloaded until the user turns it on (Settings -> On-device search).
 * The Photos search asks the server first when it answers (its own model and the
 * whole library); this index answers when the server can't be reached, or when
 * the user picks "this phone" as the search scope.
 *
 * Files (from [Prefs.embedModelUrl], a Hugging Face style repo laid out like
 * Xenova/mobileclip_s0): tokenizer.json, tokenizer_config.json,
 * preprocessor_config.json, onnx/text_model_quantized.onnx, onnx/vision_model_quantized.onnx.
 */
object OnDevice {
    const val DEFAULT_URL = "https://huggingface.co/Xenova/mobileclip_s0/resolve/main/"
    val FILES = listOf("tokenizer.json", "tokenizer_config.json", "preprocessor_config.json",
                       "onnx/text_model_quantized.onnx", "onnx/vision_model_quantized.onnx")

    private var env: OrtEnvironment? = null
    private var text: OrtSession? = null
    private var vision: OrtSession? = null
    private var tok: ClipTokenizer? = null
    private var pre: Preproc? = null

    fun dir(ctx: Context): File = File(ctx.filesDir, "mobile_embed").also { it.mkdirs() }

    fun installed(ctx: Context): Boolean = FILES.all { File(dir(ctx), it).let { f -> f.exists() && f.length() > 0 } }

    fun sizeOnDisk(ctx: Context): Long = dir(ctx).walkTopDown().filter { it.isFile }.sumOf { it.length() }

    /** @brief Download the model files (blocking). progress(file, bytesSoFar). */
    fun download(ctx: Context, baseUrl: String, progress: (String, Long) -> Unit = { _, _ -> }) {
        val http = OkHttpClient.Builder().connectTimeout(20, TimeUnit.SECONDS).readTimeout(120, TimeUnit.SECONDS).build()
        val base = baseUrl.trim().let { if (it.endsWith("/")) it else "$it/" }
        for (name in FILES) {
            val dst = File(dir(ctx), name)
            if (dst.exists() && dst.length() > 0) continue
            dst.parentFile?.mkdirs()
            val part = File(dst.path + ".part")
            http.newCall(Request.Builder().url(base + name).build()).execute().use { r ->
                if (!r.isSuccessful) throw ApiException("download $name: HTTP ${r.code}")
                val body = r.body ?: throw ApiException("download $name: empty")
                body.byteStream().use { ins ->
                    part.outputStream().use { out ->
                        val buf = ByteArray(1 shl 16); var n: Int; var tot = 0L
                        while (ins.read(buf).also { n = it } > 0) { out.write(buf, 0, n); tot += n; progress(name, tot) }
                    }
                }
            }
            if (!part.renameTo(dst)) throw ApiException("could not store $name")
        }
        close()
    }

    fun delete(ctx: Context) { close(); dir(ctx).deleteRecursively(); LocalIndex.clear(ctx) }

    @Synchronized fun close() {
        text?.close(); vision?.close(); text = null; vision = null; tok = null; pre = null
    }

    @Synchronized private fun load(ctx: Context) {
        if (text != null) return
        if (!installed(ctx)) throw ApiException("the on-device search model isn't downloaded (Settings -> On-device search)")
        val e = env ?: OrtEnvironment.getEnvironment().also { env = it }
        val opts = OrtSession.SessionOptions().apply { setIntraOpNumThreads(2) }
        // both sessions or neither: a half-loaded pair would pass the text != null check
        val t = e.createSession(File(dir(ctx), FILES[3]).path, opts)
        val v = try { e.createSession(File(dir(ctx), FILES[4]).path, opts) } catch (x: Exception) { t.close(); throw x }
        text = t; vision = v
        tok = ClipTokenizer(JSONObject(File(dir(ctx), FILES[0]).readText()),
                            runCatching { JSONObject(File(dir(ctx), FILES[1]).readText()) }.getOrNull())
        pre = Preproc(runCatching { JSONObject(File(dir(ctx), FILES[2]).readText()) }.getOrDefault(JSONObject()))
    }

    /** @brief The unit-length embedding of a text query (blocking). */
    @Synchronized fun embedText(ctx: Context, q: String): FloatArray {
        load(ctx)
        val e = env!!; val s = text!!
        val ids = tok!!.encode(q)
        val inputs = HashMap<String, OnnxTensor>()
        val shape = longArrayOf(1, ids.size.toLong())
        // real tokens: bos .. the first eos (pad may be eos itself)
        val end = (1 until ids.size).firstOrNull { ids[it] == tok!!.eos.toLong() } ?: (ids.size - 1)
        try {
            for (name in s.inputNames) {
                when {
                    name.contains("mask") -> inputs[name] = OnnxTensor.createTensor(e,
                        LongBuffer.wrap(LongArray(ids.size) { if (it <= end) 1L else 0L }), shape)
                    else -> inputs[name] = OnnxTensor.createTensor(e, LongBuffer.wrap(ids), shape)
                }
            }
            s.run(inputs).use { out -> return unit(pick(out, "text_embeds")) }
        } finally { inputs.values.forEach { it.close() } }
    }

    /** @brief The unit-length embedding of an image (blocking). */
    @Synchronized fun embedImage(ctx: Context, bmp: Bitmap): FloatArray {
        load(ctx)
        val e = env!!; val s = vision!!
        val (data, size) = pre!!.pixels(bmp)
        val t = OnnxTensor.createTensor(e, FloatBuffer.wrap(data), longArrayOf(1, 3, size.toLong(), size.toLong()))
        try {
            s.run(mapOf(s.inputNames.first() to t)).use { out -> return unit(pick(out, "image_embeds")) }
        } finally { t.close() }
    }

    private fun pick(out: OrtSession.Result, want: String): FloatArray {
        val v = (out.get(want).orElse(null) ?: out.get(0)).value
        @Suppress("UNCHECKED_CAST")
        return when (v) {
            is Array<*> -> v[0] as FloatArray
            is FloatArray -> v
            else -> throw ApiException("unexpected model output")
        }
    }

    fun unit(v: FloatArray): FloatArray {
        var n = 0.0; for (x in v) n += x * x
        val s = if (n > 0) (1.0 / sqrt(n)).toFloat() else 1f
        return FloatArray(v.size) { v[it] * s }
    }
}

/** @brief The image preprocessing preprocessor_config.json describes (CLIPImageProcessor):
 *  resize the short side, centre crop, rescale, normalise; CHW float. */
class Preproc(cfg: JSONObject) {
    private val size: Int
    private val crop: Int
    private val mean: FloatArray
    private val std: FloatArray
    private val rescale: Float = cfg.optDouble("rescale_factor", 1.0 / 255.0).toFloat()

    init {
        val s = cfg.opt("size")
        size = when (s) {
            is JSONObject -> s.optInt("shortest_edge", s.optInt("height", 224))
            is Number -> s.toInt()
            else -> 224
        }
        val c = cfg.opt("crop_size")
        crop = when (c) {
            is JSONObject -> c.optInt("height", size)
            is Number -> c.toInt()
            else -> size
        }
        val norm = cfg.optBoolean("do_normalize", true)
        mean = if (norm) floats(cfg.optJSONArray("image_mean"), 0.48145466f, 0.4578275f, 0.40821073f) else floatArrayOf(0f, 0f, 0f)
        std = if (norm) floats(cfg.optJSONArray("image_std"), 0.26862954f, 0.26130258f, 0.27577711f) else floatArrayOf(1f, 1f, 1f)
    }

    private fun floats(a: JSONArray?, vararg d: Float): FloatArray =
        if (a == null || a.length() < 3) d else FloatArray(3) { a.getDouble(it).toFloat() }

    /** @return (CHW pixels, side) */
    fun pixels(src: Bitmap): Pair<FloatArray, Int> {
        val sc = size.toFloat() / minOf(src.width, src.height)
        val w = maxOf(crop, (src.width * sc).toInt()); val h = maxOf(crop, (src.height * sc).toInt())
        val scaled = Bitmap.createScaledBitmap(src, w, h, true)
        val x0 = (w - crop) / 2; val y0 = (h - crop) / 2
        val px = IntArray(crop * crop)
        scaled.getPixels(px, 0, crop, x0, y0, crop, crop)
        if (scaled !== src) scaled.recycle()
        val out = FloatArray(3 * crop * crop)
        val plane = crop * crop
        for (i in 0 until plane) {
            val p = px[i]
            out[i] = (((p shr 16) and 0xff) * rescale - mean[0]) / std[0]
            out[plane + i] = (((p shr 8) and 0xff) * rescale - mean[1]) / std[1]
            out[2 * plane + i] = ((p and 0xff) * rescale - mean[2]) / std[2]
        }
        return out to crop
    }
}

/** @brief CLIP's byte-level BPE, read from a Hugging Face tokenizer.json. */
class ClipTokenizer(tj: JSONObject, cfg: JSONObject?) {
    private val vocab = HashMap<String, Int>()
    private val ranks = HashMap<Pair<String, String>, Int>()
    private val cache = HashMap<String, List<String>>()
    private val byteChar = bytesToUnicode()
    val bos: Int
    val eos: Int
    val pad: Int
    private val maxLen: Int = cfg?.optInt("model_max_length", 77)?.takeIf { it in 8..512 } ?: 77
    private val pat = Regex("""<\|startoftext\|>|<\|endoftext\|>|'s|'t|'re|'ve|'m|'ll|'d|[\p{L}]+|[\p{N}]|[^\s\p{L}\p{N}]+""",
                            RegexOption.IGNORE_CASE)

    init {
        val model = tj.getJSONObject("model")
        val v = model.getJSONObject("vocab")
        for (k in v.keys()) vocab[k] = v.getInt(k)
        val m = model.getJSONArray("merges")
        for (i in 0 until m.length()) {
            val e = m.get(i)
            val pair = if (e is JSONArray) e.getString(0) to e.getString(1)
                       else (e as String).split(" ", limit = 2).let { it[0] to (it.getOrNull(1) ?: "") }
            ranks[pair] = i
        }
        tj.optJSONArray("added_tokens")?.let { a -> for (i in 0 until a.length()) a.getJSONObject(i).let { vocab[it.getString("content")] = it.getInt("id") } }
        bos = vocab["<|startoftext|>"] ?: 49406
        eos = vocab["<|endoftext|>"] ?: 49407
        val padTok = cfg?.let { c -> c.opt("pad_token").let { p -> if (p is JSONObject) p.optString("content") else p as? String } }
        pad = padTok?.let { vocab[it] } ?: eos
    }

    /** @brief [bos, tokens..., eos, pad...] of exactly maxLen ids. */
    fun encode(text: String): LongArray {
        val clean = java.text.Normalizer.normalize(text, java.text.Normalizer.Form.NFC).replace(Regex("\\s+"), " ").trim().lowercase()
        val ids = ArrayList<Int>()
        for (m in pat.findAll(clean)) {
            val word = m.value.toByteArray(Charsets.UTF_8).joinToString("") { byteChar[it.toInt() and 0xff].toString() }
            for (t in bpe(word)) ids.add(vocab[t] ?: continue)
        }
        val body = ids.take(maxLen - 2)
        val out = LongArray(maxLen) { pad.toLong() }
        out[0] = bos.toLong()
        body.forEachIndexed { i, id -> out[i + 1] = id.toLong() }
        out[body.size + 1] = eos.toLong()
        return out
    }

    private fun bpe(token: String): List<String> {
        cache[token]?.let { return it }
        if (token.isEmpty()) return emptyList()
        var word = token.dropLast(1).map { it.toString() } + (token.last() + "</w>")
        while (word.size > 1) {
            var best: Pair<String, String>? = null; var bestRank = Int.MAX_VALUE
            for (i in 0 until word.size - 1) {
                val r = ranks[word[i] to word[i + 1]] ?: continue
                if (r < bestRank) { bestRank = r; best = word[i] to word[i + 1] }
            }
            val b = best ?: break
            val merged = ArrayList<String>()
            var i = 0
            while (i < word.size) {
                if (i < word.size - 1 && word[i] == b.first && word[i + 1] == b.second) { merged.add(b.first + b.second); i += 2 }
                else { merged.add(word[i]); i++ }
            }
            word = merged
        }
        if (cache.size < 10_000) cache[token] = word
        return word
    }

    private fun bytesToUnicode(): CharArray {
        val bs = ArrayList<Int>()
        bs.addAll(('!'.code)..('~'.code)); bs.addAll(('¡'.code)..('¬'.code)); bs.addAll(('®'.code)..('ÿ'.code))
        val cs = ArrayList(bs)
        var n = 0
        for (b in 0..255) if (b !in bs) { bs.add(b); cs.add(256 + n); n++ }
        val out = CharArray(256)
        for (i in bs.indices) out[bs[i]] = cs[i].toChar()
        return out
    }
}

/**
 * @brief The on-device index: key -> unit vector, for "local:<MediaStore id>" (photos
 * on the phone) and library paths (their cached thumbnails). One flat file, read
 * into memory for a search: a few thousand 512-float vectors is a few MB.
 */
object LocalIndex {
    private const val FILE = "embed_index.bin"
    private var mem: LinkedHashMap<String, FloatArray>? = null

    private fun file(ctx: Context) = File(OnDevice.dir(ctx), FILE)

    @Synchronized fun all(ctx: Context): LinkedHashMap<String, FloatArray> {
        mem?.let { return it }
        val m = LinkedHashMap<String, FloatArray>()
        val f = file(ctx)
        if (f.exists()) try {
            DataInputStream(f.inputStream().buffered()).use { d ->
                val n = d.readInt(); val dim = d.readInt()
                repeat(n) { val k = d.readUTF(); m[k] = FloatArray(dim) { d.readFloat() } }
            }
        } catch (e: Exception) { m.clear() }
        mem = m
        return m
    }

    @Synchronized fun put(ctx: Context, key: String, v: FloatArray) { all(ctx)[key] = v }

    @Synchronized fun has(ctx: Context, key: String) = all(ctx).containsKey(key)

    @Synchronized fun size(ctx: Context) = all(ctx).size

    @Synchronized fun save(ctx: Context) {
        val m = all(ctx)
        val dim = m.values.firstOrNull()?.size ?: 0
        val tmp = File(file(ctx).path + ".tmp")
        DataOutputStream(tmp.outputStream().buffered()).use { d ->
            val rows = m.filterValues { it.size == dim }
            d.writeInt(rows.size); d.writeInt(dim)
            for ((k, v) in rows) { d.writeUTF(k); for (x in v) d.writeFloat(x) }
        }
        tmp.renameTo(file(ctx))
    }

    @Synchronized fun clear(ctx: Context) { mem = null; file(ctx).delete() }

    /** @brief The best `k` keys for a query vector, best first: [(key, cosine)]. */
    fun search(ctx: Context, q: FloatArray, k: Int = 200): List<Pair<String, Float>> {
        val m = synchronized(this) { ArrayList(all(ctx).entries) }
        return m.asSequence().filter { it.value.size == q.size }
            .map { e -> var s = 0f; val v = e.value; for (i in q.indices) s += q[i] * v[i]; e.key to s }
            .sortedByDescending { it.second }.take(k).toList()
    }

    /**
     * @brief Embed what isn't indexed yet: the phone's photos and the library thumbnails
     * cached on the phone. Blocking; returns how many were added. stop() is checked
     * between images so a WorkManager job can give up cleanly.
     */
    fun update(ctx: Context, limit: Int = Int.MAX_VALUE, stop: () -> Boolean = { false },
               progress: (Int, Int) -> Unit = { _, _ -> }): Int {
        val todo = ArrayList<Api.Item>()
        val local = OfflinePhotos.local(ctx, includeVideo = false)
        todo += local.filter { !has(ctx, it.path) }
        todo += OfflinePhotos.cached(ctx).filter { !it.video && !has(ctx, it.path) &&
                                                   app.cim.family.ui.ThumbCache.hasCached(ctx, it.path) }
        var done = 0
        for (it in todo.take(limit)) {
            if (stop()) break
            val bmp = if (OfflinePhotos.isLocal(it)) OfflinePhotos.localThumb(ctx, it)
                      else app.cim.family.ui.ThumbCache.cachedBlocking(ctx, it.path)
            if (bmp != null) {
                try { put(ctx, it.path, OnDevice.embedImage(ctx, bmp)); done++ } catch (e: Exception) { }
            }
            if (done > 0 && done % 50 == 0) save(ctx)
            progress(done, todo.size)
        }
        save(ctx)
        return done
    }
}