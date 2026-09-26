import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

// Release signing: android/keystore/release.jks + keystore.properties. build.sh
// generates both when absent, so a fresh docker build still produces an
// installable, updatable APK. Keep the keystore: an APK signed with a
// different key won't install over the old one.
val ksProps = Properties().also { p ->
    val f = rootProject.file("keystore/keystore.properties")
    if (f.exists()) f.inputStream().use { p.load(it) }
}

android {
    namespace = "app.cim.family"
    compileSdk = 34

    defaultConfig {
        applicationId = "app.cim.family"
        minSdk = 26
        targetSdk = 34
        versionCode = (System.getenv("CIM_APP_VERSION_CODE") ?: "1").toInt()
        versionName = System.getenv("CIM_APP_VERSION_NAME") ?: "1.0.0"
    }

    signingConfigs {
        create("release") {
            if (ksProps.isNotEmpty()) {
                storeFile = rootProject.file("keystore/" + ksProps.getProperty("storeFile", "release.jks"))
                storePassword = ksProps.getProperty("storePassword")
                keyAlias = ksProps.getProperty("keyAlias")
                keyPassword = ksProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            if (ksProps.isNotEmpty()) signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { compose = true }
    packaging { resources.excludes += "/META-INF/{AL2.0,LGPL2.1}" }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.09.00")
    implementation(composeBom)
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-compose:1.9.2")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.5")
    implementation("androidx.work:work-runtime-ktx:2.9.1")
    implementation("androidx.security:security-crypto:1.1.0-alpha06")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.bouncycastle:bcprov-jdk18on:1.78.1")
    implementation("com.google.zxing:core:3.5.3")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
}
