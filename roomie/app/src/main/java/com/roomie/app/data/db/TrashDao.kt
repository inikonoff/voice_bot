package com.roomie.app.data.db

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import kotlinx.coroutines.flow.Flow

@Dao
interface TrashDao {

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertAll(entries: List<TrashEntry>)

    @Query("SELECT * FROM trash_entries ORDER BY trashedAtMillis DESC")
    fun observeAll(): Flow<List<TrashEntry>>

    @Query("SELECT * FROM trash_entries WHERE permanentDeleteAtMillis <= :nowMillis")
    suspend fun getExpired(nowMillis: Long): List<TrashEntry>

    @Query("DELETE FROM trash_entries WHERE stableId IN (:stableIds)")
    suspend fun deleteByIds(stableIds: List<String>)

    @Query("SELECT COUNT(*) FROM trash_entries")
    suspend fun count(): Int
}
