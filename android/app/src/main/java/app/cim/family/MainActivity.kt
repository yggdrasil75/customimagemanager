package app.cim.family

import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.darkColorScheme
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import app.cim.family.media.PlayerConnection
import app.cim.family.ui.App

/** @brief The one activity; the UI lives in ui/ (AppShell.kt: tabs, back stack, mini player). */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val prefs = Prefs.get(this)
        if (prefs.deviceName.isEmpty()) prefs.deviceName = "phone-" + (Build.MODEL ?: "android").lowercase().replace(Regex("[^a-z0-9]+"), "-").trim('-').take(20)
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(primary = Color(0xFF8B8BF5), background = Color(0xFF101014), surface = Color(0xFF17171D))) {
                Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) { App(prefs) }
            }
        }
    }

    override fun onDestroy() {
        // The player service keeps playing on its own; drop this UI's controller.
        if (isFinishing) PlayerConnection.release()
        super.onDestroy()
    }
}
