package com.roomie.app.data.media

import android.content.ContentUris
import android.content.Context
import android.database.Cursor
import android.net.Uri
import android.os.Build
import android.provider.MediaStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.Calendar

/**
 * Read-only access to the device gallery via [MediaStore]. Combines the images and video
 * collections into one chronological, groupable stream; never touches the network.
 */
class MediaRepository(private val context: Context) {

    private val resolver get() = context.contentResolver

    suspend fun getFolders(): List<GalleryFolder> = withContext(Dispatchers.IO) {
        val counts = LinkedHashMap<Long, MutableList<MediaItem>>()
        queryImages(bucketId = null, period = PeriodFilter.ALL).forEach {
            counts.getOrPut(it.bucketId) { mutableListOf() }.add(it)
        }
        queryVideos(bucketId = null, period = PeriodFilter.ALL).forEach {
            counts.getOrPut(it.bucketId) { mutableListOf() }.add(it)
        }
        counts.map { (bucketId, items) ->
            val newestFirst = items.maxByOrNull { it.dateTakenMillis }
            GalleryFolder(
                bucketId = bucketId,
                displayName = items.first().bucketName,
                itemCount = items.size,
                coverUri = newestFirst?.uri,
            )
        }.sortedByDescending { it.itemCount }
    }

    /**
     * Returns swipeable units for [bucketId] (null = "All photos"), newest-or-oldest first per
     * [sortOrder], with burst photo sequences collapsed via [groupIntoUnits].
     */
    suspend fun getMediaGroups(
        bucketId: Long?,
        period: PeriodFilter,
        sortOrder: SortOrder,
    ): List<MediaGroup> = withContext(Dispatchers.IO) {
        val items = (queryImages(bucketId, period) + queryVideos(bucketId, period))
            .sortedBy { it.dateTakenMillis } // ascending: required by groupIntoUnits
        val groups = items.groupIntoUnits()
        if (sortOrder == SortOrder.NEWEST_FIRST) groups.asReversed() else groups
    }

    private fun queryImages(bucketId: Long?, period: PeriodFilter): List<MediaItem> {
        val projection = buildList {
            add(MediaStore.Images.Media._ID)
            add(MediaStore.Images.Media.DISPLAY_NAME)
            add(MediaStore.Images.Media.BUCKET_ID)
            add(MediaStore.Images.Media.BUCKET_DISPLAY_NAME)
            add(MediaStore.Images.Media.DATE_TAKEN)
            add(MediaStore.Images.Media.DATE_ADDED)
            add(MediaStore.Images.Media.SIZE)
            add(MediaStore.Images.Media.WIDTH)
            add(MediaStore.Images.Media.HEIGHT)
            @Suppress("DEPRECATION")
            add(MediaStore.Images.Media.DATA)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                add(MediaStore.Images.Media.IS_FAVORITE)
            }
        }.toTypedArray()

        val (selection, args) = buildSelection(bucketId, period, MediaStore.Images.Media.BUCKET_ID)

        return query(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, projection, selection, args) { cursor ->
            val id = cursor.getLong(MediaStore.Images.Media._ID)
            MediaItem(
                id = id,
                uri = ContentUris.withAppendedId(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, id),
                displayName = cursor.getStringOrEmpty(MediaStore.Images.Media.DISPLAY_NAME),
                bucketId = cursor.getLong(MediaStore.Images.Media.BUCKET_ID),
                bucketName = cursor.getStringOrEmpty(MediaStore.Images.Media.BUCKET_DISPLAY_NAME),
                dateTakenMillis = cursor.dateTakenOrAdded(
                    MediaStore.Images.Media.DATE_TAKEN,
                    MediaStore.Images.Media.DATE_ADDED,
                ),
                sizeBytes = cursor.getLong(MediaStore.Images.Media.SIZE),
                isVideo = false,
                isFavorite = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    cursor.getInt(MediaStore.Images.Media.IS_FAVORITE) == 1
                } else {
                    false
                },
                filePath = cursor.getStringOrEmpty(MediaStore.Images.Media.DATA).ifBlank { null },
                width = cursor.getInt(MediaStore.Images.Media.WIDTH),
                height = cursor.getInt(MediaStore.Images.Media.HEIGHT),
            )
        }
    }

    private fun queryVideos(bucketId: Long?, period: PeriodFilter): List<MediaItem> {
        val projection = buildList {
            add(MediaStore.Video.Media._ID)
            add(MediaStore.Video.Media.DISPLAY_NAME)
            add(MediaStore.Video.Media.BUCKET_ID)
            add(MediaStore.Video.Media.BUCKET_DISPLAY_NAME)
            add(MediaStore.Video.Media.DATE_TAKEN)
            add(MediaStore.Video.Media.DATE_ADDED)
            add(MediaStore.Video.Media.SIZE)
            add(MediaStore.Video.Media.DURATION)
            add(MediaStore.Video.Media.WIDTH)
            add(MediaStore.Video.Media.HEIGHT)
            @Suppress("DEPRECATION")
            add(MediaStore.Video.Media.DATA)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                add(MediaStore.Video.Media.IS_FAVORITE)
            }
        }.toTypedArray()

        val (selection, args) = buildSelection(bucketId, period, MediaStore.Video.Media.BUCKET_ID)

        return query(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, projection, selection, args) { cursor ->
            val id = cursor.getLong(MediaStore.Video.Media._ID)
            MediaItem(
                id = id,
                uri = ContentUris.withAppendedId(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, id),
                displayName = cursor.getStringOrEmpty(MediaStore.Video.Media.DISPLAY_NAME),
                bucketId = cursor.getLong(MediaStore.Video.Media.BUCKET_ID),
                bucketName = cursor.getStringOrEmpty(MediaStore.Video.Media.BUCKET_DISPLAY_NAME),
                dateTakenMillis = cursor.dateTakenOrAdded(
                    MediaStore.Video.Media.DATE_TAKEN,
                    MediaStore.Video.Media.DATE_ADDED,
                ),
                sizeBytes = cursor.getLong(MediaStore.Video.Media.SIZE),
                isVideo = true,
                durationMillis = cursor.getLong(MediaStore.Video.Media.DURATION),
                isFavorite = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    cursor.getInt(MediaStore.Video.Media.IS_FAVORITE) == 1
                } else {
                    false
                },
                filePath = cursor.getStringOrEmpty(MediaStore.Video.Media.DATA).ifBlank { null },
                width = cursor.getInt(MediaStore.Video.Media.WIDTH),
                height = cursor.getInt(MediaStore.Video.Media.HEIGHT),
            )
        }
    }

    private fun buildSelection(
        bucketId: Long?,
        period: PeriodFilter,
        bucketColumn: String,
    ): Pair<String?, Array<String>?> {
        val clauses = mutableListOf<String>()
        val args = mutableListOf<String>()

        if (bucketId != null) {
            clauses += "$bucketColumn = ?"
            args += bucketId.toString()
        }

        periodStartMillis(period)?.let { startMillis ->
            clauses += "${MediaStore.MediaColumns.DATE_ADDED} >= ?"
            args += (startMillis / 1000).toString()
        }

        if (clauses.isEmpty()) return null to null
        return clauses.joinToString(" AND ") to args.toTypedArray()
    }

    private fun periodStartMillis(period: PeriodFilter): Long? {
        if (period == PeriodFilter.ALL) return null
        val calendar = Calendar.getInstance()
        when (period) {
            PeriodFilter.LAST_DAY -> calendar.add(Calendar.DAY_OF_YEAR, -1)
            PeriodFilter.LAST_MONTH -> calendar.add(Calendar.MONTH, -1)
            PeriodFilter.LAST_YEAR -> calendar.add(Calendar.YEAR, -1)
            PeriodFilter.ALL -> Unit
        }
        return calendar.timeInMillis
    }

    private inline fun query(
        collection: Uri,
        projection: Array<String>,
        selection: String?,
        selectionArgs: Array<String>?,
        crossinline mapRow: (Cursor) -> MediaItem,
    ): List<MediaItem> {
        val results = mutableListOf<MediaItem>()
        resolver.query(collection, projection, selection, selectionArgs, null)?.use { cursor ->
            while (cursor.moveToNext()) {
                results += mapRow(cursor)
            }
        }
        return results
    }

    private fun Cursor.getStringOrEmpty(column: String): String =
        getColumnIndex(column).takeIf { it >= 0 }?.let { getString(it) } ?: ""

    private fun Cursor.getLong(column: String): Long =
        getColumnIndex(column).takeIf { it >= 0 }?.let { getLong(it) } ?: 0L

    private fun Cursor.getInt(column: String): Int =
        getColumnIndex(column).takeIf { it >= 0 }?.let { getInt(it) } ?: 0

    /** [dateTakenColumn] is only populated by camera apps; fall back to DATE_ADDED (seconds) otherwise. */
    private fun Cursor.dateTakenOrAdded(dateTakenColumn: String, dateAddedColumn: String): Long {
        val taken = getLong(dateTakenColumn)
        if (taken > 0L) return taken
        return getLong(dateAddedColumn) * 1000
    }
}
