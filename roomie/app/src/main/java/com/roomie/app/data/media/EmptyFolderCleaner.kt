package com.roomie.app.data.media

import android.os.Build
import android.os.Environment
import java.io.File

/**
 * Best-effort physical deletion of folders left empty after a permanent-delete pass. Requires
 * `MANAGE_EXTERNAL_STORAGE` (optional permission, TZ section 10) — without it this silently does
 * nothing, since Scoped Storage gives no other way to remove a directory you don't own.
 *
 * Only deletes directories actually vacated by Roomie's own cleanup (passed in explicitly), and
 * never a top-level media root (DCIM, Pictures, WhatsApp, ...): other apps expect those to exist.
 */
class EmptyFolderCleaner {

    fun deleteEmptyFolders(candidateDirs: Set<File>) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R && !Environment.isExternalStorageManager()) {
            return
        }

        val storageRoot = Environment.getExternalStorageDirectory()
        for (dir in candidateDirs) {
            deleteIfEmptyUpToRoot(dir, storageRoot)
        }
    }

    /** Deletes [dir] if empty, then walks up deleting newly-empty parents, stopping one level
     *  below [storageRoot] so top-level folders like DCIM/ or WhatsApp/ are never removed. */
    private fun deleteIfEmptyUpToRoot(dir: File, storageRoot: File) {
        var current: File? = dir
        while (current != null && current != storageRoot && current.parentFile != storageRoot) {
            val children = current.listFiles() ?: break
            if (children.isNotEmpty()) break
            if (!current.delete()) break
            current = current.parentFile
        }
    }
}
