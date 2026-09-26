package app.cim.family

import android.util.Base64
import org.bouncycastle.crypto.agreement.X25519Agreement
import org.bouncycastle.crypto.digests.SHA256Digest
import org.bouncycastle.crypto.generators.HKDFBytesGenerator
import org.bouncycastle.crypto.params.HKDFParameters
import org.bouncycastle.crypto.params.X25519PrivateKeyParameters
import org.bouncycastle.crypto.params.X25519PublicKeyParameters
import org.json.JSONObject
import java.io.EOFException
import java.io.InputStream
import java.io.OutputStream
import java.nio.ByteBuffer
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * Byte-for-byte the same scheme as modules/family_share/crypto.py on the
 * server: X25519 (ephemeral + static) -> HKDF-SHA256 -> AES-256-GCM, a
 * sealed JSON metadata blob and a framed 1 MiB chunk stream for files.
 */
class CryptoException(msg: String) : Exception(msg)

object Crypto {
    const val VERSION = 1
    const val CHUNK = 1 shl 20
    const val MAX_SKEW = 15 * 60
    private val INFO = "family_share/v1/".toByteArray()
    private val rng = SecureRandom()
    private const val B64 = Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP

    fun b64e(b: ByteArray): String = Base64.encodeToString(b, B64)
    fun b64d(s: String): ByteArray = try { Base64.decode(s, B64) } catch (e: Exception) { throw CryptoException("bad base64") }

    fun generatePrivateKey(): ByteArray = X25519PrivateKeyParameters(rng).encoded
    fun publicKey(priv: ByteArray): ByteArray = X25519PrivateKeyParameters(priv, 0).generatePublicKey().encoded

    fun fingerprint(pub: ByteArray): String {
        val hex = MessageDigest.getInstance("SHA-256").digest(pub).joinToString("") { "%02x".format(it) }.substring(0, 16)
        return hex.chunked(4).joinToString(" ")
    }

    fun sha256Hex(input: InputStream): String {
        val md = MessageDigest.getInstance("SHA-256")
        val buf = ByteArray(1 shl 16)
        while (true) { val n = input.read(buf); if (n < 0) break; md.update(buf, 0, n) }
        return md.digest().joinToString("") { "%02x".format(it) }
    }

    private fun x25519(priv: ByteArray, pub: ByteArray): ByteArray {
        val a = X25519Agreement(); a.init(X25519PrivateKeyParameters(priv, 0))
        val out = ByteArray(32)
        a.calculateAgreement(X25519PublicKeyParameters(checkPub(pub), 0), out, 0)
        return out
    }

    private fun checkPub(pub: ByteArray): ByteArray {
        if (pub.size != 32) throw CryptoException("bad public key")
        return pub
    }

    private fun hkdf(shared: ByteArray, salt: ByteArray, label: String): ByteArray {
        val g = HKDFBytesGenerator(SHA256Digest())
        g.init(HKDFParameters(shared, salt, INFO + label.toByteArray()))
        val out = ByteArray(32); g.generateBytes(out, 0, 32); return out
    }

    private fun gcm(mode: Int, key: ByteArray, nonce: ByteArray, aad: ByteArray, data: ByteArray): ByteArray {
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(mode, SecretKeySpec(key, "AES"), GCMParameterSpec(128, nonce))
        c.updateAAD(aad)
        return c.doFinal(data)
    }

    private fun fileNonce(i: Long): ByteArray = ByteBuffer.allocate(12).putLong(i).putInt(0).array()
    private fun fileAad(i: Long, last: Boolean) = "file:$i:${if (last) 1 else 0}".toByteArray()

    class Sealer(senderPriv: ByteArray, recipientPub: ByteArray) {
        val header: JSONObject
        private val kMeta: ByteArray
        private val kFile: ByteArray

        init {
            val eph = X25519PrivateKeyParameters(rng)
            val salt = ByteArray(16).also { rng.nextBytes(it) }
            val shared = x25519(eph.encoded, recipientPub) + x25519(senderPriv, recipientPub)
            kMeta = hkdf(shared, salt, "meta")
            kFile = hkdf(shared, salt, "file")
            header = JSONObject().put("v", VERSION).put("salt", b64e(salt)).put("eph", b64e(eph.generatePublicKey().encoded))
        }

        fun sealMeta(obj: JSONObject): String {
            val nonce = ByteArray(12).also { rng.nextBytes(it) }
            val ct = gcm(Cipher.ENCRYPT_MODE, kMeta, nonce, "meta".toByteArray(), obj.toString().toByteArray())
            return b64e(nonce + ct)
        }

        fun sealStream(input: InputStream, size: Long, out: OutputStream) {
            var done = 0L; var i = 0L
            val buf = ByteArray(CHUNK)
            while (true) {
                val n = readFull(input, buf)
                done += n
                val last = done >= size
                val ct = gcm(Cipher.ENCRYPT_MODE, kFile, fileNonce(i), fileAad(i, last), buf.copyOf(n))
                out.write(ByteBuffer.allocate(4).putInt(ct.size).array()); out.write(ct)
                i++
                if (last) break
            }
        }
    }

    class Opener(recipientPriv: ByteArray, senderPub: ByteArray, header: JSONObject) {
        private val kMeta: ByteArray
        private val kFile: ByteArray

        init {
            if (header.optInt("v", 0) != VERSION) throw CryptoException("unsupported envelope version")
            val salt = b64d(header.optString("salt"))
            val eph = b64d(header.optString("eph"))
            val shared = x25519(recipientPriv, eph) + x25519(recipientPriv, senderPub)
            kMeta = hkdf(shared, salt, "meta")
            kFile = hkdf(shared, salt, "file")
        }

        fun openMeta(blob: String): JSONObject {
            val raw = b64d(blob)
            if (raw.size < 28) throw CryptoException("metadata blob too short")
            val pt = try {
                gcm(Cipher.DECRYPT_MODE, kMeta, raw.copyOfRange(0, 12), "meta".toByteArray(), raw.copyOfRange(12, raw.size))
            } catch (e: Exception) { throw CryptoException("metadata failed to authenticate") }
            return try { JSONObject(String(pt)) } catch (e: Exception) { throw CryptoException("metadata is not JSON") }
        }

        fun openStream(input: InputStream, out: OutputStream) {
            var i = 0L; var last = false
            val hdr = ByteArray(4)
            while (!last) {
                if (readFull(input, hdr) < 4) throw CryptoException("file stream truncated")
                val n = ByteBuffer.wrap(hdr).int
                if (n < 16 || n > CHUNK + 16) throw CryptoException("file frame has an impossible size")
                val ct = ByteArray(n)
                if (readFull(input, ct) < n) throw CryptoException("file stream truncated")
                var pt: ByteArray? = null
                for (flag in listOf(false, true)) {
                    try { pt = gcm(Cipher.DECRYPT_MODE, kFile, fileNonce(i), fileAad(i, flag), ct); last = flag; break }
                    catch (e: Exception) { /* try the other flag */ }
                }
                out.write(pt ?: throw CryptoException("file chunk $i failed to authenticate"))
                i++
            }
            if (input.read() != -1) throw CryptoException("data after the final chunk")
        }

        fun openBytes(data: ByteArray): ByteArray {
            val out = java.io.ByteArrayOutputStream(); openStream(data.inputStream(), out); return out.toByteArray()
        }
    }

    /** Fill buf as far as the stream allows; returns bytes read (0 at EOF). */
    private fun readFull(input: InputStream, buf: ByteArray): Int {
        var got = 0
        while (got < buf.size) {
            val n = input.read(buf, got, buf.size - got)
            if (n < 0) break
            got += n
        }
        return got
    }

    fun checkFreshness(meta: JSONObject, myId: String) {
        val ts = meta.optDouble("ts", 0.0)
        if (kotlin.math.abs(System.currentTimeMillis() / 1000.0 - ts) > MAX_SKEW) throw CryptoException("envelope is stale")
        val to = meta.optString("to", "")
        if (to.isNotEmpty() && to != myId) throw CryptoException("envelope is addressed to another device")
    }

    // ── pairing codes ────────────────────────────────────────────────────
    data class Pairing(val name: String, val url: String, val pub: ByteArray, val key: String, val id: String)

    fun makePairingCode(name: String, url: String, pub: ByteArray, keyIn: String, id: String): String {
        val body = JSONObject().put("v", VERSION).put("name", name).put("url", url).put("pub", b64e(pub)).put("key", keyIn).put("id", id)
        return "fs1." + b64e(body.toString().toByteArray())
    }

    fun parsePairingCode(code: String): Pairing {
        val c = code.trim()
        if (!c.startsWith("fs1.")) throw IllegalArgumentException("not a family_share pairing code")
        val d = try { JSONObject(String(b64d(c.substring(4)))) } catch (e: Exception) { throw IllegalArgumentException("pairing code is corrupt") }
        val name = d.optString("name"); val pub = d.optString("pub"); val key = d.optString("key")
        if (name.isEmpty() || pub.isEmpty() || key.isEmpty()) throw IllegalArgumentException("pairing code is missing fields")
        return Pairing(name, d.optString("url"), checkPub(b64d(pub)), key, d.optString("id"))
    }
}
