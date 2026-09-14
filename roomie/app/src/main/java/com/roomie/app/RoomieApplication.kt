package com.roomie.app

import android.app.Application
import coil3.ImageLoader
import coil3.PlatformContext
import coil3.SingletonImageLoader
import coil3.request.crossfade
import coil3.video.VideoFrameDecoder
import com.roomie.app.work.TrashCleanupWorker

class RoomieApplication : Application(), SingletonImageLoader.Factory {

    lateinit var container: AppContainer
        private set

    override fun onCreate() {
        super.onCreate()
        container = AppContainer(this)
        TrashCleanupWorker.schedule(this)
    }

    /** Registers the video-frame decoder so burst/video covers get a real thumbnail instead of a
     *  blank tile, and enables downsampling-friendly defaults to avoid OOM on large photos. */
    override fun newImageLoader(context: PlatformContext): ImageLoader =
        ImageLoader.Builder(context)
            .components { add(VideoFrameDecoder.Factory()) }
            .crossfade(true)
            .build()
}
