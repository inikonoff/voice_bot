package com.roomie.app.data.monetization

/**
 * Extension point for the real Ads SDK / Billing Library integration (TZ section 11 — not wired
 * up for the MVP / personal-use build). [SettingsRepository.monetizationEnabled] governs whether
 * [MonetizationGateway] is even consulted; when it's off the swipe limit never triggers, so this
 * interface can stay a no-op indefinitely without touching the swipe logic that calls it.
 */
interface MonetizationGateway {
    /** Reward ad that lifts the swipe limit for the rest of the current session. */
    suspend fun showRewardedAd(): RewardResult

    /** One-time purchase that removes the swipe limit permanently. */
    suspend fun launchOneTimePurchase(): PurchaseResult
}

enum class RewardResult { GRANTED, DISMISSED, UNAVAILABLE }
enum class PurchaseResult { PURCHASED, CANCELLED, UNAVAILABLE }

/**
 * Default gateway while no Ads SDK / Billing Library dependency is present. Swap this binding for
 * a real implementation (AdMob rewarded ads + Play Billing) without changing any caller.
 */
class NoOpMonetizationGateway : MonetizationGateway {
    override suspend fun showRewardedAd(): RewardResult = RewardResult.UNAVAILABLE
    override suspend fun launchOneTimePurchase(): PurchaseResult = PurchaseResult.UNAVAILABLE
}
