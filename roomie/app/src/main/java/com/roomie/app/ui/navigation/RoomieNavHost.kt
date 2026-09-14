package com.roomie.app.ui.navigation

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.IntentSenderRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import com.roomie.app.data.media.PeriodFilter
import com.roomie.app.ui.ViewModelFactory
import com.roomie.app.ui.screens.folders.FolderListScreen
import com.roomie.app.ui.screens.folders.FolderListViewModel
import com.roomie.app.ui.screens.limit.SwipeLimitScreen
import com.roomie.app.ui.screens.settings.SettingsScreen
import com.roomie.app.ui.screens.settings.SettingsViewModel
import com.roomie.app.ui.screens.summary.SummaryScreen
import com.roomie.app.ui.screens.swipe.SwipeScreen
import com.roomie.app.ui.screens.swipe.SwipeSessionViewModel
import com.roomie.app.ui.screens.trash.TrashPreviewScreen
import java.net.URLDecoder
import java.net.URLEncoder

private object Routes {
    const val FOLDERS = "folders"
    const val SWIPE = "swipe/{bucketId}/{displayName}/{period}"
    const val TRASH_PREVIEW = "trash_preview"
    const val SUMMARY = "summary"
    const val SWIPE_LIMIT = "swipe_limit"
    const val SETTINGS = "settings"

    const val ALL_PHOTOS_SENTINEL = "all"

    fun swipe(bucketId: Long?, displayName: String, period: PeriodFilter): String {
        val encodedName = URLEncoder.encode(displayName, "UTF-8")
        val bucket = bucketId?.toString() ?: ALL_PHOTOS_SENTINEL
        return "swipe/$bucket/$encodedName/${period.name}"
    }
}

@Composable
fun RoomieNavHost(viewModelFactory: ViewModelFactory) {
    val navController = rememberNavController()

    // Activity-scoped: the swipe/trash-preview/summary/limit screens are one continuous flow and
    // share this single session's in-memory state (stack, undo history, pending trash).
    val swipeSessionViewModel: SwipeSessionViewModel = viewModel(factory = viewModelFactory)

    // Lives here, not inside the swipe destination: "Delete all" is pressed from the trash-preview
    // screen, by which point the swipe screen has already left composition, so its own collector
    // would never see the event. This host composable stays alive for the whole app session.
    val intentSenderLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.StartIntentSenderForResult(),
    ) { /* Confirmation is driven by re-invoking onSystemTrashConfirmed below regardless of result,
          matching the OS's own "one dialog, then proceed" trash UX. */ }

    LaunchedEffect(swipeSessionViewModel) {
        swipeSessionViewModel.trashConfirmationEvents.collect { request ->
            intentSenderLauncher.launch(IntentSenderRequest.Builder(request.intentSender).build())
            swipeSessionViewModel.onSystemTrashConfirmed(request.groups)
        }
    }

    NavHost(navController = navController, startDestination = Routes.FOLDERS) {
        composable(Routes.FOLDERS) {
            val folderListViewModel: FolderListViewModel = viewModel(factory = viewModelFactory)
            FolderListScreen(
                viewModel = folderListViewModel,
                onOpenFolder = { bucketId, displayName, period ->
                    navController.navigate(Routes.swipe(bucketId, displayName, period))
                },
                onOpenSettings = { navController.navigate(Routes.SETTINGS) },
            )
        }

        composable(
            route = Routes.SWIPE,
            arguments = listOf(
                navArgument("bucketId") { type = NavType.StringType },
                navArgument("displayName") { type = NavType.StringType },
                navArgument("period") { type = NavType.StringType },
            ),
        ) { backStackEntry ->
            val args = backStackEntry.arguments!!
            val bucketIdArg = args.getString("bucketId")
            val bucketId = bucketIdArg?.takeIf { it != Routes.ALL_PHOTOS_SENTINEL }?.toLongOrNull()
            val displayName = URLDecoder.decode(args.getString("displayName") ?: "", "UTF-8")
            val period = PeriodFilter.valueOf(args.getString("period") ?: PeriodFilter.ALL.name)

            SwipeSessionEntry(
                viewModel = swipeSessionViewModel,
                bucketId = bucketId,
                displayName = displayName,
                period = period,
                onBack = { navController.popBackStack() },
                onStackExhausted = { navController.navigate(Routes.TRASH_PREVIEW) },
                onLimitReached = { navController.navigate(Routes.SWIPE_LIMIT) },
            )
        }

        composable(Routes.TRASH_PREVIEW) {
            TrashPreviewScreen(
                viewModel = swipeSessionViewModel,
                onBack = { navController.popBackStack() },
                onDeleteConfirmed = {
                    navController.navigate(Routes.SUMMARY) {
                        popUpTo(Routes.FOLDERS)
                    }
                },
            )
        }

        composable(Routes.SUMMARY) {
            val summary by swipeSessionViewModel.summaryState.collectAsState()
            summary?.let {
                SummaryScreen(
                    summary = it,
                    onDone = {
                        swipeSessionViewModel.clearSummary()
                        navController.popBackStack(Routes.FOLDERS, inclusive = false)
                    },
                )
            }
        }

        composable(Routes.SWIPE_LIMIT) {
            SwipeLimitScreen(
                viewModel = swipeSessionViewModel,
                onUnlocked = { navController.popBackStack() },
                onBackToFolders = { navController.popBackStack(Routes.FOLDERS, inclusive = false) },
            )
        }

        composable(Routes.SETTINGS) {
            val settingsViewModel: SettingsViewModel = viewModel(factory = viewModelFactory)
            SettingsScreen(
                viewModel = settingsViewModel,
                onBack = { navController.popBackStack() },
            )
        }
    }
}

@Composable
private fun SwipeSessionEntry(
    viewModel: SwipeSessionViewModel,
    bucketId: Long?,
    displayName: String,
    period: PeriodFilter,
    onBack: () -> Unit,
    onStackExhausted: () -> Unit,
    onLimitReached: () -> Unit,
) {
    LaunchedEffect(bucketId, displayName, period) {
        viewModel.loadFolder(bucketId, displayName, period)
    }
    SwipeScreen(
        viewModel = viewModel,
        onBack = onBack,
        onStackExhausted = onStackExhausted,
        onLimitReached = onLimitReached,
    )
}
