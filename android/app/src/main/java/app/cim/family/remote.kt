package app.cim.family

import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.net.InetSocketAddress
import java.net.Socket
import java.net.URI
import java.util.concurrent.TimeUnit

/**
 * @brief Picks how the app reaches its server: the paired URL when it answers
 * (home network, or a server with a public address), otherwise the direct
 * cimlink link (Link.kt) found through the server's gateways and the DHT. Api
 * asks base() for every request, so the rest of the app never knows which.
 */
object Remote {
    /** Gateways a fresh install tries before it has heard the server's own list
     *  (host:port of machines running modules/remote_access/gateway.py). */
    val DEFAULT_GATEWAYS: List<String> = emptyList()
    val DEFAULT_STUN = listOf("stun.l.google.com:19302", "stun.cloudflare.com:3478")
    private const val DIRECT_TTL_MS = 60_000L
    private const val CONFIG_TTL_MS = 6 * 3600_000L
    private const val LINK_IDLE_MS = 3 * 60_000L

    private var link: Link? = null
    private var directAt = 0L
    private var directOk = false
    private val http = OkHttpClient.Builder().connectTimeout(4, TimeUnit.SECONDS).readTimeout(6, TimeUnit.SECONDS).build()

    init {
        Thread({
            while (true) {
                try { Thread.sleep(30_000) } catch (e: InterruptedException) { break }
                synchronized(this) {
                    val l = link
                    if (l != null && (!l.alive || l.idleMillis > LINK_IDLE_MS)) { l.close(); link = null }
                }
            }
        }, "cimlink-idle").apply { isDaemon = true; start() }
    }

    /** @brief The base URL for the next request (blocking: may set up the link first). */
    @Synchronized fun base(prefs: Prefs): String {
        val url = prefs.serverUrl.trimEnd('/')
        val now = System.currentTimeMillis()
        if (!prefs.remoteEnabled) {
            // still learn, now and then, whether the server offers it (that turns it on here)
            if (now - prefs.remoteConfigAt > CONFIG_TTL_MS) fetchConfig(prefs, url)
            if (!prefs.remoteEnabled) return url
        }
        if (now - directAt > DIRECT_TTL_MS) { directOk = reachable(url); directAt = now }
        if (directOk) {
            if (now - prefs.remoteConfigAt > CONFIG_TTL_MS) fetchConfig(prefs, url)
            prefs.linkStatus = "Direct: $url answers"
            return url
        }
        return "http://127.0.0.1:" + ensureLink(prefs).localPort
    }

    /** @brief Forget the direct-reachability verdict (the network changed, a request failed). */
    @Synchronized fun recheck() { directAt = 0L }

    /** @brief Settings -> "Test remote link": build the link even at home and ping through it. */
    fun test(prefs: Prefs): String {
        val l = synchronized(this) { ensureLink(prefs) }
        val j = Api(prefs, "http://127.0.0.1:" + l.localPort).ping()
        return "Linked via ${l.pathNote}; reached ${j.optString("name")}"
    }

    private fun ensureLink(prefs: Prefs): Link {
        link?.let { if (it.alive) return it; it.close() }
        link = null
        if (lines(prefs.remoteGateways).isEmpty() && !prefs.remoteDht) {
            prefs.linkStatus = "No link: the server has no gateway and the DHT is off (Settings -> Remote access on the server)"
            throw ApiException("can't reach the server: ${prefs.serverUrl} doesn't answer from here and no remote-access gateway is set up")
        }
        val cfg = LinkConfig(prefs.privateKey, prefs.serverPub ?: throw ApiException("not paired"),
            gateways = lines(prefs.remoteGateways), stun = lines(prefs.remoteStun), dht = prefs.remoteDht)
        val l = Link(cfg)
        try {
            l.connect()
            l.serveLocal()
        } catch (e: Exception) {
            l.close()
            prefs.linkStatus = "No link: ${e.message}"
            throw ApiException("can't reach the server: ${prefs.serverUrl} doesn't answer from here and the remote link failed (${e.message})")
        }
        prefs.linkStatus = "Remote link: ${l.pathNote}"
        link = l
        return l
    }

    private fun lines(s: String) = s.split('\n', ',').map { it.trim() }.filter { it.isNotEmpty() }

    private fun reachable(url: String): Boolean = try {
        val u = URI(url)
        val port = if (u.port > 0) u.port else if (u.scheme == "https") 443 else 80
        Socket().use { it.connect(InetSocketAddress(u.host, port), 2500) }
        true
    } catch (e: Exception) { false }

    /** @brief The server's gateway / STUN list, read while it is directly reachable. */
    private fun fetchConfig(prefs: Prefs, url: String) {
        prefs.remoteConfigAt = System.currentTimeMillis()          // once per period, answered or not
        try {
            http.newCall(Request.Builder().url("$url/api/remote_access/pub/client").build()).execute().use { r ->
                if (!r.isSuccessful) return
                val j = JSONObject(r.body?.string() ?: return)
                val gw = j.optJSONArray("gateways"); val st = j.optJSONArray("stun")
                if (gw != null) prefs.remoteGateways = (0 until gw.length()).joinToString("\n") { gw.getString(it) }
                if (st != null && st.length() > 0) prefs.remoteStun = (0 until st.length()).joinToString("\n") { st.getString(it) }
                prefs.remoteDht = j.optBoolean("dht", false)
                prefs.remoteServerOn = j.optBoolean("enabled", false)
                prefs.remoteConfigAt = System.currentTimeMillis()
            }
        } catch (e: Exception) { }
    }
}