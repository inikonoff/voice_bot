package com.roomie.app.data.settings

import android.content.Context
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import com.roomie.app.data.media.SortOrder
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map

private val Context.dataStore by preferencesDataStore(name = "roomie_settings")

/**
 * All user-configurable state and the free-swipe-session counter. Backed by DataStore so it
 * survives process death without the overhead of a Room table for what is just scalar state.
 */
class SettingsRepository(private val context: Context) {

    private object Keys {
        val SORT_ORDER = stringPreferencesKey("sort_order")
        val TRASH_RETENTION_DAYS = intPreferencesKey("trash_retention_days")
        val AUTO_DELETE_EMPTY_FOLDERS = booleanPreferencesKey("auto_delete_empty_folders")
        val SESSION_SWIPE_COUNT = intPreferencesKey("session_swipe_count")
        val MONETIZATION_ENABLED = booleanPreferencesKey("monetization_enabled")
        val FREE_SWIPE_LIMIT = intPreferencesKey("free_swipe_limit")
        val IS_PREMIUM_UNLOCKED = booleanPreferencesKey("is_premium_unlocked")
    }

    val settings: Flow<RoomieSettings> = context.dataStore.data.map { prefs ->
        val defaults = RoomieSettings()
        RoomieSettings(
            sortOrder = prefs[Keys.SORT_ORDER]?.let { SortOrder.valueOf(it) } ?: defaults.sortOrder,
            trashRetentionDays = prefs[Keys.TRASH_RETENTION_DAYS] ?: defaults.trashRetentionDays,
            autoDeleteEmptyFolders = prefs[Keys.AUTO_DELETE_EMPTY_FOLDERS] ?: defaults.autoDeleteEmptyFolders,
            sessionSwipeCount = prefs[Keys.SESSION_SWIPE_COUNT] ?: defaults.sessionSwipeCount,
            monetizationEnabled = prefs[Keys.MONETIZATION_ENABLED] ?: defaults.monetizationEnabled,
            freeSwipeLimit = prefs[Keys.FREE_SWIPE_LIMIT] ?: defaults.freeSwipeLimit,
            isPremiumUnlocked = prefs[Keys.IS_PREMIUM_UNLOCKED] ?: defaults.isPremiumUnlocked,
        )
    }

    suspend fun setSortOrder(sortOrder: SortOrder) {
        context.dataStore.edit { it[Keys.SORT_ORDER] = sortOrder.name }
    }

    suspend fun setTrashRetentionDays(days: Int) {
        require(days in RoomieSettings.ALLOWED_RETENTION_DAYS)
        context.dataStore.edit { it[Keys.TRASH_RETENTION_DAYS] = days }
    }

    suspend fun setAutoDeleteEmptyFolders(enabled: Boolean) {
        context.dataStore.edit { it[Keys.AUTO_DELETE_EMPTY_FOLDERS] = enabled }
    }

    suspend fun setMonetizationEnabled(enabled: Boolean) {
        context.dataStore.edit { it[Keys.MONETIZATION_ENABLED] = enabled }
    }

    suspend fun unlockPremium() {
        context.dataStore.edit { it[Keys.IS_PREMIUM_UNLOCKED] = true }
    }

    /** Rewarded ad: lets the user keep swiping for the remainder of this session only. */
    suspend fun resetSessionSwipeCount() {
        context.dataStore.edit { it[Keys.SESSION_SWIPE_COUNT] = 0 }
    }

    suspend fun incrementSessionSwipeCount() {
        context.dataStore.edit {
            val current = it[Keys.SESSION_SWIPE_COUNT] ?: 0
            it[Keys.SESSION_SWIPE_COUNT] = current + 1
        }
    }
}
