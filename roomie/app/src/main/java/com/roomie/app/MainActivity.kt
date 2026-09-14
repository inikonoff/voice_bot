package com.roomie.app

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import com.roomie.app.ui.ViewModelFactory
import com.roomie.app.ui.navigation.RoomieNavHost
import com.roomie.app.ui.theme.RoomieTheme

class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        val container = (application as RoomieApplication).container
        val viewModelFactory = ViewModelFactory(container)

        setContent {
            RoomieTheme {
                RoomieNavHost(viewModelFactory = viewModelFactory)
            }
        }
    }
}
