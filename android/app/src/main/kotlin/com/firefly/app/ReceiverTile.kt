package com.firefly.app

import android.os.Build
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService

/** Quick-settings tile: shows whether the receiver is on; tap toggles power. */
class ReceiverTile : TileService() {
    override fun onStartListening() {
        refresh(null)
    }

    override fun onClick() {
        qsTile?.apply { state = Tile.STATE_UNAVAILABLE; updateTile() }
        refresh("power")
    }

    private fun refresh(action: String?) {
        val q = if (action == null) emptyMap() else mapOf("do" to action)
        Bridge.async(this, "x/quick", q) { j ->
            val t = qsTile ?: return@async
            t.label = "Receiver"
            if (j == null) {
                t.state = Tile.STATE_UNAVAILABLE
            } else {
                val on = j.optString("power") == "On"
                t.state = if (on) Tile.STATE_ACTIVE else Tile.STATE_INACTIVE
                if (Build.VERSION.SDK_INT >= 29) {
                    t.subtitle = when {
                        !on -> "Off"
                        j.isNull("volume_db") -> "On"
                        else -> "%.1f dB".format(j.optDouble("volume_db"))
                    }
                }
            }
            t.updateTile()
        }
    }
}
