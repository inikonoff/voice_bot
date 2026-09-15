package com.roomie.app.ui.screens.swipe

import android.content.IntentSender
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.roomie.app.data.media.MediaGroup
import com.roomie.app.data.media.MediaItem
import com.roomie.app.data.media.MediaRepository
import com.roomie.app.data.media.PeriodFilter
import com.roomie.app.data.monetization.MonetizationGateway
import com.roomie.app.data.monetization.PurchaseResult
import com.roomie.app.data.monetization.RewardResult
import com.roomie.app.data.settings.SettingsRepository
import com.roomie.app.data.trash.TrashRepository
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

enum class SwipeDirection { LEFT, RIGHT }

private const val MAX_UNDO_HISTORY = 10

private data class SwipeAction(val group: MediaGroup, val direction: SwipeDirection)

data class SwipeUiState(
    val folderName: String = "",
    val stack: List<MediaGroup> = emptyList(),
    val isLoading: Boolean = true,
    val sessionSwipeCount: Int = 0,
    val freeSwipeLimit: Int = 100,
    val hasReachedLimit: Boolean = false,
    val canUndo: Boolean = false,
    val favoritedKeys: Set<String> = emptySet(),
    val isStackExhausted: Boolean = false,
) {
    val currentGroup: MediaGroup? get() = stack.firstOrNull()
}

data class SummaryUiState(val itemCount: Int, val freedBytes: Long)

data class TrashConfirmationRequest(val intentSender: IntentSender, val groups: List<MediaGroup>)

class SwipeSessionViewModel(
    private val mediaRepository: MediaRepository,
    private val trashRepository: TrashRepository,
    private val settingsRepository: SettingsRepository,
    private val monetizationGateway: MonetizationGateway,
) : ViewModel() {

    private val _uiState = MutableStateFlow(SwipeUiState())
    val uiState: StateFlow<SwipeUiState> = _uiState.asStateFlow()

    private val _summaryState = MutableStateFlow<SummaryUiState?>(null)
    val summaryState: StateFlow<SummaryUiState?> = _summaryState.asStateFlow()

    private val _trashConfirmationEvents = MutableSharedFlow<TrashConfirmationRequest>()
    val trashConfirmationEvents: SharedFlow<TrashConfirmationRequest> = _trashConfirmationEvents

    /** Items swiped left this session, awaiting review on the trash-preview screen. */
    private val _pendingTrash = MutableStateFlow<List<MediaGroup>>(emptyList())
    val pendingTrash: StateFlow<List<MediaGroup>> = _pendingTrash.asStateFlow()

    private val undoHistory = ArrayDeque<SwipeAction>(MAX_UNDO_HISTORY)

    init {
        viewModelScope.launch {
            settingsRepository.settings.collectLatest { settings ->
                _uiState.update {
                    it.copy(
                        sessionSwipeCount = settings.sessionSwipeCount,
                        freeSwipeLimit = settings.freeSwipeLimit,
                        hasReachedLimit = settings.hasReachedSwipeLimit,
                    )
                }
            }
        }
    }

    fun loadFolder(bucketId: Long?, displayName: String, period: PeriodFilter) {
        viewModelScope.launch {
            _uiState.update { it.copy(isLoading = true, folderName = displayName, isStackExhausted = false) }
            _pendingTrash.value = emptyList()
            undoHistory.clear()
            val sortOrder = settingsRepository.settings.first().sortOrder
            val groups = mediaRepository.getMediaGroups(bucketId, period, sortOrder)
            _uiState.update {
                it.copy(stack = groups, isLoading = false, canUndo = false, isStackExhausted = groups.isEmpty())
            }
        }
    }

    fun swipe(direction: SwipeDirection) {
        val state = _uiState.value
        val group = state.currentGroup ?: return
        if (state.hasReachedLimit) return

        if (direction == SwipeDirection.LEFT) {
            _pendingTrash.update { it + group }
        }
        pushUndo(SwipeAction(group, direction))

        _uiState.update {
            val newStack = it.stack.drop(1)
            it.copy(stack = newStack, canUndo = undoHistory.isNotEmpty(), isStackExhausted = newStack.isEmpty())
        }

        viewModelScope.launch { settingsRepository.incrementSessionSwipeCount() }
    }

    fun undo() {
        val action = undoHistory.removeLastOrNull() ?: return
        if (action.direction == SwipeDirection.LEFT) {
            _pendingTrash.update { it - action.group }
        }
        _uiState.update {
            it.copy(
                stack = listOf(action.group) + it.stack,
                canUndo = undoHistory.isNotEmpty(),
                isStackExhausted = false,
            )
        }
    }

    fun toggleFavorite(item: MediaItem) {
        val newValue = item.stableId !in _uiState.value.favoritedKeys
        _uiState.update {
            it.copy(
                favoritedKeys = if (newValue) it.favoritedKeys + item.stableId else it.favoritedKeys - item.stableId,
            )
        }
        viewModelScope.launch { trashRepository.setFavoriteLocally(item, newValue) }
    }

    /** Trash-preview screen: exclude an item the user un-checked (it will be kept, not deleted). */
    fun restoreFromPendingTrash(group: MediaGroup) {
        _pendingTrash.update { it - group }
    }

    fun confirmDeleteSelected(selectedGroups: List<MediaGroup>) {
        if (selectedGroups.isEmpty()) return
        viewModelScope.launch {
            val intentSender = trashRepository.buildSystemTrashRequest(selectedGroups)
            if (intentSender != null) {
                _trashConfirmationEvents.emit(TrashConfirmationRequest(intentSender, selectedGroups))
            } else {
                completeTrashing(selectedGroups)
            }
        }
    }

    fun onSystemTrashConfirmed(groups: List<MediaGroup>) {
        viewModelScope.launch { completeTrashing(groups) }
    }

    private suspend fun completeTrashing(groups: List<MediaGroup>) {
        val retentionDays = settingsRepository.settings.first().trashRetentionDays
        trashRepository.recordTrashed(groups, retentionDays)
        trashRepository.syncFavoritesToMediaStore()
        _summaryState.value = SummaryUiState(
            itemCount = groups.sumOf { it.items.size },
            freedBytes = groups.sumOf { it.totalSizeBytes },
        )
        _pendingTrash.update { it - groups.toSet() }
        settingsRepository.resetSessionSwipeCount()
    }

    fun clearSummary() {
        _summaryState.value = null
    }

    suspend fun unlockViaRewardedAd(): Boolean {
        val result = monetizationGateway.showRewardedAd()
        if (result == RewardResult.GRANTED) settingsRepository.resetSessionSwipeCount()
        return result == RewardResult.GRANTED
    }

    suspend fun unlockViaPurchase(): Boolean {
        val result = monetizationGateway.launchOneTimePurchase()
        if (result == PurchaseResult.PURCHASED) settingsRepository.unlockPremium()
        return result == PurchaseResult.PURCHASED
    }

    private fun pushUndo(action: SwipeAction) {
        if (undoHistory.size >= MAX_UNDO_HISTORY) undoHistory.removeFirst()
        undoHistory.addLast(action)
    }
}
