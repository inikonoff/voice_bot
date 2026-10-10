package com.roomie.app.ui.screens.folders

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.roomie.app.data.media.GalleryFolder
import com.roomie.app.data.media.MediaRepository
import com.roomie.app.data.media.PeriodFilter
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

data class FolderListUiState(
    val folders: List<GalleryFolder> = emptyList(),
    val period: PeriodFilter = PeriodFilter.ALL,
    val isLoading: Boolean = true,
    val hasMediaPermission: Boolean = true,
)

class FolderListViewModel(private val mediaRepository: MediaRepository) : ViewModel() {

    private val _uiState = MutableStateFlow(FolderListUiState())
    val uiState: StateFlow<FolderListUiState> = _uiState.asStateFlow()

    fun onPermissionResult(granted: Boolean) {
        _uiState.update { it.copy(hasMediaPermission = granted) }
        if (granted) refresh()
    }

    /** The period filter narrows what's shown once inside a folder's swipe stack; it does not
     *  change the folder list itself (counts always reflect all-time contents). */
    fun onPeriodSelected(period: PeriodFilter) {
        _uiState.update { it.copy(period = period) }
    }

    fun refresh() {
        if (!_uiState.value.hasMediaPermission) return
        viewModelScope.launch {
            _uiState.update { it.copy(isLoading = true) }
            val folders = mediaRepository.getFolders()
            _uiState.update { it.copy(folders = folders, isLoading = false) }
        }
    }
}
