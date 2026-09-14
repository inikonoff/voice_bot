package com.roomie.app.data.media

import android.net.Uri

/**
 * One physical file in MediaStore (a photo or a video).
 */
data class MediaItem(
    val id: Long,
    val uri: Uri,
    val displayName: String,
    val bucketId: Long,
    val bucketName: String,
    val dateTakenMillis: Long,
    val sizeBytes: Long,
    val isVideo: Boolean,
    val durationMillis: Long = 0L,
    val isFavorite: Boolean = false,
    /** Absolute path, when readable (needs All Files Access on API 29+); used only for the
     *  optional empty-folder cleanup, never for reading/writing the file itself. */
    val filePath: String? = null,
) {
    /** Stable identity across image/video tables, since raw `_ID` can collide between them. */
    val stableId: String get() = if (isVideo) "v$id" else "i$id"
}

/**
 * A swipeable unit: either a single photo/video, or a burst of photos collapsed into one card
 * (see [com.roomie.app.data.media.groupIntoUnits]).
 */
data class MediaGroup(
    val key: String,
    val items: List<MediaItem>,
) {
    init {
        require(items.isNotEmpty()) { "MediaGroup must contain at least one item" }
    }

    val cover: MediaItem get() = items.first()
    val isBurst: Boolean get() = items.size > 1
    val totalSizeBytes: Long get() = items.sumOf { it.sizeBytes }
    val allUris: List<Uri> get() = items.map { it.uri }
}

/** A folder ("bucket" in MediaStore terms) discovered on-device. */
data class GalleryFolder(
    val bucketId: Long,
    val displayName: String,
    val itemCount: Int,
    val coverUri: Uri?,
)

enum class PeriodFilter {
    ALL,
    LAST_DAY,
    LAST_MONTH,
    LAST_YEAR,
}

enum class SortOrder {
    NEWEST_FIRST,
    OLDEST_FIRST,
}
