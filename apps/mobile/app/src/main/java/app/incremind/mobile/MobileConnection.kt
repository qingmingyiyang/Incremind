package app.incremind.mobile

import android.content.Context
import android.os.Build
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import android.util.Log
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.OutputStream
import java.net.Socket
import java.net.URL
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.Principal
import java.security.PrivateKey
import java.security.cert.CertificateFactory
import java.security.cert.CertificateExpiredException
import java.security.cert.X509Certificate
import java.security.spec.ECGenParameterSpec
import java.util.UUID
import java.util.concurrent.TimeUnit
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLContext
import javax.net.ssl.X509KeyManager

internal val bootstrapOrigin = URL(BuildConfig.SERVER_ORIGIN)
internal val mobileOrigin = URL("${BuildConfig.SERVER_ORIGIN}:8443")
internal val invitation = Regex("incremind-mobile:v1:([A-Za-z0-9_-]{43})")
internal val opaqueId = Regex("[A-Za-z0-9][A-Za-z0-9._~-]{0,127}")

internal class ServerFailure(val status: Int) : IOException("HTTP $status")
internal data class Device(val id: String, val user: String, val alias: String, val certificate: X509Certificate) {
    fun privateKey(): PrivateKey = keyStore().getKey(alias, null) as? PrivateKey ?: error("device_key_missing")
}

private fun keyStore(): KeyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
private fun certificate(pem: String): X509Certificate = CertificateFactory.getInstance("X.509")
    .generateCertificate(pem.byteInputStream()) as X509Certificate

internal class Enrollment(context: Context) {
    private val settings = context.getSharedPreferences("mobile-device", Context.MODE_PRIVATE)

    private fun readDevice(raw: String): Device? {
        val row = JSONObject(raw)
        val alias = row.getString("alias")
        if (!keyStore().containsAlias(alias)) return null
        return Device(row.getString("device_id"), row.getString("user_id"), alias, certificate(row.getString("certificate")))
    }

    fun current(): Device? = settings.getString("device", null)?.let(::readDevice)

    fun pending(): Device? = settings.getString("pending-device", null)?.let(::readDevice)

    private fun previousAlias(slot: String): String? = settings.getString(slot, null)?.let { raw ->
        try { JSONObject(raw).optString("alias").takeIf { it.startsWith("incremind-device-") } }
        catch (_: JSONException) { null }
    }

    private fun retire(alias: String) {
        runCatching { keyStore().deleteEntry(alias) }.onFailure {
            Log.w("Incremind", "Unused device key cleanup failed")
        }
    }

    private fun validate(row: JSONObject, alias: String): Device {
        val id = row.getString("device_id")
        val user = row.getString("user_id")
        require(opaqueId.matches(id) && opaqueId.matches(user))
        val cert = certificate(row.getString("certificate"))
        cert.checkValidity()
        require(cert.publicKey == keyStore().getCertificate(alias).publicKey)
        row.put("alias", alias)
        return Device(id, user, alias, cert)
    }

    fun activate(code: String): Device = synchronized(lock) {
        val challenge = jsonPost(bootstrapOrigin, "/api/v2/mobile/challenge", JSONObject().put("code", code))
        val challengeBytes = Base64.decode(challenge.getString("challenge"), Base64.URL_SAFE or Base64.NO_WRAP)
        require(challengeBytes.size in 16..128)
        val alias = "incremind-device-${UUID.randomUUID()}"
        val previous = previousAlias("pending-device")
        val generator = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, "AndroidKeyStore")
        generator.initialize(KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_SIGN)
            .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
            // TLS 的委托签名可使用已经计算好的摘要。
            .setDigests(KeyProperties.DIGEST_NONE, KeyProperties.DIGEST_SHA256)
            .setAttestationChallenge(challengeBytes).build())
        var persisted = false
        try {
            generator.generateKeyPair()
            val chain = keyStore().getCertificateChain(alias)
            require(chain != null && chain.size >= 2)
            val attestation = JSONArray(chain.map { Base64.encodeToString(it.encoded, Base64.NO_WRAP) })
            val response = jsonPost(bootstrapOrigin, "/api/v2/mobile/activate", JSONObject().put("code", code)
                .put("challenge_id", challenge.getString("challenge_id"))
                .put("device_name", "Android ${Build.MODEL}".take(80))
                .put("attestation_chain", attestation))
            val device = validate(response, alias)
            check(settings.edit().putString("pending-device", response.toString()).commit())
            persisted = true
            previous?.let(::retire)
            device
        } finally {
            if (!persisted) retire(alias)
        }
    }

    fun <T> confirmActivation(device: Device, verify: (Device) -> T): T = synchronized(lock) {
        val raw = checkNotNull(settings.getString("pending-device", null))
        val candidate = checkNotNull(readDevice(raw))
        check(candidate.alias == device.alias)
        candidate.certificate.checkValidity()
        val result = verify(candidate)
        val oldAlias = previousAlias("device")
        check(settings.edit().putString("device", raw).remove("pending-device").commit())
        oldAlias?.takeIf { it != candidate.alias }?.let(::retire)
        result
    }

    fun renewIfDue(device: Device): Device = synchronized(lock) {
        val latest = current()?.takeIf { it.id == device.id } ?: throw ServerFailure(401)
        if (latest.certificate.notAfter.time - System.currentTimeMillis() > 14L * 24 * 60 * 60 * 1000) return latest
        latest.certificate.checkValidity()
        val response = jsonPost(mobileOrigin, "/api/v2/mobile/renew", JSONObject(), latest)
        require(response.getString("device_id") == latest.id && response.getString("user_id") == latest.user)
        val renewed = validate(response, latest.alias)
        check(settings.edit().putString("device", response.toString()).commit())
        renewed
    }

    companion object { private val lock = Any() }
}

private class DeviceKeyManager(private val device: Device) : X509KeyManager {
    override fun chooseClientAlias(types: Array<out String>, issuers: Array<out Principal>?, socket: Socket?) =
        device.alias.takeIf { types.any { type -> type.startsWith("EC") } }
    override fun getClientAliases(type: String, issuers: Array<out Principal>?) =
        arrayOf(device.alias).takeIf { type.startsWith("EC") }
    override fun getPrivateKey(alias: String?) = if (alias == device.alias) device.privateKey() else null
    override fun getCertificateChain(alias: String?) = if (alias == device.alias) arrayOf(device.certificate) else null
    override fun chooseServerAlias(type: String?, issuers: Array<out Principal>?, socket: Socket?) = null
    override fun getServerAliases(type: String?, issuers: Array<out Principal>?) = null
}

internal fun send(origin: URL, path: String, device: Device? = null, contentType: String,
                  requestId: String? = null, write: ((OutputStream) -> Unit)?): JSONObject {
    require(path.startsWith("/api/") && (device == null && origin === bootstrapOrigin || device != null && origin === mobileOrigin))
    val connection = URL(origin, path).openConnection() as HttpsURLConnection
    try {
        if (device != null) connection.sslSocketFactory = SSLContext.getInstance("TLS").apply {
            init(arrayOf(DeviceKeyManager(device)), null, null)
        }.socketFactory
        connection.instanceFollowRedirects = false
        connection.useCaches = false
        connection.connectTimeout = 15_000
        connection.readTimeout = 60_000
        connection.requestMethod = if (write == null) "GET" else "POST"
        if (write != null) {
            connection.doOutput = true
            connection.setRequestProperty("Content-Type", contentType)
        }
        connection.setRequestProperty("Accept", "application/json")
        if (requestId != null) {
            connection.setRequestProperty("Idempotency-Key", requestId)
            connection.setChunkedStreamingMode(32 * 1024)
        }
        if (write != null) connection.outputStream.use(write)
        val status = connection.responseCode
        if (status !in 200..299) throw ServerFailure(status)
        val response = ByteArrayOutputStream()
        connection.inputStream.use { input ->
            val buffer = ByteArray(8192)
            while (true) {
                val count = input.read(buffer)
                if (count == -1) break
                require(response.size() + count <= 1024 * 1024)
                response.write(buffer, 0, count)
            }
        }
        return JSONObject(response.toString(Charsets.UTF_8.name()))
    } finally {
        connection.disconnect()
    }
}

private fun jsonPost(origin: URL, path: String, body: JSONObject, device: Device? = null) =
    send(origin, path, device, "application/json") { it.write(body.toString().toByteArray()) }

internal fun scheduleRenewal(context: Context) {
    val renewal = PeriodicWorkRequestBuilder<RenewalWorker>(1, TimeUnit.DAYS)
        .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()).build()
    WorkManager.getInstance(context).enqueueUniquePeriodicWork("device-renewal", ExistingPeriodicWorkPolicy.KEEP, renewal)
}

class RenewalWorker(context: Context, params: WorkerParameters) : Worker(context, params) {
    override fun doWork(): Result {
        val enrollment = Enrollment(applicationContext)
        val device = enrollment.current() ?: return Result.success()
        return try {
            enrollment.renewIfDue(device)
            Result.success()
        } catch (_: CertificateExpiredException) {
            Result.failure()
        } catch (failure: ServerFailure) {
            if (failure.status in 500..599 || failure.status == 429) Result.retry() else Result.failure()
        } catch (_: IOException) {
            Result.retry()
        }
    }
}
