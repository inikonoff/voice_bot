package com.roomie.app.ui.screens.swipe

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Favorite
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.unit.dp
import coil3.compose.AsyncImage
import com.roomie.app.data.media.MediaGroup
import com.roomie.app.ui.theme.CardShape
import java.util.concurrent.TimeUnit

@Composable
fun SwipeCard(
    group: MediaGroup,
    isFavorited: Boolean,
    modifier: Modifier = Modifier,
) {
    Box(
        modifier = modifier
            .clip(CardShape)
            .background(MaterialTheme.colorScheme.surface, CardShape),
    ) {
        AsyncImage(
            model = group.cover.uri,
            contentDescription = group.cover.displayName,
            contentScale = ContentScale.Crop,
            modifier = Modifier.fillMaxSize(),
        )

        if (group.isBurst) {
            CardBadge(text = "1/${group.items.size}", modifier = Modifier.align(Alignment.TopEnd))
        }

        if (group.cover.isVideo) {
            Box(
                modifier = Modifier
                    .align(Alignment.Center)
                    .size(56.dp)
                    .clip(CircleShape)
                    .background(Color.Black.copy(alpha = 0.45f)),
                contentAlignment = Alignment.Center,
            ) {
                Icon(Icons.Filled.PlayArrow, contentDescription = "Video", tint = Color.White)
            }
            CardBadge(
                text = formatDuration(group.cover.durationMillis),
                modifier = Modifier.align(Alignment.BottomStart),
            )
        }

        if (isFavorited) {
            Icon(
                Icons.Filled.Favorite,
                contentDescription = "Favorited",
                tint = MaterialTheme.colorScheme.primary,
                modifier = Modifier
                    .align(Alignment.TopStart)
                    .padding(12.dp),
            )
        }
    }
}

@Composable
private fun BoxScope.CardBadge(text: String, modifier: Modifier = Modifier) {
    Box(
        modifier = modifier
            .padding(12.dp)
            .clip(RoundedCornerShape(12.dp))
            .background(Color.Black.copy(alpha = 0.55f))
            .padding(horizontal = 8.dp, vertical = 4.dp),
    ) {
        Text(text, color = Color.White, style = MaterialTheme.typography.labelMedium)
    }
}

private fun formatDuration(durationMillis: Long): String {
    val totalSeconds = TimeUnit.MILLISECONDS.toSeconds(durationMillis)
    val minutes = totalSeconds / 60
    val seconds = totalSeconds % 60
    return "%d:%02d".format(minutes, seconds)
}
