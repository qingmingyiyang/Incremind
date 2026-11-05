import java.net.URI

plugins { id("com.android.application") version "9.0.1" }

val serverOrigin = providers.gradleProperty("incremindOrigin").orElse("https://example.invalid").get()
val endpoint = URI(serverOrigin)
require(endpoint.scheme == "https" && endpoint.host != null && endpoint.rawAuthority == endpoint.host
    && endpoint.rawPath.isEmpty() && endpoint.rawQuery == null && endpoint.rawFragment == null) {
    "incremindOrigin must be an HTTPS host without a port, path, or credentials"
}
val signingStore = providers.environmentVariable("INCREMIND_ANDROID_SIGNING_STORE").orNull
val signingPassword = providers.environmentVariable("INCREMIND_ANDROID_SIGNING_PASSWORD").orNull
val signingAlias = providers.environmentVariable("INCREMIND_ANDROID_SIGNING_ALIAS").orNull
    ?.takeIf { it.isNotBlank() } ?: "incremind"
val releaseBuildDir = providers.gradleProperty("incremindReleaseBuildDir")
if (releaseBuildDir.isPresent) layout.buildDirectory.set(file(releaseBuildDir.get()))

android {
    namespace = "app.incremind.mobile"
    compileSdk = 36
    defaultConfig {
        applicationId = "app.incremind.mobile"
        minSdk = 28
        targetSdk = 36
        versionCode = providers.gradleProperty("incremindVersionCode").get().toInt()
        versionName = providers.gradleProperty("incremindVersionName").get()
        buildConfigField("String", "SERVER_ORIGIN", "\"$serverOrigin\"")
    }
    buildFeatures { buildConfig = true }
    signingConfigs {
        if (signingStore != null && signingPassword != null) create("privateRelease") {
            storeFile = file(signingStore)
            storePassword = signingPassword
            keyAlias = signingAlias
            keyPassword = signingPassword
        }
    }
    buildTypes {
        getByName("release") { signingConfig = signingConfigs.findByName("privateRelease") }
    }
}

tasks.register("privateReleaseApk") {
    require(signingStore != null && signingPassword != null && endpoint.host != "example.invalid") {
        "A server origin and private release signing credentials are required"
    }
    dependsOn("assembleRelease")
}

dependencies {
    implementation("androidx.activity:activity:1.10.1")
    implementation("androidx.work:work-runtime:2.10.1")
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
}
