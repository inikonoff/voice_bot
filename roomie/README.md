# Roomie

Android app for fast, swipe-based gallery cleanup (Tinder-style cards), built per the MVP TZ.
Kotlin + Jetpack Compose, 100% offline, deleted files go to the system trash
(`MediaStore.createTrashRequest`) with a configurable auto-purge timer.

## Status

This is a from-scratch implementation of the full MVP spec — no prior code existed. All screens,
the data layer, and the background cleanup worker are written. **It has not been compiled or run**:
this sandbox's network policy blocks `dl.google.com`, so the Android Gradle Plugin and Android SDK
platform cannot be downloaded here, and there is no emulator/device attached. Before relying on it,
build and run it on a real Android Studio setup and walk through the flows in section
"Suggested manual QA" below — code review alone cannot substitute for that.

## Project layout

```
roomie/
  app/src/main/java/com/roomie/app/
    data/
      media/       MediaStore access (MediaRepository), burst/video grouping, empty-folder cleanup
      db/          Room: trash registry + favorites (pre-Q fallback / batched sync queue)
      settings/    DataStore-backed user settings + session swipe counter
      trash/       TrashRepository: system trash dialog, retention countdown, favorite sync
      monetization/ MonetizationGateway interface (no-op stub; extension point for Ads/Billing)
    ui/
      screens/folders   Auto-discovered folder grid + period filter
      screens/swipe      The card stack: drag gesture, spring physics, undo, favorites, limit
      screens/trash      Pre-deletion review grid ("uncheck to keep")
      screens/summary    Post-deletion summary (count + freed space)
      screens/limit      Swipe-limit paywall (ad / one-time purchase stubs)
      screens/settings   Sort order, retention days, auto-delete-empty-folders, monetization toggle
      navigation         Single-Activity NavHost wiring all of the above
      theme              Warm & Cozy color palette, shapes, spring constants
    work/          TrashCleanupWorker (WorkManager, device-idle + battery-not-low)
    AppContainer / RoomieApplication / MainActivity   manual DI wiring (no DI framework)
```

## Key design decisions (and why)

- **Manual DI, no Hilt.** The app is small enough that a DI framework would add build complexity
  without buying much; `AppContainer` + `ViewModelFactory` cover every screen.
- **Room stores the trash registry**, not just a DataStore flag, because the app enforces its own
  configurable retention (1/3/7/30 days) independent of whatever the OS's own trash auto-purge
  window is. On API 30+, `createTrashRequest` hides the file immediately (`IS_TRASHED`); Roomie's
  own worker permanently deletes it once *its* countdown elapses. On API 26-29 (no system trash),
  the file stays visible until that same countdown fires — an accepted MVP simplification for
  legacy Android, called out in the TZ (section 8.2).
- **Favorites are batched, not synced instantly**, mirroring the "one system dialog per session"
  rule for deletions: double-tapping during a swipe session writes to a local Room table only;
  everything pending gets pushed to the real `MediaStore.IS_FAVORITE` column in one pass when the
  session is finalized (Q+ only — that column doesn't exist below Android 10).
- **Burst grouping is timestamp-based**, not `burst_id`-based, because there is no public,
  cross-device MediaStore column exposing a burst id to third-party apps. Consecutive photos in the
  same folder taken within ~1.5s of each other collapse into one card; short videos are always their
  own single-item unit. See `BurstGrouping.kt` for the exact heuristic and how to plug in a real
  burst id if a target device happens to expose one.
- **Empty-folder deletion only touches directories Roomie itself just vacated** (passed in
  explicitly by the cleanup worker), and never removes a top-level media folder (DCIM, Pictures,
  WhatsApp, ...) even if it's empty — those are here for other apps to write into. It requires
  `MANAGE_EXTERNAL_STORAGE` and is a no-op without it.
- **Ads/Billing are a `NoOpMonetizationGateway` stub.** The swipe limit, paywall screen, and
  settings toggle are fully wired; swap the gateway implementation for real AdMob/Play Billing
  calls without touching any caller. Monetization is off by default (`monetizationEnabled = false`
  in `RoomieSettings`), matching "off during personal use" from the TZ.
- **The trash-confirmation dialog listener lives at the NavHost level**, not inside the swipe
  screen. "Delete all" is pressed from the trash-preview screen, one navigation hop after the swipe
  screen has already left composition — so the `IntentSender` launcher has to live somewhere that
  outlives individual screens (see `RoomieNavHost.kt`).

## Suggested manual QA

Once this builds in a real environment, exercise at minimum:

1. Grant/deny the media permission dialog on first launch; deny → rationale screen → grant.
2. Open a folder with bursts (rapid continuous shots) and confirm they collapse into one card.
3. Swipe left/right, confirm rotation + spring-back/spring-out animation and haptic tick at the
   threshold; double-tap a card and confirm the favorite badge appears.
4. Undo several times in a row (up to 10) and confirm cards return in the correct order.
5. Exhaust a folder's stack, review the trash grid, uncheck an item, then "Delete all" — confirm
   exactly one system trash dialog appears (API 30+) and the summary screen shows correct
   count/size.
6. In Settings, flip "Enable swipe limit" on, set a low value in code temporarily (or swipe ~100
   times) to confirm the limit screen appears and both stub buttons report "not available" cleanly.
7. Change sort order and retention days in Settings and confirm they take effect on the next
   session / cleanup run.
8. Turn off networking entirely and confirm nothing breaks (there should be no network calls at
   all outside a real Ads SDK, which isn't wired in).

## Known gaps vs. a production build

- No real Ads SDK / Billing Library integration (by design for this MVP — see TZ section 11).
- No automated tests yet (no test runner available in this sandbox to scaffold against).
- `EmptyFolderCleaner` and the legacy (<API 30) trash path use `MediaStore.MediaColumns.DATA`,
  which is deprecated and only returns real paths with All Files Access granted; without it, both
  features degrade to no-ops rather than crashing.
