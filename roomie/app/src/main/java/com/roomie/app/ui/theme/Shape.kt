package com.roomie.app.ui.theme

import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Shapes
import androidx.compose.ui.unit.dp

val CardShape = RoundedCornerShape(32.dp)
val ContainerShape = RoundedCornerShape(24.dp)

val RoomieShapes = Shapes(
    small = RoundedCornerShape(12.dp),
    medium = ContainerShape,
    large = CardShape,
)
