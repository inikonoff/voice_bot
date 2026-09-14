package com.roomie.app

import android.content.Context
import java.io.File

/**
 * Minimal crash capture so a crash on a real device (which we have no logcat access to during
 * development) can be read back on the device itself instead of being lost. Installed as early
 * as possible in [RoomieApplication.onCreate]; the previous handler still runs afterward so the
 * OS's own "app has stopped" behavior is unaffected.
 */
object CrashReporter {
    private const val FILE_NAME = "last_crash.txt"

    fun install(context: Context) {
        val appContext = context.applicationContext
        val previousHandler = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, throwable ->
            try {
                File(appContext.filesDir, FILE_NAME).writeText(throwable.stackTraceToString())
            } catch (_: Throwable) {
                // Best-effort only; never let the crash handler itself throw.
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
