package app.cim.family.ui

import app.cim.family.Album
import app.cim.family.Api
import app.cim.family.Book
import app.cim.family.Movie
import app.cim.family.Season
import app.cim.family.Show
import app.cim.family.SubtitleRef

/** @brief What the in-app video player plays: a library file, its subtitles and where to fetch them. */
data class VideoSpec(
    val rel: String,
    val title: String,
    val subtitle: String,
    val artKey: String?,
    val subs: List<SubtitleRef>,
    /** Proxied subtitle route prefix ("tv/subtitle", "movies/subtitle"); "" = none. */
    val subRoute: String,
)

/** @brief Screens pushed over the tabs (a simple back stack; system back pops). */
sealed class Route {
    /** Full-screen routes hide the bottom bar and the mini player. */
    open val fullScreen: Boolean get() = false

    data class Viewer(val item: Api.Item) : Route() { override val fullScreen: Boolean get() = true }
    data class Video(val spec: VideoSpec) : Route() { override val fullScreen: Boolean get() = true }
    object NowPlaying : Route() { override val fullScreen: Boolean get() = true }

    data class AlbumR(val album: Album) : Route()
    data class ArtistR(val name: String) : Route()
    data class GenreR(val name: String) : Route()
    data class PlaylistR(val id: Long, val name: String) : Route()
    data class OfflineR(val id: String, val name: String) : Route()

    data class ShowR(val show: Show) : Route()
    data class SeasonR(val show: Show, val season: Season) : Route()
    data class MovieR(val movie: Movie) : Route()

    data class BookR(val book: Book) : Route()
}
