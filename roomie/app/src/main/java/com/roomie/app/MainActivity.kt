package com.roomie.app

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import com.roomie.app.ui.CrashScreen
import com.roomie.app.ui.ViewModelFactory
import com.roomie.app.ui.navigation.RoomieNavHost
import com.roomie.app.ui.theme.RoomieTheme

class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        val container = (application as RoomieApplication).container
        val viewModelFactory = ViewModelFactory(container)

        // If we're here at all, this launch's Application.onCreate already finished successfully
        // (Android always completes it before starting an Activity), so a crash log or a
        // checkpoint short of "onCreate:done" can only be left over from a PREVIOUS attempt.
        val crashLog = CrashReporter.readAndClear(this)
        val staleCheckpoint = CrashReporter.readCheckpoint(this)?.takeIf { !it.endsWith("onCreate:done") }
        val diagnosticText = crashLog ?: staleCheckpoint?.let {
            "No exception was caught, but a previous launch didn't finish starting.\n\nLast checkpoint reached:\n$it"
        }

        setContent {
            var showDiagnostic by remember { mutableStateOf(diagnosticText != null) }
            RoomieTheme {
                if (showDiagnostic && diagnosticText != null) {
                    CrashScreen(stackTrace = diagnosticText, onContinue = { showDiagnostic = false })
                } else {
                    RoomieNavHost(viewModelFactory = viewModelFactory)
                }
            }
        }
    }
}
