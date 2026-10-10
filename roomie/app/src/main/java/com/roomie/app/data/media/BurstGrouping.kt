package com.roomie.app.data.media

/**
 * Collapses consecutive burst-shot photos into a single [MediaGroup] so the swipe stack shows
 * one card per "moment" instead of one per frame.
 *
 * Videos are never merged with neighbours — a short video is already a single unit — they just
 * pass through as singleton groups. Photos are clustered when they belong to the same folder and
 * were taken within [clusterWindowMillis] of each other. There is no public, cross-device
 * MediaStore column for a burst id, so timestamp proximity is the primary signal; where a device
 * does expose one (e.g. via `EXTRA_ID` style vendor columns), plug it into [MediaItem] and prefer
 * it over the timestamp heuristic in [belongsToSameBurst].
 *
 * Input must already be sorted by [MediaItem.dateTakenMillis] ascending; output preserves that
 * chronological order at the group level.
 */
fun List<MediaItem>.groupIntoUnits(clusterWindowMillis: Long = 1_500L): List<MediaGroup> {
    if (isEmpty()) return emptyList()

    val groups = mutableListOf<MediaGroup>()
    var currentCluster = mutableListOf(first())

    for (index in 1 until size) {
        val item = this[index]
        val previous = currentCluster.last()
        if (belongsToSameBurst(previous, item, clusterWindowMillis)) {
            currentCluster.add(item)
        } else {
            groups += currentCluster.toGroup()
            currentCluster = mutableListOf(item)
        }
    }
    groups += currentCluster.toGroup()
    return groups
}

private fun belongsToSameBurst(a: MediaItem, b: MediaItem, windowMillis: Long): Boolean {
    if (a.isVideo || b.isVideo) return false
    if (a.bucketId != b.bucketId) return false
    return kotlin.math.abs(b.dateTakenMillis - a.dateTakenMillis) < windowMillis
}

private fun List<MediaItem>.toGroup(): MediaGroup = MediaGroup(
    key = "group_${first().stableId}_$size",
    items = toList(),
)
