package com.roomie.app.ui.screens.trash

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.unit.dp
import coil3.compose.AsyncImage
import com.roomie.app.data.media.MediaGroup
import com.roomie.app.ui.screens.swipe.SwipeSessionViewModel
import com.roomie.app.ui.theme.ContainerShape

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun TrashPreviewScreen(
    viewModel: SwipeSessionViewModel,
    onBack: () -> Unit,
    onDeleteConfirmed: () -> Unit,
) {
    val pendingTrash by viewModel.pendingTrash.collectAsState()

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Review trash (${pendingTrash.size})") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.Filled.ArrowBack, contentDescription = "Back")
                    }
                },
            )
        },
        bottomBar = {
            Column(modifier = Modifier.padding(16.dp)) {
                Button(
                    onClick = {
                        viewModel.confirmDeleteSelected(pendingTrash)
                        onDeleteConfirmed()
                    },
                    enabled = pendingTrash.isNotEmpty(),
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Text("Delete all (${pendingTrash.size})")
                }
            }
        },
    ) { padding ->
        if (pendingTrash.isEmpty()) {
            Box(
                modifier = Modifier.fillMaxSize().padding(padding),
                contentAlignment = Alignment.Center,
            ) {
                Text("Nothing marked for deletion.", style = MaterialTheme.typography.bodyLarge)
            }
        } else {
            LazyVerticalGrid(
                columns = GridCells.Fixed(3),
                contentPadding = PaddingValues(16.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
                modifier = Modifier.padding(padding),
            ) {
                items(pendingTrash, key = { it.key }) { group ->
                    TrashGridTile(
                        group = group,
                        onKeep = { viewModel.restoreFromPendingTrash(group) },
                    )
                }
            }
        }
    }
}

@Composable
private fun TrashGridTile(group: MediaGroup, onKeep: () -> Unit) {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .aspectRatio(1f)
            .clip(ContainerShape)
            .background(MaterialTheme.colorScheme.surface),
    ) {
        AsyncImage(
            model = group.cover.uri,
            contentDescription = group.cover.displayName,
            contentScale = ContentScale.Crop,
            modifier = Modifier.fillMaxSize(),
        )
        IconButton(
            onClick = onKeep,
            modifier = Modifier
                .align(Alignment.TopEnd)
                .padding(4.dp)
                .size(28.dp)
                .clip(CircleShape)
                .background(Color.Black.copy(alpha = 0.55f)),
        ) {
            Icon(Icons.Filled.Close, contentDescription = "Keep this item", tint = Color.White)
        }
    }
}
