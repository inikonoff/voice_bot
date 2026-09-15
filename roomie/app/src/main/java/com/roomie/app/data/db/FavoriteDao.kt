package com.roomie.app.data.db

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import kotlinx.coroutines.flow.Flow

@Dao
interface FavoriteDao {

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsert(entity: FavoriteEntity)

    @Query("SELECT * FROM favorites")
    fun observeAll(): Flow<List<FavoriteEntity>>

    @Query("SELECT * FROM favorites WHERE syncedToMediaStore = 0")
    suspend fun getUnsynced(): List<FavoriteEntity>

    @Query("UPDATE favorites SET syncedToMediaStore = 1 WHERE stableId IN (:stableIds)")
    suspend fun markSynced(stableIds: List<String>)
}
