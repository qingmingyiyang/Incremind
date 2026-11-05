package app.incremind.mobile

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.OpenableColumns
import android.util.AtomicFile
import androidx.work.Constraints
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.security.cert.CertificateExpiredException
import java.util.UUID
import javax.net.ssl.SSLHandshakeException

private const val MAX_BYTES = 100L * 1024 * 1024
private const val MAX_PENDING = 16

internal class ShareQueue(private val context: Context) {
    private val root = File(context.noBackupFilesDir, "incoming-shares").apply { mkdirs() }

    fun stage(intent: Intent, device: Device?): String = synchronized(lock) {
        require(intent.action == Intent.ACTION_SEND || intent.action == Intent.ACTION_SEND_MULTIPLE)
        clearIncompleteStaging()
        require(list().count { it.getString("state") != "received" } < MAX_PENDING) {
            "share_queue_full"
        }
        val id = UUID.randomUUID().toString()
        val pending = File(root, "$id.incoming")
        require(pending.mkdir())
        try {
            val text = intent.getCharSequenceExtra(Intent.EXTRA_TEXT)?.toString().orEmpty()
            require(text.length <= 60_000)
            val streams = receivedFiles(intent)
            require(streams.size <= 16 && (text.isNotBlank() || streams.isNotEmpty()))
            var total = 0L
            val files = JSONArray()
            streams.forEachIndexed { index, uri ->
                require(uri.scheme == "content")
                val name = context.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)
                    ?.use { if (it.moveToFirst()) it.getString(0) else null }.orEmpty()
                    .replace(Regex("[\\p{Cntrl}\\\"/\\\\]"), "_").trim().take(180).ifEmpty { "附件-$index" }
                val type = context.contentResolver.getType(uri).orEmpty()
                    .takeIf { it.matches(Regex("[a-zA-Z0-9.+_-]+/[a-zA-Z0-9.+_-]+")) } ?: "application/octet-stream"
                context.contentResolver.openInputStream(uri)?.use { input ->
                    File(pending, "$index.part").outputStream().use { output ->
                        val buffer = ByteArray(32 * 1024)
                        while (true) {
                            val count = input.read(buffer)
                            if (count < 0) break
                            total += count
                            require(total <= MAX_BYTES) { "share_too_large" }
                            output.write(buffer, 0, count)
                        }
                        output.fd.sync()
                    }
                } ?: throw IOException("share_unreadable")
                files.put(JSONObject().put("name", name).put("type", type))
            }
            write(pending, JSONObject().put("id", id).put("user", device?.user.orEmpty())
                .put("device", device?.id.orEmpty()).put("text", text).put("files", files)
                .put("state", if (device == null) "unassigned" else "pending"))
            check(pending.renameTo(directory(id)))
            if (device != null) schedule(id)
            id
        } finally {
            if (pending.isDirectory) pending.deleteRecursively()
        }
    }

    fun bindUnassigned(device: Device, ids: List<String>): List<String> = synchronized(lock) {
        val directories = ids.distinct().map(::directory)
        val enrollment = Enrollment(context)
        fun requireCurrent() {
            val current = enrollment.current()
            require(current != null && current.id == device.id && current.user == device.user && current.alias == device.alias) {
                "share_device_changed"
            }
            current.certificate.checkValidity()
        }
        requireCurrent()
        buildList {
            for (dir in directories) {
                if (!dir.isDirectory) continue
                val row = read(dir.name)
                if (row.getString("state") != "unassigned" || row.getString("user").isNotEmpty()
                    || row.getString("device").isNotEmpty()) continue
                requireCurrent()
                write(dir, row.put("user", device.user).put("device", device.id).put("state", "pending"))
                schedule(dir.name)
                add(dir.name)
            }
        }
    }

    @Suppress("DEPRECATION")
    private fun receivedFiles(intent: Intent): Set<Uri> = buildSet {
        intent.clipData?.let { clip -> repeat(clip.itemCount) { clip.getItemAt(it).uri?.let(::add) } }
        if (intent.action == Intent.ACTION_SEND_MULTIPLE) {
            intent.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM)?.let(::addAll)
        } else intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM)?.let(::add)
    }

    fun directory(id: String): File {
        require(UUID.fromString(id).toString() == id)
        return File(root, id)
    }

    fun read(id: String): JSONObject = synchronized(lock) {
        JSONObject(AtomicFile(File(directory(id), "share.json")).readFully().decodeToString())
    }

    private fun write(dir: File, row: JSONObject) {
        val file = AtomicFile(File(dir, "share.json"))
        val stream = file.startWrite()
        try {
            stream.write(row.toString().toByteArray())
            file.finishWrite(stream)
        } catch (failure: Exception) {
            file.failWrite(stream)
            throw failure
        }
    }

    fun mark(id: String, row: JSONObject, state: String, receipts: JSONArray? = null) {
        if (receipts == null) {
            write(directory(id), row.put("state", state))
            return
        }
        val clean = JSONObject().put("id", id).put("user", row.getString("user"))
            .put("device", row.getString("device")).put("state", state).put("receipts", receipts)
        write(directory(id), clean)
        clearAttachments(directory(id))
        list().filter { it.getString("state") == "received" }.drop(24)
            .forEach { directory(it.getString("id")).deleteRecursively() }
    }

    private fun schedule(id: String, policy: ExistingWorkPolicy = ExistingWorkPolicy.KEEP) {
        val request = OneTimeWorkRequestBuilder<ShareWorker>()
            .setInputData(workDataOf("share_id" to id))
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork("share-$id", policy, request)
    }

    fun list(): List<JSONObject> = synchronized(lock) {
        root.listFiles().orEmpty()
            .filter { it.isDirectory && runCatching { UUID.fromString(it.name).toString() == it.name }.getOrDefault(false) }
            .sortedByDescending(File::lastModified).mapNotNull { runCatching { read(it.name) }.getOrNull() }
    }

    fun discard(id: String) = synchronized(lock) {
        val dir = directory(id)
        if (!dir.isDirectory) return@synchronized
        val state = read(id).getString("state")
        if (state != "pending" && id !in inFlight) check(dir.deleteRecursively())
    }

    fun retryConnection(id: String, device: Device) = synchronized(lock) {
        val row = read(id)
        if (id !in inFlight && row.getString("state") == "connection"
            && row.getString("device") == device.id && row.getString("user") == device.user) {
            mark(id, row, "pending")
            // 上传已经退出，但 WorkManager 可能还没记录上一次失败的终态。
            schedule(id, ExistingWorkPolicy.REPLACE)
        }
    }

    fun resume(device: Device) = synchronized(lock) {
        clearIncompleteStaging()
        list().forEach { row ->
            val id = row.getString("id")
            when (row.getString("state")) {
                "pending" -> if (row.getString("device") == device.id && row.getString("user") == device.user) schedule(id)
                    else if (id !in inFlight) mark(id, row, "authorization")
                "received" -> clearAttachments(directory(id))
            }
        }
    }

    private fun clearIncompleteStaging() {
        // 只有已经移入正式目录的记录才可在进程重启后恢复。
        root.listFiles().orEmpty().filter { it.isDirectory && it.name.endsWith(".incoming") }
            .forEach(File::deleteRecursively)
    }

    private fun clearAttachments(dir: File) {
        dir.listFiles().orEmpty().filter { it.name.endsWith(".part") }.forEach(File::delete)
    }

    companion object {
        internal val lock = Any()
        internal val inFlight = mutableSetOf<String>()
    }
}

class ShareWorker(context: Context, params: WorkerParameters) : Worker(context, params) {
    override fun doWork(): Result {
        val id = inputData.getString("share_id") ?: return Result.failure()
        val queue = ShareQueue(applicationContext)
        val row = synchronized(ShareQueue.lock) {
            if (!queue.directory(id).isDirectory) return Result.success()
            val pending = queue.read(id)
            if (pending.getString("state") != "pending") return Result.success()
            if (!ShareQueue.inFlight.add(id)) return Result.retry()
            pending
        }
        return try {
            upload(queue, id, row)
        } finally {
            synchronized(ShareQueue.lock) { ShareQueue.inFlight.remove(id) }
        }
    }

    private fun upload(queue: ShareQueue, id: String, row: JSONObject): Result {
        fun mark(state: String, receipts: JSONArray? = null) = synchronized(ShareQueue.lock) {
            queue.mark(id, row, state, receipts)
        }
        val enrollment = Enrollment(applicationContext)
        val device = enrollment.current()
        if (device == null || row.getString("device") != device.id || row.getString("user") != device.user) {
            mark("authorization")
            return Result.failure()
        }
        return try {
            val current = enrollment.renewIfDue(device)
            val boundary = UUID.randomUUID().toString()
            val reply = send(mobileOrigin, "/api/v2/workbench/share", current, "multipart/form-data; boundary=$boundary", id) { output ->
                fun ascii(value: String) = output.write(value.toByteArray(Charsets.UTF_8))
                ascii("--$boundary\r\nContent-Disposition: form-data; name=\"project_id\"\r\n\r\ninbox\r\n")
                ascii("--$boundary\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n${row.getString("text")}\r\n")
                val files = row.getJSONArray("files")
                for (index in 0 until files.length()) {
                    val file = files.getJSONObject(index)
                    ascii("--$boundary\r\nContent-Disposition: form-data; name=\"files\"; filename=\"${file.getString("name")}\"\r\nContent-Type: ${file.getString("type")}\r\n\r\n")
                    File(queue.directory(id), "$index.part").inputStream().use { it.copyTo(output) }
                    ascii("\r\n")
                }
                ascii("--$boundary--\r\n")
            }
            val results = reply.getJSONArray("results")
            val receipts = JSONArray()
            for (index in 0 until results.length()) {
                val result = results.getJSONObject(index)
                val project = result.getString("project_id")
                val thread = result.getString("thread_id")
                val turn = result.getJSONObject("turn").getString("id")
                require(listOf(project, thread, turn).all(opaqueId::matches))
                receipts.put("/#view=workbench&project_id=$project&thread_id=$thread&turn_id=$turn")
            }
            require(receipts.length() > 0)
            mark("received", receipts)
            Result.success()
        } catch (failure: ServerFailure) {
            when (failure.status) {
                401, 403 -> mark("authorization")
                409 -> mark("check")
                408, 429, in 500..599 -> return Result.retry()
                else -> mark("rejected")
            }
            Result.failure()
        } catch (_: CertificateExpiredException) {
            mark("authorization")
            Result.failure()
        } catch (_: SSLHandshakeException) {
            mark("connection")
            Result.failure()
        } catch (_: IOException) {
            Result.retry()
        } catch (_: Exception) {
            mark("check")
            Result.failure()
        }
    }
}
