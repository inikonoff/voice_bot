package com.roomie.app.ui.screens.swipe

import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.VectorConverter
import androidx.compose.animation.core.spring
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.Favorite
import androidx.compose.material.icons.filled.Replay
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.unit.dp
import com.roomie.app.data.media.MediaGroup
import kotlinx.coroutines.launch
import kotlin.math.abs

private const val SWIPE_THRESHOLD_DP = 120f

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SwipeScreen(
    viewModel: SwipeSessionViewModel,
    onBack: () -> Unit,
    onStackExhausted: () -> Unit,
    onLimitReached: () -> Unit,
) {
    val uiState by viewModel.uiState.collectAsState()

    LaunchedEffect(uiState.hasReachedLimit) {
        if (uiState.hasReachedLimit) onLimitReached()
    }

    LaunchedEffect(uiState.isStackExhausted, uiState.isLoading) {
        if (!uiState.isLoading && uiState.isStackExhausted) onStackExhausted()
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(uiState.folderName) },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.Filled.ArrowBack, contentDescription = "Back")
                    }
                },
            )
        },
    ) { padding ->
        Column(modifier = Modifier.fillMaxSize().padding(padding)) {
            SwipeProgressBar(
                current = uiState.sessionSwipeCount,
                limit = uiState.freeSwipeLimit,
            )

            Box(modifier = Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.Center) {
                when {
                    uiState.isLoading -> CircularProgressIndicator()
                    uiState.stack.isEmpty() -> Text(
                        "Nothing left here — this folder is clean.",
                        style = MaterialTheme.typography.bodyLarge,
                    )
                    else -> CardStack(
                        stack = uiState.stack,
                        favoritedKeys = uiState.favoritedKeys,
                        onSwiped = viewModel::swipe,
                        onDoubleTap = { viewModel.toggleFavorite(it.cover) },
                    )
                }
            }

            BottomActionBar(canUndo = uiState.canUndo, onUndo = viewModel::undo)
        }
    }
}

@Composable
private fun SwipeProgressBar(current: Int, limit: Int) {
    if (limit <= 0) return
    LinearProgressIndicator(
        progress = { (current.toFloat() / limit).coerceIn(0f, 1f) },
        modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp),
    )
}

@Composable
private fun BottomActionBar(canUndo: Boolean, onUndo: () -> Unit) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(16.dp),
        horizontalArrangement = Arrangement.Center,
    ) {
        FloatingActionButton(onClick = { if (canUndo) onUndo() }) {
            Icon(
                Icons.Filled.Replay,
                contentDescription = "Undo",
                tint = if (canUndo) {
                    MaterialTheme.colorScheme.onPrimaryContainer
                } else {
                    MaterialTheme.colorScheme.onSurface.copy(alpha = 0.3f)
                },
            )
        }
    }
}

@Composable
private fun CardStack(
    stack: List<MediaGroup>,
    favoritedKeys: Set<String>,
    onSwiped: (SwipeDirection) -> Unit,
    onDoubleTap: (MediaGroup) -> Unit,
) {
    val top = stack.getOrNull(0)
    val behind = stack.getOrNull(1)

    Box(modifier = Modifier.fillMaxWidth().aspectRatio(0.72f), contentAlignment = Alignment.Center) {
        if (behind != null) {
            SwipeCard(
                group = behind,
                isFavorited = behind.cover.stableId in favoritedKeys,
                modifier = Modifier
                    .fillMaxSize()
                    .graphicsLayer { scaleX = 0.94f; scaleY = 0.94f; alpha = 0.6f },
            )
        }
        if (top != null) {
            DraggableCard(
                group = top,
                isFavorited = top.cover.stableId in favoritedKeys,
                onSwiped = onSwiped,
                onDoubleTap = { onDoubleTap(top) },
            )
        }
    }
}

private val SWIPE_SPRING = spring<Offset>(
    dampingRatio = Spring.DampingRatioLowBouncy,
    stiffness = Spring.StiffnessLow,
)

@Composable
private fun DraggableCard(
    group: MediaGroup,
    isFavorited: Boolean,
    onSwiped: (SwipeDirection) -> Unit,
    onDoubleTap: () -> Unit,
) {
    val offset = remember(group.key) { Animatable(Offset.Zero, Offset.VectorConverter) }
    val scope = rememberCoroutineScope()
    val haptic = LocalHapticFeedback.current
    var pastThreshold by remember(group.key) { mutableStateOf(false) }
    val thresholdPx = with(LocalDensity.current) { SWIPE_THRESHOLD_DP.dp.toPx() }

    SwipeCard(
        group = group,
        isFavorited = isFavorited,
        modifier = Modifier
            .fillMaxSize()
            .graphicsLayer {
                translationX = offset.value.x
                translationY = offset.value.y * 0.2f
                rotationZ = (offset.value.x / thresholdPx) * 12f
            }
            .pointerInput(group.key) {
                detectDragGestures(
                    onDrag = { change, dragAmount ->
                        change.consume()
                        val newValue = offset.value + dragAmount
                        scope.launch { offset.snapTo(newValue) }
                        val crossed = abs(newValue.x) > thresholdPx
                        if (crossed != pastThreshold) {
                            pastThreshold = crossed
                            if (crossed) haptic.performHapticFeedback(HapticFeedbackType.LongPress)
                        }
                    },
                    onDragEnd = {
                        val current = offset.value
                        if (abs(current.x) > thresholdPx) {
                            val direction = if (current.x > 0) SwipeDirection.RIGHT else SwipeDirection.LEFT
                            val flingX = if (direction == SwipeDirection.RIGHT) 1600f else -1600f
                            scope.launch {
                                offset.animateTo(Offset(flingX, current.y), SWIPE_SPRING)
                                onSwiped(direction)
                            }
                        } else {
                            scope.launch { offset.animateTo(Offset.Zero, SWIPE_SPRING) }
                        }
                    },
                )
            }
            .pointerInput(group.key) {
                detectTapGestures(onDoubleTap = { onDoubleTap() })
            },
    )
}
