package com.roomie.app.ui.screens.limit

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.roomie.app.ui.screens.swipe.SwipeSessionViewModel
import kotlinx.coroutines.launch

/**
 * Shown when the free-swipe-session limit is hit (TZ section 9). Both actions go through
 * [com.roomie.app.data.monetization.MonetizationGateway], which is a no-op stub until a real Ads
 * SDK / Billing Library is wired in — the message below reflects that honestly instead of
 * pretending an ad or purchase flow exists.
 */
@Composable
fun SwipeLimitScreen(
    viewModel: SwipeSessionViewModel,
    onUnlocked: () -> Unit,
    onBackToFolders: () -> Unit,
) {
    val scope = rememberCoroutineScope()
    var message by remember { mutableStateOf<String?>(null) }

    Column(
        modifier = Modifier.fillMaxSize().padding(32.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Icon(
            Icons.Filled.Lock,
            contentDescription = null,
            tint = MaterialTheme.colorScheme.primary,
            modifier = Modifier.padding(bottom = 16.dp),
        )
        Text("Free swipe limit reached", style = MaterialTheme.typography.headlineSmall)
        Text(
            "Watch a short ad to keep going, or unlock unlimited swiping for good.",
            style = MaterialTheme.typography.bodyMedium,
            modifier = Modifier.padding(top = 8.dp, bottom = 24.dp),
        )

        Button(
            onClick = {
                scope.launch {
                    val granted = viewModel.unlockViaRewardedAd()
                    if (granted) onUnlocked() else message = "No ad available right now."
                }
            },
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text("Watch ad to continue")
        }

        OutlinedButton(
            onClick = {
                scope.launch {
                    val purchased = viewModel.unlockViaPurchase()
                    if (purchased) onUnlocked() else message = "Purchases aren't set up yet."
                }
            },
            modifier = Modifier.fillMaxWidth().padding(top = 12.dp),
        ) {
            Text("Unlock forever")
        }

        message?.let {
            Text(
                it,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error,
                modifier = Modifier.padding(top = 16.dp),
            )
        }

        TextButton(
            onClick = onBackToFolders,
            modifier = Modifier.padding(top = 8.dp),
        ) {
            Text("Back to folders")
        }
    }
}
