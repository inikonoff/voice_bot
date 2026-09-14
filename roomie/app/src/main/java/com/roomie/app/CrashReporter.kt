package com.roomie.app

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import java.io.File

/**
 * Minimal crash capture so a crash on a real device (which we have no logcat access to during
 * development) can be read back on the device itself instead of being lost.
 *
 * Copies the stack trace to the clipboard synchronously at the moment of the crash — the process
 * is still alive inside the exception handler, so this reliably completes before it dies — rather
 * than only relying on a second launch to show it, in case whatever crashes here never gets that
 * far again. Also writes it to a file as a fallback the app can show on the next successful
 * launch. Installed from [RoomieApplication.attachBaseContext], the earliest hook available,
 * since a crash can happen inside a library's own startup (WorkManager, DataStore, ...) which
 * runs before [RoomieApplication.onCreate] — installing there would be too late to catch it.
 */
object CrashReporter {
    private const val FILE_NAME = "last_crash.txt"

    fun install(context: Context) {
        val appContext = context.applicationContext
        val previousHandler = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, throwable ->
            val trace = throwable.stackTraceToString()

            try {
                File(appContext.filesDir, FILE_NAME).writeText(trace)
            } catch (_: Throwable) {
                // Best-effort only; never let the crash handler itself throw.
            }

            try {
                val clipboard = appContext.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager
                clipboard?.setPrimaryClip(ClipData.newPlainText("Roomie crash", trace))
            } catch (_: Throwable) {
                // Same: best-effort, some OEMs restrict clipboard access from a dying process.
            }

            previousHandler?.uncaughtException(thread, throwable)
        }
    }

    /** Returns the last saved crash, if any, and deletes it so it's only shown once. */
    fun readAndClear(context: Context): String? {
        val file = File(context.applicationContext.filesDir, FILE_NAME)
        if (!file.exists()) return null
        val content = file.readText()
        file.delete()
        return content
    }
}
