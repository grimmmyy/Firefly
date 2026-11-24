package com.firefly.app

import android.content.Context
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/** Talks to the Pi bridge from native code (tile, widget, call ducking),
 *  using the host and pairing token the Flutter app saved. */
object Bridge {
    private fun prefs(ctx: Context) =
        ctx.getSharedPreferences("FlutterSharedPreferences", Context.MODE_PRIVATE)

    fun enabled(ctx: Context, key: String, default: Boolean = true): Boolean =
        prefs(ctx).getBoolean("flutter.$key", default)

    /** Blocking GET of /api/<path>; parsed JSON or null. Call off the main thread. */
    fun get(ctx: Context, path: String, query: Map<String, String> = emptyMap()): JSONObject? {
        val p = prefs(ctx)
        val host = p.getString("flutter.spotcastHost", null) ?: return null
        val token = p.getString("flutter.spotcastToken", null)
        val q = query.entries.joinToString("&") {
            "${it.key}=${URLEncoder.encode(it.value, "UTF-8")}"
        }
        return try {
            val c = URL("http://$host:8090/api/$path" + (if (q.isEmpty()) "" else "?$q"))
                .openConnection() as HttpURLConnection
            c.connectTimeout = 4000
            c.readTimeout = 12000
            if (token != null) c.setRequestProperty("X-Firefly-Token", token)
            try {
                if (c.responseCode != 200) null
                else JSONObject(c.inputStream.bufferedReader().readText())
            } finally {
                c.disconnect()
            }
        } catch (e: Exception) {
            null
        }
    }

    fun async(ctx: Context, path: String, query: Map<String, String> = emptyMap(),
              done: ((JSONObject?) -> Unit)? = null) {
        val app = ctx.applicationContext
        Thread { done?.invoke(get(app, path, query)) }.start()
    }
}
