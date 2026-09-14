package com.roomie.app.ui

import android.content.Intent
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp

/**
 * Shown once, on the launch right after a crash, so the stack trace can be read (and shared)
 * directly from the device instead of needing adb/logcat access.
 */
@Composable
fun CrashScreen(stackTrace: String, onContinue: () -> Unit) {
    val context = LocalContext.current

    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        Text("Roomie crashed last time", style = MaterialTheme.typography.titleLarge)
        Text(
            "Here is the full error. Tap Share to send it, or Continue to use the app.",
            style = MaterialTheme.typography.bodyMedium,
            modifier = Modifier.padding(top = 4.dp, bottom = 12.dp),
        )

        SelectionContainer(
            modifier = Modifier
                .weight(1f)
                .fillMaxWidth()
                .verticalScroll(rememberScrollState()),
        ) {
            Text(stackTrace, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall)
        }

        Column(modifier = Modifier.fillMaxWidth().padding(top = 12.dp)) {
            Button(
                onClick = {
                    val sendIntent = Intent(Intent.ACTION_SEND).apply {
                        type = "text/plain"
                        putExtra(Intent.EXTRA_TEXT, stackTrace)
                    }
                    context.startActivity(Intent.createChooser(sendIntent, "Share crash log"))
                },
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text("Share crash log")
            }
            OutlinedButton(
                onClick = onContinue,
                modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
            ) {
                Text("Continue to app")
            }
        }
    }
}
