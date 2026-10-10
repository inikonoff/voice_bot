package com.roomie.app.data.settings

import com.roomie.app.data.media.SortOrder

data class RoomieSettings(
    val sortOrder: SortOrder = SortOrder.NEWEST_FIRST,
    val trashRetentionDays: Int = 3,
    val autoDeleteEmptyFolders: Boolean = true,
    val sessionSwipeCount: Int = 0,
    val monetizationEnabled: Boolean = false,
    val freeSwipeLimit: Int = 100,
    val isPremiumUnlocked: Boolean = false,
) {
    val hasReachedSwipeLimit: Boolean
        get() = monetizationEnabled && !isPremiumUnlocked && sessionSwipeCount >= freeSwipeLimit

    companion object {
        val ALLOWED_RETENTION_DAYS = listOf(1, 3, 7, 30)
    }
}
