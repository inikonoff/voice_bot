package com.roomie.app.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewmodel.CreationExtras
import com.roomie.app.AppContainer
import com.roomie.app.ui.screens.folders.FolderListViewModel
import com.roomie.app.ui.screens.settings.SettingsViewModel
import com.roomie.app.ui.screens.swipe.SwipeSessionViewModel

/** Manual DI: builds each screen's ViewModel from the [AppContainer] the Application owns. */
class ViewModelFactory(private val container: AppContainer) : ViewModelProvider.Factory {

    @Suppress("UNCHECKED_CAST")
    override fun <T : ViewModel> create(modelClass: Class<T>, extras: CreationExtras): T = when (modelClass) {
        FolderListViewModel::class.java ->
            FolderListViewModel(container.mediaRepository) as T

        SwipeSessionViewModel::class.java ->
            SwipeSessionViewModel(
                mediaRepository = container.mediaRepository,
                trashRepository = container.trashRepository,
                settingsRepository = container.settingsRepository,
                monetizationGateway = container.monetizationGateway,
            ) as T

        SettingsViewModel::class.java ->
            SettingsViewModel(container.settingsRepository) as T

        else -> throw IllegalArgumentException("Unknown ViewModel class: $modelClass")
    }
}
