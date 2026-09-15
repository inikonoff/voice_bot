package com.roomie.app.work

import android.content.Context
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import com.roomie.app.RoomieApplication
import kotlinx.coroutines.flow.first
import java.util.concurrent.TimeUnit

/**
 * Periodically purges trash entries whose retention window has passed, and — if the user opted
 * in — deletes folders left empty by that purge. Runs only when the device is idle and not on
 * low battery, per TZ section 8.4; there is no network constraint since the app never touches
 * the network for this work.
 */
class TrashCleanupWorker(
    context: Context,
    params: WorkerParameters,
) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val container = (applicationContext as RoomieApplication).container
        return try {
            val cleanup = container.trashRepository.permanentlyDeleteExpired()
            container.trashRepository.syncFavoritesToMediaStore()

            val settings = container.settingsRepository.settings.first()
            if (settings.autoDeleteEmptyFolders && cleanup.affectedDirs.isNotEmpty()) {
                container.emptyFolderCleaner.deleteEmptyFolders(cleanup.affectedDirs)
            }

            Result.success(workDataOf(KEY_FREED_BYTES to cleanup.freedBytes))
        } catch (_: Throwable) {
            // WorkManager's default backoff policy handles transient I/O failures.
            Result.retry()
        }
    }

    companion object {
        const val KEY_FREED_BYTES = "freed_bytes"
        private const val UNIQUE_WORK_NAME = "trash_cleanup"

        fun schedule(context: Context) {
            val constraints = Constraints.Builder()
                .setRequiresDeviceIdle(true)
                .setRequiresBatteryNotLow(true)
                .setRequiredNetworkType(NetworkType.NOT_REQUIRED)
                .build()

            // No explicit backoff criteria: WorkManager throws IllegalArgumentException ("Cannot
            // set backoff criteria on an idle mode job") when combined with setRequiresDeviceIdle
            // — this crashed every launch on every device. WorkManager still retries a Result.retry()
            // with its own default backoff even without an explicit policy here.
            val request = PeriodicWorkRequestBuilder<TrashCleanupWorker>(6, TimeUnit.HOURS)
                .setConstraints(constraints)
                .build()

            WorkManager.getInstance(context).enqueueUniquePeriodicWork(
                UNIQUE_WORK_NAME,
                ExistingPeriodicWorkPolicy.KEEP,
                request,
            )
        }
    }
}
