package app.cim.family

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.FilterInputStream
import java.io.InputStream
import java.security.MessageDigest
import java.util.Random

/**
 * Crypto.kt against vectors sealed by the server's modules/family_share/crypto.py
 * (android/tools/gen_vectors.py; the Python side re-checks the same files in
 * modules/family_share/tests/test_phone_vectors.py), plus Kotlin round trips.
 * Pure JVM: no Android classes, no Robolectric.
 */
class CryptoInteropTest {
    private val vec: JSONObject by lazy { JSONObject(String(resource("vectors.json"))) }
    private val frames: ByteArray by lazy { resource(vec.getJSONObject("stream").getString("resource")) }
    private val chunk get() = vec.getInt("chunk")

    private fun resource(name: String): ByteArray =
        (javaClass.getResourceAsStream("/$name") ?: error("test resource $name missing (src/test/resources)")).use { it.readBytes() }

    private fun key(who: String, k: String) = Crypto.b64d(vec.getJSONObject(who).getString(k))
    private val phonePriv get() = key("phone", "priv")
    private val phonePub get() = key("phone", "pub")
    private val serverPriv get() = key("server", "priv")
    private val serverPub get() = key("server", "pub")

    private fun opener(part: String) = Crypto.Opener(phonePriv, serverPub, vec.getJSONObject(part).getJSONObject("header"))

    private fun sha(b: ByteArray) = MessageDigest.getInstance("SHA-256").digest(b).joinToString("") { "%02x".format(it) }

    private fun offsets(): IntArray {
        val a = vec.getJSONObject("stream").getJSONArray("frame_offsets")
        return IntArray(a.length()) { a.getInt(it) }
    }

    private fun range(name: String) = vec.getJSONObject("stream").getJSONObject("ranges").getJSONObject(name)
    private fun rangeBytes(r: JSONObject) = frames.copyOfRange(r.getInt("start"), r.getInt("end"))
    private val totalFrames get() = vec.getJSONObject("stream").getLong("frames")

    private fun openFrames(o: Crypto.Opener, body: ByteArray, first: Long, expected: Int, total: Long): ByteArray {
        val out = ByteArrayOutputStream()
        o.openFrames(ByteArrayInputStream(body), out, first, expected, total)
        return out.toByteArray()
    }

    private fun openStream(o: Crypto.Opener, body: ByteArray): ByteArray {
        val out = ByteArrayOutputStream(); o.openStream(ByteArrayInputStream(body), out); return out.toByteArray()
    }

    private fun flip(b: ByteArray, at: Int) = b.copyOf().also { it[at] = (it[at].toInt() xor 1).toByte() }

    // -- Python vectors ---------------------------------------------------

    @Test fun keysAndFingerprintsMatchPython() {
        assertEquals(Crypto.CHUNK, chunk)
        for (who in listOf("server", "phone")) {
            val pub = Crypto.publicKey(key(who, "priv"))
            assertEquals(vec.getJSONObject(who).getString("pub"), Crypto.b64e(pub))
            assertEquals(vec.getJSONObject(who).getString("fingerprint"), Crypto.fingerprint(pub))
        }
    }

    @Test fun opensPythonMeta() {
        val m = vec.getJSONObject("meta")
        val got = opener("meta").openMeta(m.getString("blob"))
        val want = m.getJSONObject("plaintext")
        assertEquals(want.keys().asSequence().toSet(), got.keys().asSequence().toSet())
        assertEquals(want.getLong("ts"), got.getLong("ts"))
        assertEquals("phone-1", got.getString("to"))
        assertEquals("ü photo 1.jpg", got.getString("name"))
        assertEquals(want.getLong("size"), got.getLong("size"))
        assertEquals(want.getString("mime"), got.getString("mime"))
        assertEquals(want.getJSONArray("tags").toString(), got.getJSONArray("tags").toString())
    }

    @Test fun pythonMetaRejectsWrongKeysAndTampering() {
        val m = vec.getJSONObject("meta")
        val hdr = m.getJSONObject("header")
        // wrong sender key (the static half of the agreement differs)
        assertThrows(CryptoException::class.java) { Crypto.Opener(phonePriv, phonePub, hdr).openMeta(m.getString("blob")) }
        // wrong recipient key
        assertThrows(CryptoException::class.java) { Crypto.Opener(serverPriv, serverPub, hdr).openMeta(m.getString("blob")) }
        val raw = Crypto.b64d(m.getString("blob"))
        assertThrows(CryptoException::class.java) { opener("meta").openMeta(Crypto.b64e(flip(raw, raw.size - 1))) }
        assertThrows(CryptoException::class.java) { opener("meta").openMeta(Crypto.b64e(flip(raw, 0))) }  // nonce
        assertThrows(CryptoException::class.java) { opener("meta").openMeta(Crypto.b64e(raw.copyOf(20))) }
        // header for another envelope
        assertThrows(CryptoException::class.java) {
            Crypto.Opener(phonePriv, serverPub, vec.getJSONObject("empty").getJSONObject("header")).openMeta(m.getString("blob"))
        }
    }

    @Test fun badHeadersAndKeysAreCryptoExceptions() {
        val hdr = JSONObject(vec.getJSONObject("meta").getJSONObject("header").toString())
        assertThrows(CryptoException::class.java) { Crypto.Opener(phonePriv, serverPub, JSONObject(hdr.toString()).put("v", 2)) }
        assertThrows(CryptoException::class.java) { Crypto.Opener(phonePriv, serverPub, JSONObject(hdr.toString()).put("eph", Crypto.b64e(ByteArray(31)))) }
        assertThrows(CryptoException::class.java) { Crypto.Opener(phonePriv, ByteArray(16), hdr) }
        assertThrows(CryptoException::class.java) { Crypto.Sealer(serverPriv, ByteArray(33)) }
    }

    @Test fun opensPythonFullStream() {
        val st = vec.getJSONObject("stream")
        val pt = openStream(opener("stream"), frames)
        assertEquals(st.getInt("plain_size"), pt.size)
        assertEquals(st.getString("plain_sha256"), sha(pt))
    }

    @Test fun openFramesOnWholeStreamEqualsOpenStream() {
        val st = vec.getJSONObject("stream")
        val pt = openFrames(opener("stream"), frames, 0, totalFrames.toInt(), totalFrames)
        assertEquals(st.getString("plain_sha256"), sha(pt))
    }

    @Test fun openFramesOnPythonRanges() {
        for (name in listOf("head", "middle", "tail", "last")) {
            val r = range(name)
            val pt = openFrames(opener("stream"), rangeBytes(r), r.getLong("first"), r.getInt("count"), totalFrames)
            assertEquals(name, r.getInt("plain_size"), pt.size)
            assertEquals(name, r.getString("plain_sha256"), sha(pt))
        }
    }

    @Test fun openFramesRejectsWrongFirst() {
        val r = range("middle")
        assertThrows(CryptoException::class.java) { openFrames(opener("stream"), rangeBytes(r), 0, 2, totalFrames) }
        assertThrows(CryptoException::class.java) { openFrames(opener("stream"), rangeBytes(r), 2, 2, totalFrames) }
    }

    @Test fun openFramesRejectsMissingFrames() {
        val r = range("middle")
        val off = offsets()
        // only frame 1 of the requested 1..2 arrived
        val one = frames.copyOfRange(off[1], off[2])
        assertThrows(CryptoException::class.java) { openFrames(opener("stream"), one, 1, 2, totalFrames) }
        // a frame dropped out of the middle of a 3-frame range
        val gap = frames.copyOfRange(off[0], off[1]) + frames.copyOfRange(off[2], off[3])
        assertThrows(CryptoException::class.java) { openFrames(opener("stream"), gap, 0, 2, totalFrames) }
        // nothing at all
        assertThrows(CryptoException::class.java) { openFrames(opener("stream"), ByteArray(0), 1, 2, totalFrames) }
        // half a frame
        assertThrows(CryptoException::class.java) { openFrames(opener("stream"), rangeBytes(r).copyOf(off[2] - off[1] + 100), 1, 2, totalFrames) }
    }

    @Test fun openFramesRejectsLastFlagOnWrongFrame() {
        // file really has 4 frames: claiming 3 means frame 2 must carry the last flag (it doesn't)
        val mid = range("middle")
        assertThrows(CryptoException::class.java) { openFrames(opener("stream"), rangeBytes(mid), 1, 2, totalFrames - 1) }
        // claiming 5 means frame 3 must NOT carry the last flag (it does)
        val tail = range("tail")
        assertThrows(CryptoException::class.java) { openFrames(opener("stream"), rangeBytes(tail), 2, 2, totalFrames + 1) }
        val last = range("last")
        assertThrows(CryptoException::class.java) { openFrames(opener("stream"), rangeBytes(last), 3, 1, totalFrames + 1) }
    }

    @Test fun openFramesRejectsTrailingData() {
        val r = range("middle")
        assertThrows(CryptoException::class.java) { openFrames(opener("stream"), rangeBytes(r) + byteArrayOf(0), 1, 2, totalFrames) }
        // one frame more than expected is trailing data too
        val off = offsets()
        val three = frames.copyOfRange(off[1], off[4])
        assertThrows(CryptoException::class.java) { openFrames(opener("stream"), three, 1, 2, totalFrames) }
    }

    @Test fun openFramesRejectsTamperedFrame() {
        val r = range("middle")
        assertThrows(CryptoException::class.java) { openFrames(opener("stream"), flip(rangeBytes(r), 4 + 10), 1, 2, totalFrames) }
        // range from another envelope (wrong header)
        assertThrows(CryptoException::class.java) {
            openFrames(Crypto.Opener(phonePriv, serverPub, vec.getJSONObject("meta").getJSONObject("header")), rangeBytes(r), 1, 2, totalFrames)
        }
    }

    @Test fun pythonStreamNegativeCases() {
        val off = offsets()
        // flipped ciphertext byte
        assertThrows(CryptoException::class.java) { openStream(opener("stream"), flip(frames, 100)) }
        // flipped tag byte of the last frame
        assertThrows(CryptoException::class.java) { openStream(opener("stream"), flip(frames, frames.size - 1)) }
        // a frame dropped
        assertThrows(CryptoException::class.java) { openStream(opener("stream"), frames.copyOfRange(0, off[1]) + frames.copyOfRange(off[2], frames.size)) }
        // two frames swapped
        assertThrows(CryptoException::class.java) {
            openStream(opener("stream"), frames.copyOfRange(off[1], off[2]) + frames.copyOfRange(off[0], off[1]) + frames.copyOfRange(off[2], frames.size))
        }
        // truncated after a whole (non-last) frame, and mid-frame
        assertThrows(CryptoException::class.java) { openStream(opener("stream"), frames.copyOfRange(0, off[2])) }
        assertThrows(CryptoException::class.java) { openStream(opener("stream"), frames.copyOf(frames.size - 1)) }
        // trailing garbage after the last frame
        assertThrows(CryptoException::class.java) { openStream(opener("stream"), frames + byteArrayOf(1, 2, 3)) }
        // impossible frame length
        val huge = frames.copyOf().also { it[0] = 0x7f }
        assertThrows(CryptoException::class.java) { openStream(opener("stream"), huge) }
        val tiny = byteArrayOf(0, 0, 0, 3, 1, 2, 3)
        assertThrows(CryptoException::class.java) { openStream(opener("stream"), tiny) }
    }

    @Test fun opensPythonEmptyAndSmall() {
        val e = Crypto.b64d(vec.getJSONObject("empty").getString("blob"))
        assertEquals(4 + 16, e.size)
        assertEquals(0, opener("empty").openBytes(e).size)
        assertEquals(0, openFrames(opener("empty"), e, 0, 1, 1).size)
        assertThrows(CryptoException::class.java) { opener("empty").openBytes(e + byteArrayOf(0)) }
        val s = vec.getJSONObject("small")
        val pt = opener("small").openBytes(Crypto.b64d(s.getString("blob")))
        assertEquals(s.getInt("plain_size"), pt.size)
        assertEquals(s.getString("plain_sha256"), sha(pt))
    }

    @Test fun parsesPythonPairingCode() {
        val p = vec.getJSONObject("pairing")
        val got = Crypto.parsePairingCode(p.getString("code"))
        assertEquals(p.getString("name"), got.name)
        assertEquals(p.getString("url"), got.url)
        assertArrayEquals(Crypto.b64d(p.getString("pub")), got.pub)
        assertEquals(p.getString("key"), got.key)
        assertEquals(p.getString("id"), got.id)
        assertEquals(p.getString("peer_name"), got.peerName)
        // surrounding whitespace (pasted from a chat) is fine
        assertEquals(p.getString("name"), Crypto.parsePairingCode("  " + p.getString("code") + "\n").name)
    }

    @Test fun badPairingCodes() {
        assertThrows(IllegalArgumentException::class.java) { Crypto.parsePairingCode("fs2.abc") }
        assertThrows(IllegalArgumentException::class.java) { Crypto.parsePairingCode("fs1.!!!!") }
        val noKey = "fs1." + Crypto.b64e(JSONObject().put("v", 1).put("name", "x").put("pub", Crypto.b64e(serverPub)).toString().toByteArray())
        assertThrows(IllegalArgumentException::class.java) { Crypto.parsePairingCode(noKey) }
        val badPub = "fs1." + Crypto.b64e(JSONObject().put("v", 1).put("name", "x").put("pub", Crypto.b64e(ByteArray(5))).put("key", "k").toString().toByteArray())
        assertThrows(Exception::class.java) { Crypto.parsePairingCode(badPub) }
    }

    // -- Kotlin round trips -----------------------------------------------

    private fun roundTrip(plain: ByteArray, chunked: Boolean = false): Pair<ByteArray, ByteArray> {
        val s = Crypto.Sealer(phonePriv, serverPub)              // phone -> server (uploads)
        val sealed = ByteArrayOutputStream()
        val input: InputStream = ByteArrayInputStream(plain).let { if (chunked) Dribble(it) else it }
        s.sealStream(input, sealed)
        val header = JSONObject(s.header.toString())             // as it travels: serialized
        val pt = Crypto.Opener(serverPriv, phonePub, header).openBytes(sealed.toByteArray())
        return pt to sealed.toByteArray()
    }

    /** An InputStream that returns short reads, like a network or content-resolver stream. */
    private class Dribble(inner: InputStream) : FilterInputStream(inner) {
        override fun read(b: ByteArray, off: Int, len: Int): Int = super.read(b, off, minOf(len, 777))
    }

    @Test fun sealerOpenerRoundTripSizes() {
        val rnd = Random(42)
        for (n in listOf(0, 1, 15, 16, 17, 1000, Crypto.CHUNK - 1, Crypto.CHUNK, Crypto.CHUNK + 1, 2 * Crypto.CHUNK)) {
            val plain = ByteArray(n).also { rnd.nextBytes(it) }
            val (pt, sealed) = roundTrip(plain)
            assertArrayEquals("size $n", plain, pt)
            val frames = maxOf(1, (n + Crypto.CHUNK - 1) / Crypto.CHUNK)
            assertEquals("size $n", n + frames * (4 + 16), sealed.size)
        }
    }

    @Test fun sealerOpenerRoundTripMultiMiBWithShortReads() {
        val plain = ByteArray(5 * Crypto.CHUNK + 12345).also { Random(7).nextBytes(it) }
        val (pt, sealed) = roundTrip(plain, chunked = true)
        assertArrayEquals(plain, pt)
        assertEquals(plain.size + 6 * 20, sealed.size)
    }

    @Test fun kotlinStreamOpensWithOpenFrames() {
        val plain = ByteArray(3 * Crypto.CHUNK + 5).also { Random(9).nextBytes(it) }
        val s = Crypto.Sealer(serverPriv, phonePub)
        val out = ByteArrayOutputStream(); s.sealStream(ByteArrayInputStream(plain), out)
        val sealed = out.toByteArray()
        val o = Crypto.Opener(phonePriv, serverPub, JSONObject(s.header.toString()))
        val f = Crypto.CHUNK + 20                                  // one full sealed frame
        val mid = openFrames(o, sealed.copyOfRange(f, 3 * f), 1, 2, 4)
        assertArrayEquals(plain.copyOfRange(Crypto.CHUNK, 3 * Crypto.CHUNK), mid)
        val tail = openFrames(o, sealed.copyOfRange(3 * f, sealed.size), 3, 1, 4)
        assertArrayEquals(plain.copyOfRange(3 * Crypto.CHUNK, plain.size), tail)
    }

    @Test fun sealerOpenerMeta() {
        val s = Crypto.Sealer(phonePriv, serverPub)
        val meta = JSONObject().put("ts", 1700000000.5).put("to", "server").put("name", "ü日本.jpg").put("n", JSONArray().put(1).put(2))
        val blob = s.sealMeta(meta)
        val got = Crypto.Opener(serverPriv, phonePub, JSONObject(s.header.toString())).openMeta(blob)
        assertEquals(1700000000.5, got.getDouble("ts"), 0.0)
        assertEquals("server", got.getString("to"))
        assertEquals("\u00fc\u65e5\u672c.jpg", got.getString("name"))
        assertEquals("[1,2]", got.getJSONArray("n").toString())
        // every envelope is fresh: new ephemeral key and salt
        val s2 = Crypto.Sealer(phonePriv, serverPub)
        assertFalse(s.header.getString("eph") == s2.header.getString("eph"))
        assertFalse(s.header.getString("salt") == s2.header.getString("salt"))
        assertEquals(1, s.header.getInt("v"))
    }

    @Test fun freshness() {
        val now = System.currentTimeMillis() / 1000.0
        Crypto.checkFreshness(JSONObject().put("ts", now).put("to", "me"), "me")
        Crypto.checkFreshness(JSONObject().put("ts", now), "me")
        assertThrows(CryptoException::class.java) { Crypto.checkFreshness(JSONObject().put("ts", now - 3600), "me") }
        assertThrows(CryptoException::class.java) { Crypto.checkFreshness(JSONObject().put("ts", now + 3600), "me") }
        assertThrows(CryptoException::class.java) { Crypto.checkFreshness(JSONObject().put("ts", now).put("to", "other"), "me") }
    }

    @Test fun pairingRoundTrip() {
        val pub = Crypto.publicKey(Crypto.generatePrivateKey())
        val code = Crypto.makePairingCode("ann-pixel", "", pub, "secret-key", "dev-1")
        assertTrue(code.startsWith("fs1."))
        val p = Crypto.parsePairingCode(code)
        assertEquals("ann-pixel", p.name); assertEquals("", p.url); assertArrayEquals(pub, p.pub)
        assertEquals("secret-key", p.key); assertEquals("dev-1", p.id)
    }

    @Test fun keysAreFreshAndAgree() {
        val a = Crypto.generatePrivateKey(); val b = Crypto.generatePrivateKey()
        assertEquals(32, a.size); assertFalse(a.contentEquals(b))
        assertEquals(32, Crypto.publicKey(a).size)
        val fp = Crypto.fingerprint(Crypto.publicKey(a))
        assertTrue(fp, Regex("[0-9a-f]{4}( [0-9a-f]{4}){3}").matches(fp))
    }

    // -- base64 (url-safe, unpadded out; padded or not in, like Python's b64d) --

    @Test fun base64RoundTripsWithAndWithoutPadding() {
        val rnd = Random(1)
        for (n in 0..40) {
            val b = ByteArray(n).also { rnd.nextBytes(it) }
            val e = Crypto.b64e(b)
            assertFalse(e, e.contains('=') || e.contains('+') || e.contains('/') || e.contains('\n'))
            assertArrayEquals(b, Crypto.b64d(e))
            val padded = e + "=".repeat((4 - e.length % 4) % 4)
            assertArrayEquals("padded $n", b, Crypto.b64d(padded))
        }
        assertEquals("-_8", Crypto.b64e(byteArrayOf(0xfb.toByte(), 0xff.toByte())))
        assertArrayEquals(byteArrayOf(0xfb.toByte(), 0xff.toByte()), Crypto.b64d("-_8="))
    }

    @Test fun base64MatchesPythonEncoding() {
        // Python's b64e of the raw keys is what the vectors hold
        val pub = key("server", "pub")
        assertEquals(vec.getJSONObject("server").getString("pub"), Crypto.b64e(pub))
    }

    @Test fun badBase64IsCryptoException() {
        assertThrows(CryptoException::class.java) { Crypto.b64d("!!!!") }
        assertThrows(CryptoException::class.java) { Crypto.b64d("a") }
    }

}
