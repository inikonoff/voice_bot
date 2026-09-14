package com.roomie.app.data.db

import androidx.room.Entity
import androidx.room.PrimaryKey

/**
 * App-owned registry of trashed files. `MediaStore.createTrashRequest` marks a file IS_TRASHED
 * but the OS controls its own purge schedule, not us — this table is what lets Roomie enforce the
 * user's chosen retention (1/3/7/30 days) via [com.roomie.app.work.TrashCleanupWorker], and is the
 * only bookkeeping needed on pre-trash (legacy) Android versions.
 */
@Entity(tableName = "trash_entries")
data class TrashEntry(
    @PrimaryKey val stableId: String,
    val uri: String,
    val displayName: String,
    val bucketId: Long,
    val sizeBytes: Long,
    val trashedAtMillis: Long,
    val permanentDeleteAtMillis: Long,
    /** Absolute path when known; used only to check the parent folder for emptiness afterward. */
    val filePath: String?,
)
