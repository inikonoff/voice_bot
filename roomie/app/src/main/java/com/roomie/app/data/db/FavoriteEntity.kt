package com.roomie.app.data.db

import androidx.room.Entity
import androidx.room.PrimaryKey

/**
 * Favorite state, kept locally so double-tapping during a swipe session never blocks on a system
 * consent dialog. On Android 10 (Q) and above, unsynced rows are pushed to the real
 * `MediaStore.Images.Media.IS_FAVORITE` column in one batched request at session finalization
 * (mirrors the trash flow's "one dialog per session" rule). Below Q there is no such column, so
 * this table is the sole source of truth.
 */
@Entity(tableName = "favorites")
data class FavoriteEntity(
    @PrimaryKey val stableId: String,
    val uri: String,
    val isFavorite: Boolean,
    val syncedToMediaStore: Boolean,
    val updatedAtMillis: Long,
)
