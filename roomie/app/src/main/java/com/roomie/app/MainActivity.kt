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
        val crashLog = CrashReporter.readAndClear(this)

        setContent {
            var showCrash by remember { mutableStateOf(crashLog != null) }
            RoomieTheme {
                if (showCrash && crashLog != null) {
                    CrashScreen(stackTrace = crashLog, onContinue = { showCrash = false })
                } else {
                    RoomieNavHost(viewModelFactory = viewModelFactory)
                }
            }
        }
    }
}
