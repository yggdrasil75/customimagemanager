package app.cim.family

import android.content.Context
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import org.json.JSONObject

/**
 * @brief The server's capabilities: in memory, persisted in Prefs (so the tabs and
 * the chunk size are known offline), refreshed from inbound/caps on app start.
 */
object CapsStore {
    private val state = MutableStateFlow<Caps?>(null)
    val flow: StateFlow<Caps?> get() = state
    private val err = MutableStateFlow("")
    /** Why the last refresh failed ("" after a success): the screens show it, since the
     *  photos-only fallback looks just like an old server otherwise. */
    val error: StateFlow<String> get() = err

    /** @brief True once the server has answered inbound/caps (an old server's 404 counts). */
    fun known(ctx: Context): Boolean = Prefs.get(ctx).capsJson.isNotEmpty()

    /** @brief The last known caps (Prefs when not loaded yet; v1 when never fetched). */
    fun current(ctx: Context): Caps {
        state.value?.let { return it }
        val raw = Prefs.get(ctx).capsJson
        val c = if (raw.isEmpty()) Caps.V1 else try { Caps.parse(JSONObject(raw)) } catch (e: Exception) { Caps.V1 }
        state.value = c
        return c
    }

    /** @brief Blocking: ask the server. Returns the new caps, or throws (the old ones stay). */
    fun refresh(ctx: Context): Caps {
        val prefs = Prefs.get(ctx)
        val c = try { ApiV2(prefs).fetchCaps() } catch (e: Exception) {
            err.value = e.message ?: e.javaClass.simpleName
            throw e
        }
        prefs.capsJson = c.toJson().toString()
        err.value = ""
        state.value = c
        return c
    }

    fun clear(ctx: Context) {
        Prefs.get(ctx).capsJson = ""
        state.value = null
    }

    fun chunk(ctx: Context): Int = current(ctx).chunk
}