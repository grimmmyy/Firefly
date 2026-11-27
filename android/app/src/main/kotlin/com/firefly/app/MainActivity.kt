package com.firefly.app

import android.Manifest
import android.content.pm.PackageManager
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
    private var pendingResult: MethodChannel.Result? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "firefly/native")
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "phonePermission" -> {
                        if (checkSelfPermission(Manifest.permission.READ_PHONE_STATE) ==
                            PackageManager.PERMISSION_GRANTED) {
                            result.success(true)
                        } else {
                            pendingResult = result
                            requestPermissions(arrayOf(Manifest.permission.READ_PHONE_STATE), 41)
                        }
                    }
                    "refreshWidget" -> {
                        RemoteWidget.update(this, null)
                        result.success(true)
                    }
                    else -> result.notImplemented()
                }
            }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 41) {
            pendingResult?.success(
                grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED)
            pendingResult = null
        }
    }
}
