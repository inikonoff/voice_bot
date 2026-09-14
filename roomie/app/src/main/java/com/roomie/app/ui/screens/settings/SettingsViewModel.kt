package com.roomie.app.ui.screens.settings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.roomie.app.data.media.SortOrder
import com.roomie.app.data.settings.RoomieSettings
import com.roomie.app.data.settings.SettingsRepository
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

class SettingsViewModel(private val settingsRepository: SettingsRepository) : ViewModel() {

    val settings: StateFlow<RoomieSettings> = settingsRepository.settings.stateIn(
        scope = viewModelScope,
        started = SharingStarted.WhileSubscribed(5_000),
        initialValue = RoomieSettings(),
    )

    fun setSortOrder(order: SortOrder) {
        viewModelScope.launch { settingsRepository.setSortOrder(order) }
    }

    fun setTrashRetentionDays(days: Int) {
        viewModelScope.launch { settingsRepository.setTrashRetentionDays(days) }
    }

    fun setAutoDeleteEmptyFolders(enabled: Boolean) {
        viewModelScope.launch { settingsRepository.setAutoDeleteEmptyFolders(enabled) }
    }

    fun setMonetizationEnabled(enabled: Boolean) {
        viewModelScope.launch { settingsRepository.setMonetizationEnabled(enabled) }
    }
}
