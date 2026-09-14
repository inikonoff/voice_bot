package com.roomie.app

import android.content.Context
import com.roomie.app.data.db.RoomieDatabase
import com.roomie.app.data.media.EmptyFolderCleaner
import com.roomie.app.data.media.MediaRepository
import com.roomie.app.data.monetization.MonetizationGateway
import com.roomie.app.data.monetization.NoOpMonetizationGateway
import com.roomie.app.data.settings.SettingsRepository
import com.roomie.app.data.trash.TrashRepository

/**
 * Hand-rolled DI container. The app is small enough (a handful of repositories, no multi-module
 * setup) that a DI framework would add build complexity without buying anything here.
 */
class AppContainer(context: Context) {
    private val appContext = context.applicationContext
    private val database = RoomieDatabase.getInstance(appContext)

    val mediaRepository = MediaRepository(appContext)
    val settingsRepository = SettingsRepository(appContext)
    val trashRepository = TrashRepository(appContext, database.trashDao(), database.favoriteDao())
    val emptyFolderCleaner = EmptyFolderCleaner()
    val monetizationGateway: MonetizationGateway = NoOpMonetizationGateway()
}
