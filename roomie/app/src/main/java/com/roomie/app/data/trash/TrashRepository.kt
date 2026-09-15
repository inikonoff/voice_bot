package com.roomie.app.data.trash

import android.app.RecoverableSecurityException
import android.content.ContentValues
import android.content.Context
import android.content.IntentSender
import android.net.Uri
import android.os.Build
import android.provider.MediaStore
import java.io.File
import com.roomie.app.data.db.FavoriteDao
import com.roomie.app.data.db.FavoriteEntity
import com.roomie.app.data.db.TrashDao
import com.roomie.app.data.db.TrashEntry
import com.roomie.app.data.media.MediaGroup
import com.roomie.app.data.media.MediaItem
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.concurrent.TimeUnit

/**
 * Owns the "one system dialog per session" flow: batches every card the user swiped left into a
 * single [MediaStore.createTrashRequest] (API 30+) and records our own retention countdown in
 * Room so [com.roomie.app.work.TrashCleanupWorker] can permanently delete on the user's configured
 * schedule (1/3/7/30 days) instead of whatever the OS would otherwise pick.
 *
 * On API 26-29, `createTrashRequest` does not exist. There the file simply stays put and visible
 * until the retention window elapses, at which point the worker deletes it directly — an accepted
 * MVP simplification for pre-scoped-storage devices (see TZ section 8.2, "Legacy Android").
 */
class TrashRepository(
    private val context: Context,
    private val trashDao: TrashDao,
    private val favoriteDao: FavoriteDao,
) {
    private val resolver get() = context.contentResolver

    /**
     * Returns the [IntentSender] for the single system confirmation dialog on API 30+, or null on
     * older versions (nothing to confirm) or when [groups] is empty.
     */
    fun buildSystemTrashRequest(groups: List<MediaGroup>): IntentSender? {
        if (groups.isEmpty() || Build.VERSION.SDK_INT < Build.VERSION_CODES.R) return null
        val uris = groups.flatMap { it.allUris }
        val pendingIntent = MediaStore.createTrashRequest(resolver, uris, /* trashed = */ true)
        return pendingIntent.intentSender
    }

    /** Call once the system dialog (if any) has been confirmed, to start our retention countdown. */
    suspend fun recordTrashed(groups: List<MediaGroup>, retentionDays: Int) = withContext(Dispatchers.IO) {
        val now = System.currentTimeMillis()
        val deleteAt = now + TimeUnit.DAYS.toMillis(retentionDays.toLong())
        val entries = groups.flatMap { group ->
            group.items.map { item ->
                TrashEntry(
                    stableId = item.stableId,
                    uri = item.uri.toString(),
                    displayName = item.displayName,
                    bucketId = item.bucketId,
                    sizeBytes = item.sizeBytes,
                    trashedAtMillis = now,
                    permanentDeleteAtMillis = deleteAt,
                    filePath = item.filePath,
                )
            }
        }
        trashDao.insertAll(entries)
    }

    /** Runs on [com.roomie.app.work.TrashCleanupWorker]'s schedule. */
    suspend fun permanentlyDeleteExpired(): CleanupResult = withContext(Dispatchers.IO) {
        val now = System.currentTimeMillis()
        val expired = trashDao.getExpired(now)
        var freedBytes = 0L
        val deletedIds = mutableListOf<String>()
        val affectedDirs = mutableSetOf<File>()

        for (entry in expired) {
            val deleted = try {
                resolver.delete(Uri.parse(entry.uri), null, null) > 0
            } catch (_: RecoverableSecurityException) {
                // Needs a fresh user consent we can't show from a background worker; retry next run.
                false
            } catch (_: SecurityException) {
                false
            }
            if (deleted) {
                freedBytes += entry.sizeBytes
                deletedIds += entry.stableId
                entry.filePath?.let { path -> File(path).parentFile?.let { affectedDirs += it } }
            }
        }

        if (deletedIds.isNotEmpty()) {
            trashDao.deleteByIds(deletedIds)
        }
        CleanupResult(freedBytes, affectedDirs)
    }

    // --- Favorites: same "batch, then one consent" principle as trashing (see FavoriteEntity). ---

    suspend fun setFavoriteLocally(item: MediaItem, isFavorite: Boolean) =
        withContext(Dispatchers.IO) {
            favoriteDao.upsert(
                FavoriteEntity(
                    stableId = item.stableId,
                    uri = item.uri.toString(),
                    isFavorite = isFavorite,
                    syncedToMediaStore = Build.VERSION.SDK_INT < Build.VERSION_CODES.Q,
                    updatedAtMillis = System.currentTimeMillis(),
                ),
            )
        }

    /** Pushes any pending favorite/unfavorite state to real MediaStore rows in one batch, Q+ only. */
    suspend fun syncFavoritesToMediaStore() = withContext(Dispatchers.IO) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return@withContext
        val unsynced = favoriteDao.getUnsynced()
        if (unsynced.isEmpty()) return@withContext

        val syncedIds = mutableListOf<String>()
        for (fav in unsynced) {
            val uri = Uri.parse(fav.uri)
            val values = ContentValues().apply {
                put(MediaStore.MediaColumns.IS_FAVORITE, if (fav.isFavorite) 1 else 0)
            }
            val ok = try {
                resolver.update(uri, values, null, null) > 0
            } catch (_: RecoverableSecurityException) {
                false
            } catch (_: SecurityException) {
                false
            }
            if (ok) syncedIds += fav.stableId
        }
        if (syncedIds.isNotEmpty()) favoriteDao.markSynced(syncedIds)
    }
}

data class CleanupResult(val freedBytes: Long, val affectedDirs: Set<File>)
