package com.roomie.app.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

// The app is designed around one warm, light palette. Dark theme reuses the same
// hues at reduced contrast rather than a separate design pass, since this is an MVP.
private val LightColors = lightColorScheme(
    background = Background,
    surface = Surface,
    primary = Primary,
    secondary = Secondary,
    onBackground = OnSurface,
    onSurface = OnSurface,
    error = SwipeLeftDelete,
)

private val DarkColors = darkColorScheme(
    background = Color(0xFF221C19),
    surface = Color(0xFF2D2522),
    primary = Primary,
    secondary = Secondary,
    onBackground = Surface,
    onSurface = Surface,
    error = SwipeLeftDelete,
)

@Composable
fun RoomieTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    val colors = if (darkTheme) DarkColors else LightColors
    MaterialTheme(
        colorScheme = colors,
        shapes = RoomieShapes,
        content = content,
    )
}
