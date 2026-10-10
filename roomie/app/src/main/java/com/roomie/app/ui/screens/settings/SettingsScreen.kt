package com.roomie.app.ui.screens.settings

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.roomie.app.data.media.SortOrder
import com.roomie.app.data.settings.RoomieSettings

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(
    viewModel: SettingsViewModel,
    onBack: () -> Unit,
) {
    val settings by viewModel.settings.collectAsState()

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Settings") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.Filled.ArrowBack, contentDescription = "Back")
                    }
                },
            )
        },
    ) { padding ->
        Column(modifier = Modifier.fillMaxSize().padding(padding).padding(16.dp)) {
            SectionTitle("Card order")
            SortOrderSelector(settings.sortOrder, viewModel::setSortOrder)

            SectionTitle("Trash retention")
            RetentionSelector(settings.trashRetentionDays, viewModel::setTrashRetentionDays)

            SwitchRow(
                title = "Delete empty folders automatically",
                checked = settings.autoDeleteEmptyFolders,
                onCheckedChange = viewModel::setAutoDeleteEmptyFolders,
            )

            SwitchRow(
                title = "Enable swipe limit & monetization",
                subtitle = if (settings.isPremiumUnlocked) "Unlocked — limit disabled" else null,
                checked = settings.monetizationEnabled,
                onCheckedChange = viewModel::setMonetizationEnabled,
            )
        }
    }
}

@Composable
private fun SectionTitle(text: String) {
    Text(
        text,
        style = MaterialTheme.typography.titleMedium,
        modifier = Modifier.padding(top = 16.dp, bottom = 8.dp),
    )
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun SortOrderSelector(current: SortOrder, onSelected: (SortOrder) -> Unit) {
    val options = listOf(SortOrder.NEWEST_FIRST to "Newest first", SortOrder.OLDEST_FIRST to "Oldest first")
    SingleChoiceSegmentedButtonRow(modifier = Modifier.fillMaxWidth()) {
        options.forEachIndexed { index, (order, label) ->
            SegmentedButton(
                selected = current == order,
                onClick = { onSelected(order) },
                shape = SegmentedButtonDefaults.itemShape(index = index, count = options.size),
            ) {
                Text(label)
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun RetentionSelector(currentDays: Int, onSelected: (Int) -> Unit) {
    val options = RoomieSettings.ALLOWED_RETENTION_DAYS
    SingleChoiceSegmentedButtonRow(modifier = Modifier.fillMaxWidth()) {
        options.forEachIndexed { index, days ->
            SegmentedButton(
                selected = currentDays == days,
                onClick = { onSelected(days) },
                shape = SegmentedButtonDefaults.itemShape(index = index, count = options.size),
            ) {
                Text("${days}d")
            }
        }
    }
}

@Composable
private fun SwitchRow(
    title: String,
    subtitle: String? = null,
    checked: Boolean,
    onCheckedChange: (Boolean) -> Unit,
) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(top = 20.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(title, style = MaterialTheme.typography.bodyLarge)
            subtitle?.let {
                Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.secondary)
            }
        }
        Switch(checked = checked, onCheckedChange = onCheckedChange)
    }
}
