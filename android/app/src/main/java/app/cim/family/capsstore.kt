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
        val c = ApiV2(prefs).fetchCaps()
        prefs.capsJson = c.toJson().toString()
        state.value = c
        return c
    }

    fun clear(ctx: Context) {
        Prefs.get(ctx).capsJson = ""
        state.value = null
    }

    fun chunk(ctx: Context): Int = current(ctx).chunk
}
