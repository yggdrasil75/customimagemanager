package app.cim.family

import org.bouncycastle.crypto.agreement.X25519Agreement
import org.bouncycastle.crypto.digests.SHA256Digest
import org.bouncycastle.crypto.generators.HKDFBytesGenerator
import org.bouncycastle.crypto.params.Ed25519PrivateKeyParameters
import org.bouncycastle.crypto.params.Ed25519PublicKeyParameters
import org.bouncycastle.crypto.params.HKDFParameters
import org.bouncycastle.crypto.params.X25519PrivateKeyParameters
import org.bouncycastle.crypto.params.X25519PublicKeyParameters
import org.bouncycastle.crypto.signers.Ed25519Signer
import java.io.ByteArrayOutputStream
import java.io.Closeable
import java.io.IOException
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.Inet4Address
import java.net.Inet6Address
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.nio.ByteBuffer
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.TreeMap
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.locks.ReentrantLock
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec
import kotlin.concurrent.withLock

/*
 * cimlink, the phone end. Byte-for-byte the protocol of
 * modules/remote_access/link.py (read its header for the design): a paired
 * phone finds the server through rendezvous gateways and/or the BitTorrent DHT,
 * both sides punch through their NATs, an X25519 handshake keyed by the
 * family_share pairing keys sets up an AES-GCM link, and every TCP connection
 * to a local port becomes a reliable stream to the server's app. No Android
 * imports here: this file compiles and runs on a plain JVM.
 */

class LinkException(msg: String) : IOException(msg)

/** @brief Wire constants, key derivation and address coding shared by the link pieces. */
object LinkProto {
    const val VER = 1
    const val MAX_DGRAM = 1232
    const val T_HELLO = 0xC1
    const val T_WELCOME = 0xC2
    const val T_DATA = 0xC3
    const val T_PUNCH = 0xC4
    const val T_QUERY = 0xC7
    const val T_ANSWER = 0xC8
    const val F_PAD = 0
    const val F_PING = 1
    const val F_ACK = 2
    const val F_STREAM = 3
    const val F_MAXDATA = 4
    const val F_RST = 5
    const val F_CLOSE = 6
    const val HDR_DATA = 17
    const val TAG = 16
    const val PAYLOAD_MAX = MAX_DGRAM - HDR_DATA - TAG
    const val STREAM_HDR = 16
    const val STREAM_WINDOW = 1L shl 20
    const val SEND_BUF_MAX = 1 shl 20
    const val MAX_ACK_RANGES = 16
    const val INIT_CWND = 10 * MAX_DGRAM
    const val MIN_CWND = 2 * MAX_DGRAM
    const val IDLE_TIMEOUT_MS = 120_000L
    const val KEEPALIVE_MS = 15_000L
    const val ACK_DELAY_MS = 20L
    const val TICK_MS = 10L
    val KDF_SALT = "cimlink/v1".toByteArray()
    val DHT_SALT = "cimlink".toByteArray()
    val rng = SecureRandom()

    fun hkdf(ikm: ByteArray, salt: ByteArray, info: String, n: Int = 32): ByteArray {
        val g = HKDFBytesGenerator(SHA256Digest())
        g.init(HKDFParameters(ikm, salt, info.toByteArray()))
        return ByteArray(n).also { g.generateBytes(it, 0, n) }
    }

    fun x25519Gen(): ByteArray = X25519PrivateKeyParameters(rng).encoded
    fun x25519Pub(priv: ByteArray): ByteArray = X25519PrivateKeyParameters(priv, 0).generatePublicKey().encoded

    fun dh(priv: ByteArray, pub: ByteArray): ByteArray {
        if (pub.size != 32) throw LinkException("bad public key")
        val a = X25519Agreement(); a.init(X25519PrivateKeyParameters(priv, 0))
        val out = ByteArray(32)
        try { a.calculateAgreement(X25519PublicKeyParameters(pub, 0), out, 0) } catch (e: IllegalStateException) { throw LinkException("bad public key") }
        if (out.all { it.toInt() == 0 }) throw LinkException("bad public key")
        return out
    }

    fun edPub(seed: ByteArray): ByteArray = Ed25519PrivateKeyParameters(seed, 0).generatePublicKey().encoded

    fun edSign(seed: ByteArray, msg: ByteArray): ByteArray {
        val s = Ed25519Signer(); s.init(true, Ed25519PrivateKeyParameters(seed, 0)); s.update(msg, 0, msg.size)
        return s.generateSignature()
    }

    fun edVerify(pub: ByteArray, msg: ByteArray, sig: ByteArray): Boolean = try {
        val s = Ed25519Signer(); s.init(false, Ed25519PublicKeyParameters(pub, 0)); s.update(msg, 0, msg.size)
        s.verifySignature(sig)
    } catch (e: Exception) { false }

    fun gcm(encrypt: Boolean, key: ByteArray, nonce: ByteArray, aad: ByteArray, data: ByteArray, off: Int = 0, len: Int = data.size): ByteArray {
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(if (encrypt) Cipher.ENCRYPT_MODE else Cipher.DECRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, nonce))
        c.updateAAD(aad)
        return c.doFinal(data, off, len)
    }

    fun gcmOpen(key: ByteArray, nonce: ByteArray, aad: ByteArray, data: ByteArray, off: Int = 0, len: Int = data.size): ByteArray? =
        try { gcm(false, key, nonce, aad, data, off, len) } catch (e: Exception) { null }

    fun u64(v: Long): ByteArray = ByteBuffer.allocate(8).putLong(v).array()

    // -- addresses -------------------------------------------------------------
    /** @brief An IPv4-mapped IPv6 address back to plain IPv4. */
    fun norm(a: InetSocketAddress): InetSocketAddress {
        val ip = a.address
        if (ip is Inet6Address) {
            val b = ip.address
            if (b.copyOfRange(0, 10).all { it.toInt() == 0 } && b[10] == 0xFF.toByte() && b[11] == 0xFF.toByte())
                return InetSocketAddress(InetAddress.getByAddress(b.copyOfRange(12, 16)), a.port)
            if (ip.scopeId != 0) return InetSocketAddress(InetAddress.getByAddress(b), a.port)
        }
        return a
    }

    fun encAddr(a: InetSocketAddress): ByteArray {
        val ip = norm(a).address.address
        return byteArrayOf((if (ip.size == 4) 4 else 6).toByte()) + ip + byteArrayOf((a.port shr 8).toByte(), a.port.toByte())
    }

    fun decAddr(buf: ByteArray, off: Int): Pair<InetSocketAddress, Int> {
        val fam = buf[off].toInt()
        val n = when (fam) { 4 -> 4; 6 -> 16; else -> throw LinkException("bad address") }
        if (off + 1 + n + 2 > buf.size) throw LinkException("bad address")
        val ip = InetAddress.getByAddress(buf.copyOfRange(off + 1, off + 1 + n))
        val port = ((buf[off + 1 + n].toInt() and 0xFF) shl 8) or (buf[off + 2 + n].toInt() and 0xFF)
        return InetSocketAddress(ip, port) to off + 3 + n
    }

    fun encCands(cands: List<InetSocketAddress>): ByteArray {
        val c = cands.take(16)
        val out = ByteArrayOutputStream(); out.write(c.size); c.forEach { out.write(encAddr(it)) }
        return out.toByteArray()
    }

    fun decCands(buf: ByteArray, off0: Int): Pair<List<InetSocketAddress>, Int> {
        var off = off0
        val n = buf[off].toInt() and 0xFF; off++
        val out = ArrayList<InetSocketAddress>()
        repeat(n) { val (a, o) = decAddr(buf, off); out.add(a); off = o }
        return out to off
    }

    fun usable(a: InetSocketAddress): Boolean {
        val ip = a.address ?: return false
        return a.port > 0 && !ip.isAnyLocalAddress && !ip.isMulticastAddress && !(ip is Inet6Address && ip.isLinkLocalAddress)
    }

    /** @brief This device's own addresses: the IPv4 it routes out with and a global IPv6. */
    fun hostCandidates(port: Int): List<InetSocketAddress> {
        val out = ArrayList<InetSocketAddress>()
        for (probe in listOf("192.0.2.1", "2001:db8::1")) {
            try {
                DatagramSocket().use { s ->
                    s.connect(InetSocketAddress(InetAddress.getByName(probe), 9))
                    val ip = s.localAddress
                    if (ip != null && !ip.isLoopbackAddress && !ip.isAnyLocalAddress && !(ip is Inet6Address && ip.isLinkLocalAddress))
                        out.add(InetSocketAddress(InetAddress.getByAddress(ip.address), port))
                }
            } catch (e: Exception) { }
        }
        return out
    }

    fun splitHostPort(s0: String, defPort: Int): Pair<String, Int>? {
        val s = s0.trim()
        if (s.isEmpty()) return null
        val (host, port) = when {
            s.startsWith("[") -> { val h = s.substring(1).substringBefore("]"); val r = s.substringAfter("]", ""); h to r.removePrefix(":") }
            s.count { it == ':' } == 1 -> s.substringBefore(":") to s.substringAfter(":")
            else -> s to ""
        }
        return host to (if (port.isEmpty()) defPort else port.toIntOrNull() ?: return null)
    }

    fun resolve(host: String, port: Int): List<InetSocketAddress> = try {
        InetAddress.getAllByName(host).map { InetSocketAddress(it, port) }.sortedBy { if (it.address is Inet6Address) 1 else 0 }
    } catch (e: Exception) { emptyList() }

    // -- STUN --------------------------------------------------------------------
    const val STUN_MAGIC = 0x2112A442

    fun stunRequest(): Pair<ByteArray, ByteArray> {
        val txid = ByteArray(12).also { rng.nextBytes(it) }
        return txid to (ByteBuffer.allocate(20).putShort(1).putShort(0).putInt(STUN_MAGIC).put(txid).array())
    }

    fun stunParse(d: ByteArray): Pair<ByteArray, InetSocketAddress>? {
        if (d.size < 20) return null
        val bb = ByteBuffer.wrap(d)
        if (bb.getShort(0).toInt() and 0xFFFF != 0x0101 || bb.getInt(4) != STUN_MAGIC) return null
        val txid = d.copyOfRange(8, 20)
        val end = minOf(d.size, 20 + (bb.getShort(2).toInt() and 0xFFFF))
        var off = 20
        var mapped: InetSocketAddress? = null
        while (off + 4 <= end) {
            val at = bb.getShort(off).toInt() and 0xFFFF
            val al = bb.getShort(off + 2).toInt() and 0xFFFF
            if ((at == 0x0020 || at == 0x0001) && al >= 8 && off + 4 + al <= d.size) {
                val fam = d[off + 5].toInt()
                var port = bb.getShort(off + 6).toInt() and 0xFFFF
                var raw = d.copyOfRange(off + 8, off + 8 + if (fam == 1) 4 else 16)
                if (at == 0x0020) {
                    port = port xor (STUN_MAGIC ushr 16)
                    val key = ByteBuffer.allocate(4).putInt(STUN_MAGIC).array() + (if (fam == 2) txid else ByteArray(0))
                    raw = ByteArray(raw.size) { (raw[it].toInt() xor key[it].toInt()).toByte() }
                }
                if (at == 0x0020 || mapped == null) mapped = InetSocketAddress(InetAddress.getByAddress(raw), port)
            }
            off += 4 + al + ((4 - al % 4) % 4)
        }
        return mapped?.let { txid to it }
    }

    // -- DHT records -------------------------------------------------------------
    fun sealRecord(box: ByteArray, cands: List<InetSocketAddress>, role: String): ByteArray {
        val nonce = ByteArray(12).also { rng.nextBytes(it) }
        return nonce + gcm(true, box, nonce, role.toByteArray(), encCands(cands) + u64(System.currentTimeMillis()))
    }

    fun openRecord(box: ByteArray, blob: ByteArray, role: String): Pair<List<InetSocketAddress>, Long>? {
        if (blob.size < 12 + TAG) return null
        val pt = gcmOpen(box, blob.copyOfRange(0, 12), role.toByteArray(), blob, 12, blob.size - 12) ?: return null
        return try {
            val (c, off) = decCands(pt, 0)
            c to ByteBuffer.wrap(pt, off, 8).long
        } catch (e: Exception) { null }
    }
}

/** @brief What the phone (initiator) and its server (responder) derive from their pair
 *  secret. The token and DHT record names are bound to the responder's key. */
class PairKeys(myPriv: ByteArray, theirPub: ByteArray, responderPub: ByteArray = theirPub) {
    private val ps = LinkProto.dh(myPriv, theirPub)
    val token: ByteArray = hkdfRaw(ps, "rv".toByteArray() + responderPub).copyOf(16)
    val srvSalt: ByteArray = LinkProto.DHT_SALT + responderPub.copyOf(8)
    val knockSalt: ByteArray = LinkProto.DHT_SALT + "k".toByteArray() + responderPub.copyOf(8)
    val box: ByteArray = LinkProto.hkdf(ps, LinkProto.KDF_SALT, "box")
    val srvSeed: ByteArray = LinkProto.hkdf(ps, LinkProto.KDF_SALT, "dht-srv")
    val devSeed: ByteArray = LinkProto.hkdf(ps, LinkProto.KDF_SALT, "dht-dev")

    private fun hkdfRaw(ikm: ByteArray, info: ByteArray): ByteArray {
        val g = HKDFBytesGenerator(SHA256Digest())
        g.init(HKDFParameters(ikm, LinkProto.KDF_SALT, info))
        return ByteArray(32).also { g.generateBytes(it, 0, 32) }
    }
}

/** @brief Bencoding: Long, ByteArray, String, List, Map<String, Any> (keys as Latin-1). */
object Bencode {
    private val L1 = Charsets.ISO_8859_1

    fun enc(x: Any): ByteArray = ByteArrayOutputStream().also { encTo(x, it) }.toByteArray()

    private fun encTo(x: Any, o: ByteArrayOutputStream) {
        when (x) {
            is Int, is Long -> o.write("i${x}e".toByteArray(L1))
            is ByteArray -> { o.write("${x.size}:".toByteArray(L1)); o.write(x) }
            is String -> encTo(x.toByteArray(), o)
            is List<*> -> { o.write('l'.code); x.forEach { encTo(it!!, o) }; o.write('e'.code) }
            is Map<*, *> -> {
                o.write('d'.code)
                x.entries.map { (it.key as String) to it.value!! }.sortedBy { it.first }.forEach { (k, v) ->
                    encTo(k.toByteArray(L1), o); encTo(v, o)
                }
                o.write('e'.code)
            }
            else -> throw IllegalArgumentException("cannot bencode ${x::class}")
        }
    }

    fun dec(d: ByteArray): Any {
        val (v, i) = decAt(d, 0, 0)
        if (i != d.size) throw IllegalArgumentException("trailing data")
        return v
    }

    private fun decAt(d: ByteArray, i0: Int, depth: Int): Pair<Any, Int> {
        if (depth > 32 || i0 >= d.size) throw IllegalArgumentException("bad bencode")
        var i = i0
        return when (d[i].toInt().toChar()) {
            'i' -> { var j = i + 1; while (d[j] != 'e'.code.toByte()) j++; String(d, i + 1, j - i - 1, L1).toLong() to j + 1 }
            'l' -> {
                val out = ArrayList<Any>(); i++
                while (d[i] != 'e'.code.toByte()) { val (v, n) = decAt(d, i, depth + 1); out.add(v); i = n }
                out to i + 1
            }
            'd' -> {
                val out = HashMap<String, Any>(); i++
                while (d[i] != 'e'.code.toByte()) {
                    val (k, n) = decAt(d, i, depth + 1)
                    val (v, m) = decAt(d, n, depth + 1)
                    out[String(k as ByteArray, L1)] = v; i = m
                }
                out to i + 1
            }
            in '0'..'9' -> {
                var j = i; while (d[j] != ':'.code.toByte()) j++
                val n = String(d, i, j - i, L1).toInt()
                if (n < 0 || j + 1 + n > d.size) throw IllegalArgumentException("short string")
                d.copyOfRange(j + 1, j + 1 + n) to j + 1 + n
            }
            else -> throw IllegalArgumentException("bad bencode")
        }
    }
}

/** @brief A small Mainline DHT client: iterative "get", BEP 44 mutable get / put. */
class Dht(private val send: (ByteArray, InetSocketAddress) -> Unit,
          private val bootstrap: List<String> = BOOTSTRAP, private val allowPrivate: Boolean = false) {
    companion object {
        val BOOTSTRAP = listOf("router.bittorrent.com:6881", "router.utorrent.com:6881", "dht.transmissionbt.com:6881",
            "dht.libtorrent.org:25401", "dht.aelitis.com:6881")
        const val K = 8
        const val ALPHA = 4
        const val QUERY_TIMEOUT_MS = 2000L
        const val LOOKUP_TIMEOUT_MS = 20_000L

        fun sigBuffer(salt: ByteArray, seq: Long, vBencoded: ByteArray): ByteArray {
            val o = ByteArrayOutputStream()
            if (salt.isNotEmpty()) { o.write("4:salt".toByteArray()); o.write(Bencode.enc(salt)) }
            o.write("3:seqi${seq}e1:v".toByteArray()); o.write(vBencoded)
            return o.toByteArray()
        }

        fun target(pub: ByteArray, salt: ByteArray): ByteArray = MessageDigest.getInstance("SHA-1").digest(pub + salt)
    }

    val id = ByteArray(20).also { LinkProto.rng.nextBytes(it) }
    private val pending = ConcurrentHashMap<String, CompletableFuture<Map<String, Any>?>>()
    private val good = ConcurrentHashMap<String, Pair<ByteArray, InetSocketAddress>>()
    private var tid = LinkProto.rng.nextInt(0x10000)

    private fun key(t: ByteArray, a: InetSocketAddress) = t.joinToString("") { "%02x".format(it) } + "@" + LinkProto.norm(a)

    fun handle(data: ByteArray, from: InetSocketAddress) {
        val msg = try { Bencode.dec(data) as? Map<*, *> } catch (e: Exception) { null } ?: return
        val t = msg["t"] as? ByteArray ?: return
        val fut = pending.remove(key(t, from)) ?: return
        @Suppress("UNCHECKED_CAST")
        when (val y = (msg["y"] as? ByteArray)?.let { String(it) }) {
            "r" -> {
                val r = msg["r"] as? Map<String, Any>
                val nid = r?.get("id") as? ByteArray
                if (nid != null && nid.size == 20) good[nid.joinToString("") { "%02x".format(it) }] = nid to LinkProto.norm(from)
                fut.complete(r)
            }
            "e" -> fut.complete(mapOf("__error" to (msg["e"] ?: "")))
            else -> fut.complete(null).also { y }
        }
    }

    private fun query(a: InetSocketAddress, q: String, args: Map<String, Any>): CompletableFuture<Map<String, Any>?> {
        val t: ByteArray
        synchronized(this) { tid = (tid + 1) and 0xFFFF; t = byteArrayOf((tid shr 8).toByte(), tid.toByte()) }
        val fut = CompletableFuture<Map<String, Any>?>()
        pending[key(t, a)] = fut
        send(Bencode.enc(mapOf("t" to t, "y" to "q", "q" to q, "a" to (args + ("id" to id)), "ro" to 1L)), a)
        return fut
    }

    private fun await(a: InetSocketAddress, f: CompletableFuture<Map<String, Any>?>, until: Long): Map<String, Any>? = try {
        f.get(maxOf(1L, until - System.currentTimeMillis()), TimeUnit.MILLISECONDS)
    } catch (e: Exception) { null }

    private fun dist(a: ByteArray, b: ByteArray): java.math.BigInteger =
        java.math.BigInteger(1, ByteArray(20) { (a[it].toInt() xor b[it].toInt()).toByte() })

    private fun parseNodes(blob: ByteArray): List<Pair<ByteArray, InetSocketAddress>> {
        val out = ArrayList<Pair<ByteArray, InetSocketAddress>>()
        var i = 0
        while (i + 26 <= blob.size) {
            val nid = blob.copyOfRange(i, i + 20)
            val ip = InetAddress.getByAddress(blob.copyOfRange(i + 20, i + 24))
            val port = ((blob[i + 24].toInt() and 0xFF) shl 8) or (blob[i + 25].toInt() and 0xFF)
            i += 26
            if (port == 0 || ip.isAnyLocalAddress || ip.isMulticastAddress) continue
            if (!allowPrivate && (ip.isSiteLocalAddress || ip.isLoopbackAddress || ip.isLinkLocalAddress)) continue
            out.add(nid to InetSocketAddress(ip, port))
        }
        return out
    }

    /** @brief Iterative get toward target: -> (id, addr, reply) closest first. */
    fun lookup(target: ByteArray): List<Triple<ByteArray, InetSocketAddress, Map<String, Any>>> {
        val known = LinkedHashMap<InetSocketAddress, ByteArray?>()
        good.values.sortedBy { dist(it.first, target) }.take(K * 2).forEach { known[it.second] = it.first }
        if (known.size < K) for (spec in bootstrap) {
            val hp = LinkProto.splitHostPort(spec, 6881) ?: continue
            LinkProto.resolve(hp.first, hp.second).firstOrNull { it.address is Inet4Address }?.let { known.putIfAbsent(it, null) }
        }
        val asked = HashSet<InetSocketAddress>()
        val replies = LinkedHashMap<InetSocketAddress, Pair<ByteArray, Map<String, Any>>>()
        val deadline = System.currentTimeMillis() + LOOKUP_TIMEOUT_MS
        while (System.currentTimeMillis() < deadline) {
            val closest = replies.values.map { dist(it.first, target) }.sorted().take(K)
            val batch = known.entries.sortedWith(compareBy({ it.value != null }, { it.value?.let { v -> dist(v, target) } ?: java.math.BigInteger.ZERO }))
                .filter { it.key !in asked }
                .filter { e -> e.value == null || closest.size < K || dist(e.value!!, target) < closest.last() }
                .take(ALPHA).map { it.key }
            if (batch.isEmpty()) break
            asked.addAll(batch)
            val until = System.currentTimeMillis() + QUERY_TIMEOUT_MS
            val futs = batch.map { it to query(it, "get", mapOf("target" to target)) }
            for ((a, f) in futs) {
                val r = await(a, f, until) ?: continue
                if (r.containsKey("__error")) continue
                val nid = r["id"] as? ByteArray ?: continue
                if (nid.size != 20) continue
                known[a] = nid
                replies[a] = nid to r
                (r["nodes"] as? ByteArray)?.let { parseNodes(it).forEach { (n, na) -> known.putIfAbsent(na, n) } }
            }
        }
        return replies.entries.map { Triple(it.value.first, it.key, it.value.second) }.sortedBy { dist(it.first, target) }
    }

    /** @brief The newest validly signed item under (pub, salt) -> (v, seq). */
    fun get(pub: ByteArray, salt: ByteArray): Pair<ByteArray, Long>? {
        var best: Pair<ByteArray, Long>? = null
        for ((_, _, r) in lookup(target(pub, salt))) {
            val v = r["v"] as? ByteArray ?: continue
            val seq = r["seq"] as? Long ?: continue
            val sig = r["sig"] as? ByteArray ?: continue
            if (!(r["k"] as? ByteArray ?: continue).contentEquals(pub)) continue
            if (best != null && seq <= best.second) continue
            if (LinkProto.edVerify(pub, sigBuffer(salt, seq, Bencode.enc(v)), sig)) best = v to seq
        }
        return best
    }

    /** @brief Store a signed mutable item on the closest nodes -> how many took it. */
    fun put(seed: ByteArray, salt: ByteArray, v: ByteArray, seq: Long): Int {
        val pub = LinkProto.edPub(seed)
        val sig = LinkProto.edSign(seed, sigBuffer(salt, seq, Bencode.enc(v)))
        val closest = lookup(target(pub, salt)).filter { it.third["token"] is ByteArray }.take(K)
        val until = System.currentTimeMillis() + QUERY_TIMEOUT_MS
        val futs = closest.map { (_, a, r) ->
            a to query(a, "put", mapOf("v" to v, "k" to pub, "seq" to seq, "sig" to sig, "salt" to salt, "token" to r["token"]!!))
        }
        return futs.count { (a, f) -> await(a, f, until)?.containsKey("__error") == false }
    }

    fun close() { pending.values.forEach { it.complete(null) }; pending.clear() }
}

// -- streams and the link --------------------------------------------------------
private sealed class Frame {
    data class S(val sid: Int, val off: Long, val n: Int, val fin: Boolean) : Frame()
    data class M(val sid: Int, val max: Long) : Frame()
    data class R(val sid: Int, val code: Int) : Frame()
    object P : Frame()
}

private class Sent(val pn: Long, val t: Long, val size: Int, val frames: List<Frame>, val eliciting: Boolean)

/** @brief One multiplexed byte stream = one local TCP connection. */
private class LStream(val sid: Int) {
    // send side: buf[start until start+len] holds offsets base until base+len
    var buf = ByteArray(64 * 1024); var start = 0; var len = 0
    var base = 0L; var nextOff = 0L
    var eof = false; var finSent = false; var finAcked = false
    var peerMax = LinkProto.STREAM_WINDOW
    val acked = TreeMap<Long, Long>()
    // receive side
    val chunks = TreeMap<Long, ByteArray>()
    var recvOff = 0L; var finAt = -1L; var consumed = 0L; var advMax = LinkProto.STREAM_WINDOW
    val inbox = LinkedBlockingQueue<ByteArray>()
    var deliveredFin = false; var doneWriting = false; var reset = false
    var sock: Socket? = null

    fun push(d: ByteArray, n: Int) {
        if (start + len + n > buf.size) {
            if (start > 0) { System.arraycopy(buf, start, buf, 0, len); start = 0 }
            if (len + n > buf.size) buf = buf.copyOf(maxOf(buf.size * 2, len + n))
        }
        System.arraycopy(d, 0, buf, start + len, n); len += n
    }

    fun sendable(): Int = maxOf(0L, minOf(base + len, peerMax) - nextOff).toInt()
    fun wantsSend() = sendable() > 0 || (eof && !finSent && nextOff == base + len)
    fun dataAt(off: Long, n: Int): ByteArray {
        val i = (off - base).toInt()
        if (i < 0 || i > len) return ByteArray(0)
        return buf.copyOfRange(start + i, start + i + minOf(n, len - i))
    }

    fun onAcked(off: Long, n: Int, fin: Boolean) {
        if (fin) finAcked = true
        if (n == 0) return
        val hi = off + n
        acked[off] = maxOf(acked[off] ?: 0L, hi)
        var floor = base
        while (acked.isNotEmpty() && acked.firstKey() <= floor) floor = maxOf(floor, acked.pollFirstEntry().value)
        if (floor > base) {
            val cut = minOf((floor - base).toInt(), len)
            start += cut; len -= cut; base = floor
            if (len == 0) start = 0
        }
    }

    fun sendDone() = finAcked && base == nextOff && len == 0

    fun onData(off0: Long, data0: ByteArray, fin: Boolean) {
        var off = off0; var data = data0
        val end = off + data.size
        if (fin) finAt = end
        if (end > advMax + LinkProto.STREAM_WINDOW) throw LinkException("flow control violated")
        if (end > recvOff && data.isNotEmpty()) {
            if (off < recvOff) { data = data.copyOfRange((recvOff - off).toInt(), data.size); off = recvOff }
            val old = chunks[off]
            if (old == null || old.size < data.size) chunks[off] = data
        }
        while (chunks.isNotEmpty() && chunks.firstKey() <= recvOff) {
            val e = chunks.pollFirstEntry()
            if (e.key + e.value.size > recvOff) {
                val d = e.value.copyOfRange((recvOff - e.key).toInt(), e.value.size)
                recvOff += d.size
                inbox.put(d)
            }
        }
        if (finAt >= 0 && recvOff >= finAt && !deliveredFin) { deliveredFin = true; inbox.put(EOF) }
    }

    fun finished() = reset || (sendDone() && doneWriting)

    companion object { val EOF = ByteArray(0) }
}

/** @brief Settings for one link attempt. */
class LinkConfig(
    val devPriv: ByteArray,
    val srvPub: ByteArray,
    val gateways: List<String> = emptyList(),
    val stun: List<String> = emptyList(),
    val dht: Boolean = false,
    val bootstrap: List<String> = Dht.BOOTSTRAP,
    val dhtAllowPrivate: Boolean = false,
    val extraTargets: List<InetSocketAddress> = emptyList(),
)

/**
 * @brief The phone end of cimlink: connect() finds and authenticates the server,
 * serveLocal() opens 127.0.0.1:<port> whose connections are streams to the
 * server's app. One instance = one link; make a new one after it dies.
 */
class Link(private val cfg: LinkConfig, private val log: (String) -> Unit = {}) : Closeable {
    private val lock = ReentrantLock()
    private val room = lock.newCondition()
    private val sock = DatagramSocket(null as java.net.SocketAddress?).apply {
        try { receiveBufferSize = 4 shl 20; sendBufferSize = 4 shl 20 } catch (e: Exception) { }
        bind(InetSocketAddress(0))
    }
    private val keys = PairKeys(cfg.devPriv, cfg.srvPub)
    private val devPub = LinkProto.x25519Pub(cfg.devPriv)
    private val dht: Dht? = if (cfg.dht) Dht({ d, a -> sendTo(d, a) }, cfg.bootstrap, cfg.dhtAllowPrivate) else null
    @Volatile private var closed = false
    private val targets = LinkedHashMap<InetSocketAddress, String>()
    private val reflexive = ConcurrentHashMap<String, InetSocketAddress>()
    private val stunPending = ConcurrentHashMap<String, Long>()
    private var hello: ByteArray? = null
    private lateinit var ePriv: ByteArray
    private lateinit var ePub: ByteArray
    private lateinit var dh12: ByteArray
    private val sidI = ByteArray(8).also { LinkProto.rng.nextBytes(it) }
    private val ready = CompletableFuture<Unit>()
    private var listener: ServerSocket? = null
    @Volatile var localPort = 0; private set
    @Volatile var pathNote = ""; private set
    @Volatile var closeReason = ""; private set
    @Volatile private var lastActivity = System.currentTimeMillis()

    // -- connection state (guarded by lock) --
    private var sidR = ByteArray(8)
    private var kTx = SecretKeySpec(ByteArray(32), "AES")
    private var kRx = SecretKeySpec(ByteArray(32), "AES")
    private val txCipher = Cipher.getInstance("AES/GCM/NoPadding")
    private val rxCipher = Cipher.getInstance("AES/GCM/NoPadding")
    private var path: InetSocketAddress? = null
    private var established = false
    private var pn = 1L
    private val recvRanges = ArrayList<LongArray>()       // [hi, lo], highest first
    private var ackPending = 0
    private var ackSince = 0L
    private val sent = TreeMap<Long, Sent>()
    private var inFlight = 0
    private var cwnd = LinkProto.INIT_CWND
    private var ssthresh = Int.MAX_VALUE
    private var recoveryStart = 0L
    private var srtt = -1.0
    private var rttvar = 0.0
    private var largestAcked = 0L
    private var ptoCount = 0
    private var lastEliciting = 0L
    private val retx = ArrayDeque<Frame>()
    private val control = ArrayDeque<Frame>()
    private val streams = HashMap<Int, LStream>()
    private var nextSid = 1
    private var lastRecv = System.currentTimeMillis()
    private var lastSend = 0L
    private var rr = 0

    private val rxThread = Thread({ rxLoop() }, "cimlink-rx").apply { isDaemon = true; start() }
    private val tickThread = Thread({ tickLoop() }, "cimlink-tick").apply { isDaemon = true; start() }

    val alive: Boolean get() = !closed && established
    val idleMillis: Long get() = lock.withLock { if (streams.isEmpty()) System.currentTimeMillis() - lastActivity else 0L }
    val udpPort: Int get() = sock.localPort

    private fun sendTo(d: ByteArray, a: InetSocketAddress) {
        if (closed) return
        try { sock.send(DatagramPacket(d, d.size, a)) } catch (e: Exception) { }
    }

    // -- receive / timers ----------------------------------------------------------
    private fun rxLoop() {
        val buf = ByteArray(65536)
        while (!closed) {
            val p = DatagramPacket(buf, buf.size)
            try { sock.receive(p) } catch (e: Exception) { if (closed) return else continue }
            if (p.length == 0) continue
            val data = buf.copyOf(p.length)
            val from = LinkProto.norm(p.socketAddress as InetSocketAddress)
            try {
                when (data[0].toInt() and 0xFF) {
                    LinkProto.T_DATA -> lock.withLock { if (established && data.size > 9 && data.copyOfRange(1, 9).contentEquals(sidI)) onData(data, from) }
                    LinkProto.T_WELCOME -> lock.withLock { onWelcome(data, from) }
                    LinkProto.T_PUNCH -> if (data.size >= 18 && data.copyOfRange(2, 18).contentEquals(keys.token)) lock.withLock { addTarget(from, "punch") }
                    LinkProto.T_ANSWER -> onAnswer(data)
                    0x64 -> dht?.handle(data, from)
                    0x00, 0x01 -> onStun(data)
                }
            } catch (e: Exception) { }
        }
    }

    private fun tickLoop() {
        while (!closed) {
            try { Thread.sleep(LinkProto.TICK_MS) } catch (e: InterruptedException) { return }
            lock.withLock { if (established && !closed) tick(System.currentTimeMillis()) }
        }
    }

    private fun onStun(d: ByteArray) {
        val (txid, mapped) = LinkProto.stunParse(d) ?: return
        if (stunPending.remove(txid.joinToString("") { "%02x".format(it) }) != null)
            reflexive["stun" + (if (mapped.address is Inet6Address) "6" else "4")] = LinkProto.norm(mapped)
    }

    private fun onAnswer(d: ByteArray) {
        if (d.size < 20 || d[1].toInt() != LinkProto.VER) return
        val found = d[2].toInt()
        if (!d.copyOfRange(3, 19).contentEquals(keys.token)) return
        val (mine, off) = LinkProto.decAddr(d, 19)
        reflexive["gw" + (if (mine.address is Inet6Address) "6" else "4")] = mine
        if (found == 1) {
            val (srv, off2) = LinkProto.decAddr(d, off)
            val (cands, _) = LinkProto.decCands(d, off2)
            lock.withLock { (listOf(srv) + cands).forEach { addTarget(it, "gateway") } }
        }
    }

    private fun candidates(): List<InetSocketAddress> {
        val out = ArrayList<InetSocketAddress>()
        (LinkProto.hostCandidates(sock.localPort) + reflexive.values).forEach { if (LinkProto.usable(it) && it !in out) out.add(it) }
        return out
    }

    private fun addTarget(a: InetSocketAddress, why: String) {
        if (!LinkProto.usable(a) || targets.containsKey(a)) return
        targets[a] = why
        hello?.let { if (!established) sendTo(it, a) }
    }

    // -- handshake -------------------------------------------------------------------
    private fun makeHello() {
        ePriv = LinkProto.x25519Gen(); ePub = LinkProto.x25519Pub(ePriv)
        dh12 = LinkProto.dh(ePriv, cfg.srvPub) + LinkProto.dh(cfg.devPriv, cfg.srvPub)
        val k1 = LinkProto.hkdf(dh12, ePub, "cimlink/v1/hello")
        val hdr = byteArrayOf(LinkProto.T_HELLO.toByte(), LinkProto.VER.toByte()) + sidI + keys.token + ePub
        hello = hdr + LinkProto.gcm(true, k1, ByteArray(12), hdr, devPub + LinkProto.u64(System.currentTimeMillis()))
    }

    private fun onWelcome(d: ByteArray, from: InetSocketAddress) {
        if (established || hello == null || d.size < 2 + 8 + 8 + 32 + 8 + LinkProto.TAG) return
        if (!d.copyOfRange(2, 10).contentEquals(sidI)) return
        val sid = d.copyOfRange(10, 18); val eR = d.copyOfRange(18, 50)
        val okm = LinkProto.hkdf(dh12 + LinkProto.dh(ePriv, eR) + LinkProto.dh(cfg.devPriv, eR), ePub + eR, "cimlink/v1/session", 64)
        val kI2R = okm.copyOfRange(0, 32); val kR2I = okm.copyOfRange(32, 64)
        LinkProto.gcmOpen(kR2I, ByteArray(12), d.copyOfRange(0, 50), d, 50, d.size - 50) ?: return
        sidR = sid; kTx = SecretKeySpec(kI2R, "AES"); kRx = SecretKeySpec(kR2I, "AES"); path = from; established = true
        lastRecv = System.currentTimeMillis()
        pathNote = "${from.address.hostAddress}:${from.port} (${targets[from] ?: "reply"})"
        ready.complete(Unit)
    }

    /** @brief Find the server and complete the handshake (blocking). */
    fun connect(timeoutMs: Long = 45_000) {
        lock.withLock { makeHello(); cfg.extraTargets.forEach { addTarget(it, "given") } }
        for (spec in cfg.stun) {
            val hp = LinkProto.splitHostPort(spec, 3478) ?: continue
            LinkProto.resolve(hp.first, hp.second).take(2).forEach { a ->
                val (txid, pkt) = LinkProto.stunRequest()
                stunPending[txid.joinToString("") { "%02x".format(it) }] = System.currentTimeMillis()
                sendTo(pkt, a)
            }
        }
        try { ready.get(300, TimeUnit.MILLISECONDS) } catch (e: Exception) { }
        val gws = cfg.gateways.mapNotNull { LinkProto.splitHostPort(it, 7777) }.mapNotNull { LinkProto.resolve(it.first, it.second).firstOrNull() }
        val dhtThread = dht?.let { d -> Thread({ dhtFind(d) }, "cimlink-dht").apply { isDaemon = true; start() } }
        val deadline = System.currentTimeMillis() + timeoutMs
        var lastQuery = 0L
        try {
            while (!ready.isDone) {
                if (closed) throw LinkException("closed")
                val now = System.currentTimeMillis()
                if (now - lastQuery >= 2000) {
                    lastQuery = now
                    val q = byteArrayOf(LinkProto.T_QUERY.toByte(), LinkProto.VER.toByte()) + keys.token + LinkProto.encCands(candidates())
                    gws.forEach { sendTo(q, it) }
                }
                val h = lock.withLock { hello!! to targets.keys.toList() }
                h.second.forEach { sendTo(h.first, it) }
                try { ready.get(250, TimeUnit.MILLISECONDS) } catch (e: java.util.concurrent.TimeoutException) { }
                if (System.currentTimeMillis() > deadline) {
                    val n = lock.withLock { targets.size }
                    throw LinkException(if (n == 0) "server not found (no gateway answered and nothing on the DHT yet)"
                                        else "server not reachable (tried $n address(es); both sides may be behind strict NATs)")
                }
            }
        } finally { dhtThread?.interrupt() }
        log("linked via $pathNote")
    }

    private fun dhtFind(d: Dht) {
        try {
            val rec = LinkProto.sealRecord(keys.box, candidates(), "dev")
            d.put(keys.devSeed, keys.knockSalt, rec, System.currentTimeMillis() / 1000)
            if (ready.isDone) return
            val got = d.get(LinkProto.edPub(keys.srvSeed), keys.srvSalt) ?: return
            val (cands, _) = LinkProto.openRecord(keys.box, got.first, "srv") ?: return
            lock.withLock { cands.forEach { addTarget(it, "dht") } }
        } catch (e: Exception) { log("dht: ${e.message}") }
    }

    /** @brief Listen on 127.0.0.1; every accepted connection becomes a stream to the server's app. */
    fun serveLocal(port: Int = 0): Int {
        val ss = ServerSocket(port, 50, InetAddress.getByName("127.0.0.1"))
        listener = ss
        localPort = ss.localPort
        Thread({
            while (!closed) {
                val s = try { ss.accept() } catch (e: Exception) { break }
                s.tcpNoDelay = true
                val st = lock.withLock {
                    if (!established || closed) null else LStream(nextSid).also { nextSid += 2; streams[it.sid] = it; it.sock = s; lastActivity = System.currentTimeMillis() }
                }
                if (st == null) { s.close(); continue }
                Thread({ pumpOut(st, s) }, "cimlink-out-${st.sid}").apply { isDaemon = true; start() }
                Thread({ pumpIn(st, s) }, "cimlink-in-${st.sid}").apply { isDaemon = true; start() }
            }
        }, "cimlink-accept").apply { isDaemon = true; start() }
        return localPort
    }

    private fun pumpOut(s: LStream, sock: Socket) {
        val buf = ByteArray(32768)
        try {
            val inp = sock.getInputStream()
            while (true) {
                lock.withLock { while (s.len >= LinkProto.SEND_BUF_MAX && !s.reset && !closed) room.await(200, TimeUnit.MILLISECONDS) }
                if (s.reset || closed) return
                val n = inp.read(buf)
                if (n < 0) break
                lock.withLock { s.push(buf, n); lastActivity = System.currentTimeMillis(); flush() }
            }
            lock.withLock { s.eof = true; flush() }
        } catch (e: Exception) {
            lock.withLock { if (!s.reset) killStream(s, true) }
        }
    }

    private fun pumpIn(s: LStream, sock: Socket) {
        try {
            val out = sock.getOutputStream()
            while (true) {
                val chunk = s.inbox.take()
                if (chunk === LStream.EOF) break
                out.write(chunk); out.flush()
                lock.withLock { consumed(s, chunk.size) }
            }
            lock.withLock {
                if (!s.reset) {
                    try { sock.shutdownOutput() } catch (e: Exception) { }
                    s.doneWriting = true
                    maybeDone(s)
                }
            }
        } catch (e: Exception) {
            lock.withLock { if (!s.reset) killStream(s, true) }
        }
    }

    // -- packets (lock held) ----------------------------------------------------------
    private fun nonce(p: Long) = ByteArray(4) + LinkProto.u64(p)

    private fun onData(d: ByteArray, from: InetSocketAddress) {
        if (d.size < LinkProto.HDR_DATA + LinkProto.TAG) return
        val p = ByteBuffer.wrap(d, 9, 8).long
        val pt = try {
            rxCipher.init(Cipher.DECRYPT_MODE, kRx, GCMParameterSpec(128, nonce(p)))
            rxCipher.updateAAD(d, 0, LinkProto.HDR_DATA)
            rxCipher.doFinal(d, LinkProto.HDR_DATA, d.size - LinkProto.HDR_DATA)
        } catch (e: Exception) { return }
        val now = System.currentTimeMillis()
        lastRecv = now
        if (from != path) path = from
        if (seen(p)) { ackPending++; return }
        var eliciting = false
        try {
            val bb = ByteBuffer.wrap(pt)
            loop@ while (bb.hasRemaining()) {
                when (bb.get().toInt() and 0xFF) {
                    LinkProto.F_PAD -> break@loop
                    LinkProto.F_PING -> eliciting = true
                    LinkProto.F_ACK -> {
                        val n = bb.get().toInt() and 0xFF
                        val ranges = List(n) { longArrayOf(bb.long, bb.long) }
                        onAck(ranges, now)
                    }
                    LinkProto.F_STREAM -> {
                        val sid = bb.int; val off = bb.long; val ln = bb.short.toInt() and 0xFFFF; val fin = bb.get().toInt() != 0
                        val chunk = ByteArray(ln); bb.get(chunk)
                        eliciting = true
                        streams[sid]?.let { it.onData(off, chunk, fin); lastActivity = now }
                    }
                    LinkProto.F_MAXDATA -> {
                        val sid = bb.int; val mx = bb.long
                        eliciting = true
                        streams[sid]?.let { if (mx > it.peerMax) it.peerMax = mx }
                    }
                    LinkProto.F_RST -> {
                        val sid = bb.int; bb.get()
                        eliciting = true
                        streams[sid]?.let { killStream(it, false) }
                    }
                    LinkProto.F_CLOSE -> {
                        val n = bb.get().toInt() and 0xFF
                        val r = ByteArray(n); bb.get(r)
                        closeReason = String(r).ifEmpty { "closed by server" }
                        shutdown(false)
                        return
                    }
                    else -> break@loop
                }
            }
        } catch (e: Exception) {
            closeReason = "protocol error"; shutdown(true); return
        }
        record(p)
        if (eliciting) { ackPending++; if (ackPending == 1) ackSince = now }
        flush()
    }

    private fun seen(p: Long): Boolean {
        for (r in recvRanges) if (p <= r[0] && p >= r[1]) return true
        return recvRanges.size >= 64 && p < recvRanges.last()[1]
    }

    private fun record(p: Long) {
        val r = recvRanges
        var i = 0
        while (i < r.size) {
            val hi = r[i][0]; val lo = r[i][1]
            if (p >= lo - 1 && p <= hi + 1) {
                r[i][0] = maxOf(hi, p); r[i][1] = minOf(lo, p)
                if (i > 0 && r[i - 1][1] <= r[i][0] + 1) { r[i - 1][1] = minOf(r[i - 1][1], r[i][1]); r.removeAt(i); i-- }
                if (i + 1 < r.size && r[i + 1][0] >= r[i][1] - 1) { r[i][1] = minOf(r[i][1], r[i + 1][1]); r.removeAt(i + 1) }
                return
            }
            if (p > hi) { r.add(i, longArrayOf(p, p)); while (r.size > 64) r.removeAt(r.size - 1); return }
            i++
        }
        r.add(longArrayOf(p, p)); while (r.size > 64) r.removeAt(r.size - 1)
    }

    private fun ackFrame(): ByteArray {
        val rs = recvRanges.take(LinkProto.MAX_ACK_RANGES)
        val bb = ByteBuffer.allocate(2 + 16 * rs.size)
        bb.put(LinkProto.F_ACK.toByte()).put(rs.size.toByte())
        rs.forEach { bb.putLong(it[0]).putLong(it[1]) }
        return bb.array()
    }

    private fun onAck(ranges: List<LongArray>, now: Long) {
        val acked = ArrayList<Sent>()
        for (r in ranges) {
            val sub = sent.subMap(r[1], true, r[0], true)
            acked.addAll(sub.values); sub.clear()
        }
        if (acked.isEmpty()) return
        val largest = acked.maxOf { it.pn }
        var newly = 0
        for (p in acked) {
            if (p.eliciting) { inFlight -= p.size; newly += p.size }
            if (p.pn == largest && p.eliciting) rtt((now - p.t) / 1000.0)
            for (f in p.frames) if (f is Frame.S) streams[f.sid]?.let { it.onAcked(f.off, f.n, f.fin); maybeDone(it) }
        }
        room.signalAll()
        if (largest > largestAcked) largestAcked = largest
        ptoCount = 0
        if (newly > 0) cwnd += if (cwnd < ssthresh) newly else maxOf(1, LinkProto.MAX_DGRAM * newly / cwnd)
        detectLoss(now)
    }

    private fun rtt(sample: Double) {
        if (srtt < 0) { srtt = sample; rttvar = sample / 2 }
        else { rttvar = 0.75 * rttvar + 0.25 * Math.abs(srtt - sample); srtt = 0.875 * srtt + 0.125 * sample }
    }

    private fun detectLoss(now: Long) {
        val delay = (9.0 / 8 * maxOf(if (srtt < 0) 0.1 else srtt, 0.001) * 1000).toLong()
        val lost = ArrayList<Sent>()
        for (p in sent.values) {
            if (p.pn >= largestAcked) break
            if (p.pn <= largestAcked - 3 || p.t < now - delay) lost.add(p) else break
        }
        if (lost.isNotEmpty()) lose(lost, now)
    }

    private fun lose(packets: List<Sent>, now: Long) {
        var newest = 0L
        for (p in packets) {
            sent.remove(p.pn)
            if (p.eliciting) inFlight -= p.size
            newest = maxOf(newest, p.t)
            for (f in p.frames) when (f) {
                is Frame.S, is Frame.R -> retx.addLast(f)
                is Frame.M -> streams[f.sid]?.let { control.addLast(Frame.M(f.sid, it.advMax)) }
                else -> {}
            }
        }
        if (newest > recoveryStart) {
            recoveryStart = now
            ssthresh = maxOf(cwnd / 2, LinkProto.MIN_CWND)
            cwnd = ssthresh
        }
    }

    private fun pto(): Long {
        val r = if (srtt < 0) 0.3 else srtt
        return ((r + maxOf(4 * rttvar, 0.01) + LinkProto.ACK_DELAY_MS / 1000.0) * (1 shl minOf(ptoCount, 6)) * 1000).toLong()
    }

    private fun sendPacket(payload0: ByteArray, frames: List<Frame>, eliciting: Boolean) {
        val payload = if (payload0.size < 8) payload0 + ByteArray(8 - payload0.size) else payload0
        val p = pn++
        val hdr = byteArrayOf(LinkProto.T_DATA.toByte()) + sidR + LinkProto.u64(p)
        txCipher.init(Cipher.ENCRYPT_MODE, kTx, GCMParameterSpec(128, nonce(p)))
        txCipher.updateAAD(hdr)
        val pkt = hdr + txCipher.doFinal(payload)
        val now = System.currentTimeMillis()
        if (eliciting || frames.isNotEmpty()) sent[p] = Sent(p, now, pkt.size, frames, eliciting)
        if (eliciting) { inFlight += pkt.size; lastEliciting = now }
        lastSend = now
        path?.let { sendTo(pkt, it) }
    }

    private class Built(val frame: ByteArray, val took: Int, val fin: Boolean)

    private fun streamFrame(s: LStream, off: Long, n: Int, fin: Boolean, room: Int): Built? {
        val take = minOf(n, room - LinkProto.STREAM_HDR)
        if (take < 0 || (take == 0 && n > 0)) return null
        val data = s.dataAt(off, take)
        val finNow = fin && data.size == n
        val bb = ByteBuffer.allocate(LinkProto.STREAM_HDR + data.size)
        bb.put(LinkProto.F_STREAM.toByte()).putInt(s.sid).putLong(off).putShort(data.size.toShort()).put(if (finNow) 1 else 0).put(data)
        return Built(bb.array(), data.size, finNow)
    }

    private fun flush() {
        if (closed || !established) return
        val now = System.currentTimeMillis()
        while (true) {
            var roomLeft = LinkProto.PAYLOAD_MAX
            val parts = ByteArrayOutputStream()
            val frames = ArrayList<Frame>()
            var eliciting = false
            if (ackPending > 0 && (ackPending >= 2 || now - ackSince >= LinkProto.ACK_DELAY_MS)) {
                val a = ackFrame(); parts.write(a); roomLeft -= a.size; ackPending = 0
            }
            val canMore = inFlight + LinkProto.MAX_DGRAM <= cwnd
            while (canMore && control.isNotEmpty() && roomLeft > 13) {
                val f = control.removeFirst()
                if (f is Frame.M) { parts.write(ByteBuffer.allocate(13).put(LinkProto.F_MAXDATA.toByte()).putInt(f.sid).putLong(f.max).array()); roomLeft -= 13 }
                else { parts.write(LinkProto.F_PING); roomLeft -= 1 }
                frames.add(f); eliciting = true
            }
            while (canMore && retx.isNotEmpty() && roomLeft > LinkProto.STREAM_HDR + 1) {
                val f = retx.removeFirst()
                if (f is Frame.R) {
                    parts.write(ByteBuffer.allocate(6).put(LinkProto.F_RST.toByte()).putInt(f.sid).put(f.code.toByte()).array())
                    roomLeft -= 6; frames.add(f); eliciting = true; continue
                }
                if (f !is Frame.S) continue
                val s = streams[f.sid] ?: continue
                if (s.reset) continue
                var off = f.off; var n = f.n
                if (off < s.base) { val cut = minOf(n.toLong(), s.base - off).toInt(); off += cut; n -= cut }
                if (n == 0 && (!f.fin || s.finAcked)) continue
                val b = streamFrame(s, off, n, f.fin, roomLeft)
                if (b == null) { retx.addFirst(Frame.S(s.sid, off, n, f.fin)); break }
                if (b.took < n) retx.addFirst(Frame.S(s.sid, off + b.took, n - b.took, f.fin))
                parts.write(b.frame); roomLeft -= b.frame.size
                frames.add(Frame.S(s.sid, off, b.took, b.fin)); eliciting = true
            }
            if (canMore && retx.isEmpty()) {
                val ids = streams.values.filter { !it.reset && it.wantsSend() }.map { it.sid }.sorted()
                if (ids.isNotEmpty()) {
                    val st = rr % ids.size; rr++
                    for (sid in ids.drop(st) + ids.take(st)) {
                        if (roomLeft <= LinkProto.STREAM_HDR + 1) break
                        val s = streams[sid]!!
                        val n = s.sendable()
                        val fin = s.eof && !s.finSent && s.nextOff + n == s.base + s.len
                        if (n == 0 && !fin) continue
                        val b = streamFrame(s, s.nextOff, n, fin, roomLeft) ?: break
                        parts.write(b.frame); roomLeft -= b.frame.size
                        frames.add(Frame.S(sid, s.nextOff, b.took, b.fin))
                        s.nextOff += b.took
                        if (b.fin) s.finSent = true
                        eliciting = true
                    }
                }
            }
            if (parts.size() == 0) break
            sendPacket(parts.toByteArray(), frames, eliciting)
            if (!eliciting) break
        }
    }

    private fun tick(now: Long) {
        if (now - lastRecv > LinkProto.IDLE_TIMEOUT_MS) { closeReason = closeReason.ifEmpty { "idle" }; shutdown(false); return }
        if (sent.isNotEmpty() && lastEliciting > 0 && now - lastEliciting > pto()) {
            ptoCount++
            if (ptoCount > 12) { closeReason = "server unreachable"; shutdown(false); return }
            val oldest = sent.values.filter { it.eliciting }.take(2)
            if (oldest.isNotEmpty()) lose(oldest, now) else control.addLast(Frame.P)
            lastEliciting = now
        }
        if (now - maxOf(lastSend, lastRecv) > LinkProto.KEEPALIVE_MS) sendPacket(byteArrayOf(LinkProto.F_PING.toByte()), listOf(Frame.P), true)
        if (ackPending > 0 && now - ackSince >= LinkProto.ACK_DELAY_MS) ackPending = maxOf(ackPending, 2)
        flush()
    }

    private fun consumed(s: LStream, n: Int) {
        s.consumed += n
        if (s.consumed + LinkProto.STREAM_WINDOW - s.advMax >= LinkProto.STREAM_WINDOW / 2) {
            s.advMax = s.consumed + LinkProto.STREAM_WINDOW
            control.addLast(Frame.M(s.sid, s.advMax))
            flush()
        }
    }

    private fun killStream(s: LStream, sendRst: Boolean) {
        if (s.reset) return
        s.reset = true
        if (sendRst) retx.addLast(Frame.R(s.sid, 0))
        s.inbox.put(LStream.EOF)
        try { s.sock?.close() } catch (e: Exception) { }
        streams.remove(s.sid)
        room.signalAll()
        flush()
    }

    private fun maybeDone(s: LStream) {
        if (s.finished()) {
            streams.remove(s.sid)
            try { s.sock?.close() } catch (e: Exception) { }
            lastActivity = System.currentTimeMillis()
        }
    }

    private fun shutdown(sendClose: Boolean) {
        if (closed) return
        if (sendClose && established) {
            val r = "closed".toByteArray()
            try { sendPacket(byteArrayOf(LinkProto.F_CLOSE.toByte(), r.size.toByte()) + r, emptyList(), true) } catch (e: Exception) { }
        }
        closed = true
        streams.values.toList().forEach { s -> s.reset = true; s.inbox.put(LStream.EOF); try { s.sock?.close() } catch (e: Exception) { } }
        streams.clear()
        room.signalAll()
        try { listener?.close() } catch (e: Exception) { }
        dht?.close()
        sock.close()
        tickThread.interrupt()
    }

    override fun close() { lock.withLock { closeReason = closeReason.ifEmpty { "closed" }; shutdown(true) } }
}