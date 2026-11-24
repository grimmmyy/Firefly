package com.firefly.app

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.widget.RemoteViews
import org.json.JSONObject

/** Home-screen remote: power, volume down/up, night mode. */
class RemoteWidget : AppWidgetProvider() {
    companion object {
        const val ACTION = "com.firefly.app.WIDGET"
        private val BUTTONS = mapOf(
            R.id.w_power to "power", R.id.w_down to "vol_down",
            R.id.w_up to "vol_up", R.id.w_night to "night")

        fun statusText(j: JSONObject?): String = when {
            j == null -> "Bridge not reachable"
            j.optString("power") != "On" -> "Receiver off"
            else -> (when {
                j.optBoolean("mute") -> "Muted"
                j.isNull("volume_db") -> "On"
                else -> "%.1f dB".format(j.optDouble("volume_db"))
            }) + (if (j.optBoolean("night")) " · Night" else "")
        }

        fun render(ctx: Context, status: String) {
            val mgr = AppWidgetManager.getInstance(ctx)
            val ids = mgr.getAppWidgetIds(ComponentName(ctx, RemoteWidget::class.java))
            if (ids.isEmpty()) return
            val v = RemoteViews(ctx.packageName, R.layout.widget_remote)
            v.setTextViewText(R.id.w_status, status)
            for ((id, action) in BUTTONS) {
                val i = Intent(ctx, RemoteWidget::class.java).setAction(ACTION).putExtra("do", action)
                v.setOnClickPendingIntent(id, PendingIntent.getBroadcast(
                    ctx, id, i, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE))
            }
            v.setOnClickPendingIntent(R.id.w_status, PendingIntent.getActivity(
                ctx, 0, Intent(ctx, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE))
            mgr.updateAppWidget(ids, v)
        }

        fun update(ctx: Context, action: String?) {
            val app = ctx.applicationContext
            val q = if (action == null) emptyMap() else mapOf("do" to action)
            Bridge.async(app, "x/quick", q) { j -> render(app, statusText(j)) }
        }
    }

    override fun onUpdate(ctx: Context, mgr: AppWidgetManager, ids: IntArray) {
        render(ctx, "…")
        update(ctx, null)
    }

    override fun onReceive(ctx: Context, intent: Intent) {
        super.onReceive(ctx, intent)
        if (intent.action != ACTION) return
        val pending = goAsync()
        val app = ctx.applicationContext
        val action = intent.getStringExtra("do") ?: ""
        Thread {
            try {
                render(app, statusText(Bridge.get(app, "x/quick", mapOf("do" to action))))
            } finally {
                pending.finish()
            }
        }.start()
    }
}
