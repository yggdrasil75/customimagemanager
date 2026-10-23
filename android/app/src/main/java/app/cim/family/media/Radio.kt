package app.cim.family.media

import android.content.Context
import app.cim.family.ApiV2
import app.cim.family.Prefs
import app.cim.family.Song
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import org.json.JSONObject

/**
 * @brief The server's music radio (modules/music: radio/next, radio/played,
 * radio/status). A round is an ordered list of songs; the service appends the
 * next round when fewer than 3 tracks are left and reports each track that was
 * really listened to. Calls here block: run them off the main thread.
 */
object Radio {
    data class State(val loading: Boolean = false, val error: String = "", val round: Int = 0,
                     val tracks: Int = 0, val hours: Double = 0.0)

    private val st = MutableStateFlow(State())
    val state: StateFlow<State> get() = st

    /** @brief POST music/radio/next -> the next round's songs (throws the server's error). */
    fun nextRound(ctx: Context): List<Song> {
        st.value = st.value.copy(loading = true, error = "")
        try {
            val j = ApiV2(Prefs.get(ctx)).postJson("music/radio/next", JSONObject())
            val songs = Song.list(j)
            val info = j.optJSONObject("info") ?: JSONObject()
            st.value = State(false, if (songs.isEmpty()) "The radio returned no tracks." else "",
                info.optInt("round", st.value.round + 1), info.optInt("tracks", songs.size), info.optDouble("hours", 0.0))
            return songs
        } catch (e: Exception) {
            st.value = st.value.copy(loading = false, error = e.message ?: e.toString())
            throw e
        }
    }

    /** @brief GET music/radio/status -> (round, songs of the current round) to resume. */
    fun status(ctx: Context): Pair<Int, List<Song>> {
        val j = ApiV2(Prefs.get(ctx)).getJson("music/radio/status")
        return j.optInt("round", 0) to Song.list(j)
    }

    /** @brief POST music/radio/played: this track counts as played (radio spacing). */
    fun played(ctx: Context, rel: String) {
        ApiV2(Prefs.get(ctx)).postJson("music/radio/played", JSONObject().put("rel_path", rel))
    }

    fun setError(msg: String) { st.value = st.value.copy(loading = false, error = msg) }
    fun clearError() { st.value = st.value.copy(error = "") }
}
