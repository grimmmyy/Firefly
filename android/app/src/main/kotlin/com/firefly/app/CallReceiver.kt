package com.firefly.app

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.telephony.TelephonyManager

/** Turns the receiver down while the phone rings or a call is on, and back
 *  up when it ends ("Lower volume during calls" in the app). */
class CallReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.action != TelephonyManager.ACTION_PHONE_STATE_CHANGED) return
        if (!Bridge.enabled(ctx, "duckCalls", false)) return
        val state = intent.getStringExtra(TelephonyManager.EXTRA_STATE) ?: return
        val on = when (state) {
            TelephonyManager.EXTRA_STATE_RINGING, TelephonyManager.EXTRA_STATE_OFFHOOK -> "1"
            TelephonyManager.EXTRA_STATE_IDLE -> "0"
            else -> return
        }
        val pending = goAsync()
        val app = ctx.applicationContext
        Thread {
            try {
                Bridge.get(app, "x/duck", mapOf("on" to on))
            } finally {
                pending.finish()
            }
        }.start()
    }
}
