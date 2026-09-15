package com.roomie.app.ui.screens.swipe

import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.VectorConverter
import androidx.compose.animation.core.spring
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Favorite
import androidx.compose.material.icons.filled.Replay
import androidx.compose.material3.Badge
import androidx.compose.material3.BadgedBox
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
import androidx.compose.ui.unit.Dp
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
    onOpenTrashPreview: () -> Unit,
) {
    val uiState by viewModel.uiState.collectAsState()
    val pendingTrash by viewModel.pendingTrash.collectAsState()

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
                actions = {
                    if (pendingTrash.isNotEmpty()) {
                        IconButton(onClick = onOpenTrashPreview) {
                            BadgedBox(badge = { Badge { Text(pendingTrash.size.toString()) } }) {
                                Icon(Icons.Filled.Delete, contentDescription = "Review trash")
                            }
                        }
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

/** The card that just left the stack, still flying off-screen on its own timeline so the newly
 *  promoted top card underneath is interactive immediately instead of waiting for this to finish. */
private data class ExitingCardState(
    val group: MediaGroup,
    val isFavorited: Boolean,
    val startOffset: Offset,
    val direction: SwipeDirection,
)

/** Fits a card of [ratio] (width/height) inside a [maxWidth] x [maxHeight] box, like
 *  [androidx.compose.ui.layout.ContentScale.Fit] but sizing the composable itself rather than
 *  its content — so differently-oriented photos each get their own natural size on screen. */
private fun fitSize(ratio: Float, maxWidth: Dp, maxHeight: Dp): Pair<Dp, Dp> {
    val containerRatio = maxWidth / maxHeight
    return if (containerRatio > ratio) (maxHeight * ratio) to maxHeight else maxWidth to (maxWidth / ratio)
}

@Composable
private fun CardStack(
    stack: List<MediaGroup>,
    favoritedKeys: Set<String>,
    onSwiped: (SwipeDirection) -> Unit,
    onDoubleTap: (MediaGroup) -> Unit,
) {
    var exiting by remember { mutableStateOf<ExitingCardState?>(null) }

    val top = stack.getOrNull(0)
    val behind = stack.getOrNull(1)

    BoxWithConstraints(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        if (behind != null) {
            val (w, h) = fitSize(behind.cover.aspectRatio, maxWidth, maxHeight)
            SwipeCard(
                group = behind,
                isFavorited = behind.cover.stableId in favoritedKeys,
                modifier = Modifier
                    .size(w, h)
                    .graphicsLayer { scaleX = 0.94f; scaleY = 0.94f; alpha = 0.6f },
            )
        }

        if (top != null) {
            val (w, h) = fitSize(top.cover.aspectRatio, maxWidth, maxHeight)
            DraggableCard(
                group = top,
                isFavorited = top.cover.stableId in favoritedKeys,
                cardWidth = w,
                cardHeight = h,
                onSwiped = { direction, releaseOffset ->
                    exiting = ExitingCardState(
                        group = top,
                        isFavorited = top.cover.stableId in favoritedKeys,
                        startOffset = releaseOffset,
                        direction = direction,
                    )
                    onSwiped(direction)
                },
                onDoubleTap = { onDoubleTap(top) },
            )
        }

        exiting?.let { ex ->
            val (w, h) = fitSize(ex.group.cover.aspectRatio, maxWidth, maxHeight)
            ExitingCard(
                state = ex,
                cardWidth = w,
                cardHeight = h,
                onFinished = { exiting = null },
            )
        }
    }
}

private val SWIPE_SPRING = spring<Offset>(
    dampingRatio = Spring.DampingRatioLowBouncy,
    stiffness = Spring.StiffnessLow,
)

@Composable
private fun ExitingCard(
    state: ExitingCardState,
    cardWidth: Dp,
    cardHeight: Dp,
    onFinished: () -> Unit,
) {
    val offset = remember(state) { Animatable(state.startOffset, Offset.VectorConverter) }
    val thresholdPx = with(LocalDensity.current) { SWIPE_THRESHOLD_DP.dp.toPx() }

    LaunchedEffect(state) {
        val flingX = if (state.direction == SwipeDirection.RIGHT) 1600f else -1600f
        offset.animateTo(Offset(flingX, state.startOffset.y), SWIPE_SPRING)
        onFinished()
    }

    SwipeCard(
        group = state.group,
        isFavorited = state.isFavorited,
        modifier = Modifier
            .size(cardWidth, cardHeight)
            .graphicsLayer {
                translationX = offset.value.x
                translationY = offset.value.y * 0.2f
                rotationZ = (offset.value.x / thresholdPx) * 12f
            },
    )
}

@Composable
private fun DraggableCard(
    group: MediaGroup,
    isFavorited: Boolean,
    cardWidth: Dp,
    cardHeight: Dp,
    onSwiped: (SwipeDirection, Offset) -> Unit,
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
            .size(cardWidth, cardHeight)
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
                            // Hand off to the caller immediately — advancing to the next card
                            // doesn't wait on this card's own fly-out animation, which continues
                            // independently as an overlay (see ExitingCard).
                            val direction = if (current.x > 0) SwipeDirection.RIGHT else SwipeDirection.LEFT
                            onSwiped(direction, current)
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
