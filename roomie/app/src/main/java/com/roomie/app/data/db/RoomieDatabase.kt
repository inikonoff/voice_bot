package com.roomie.app.data.db

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase

/**
 * Schema is intentionally minimal for the MVP (trash + favorites only). Duplicate-detection
 * hashes and streak/stats tables are backlog items (see project TZ section 6) — add them as new
 * entities with a Room migration when that work starts; nothing here needs to change to allow it.
 */
@Database(
    entities = [TrashEntry::class, FavoriteEntity::class],
    version = 1,
    exportSchema = false,
)
abstract class RoomieDatabase : RoomDatabase() {

    abstract fun trashDao(): TrashDao
    abstract fun favoriteDao(): FavoriteDao

    companion object {
        @Volatile
        private var instance: RoomieDatabase? = null

        fun getInstance(context: Context): RoomieDatabase =
            instance ?: synchronized(this) {
                instance ?: Room.databaseBuilder(
                    context.applicationContext,
                    RoomieDatabase::class.java,
                    "roomie.db",
                ).build().also { instance = it }
            }
    }
}
