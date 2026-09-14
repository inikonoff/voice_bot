package com.roomie.app.ui.screens.folders

import android.Manifest
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Photo
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import android.net.Uri
import coil3.compose.AsyncImage
import com.roomie.app.data.media.GalleryFolder
import com.roomie.app.data.media.PeriodFilter
import com.roomie.app.ui.theme.ContainerShape

@Composable
fun FolderListScreen(
    viewModel: FolderListViewModel,
    onOpenFolder: (bucketId: Long?, displayName: String, period: PeriodFilter) -> Unit,
    onOpenSettings: () -> Unit,
) {
    val uiState by viewModel.uiState.collectAsState()
    val context = LocalContext.current

    val requiredPermissions = remember {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            arrayOf(Manifest.permission.READ_MEDIA_IMAGES, Manifest.permission.READ_MEDIA_VIDEO)
        } else {
            arrayOf(Manifest.permission.READ_EXTERNAL_STORAGE)
        }
    }

    val permissionLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.RequestMultiplePermissions(),
    ) { results ->
        viewModel.onPermissionResult(results.values.all { it })
    }

    LaunchedEffect(Unit) {
        val alreadyGranted = requiredPermissions.all {
            androidx.core.content.ContextCompat.checkSelfPermission(context, it) ==
                android.content.pm.PackageManager.PERMISSION_GRANTED
        }
        if (alreadyGranted) {
            viewModel.onPermissionResult(true)
        } else {
            permissionLauncher.launch(requiredPermissions)
        }
    }

    Scaffold(
        topBar = {
            RoomieTopBar(onOpenSettings = onOpenSettings)
        },
    ) { padding ->
        when {
            !uiState.hasMediaPermission -> PermissionRationale(
                modifier = Modifier.padding(padding),
                onGrantClick = { permissionLauncher.launch(requiredPermissions) },
            )

            uiState.isLoading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                CircularProgressIndicator()
            }

            else -> FolderGrid(
                modifier = Modifier.padding(padding),
                folders = uiState.folders,
                period = uiState.period,
                onPeriodSelected = viewModel::onPeriodSelected,
                onOpenFolder = onOpenFolder,
            )
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun RoomieTopBar(onOpenSettings: () -> Unit) {
    TopAppBar(
        title = { Text("Roomie") },
        actions = {
            IconButton(onClick = onOpenSettings) {
                Icon(Icons.Filled.Settings, contentDescription = "Settings")
            }
        },
    )
}

@Composable
private fun PermissionRationale(modifier: Modifier = Modifier, onGrantClick: () -> Unit) {
    Column(
        modifier = modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Icon(Icons.Filled.Photo, contentDescription = null, modifier = Modifier.padding(bottom = 16.dp))
        Text(
            "Roomie needs access to your photos and videos to help you clean up your gallery.",
            style = MaterialTheme.typography.bodyLarge,
        )
        Spacer(modifier = Modifier.padding(top = 16.dp))
        Button(onClick = onGrantClick) {
            Text("Grant access")
        }
    }
}

@Composable
private fun FolderGrid(
    modifier: Modifier = Modifier,
    folders: List<GalleryFolder>,
    period: PeriodFilter,
    onPeriodSelected: (PeriodFilter) -> Unit,
    onOpenFolder: (Long?, String, PeriodFilter) -> Unit,
) {
    Column(modifier = modifier.fillMaxSize()) {
        PeriodFilterRow(selected = period, onSelected = onPeriodSelected)

        LazyVerticalGrid(
            columns = GridCells.Fixed(2),
            contentPadding = PaddingValues(16.dp),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            item {
                AllPhotosCard(
                    totalCount = folders.sumOf { it.itemCount },
                    onClick = { onOpenFolder(null, "All photos", period) },
                )
            }
            items(folders, key = { it.bucketId }) { folder ->
                FolderCard(
                    folder = folder,
                    onClick = { onOpenFolder(folder.bucketId, folder.displayName, period) },
                )
            }
        }
    }
}

@Composable
private fun PeriodFilterRow(selected: PeriodFilter, onSelected: (PeriodFilter) -> Unit) {
    val options = listOf(
        PeriodFilter.ALL to "All time",
        PeriodFilter.LAST_DAY to "Day",
        PeriodFilter.LAST_MONTH to "Month",
        PeriodFilter.LAST_YEAR to "Year",
    )
    LazyRow(
        contentPadding = PaddingValues(horizontal = 16.dp, vertical = 8.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        items(options) { (filter, label) ->
            FilterChip(
                selected = filter == selected,
                onClick = { onSelected(filter) },
                label = { Text(label) },
            )
        }
    }
}

@Composable
private fun AllPhotosCard(totalCount: Int, onClick: () -> Unit) {
    FolderTile(
        title = "All photos",
        subtitle = "$totalCount items",
        coverUri = null,
        icon = Icons.Filled.Photo,
        onClick = onClick,
    )
}

@Composable
private fun FolderCard(folder: GalleryFolder, onClick: () -> Unit) {
    FolderTile(
        title = folder.displayName,
        subtitle = "${folder.itemCount} items",
        coverUri = folder.coverUri,
        icon = null,
        onClick = onClick,
    )
}

@Composable
private fun FolderTile(
    title: String,
    subtitle: String,
    coverUri: Uri?,
    icon: ImageVector?,
    onClick: () -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick),
    ) {
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .aspectRatio(1f)
                .background(MaterialTheme.colorScheme.surface, ContainerShape),
            contentAlignment = Alignment.Center,
        ) {
            when {
                coverUri != null -> AsyncImage(
                    model = coverUri,
                    contentDescription = title,
                    contentScale = ContentScale.Crop,
                    modifier = Modifier.fillMaxSize(),
                )
                icon != null -> Icon(
                    icon,
                    contentDescription = null,
                    tint = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.padding(24.dp),
                )
            }
        }
        Text(
            title,
            style = MaterialTheme.typography.titleSmall,
            modifier = Modifier.padding(top = 6.dp),
        )
        Text(
            subtitle,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.secondary,
        )
    }
}
