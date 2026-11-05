package app.incremind.mobile

import android.app.AlertDialog
import android.content.Intent
import android.content.pm.ShortcutInfo
import android.content.pm.ShortcutManager
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.webkit.CookieManager
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebStorage
import android.webkit.WebView
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.lifecycle.Lifecycle
import androidx.work.WorkManager
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import org.json.JSONObject
import java.security.cert.CertificateExpiredException
import java.util.concurrent.Executors
import javax.net.ssl.SSLException

private enum class Startup(val label: String, val action: String) {
    CHECKING("正在核对设备授权", "正在检查"),
    READY("工作台已连接", "核对设备"),
    UNACTIVATED("扫描管理员发来的邀请，启用这台手机", "扫描邀请"),
    UNREACHABLE("暂时无法连接服务器，请检查网络后重试", "重试连接"),
    TLS_ERROR("无法验证服务器证书，请确认网络和服务器地址", "重试连接"),
    REVOKED("设备授权已失效，请重新扫描邀请", "扫描邀请"),
}

private data class Account(val device: Device, val name: String)
private data class Receipt(val device: String, val user: String, val path: String)
private class NoLocalDevice : Exception()

class MainActivity : ComponentActivity() {
    private lateinit var enrollment: Enrollment
    private lateinit var queue: ShareQueue
    private lateinit var root: LinearLayout
    private lateinit var content: FrameLayout
    private lateinit var loginScreen: ScrollView
    private lateinit var stateLabel: TextView
    private lateinit var notice: TextView
    private lateinit var primary: Button
    private lateinit var rescan: Button
    private lateinit var previousDevice: Button
    private lateinit var pendingRecovery: Button
    private val verifiedOwner by lazy { getSharedPreferences("verified-owner", MODE_PRIVATE) }
    private val sharePrompts by lazy { getSharedPreferences("share-prompts", MODE_PRIVATE) }
    private var web: WorkbenchWebView? = null
    private var webDevice: String? = null
    private var webReady = false
    private var workbenchShown = false
    private var openShareHistory = false
    private var storedDevice: Device? = null
    private var pendingDevice: Device? = null
    private var startup = Startup.CHECKING
    private var account: Account? = null
    private var authAttempt = 0
    private var authBusy = false
    private var scanInFlight = false
    private var waitingInvitation: String? = null
    private var offered = emptySet<String>()
    private var readingUnassigned = false
    private var confirmation: AlertDialog? = null
    private var pendingReceipt: Receipt? = null
    private var fileCallback: ValueCallback<Array<Uri>>? = null

    private val scanner = registerForActivityResult(ScanContract()) { result ->
        scanInFlight = false
        val raw = result.contents ?: return@registerForActivityResult
        val code = invitation.matchEntire(raw)?.groupValues?.get(1)
        when {
            code == null -> showNotice("邀请二维码无效")
            authBusy -> waitingInvitation = code
            else -> activate(code)
        }
    }
    private val chooser = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        val files = WebChromeClient.FileChooserParams.parseResult(result.resultCode, result.data)
        fileCallback?.onReceiveValue(files?.takeIf { account != null && it.all { uri -> uri.scheme == "content" } })
        fileCallback = null
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enrollment = Enrollment(this)
        queue = ShareQueue(this)
        offered = sharePrompts.getStringSet("offered", emptySet()).orEmpty().toSet()
        root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundResource(R.color.ic_launcher_background)
        }
        root.setOnApplyWindowInsetsListener { view, insets ->
            @Suppress("DEPRECATION")
            view.setPadding(insets.systemWindowInsetLeft, insets.systemWindowInsetTop,
                insets.systemWindowInsetRight, insets.systemWindowInsetBottom)
            insets
        }
        pendingRecovery = Button(this, null, android.R.attr.borderlessButtonStyle).apply {
            text = "继续激活"
            visibility = View.GONE
            setOnClickListener { probe(showChecking = true, usePending = true) }
        }
        content = FrameLayout(this)
        loginScreen = layoutInflater.inflate(R.layout.mobile_login, content, false) as ScrollView
        primary = loginScreen.findViewById<Button>(R.id.login_primary).apply {
            setOnClickListener { primaryAction() }
        }
        loginScreen.findViewById<Button>(R.id.login_history).setOnClickListener { showQueue() }
        stateLabel = loginScreen.findViewById(R.id.login_status)
        rescan = loginScreen.findViewById<Button>(R.id.login_rescan).apply {
            setOnClickListener {
                if (startup == Startup.READY || startup == Startup.REVOKED) probe(showChecking = true, usePending = true)
                else scanInvitation()
            }
        }
        previousDevice = loginScreen.findViewById<Button>(R.id.login_previous_device).apply {
            setOnClickListener { probe(showChecking = true, reload = true, usePending = false) }
        }
        notice = loginScreen.findViewById(R.id.login_notice)
        content.addView(loginScreen)
        root.addView(pendingRecovery)
        root.addView(content, LinearLayout.LayoutParams(-1, 0, 1f))
        setContentView(root)
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                val current = web
                if (account != null && current?.canGoBack() == true) current.goBack() else finish()
            }
        })
        render(Startup.CHECKING)
        val local = runCatching { enrollment.current() }
        storedDevice = local.getOrNull()
        pendingDevice = runCatching { enrollment.pending() }.getOrNull()
        if (verifiedOwner.getString("web-user", null) == null) {
            val webUser = verifiedOwner.getString("user", null) ?: storedDevice?.user
            if (webUser != null) verifiedOwner.edit().putString("web-user", webUser).apply()
        }
        when {
            pendingDevice != null || savedInstanceState?.getBoolean("auth-in-flight") == true -> probe(showChecking = true)
            local.isFailure -> render(Startup.REVOKED)
            storedDevice == null -> render(Startup.UNACTIVATED)
            else -> probe(showChecking = true)
        }
        scheduleRenewal(this)
        registerShareHistoryShortcut()
        openShareHistory = savedInstanceState?.getBoolean("open-share-history") ?: false
        if (savedInstanceState == null) handleEntry(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleEntry(intent)
    }

    override fun onSaveInstanceState(outState: Bundle) {
        outState.putBoolean("auth-in-flight", authBusy)
        outState.putBoolean("open-share-history", openShareHistory)
        super.onSaveInstanceState(outState)
    }

    override fun onResume() {
        super.onResume()
        if (!scanInFlight) {
            if (startup == Startup.READY) probe()
            else if (startup == Startup.UNREACHABLE) probe(showChecking = true, reload = true)
        }
        maybeOfferUnassigned()
        dispatchReceipt()
    }

    override fun onPostResume() {
        super.onPostResume()
        root.post(::dispatchShareHistory)
    }

    private fun handleEntry(intent: Intent) {
        if (intent.action == SHARE_HISTORY_ACTION) {
            openShareHistory = true
            dispatchShareHistory()
        } else receiveShare(intent)
    }

    private fun dispatchShareHistory() {
        if (!openShareHistory || !lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)) return
        openShareHistory = false
        showQueue()
    }

    private fun registerShareHistoryShortcut() {
        val manager = getSystemService(ShortcutManager::class.java) ?: return
        if (manager.dynamicShortcuts.any { it.id == SHARE_HISTORY_SHORTCUT }) return
        val intent = Intent(this, MainActivity::class.java).setAction(SHARE_HISTORY_ACTION)
            .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        manager.addDynamicShortcuts(listOf(ShortcutInfo.Builder(this, SHARE_HISTORY_SHORTCUT)
            .setShortLabel("分享记录").setIntent(intent).build()))
    }

    private fun active() = !isDestroyed && !isFinishing

    private fun scanInvitation() {
        if (authBusy || scanInFlight) return
        scanInFlight = true
        scanner.launch(ScanOptions().setDesiredBarcodeFormats(ScanOptions.QR_CODE)
            .setPrompt("扫描 Incremind 邀请").setBeepEnabled(false))
    }

    private fun primaryAction() {
        when (startup) {
            Startup.UNACTIVATED, Startup.REVOKED -> scanInvitation()
            Startup.READY, Startup.UNREACHABLE, Startup.TLS_ERROR -> {
                if (storedDevice == null && pendingDevice == null) scanInvitation()
                else probe(showChecking = true, reload = startup != Startup.READY)
            }
            Startup.CHECKING -> Unit
        }
    }

    private fun render(state: Startup, detail: String = state.label) {
        startup = state
        if (state != Startup.READY) {
            account = null
            confirmation?.dismiss()
        }
        if (state == Startup.REVOKED) verifiedOwner.edit().remove("device").remove("user").apply()
        stateLabel.text = detail
        val failed = state == Startup.UNREACHABLE || state == Startup.TLS_ERROR
        val hasDevice = storedDevice != null || pendingDevice != null
        primary.text = if (failed && !hasDevice) "重新扫码" else state.action
        primary.isEnabled = state != Startup.CHECKING
        primary.alpha = if (primary.isEnabled) 1f else 0.55f
        val resumeActivation = pendingDevice != null && (state == Startup.READY || state == Startup.REVOKED)
        rescan.text = if (resumeActivation) "继续激活" else "重新扫码"
        rescan.visibility = if (failed && hasDevice || resumeActivation) View.VISIBLE else View.GONE
        previousDevice.visibility = if (failed && pendingDevice != null && storedDevice != null) View.VISIBLE else View.GONE
        web?.visibility = if (state == Startup.READY) View.VISIBLE else View.GONE
        updateConnectionScreen()
    }

    private fun updateConnectionScreen() {
        val workbenchVisible = startup == Startup.READY && workbenchShown
        loginScreen.visibility = if (workbenchVisible) View.GONE else View.VISIBLE
        pendingRecovery.visibility = if (workbenchVisible && pendingDevice != null) View.VISIBLE else View.GONE
        web?.importantForAccessibility = if (workbenchVisible) View.IMPORTANT_FOR_ACCESSIBILITY_AUTO
            else View.IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS
        if (workbenchVisible) showNotice("")
    }

    private fun showNotice(message: String) {
        val hidden = loginScreen.visibility == View.GONE
        notice.text = if (hidden) "" else message
        notice.visibility = if (notice.text.isEmpty()) View.GONE else View.VISIBLE
        if (hidden && message.isNotEmpty()) Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
    }

    private fun verify(device: Device): Account {
        val checked = send(mobileOrigin, "/api/v2/devices", device, "application/json", write = null)
        val session = checked.getJSONObject("mobile_session")
        require(checked.getString("device_id") == device.id && session.getString("device_id") == device.id
            && session.getString("user_id") == device.user)
        val caller = send(mobileOrigin, "/api/v2/server/users", device, "application/json", write = null).getJSONObject("caller")
        val name = caller.getString("name").trim()
        require(caller.getString("user_id") == device.user && name.isNotEmpty())
        return Account(device, name)
    }

    private fun probe(showChecking: Boolean = false, reload: Boolean = false, usePending: Boolean = startup != Startup.READY) {
        if (authBusy || scanInFlight) return
        if (startup == Startup.REVOKED && (!usePending || pendingDevice == null)) return
        authenticate(showChecking, reload, activating = usePending && pendingDevice != null) { publish ->
            val pending = if (usePending) enrollment.pending() else null
            if (pending != null) {
                publish(pending)
                enrollment.confirmActivation(pending, ::verify)
            } else {
                val device = enrollment.current() ?: throw NoLocalDevice()
                verify(enrollment.renewIfDue(device))
            }
        }
    }

    private fun activate(code: String) {
        if (authBusy) return
        showNotice("")
        authenticate(showChecking = true, reload = true, activating = true) { publish ->
            val device = enrollment.activate(code)
            publish(device)
            enrollment.confirmActivation(device, ::verify)
        }
    }

    private fun authenticate(showChecking: Boolean, reload: Boolean, activating: Boolean = false,
                             load: ((Device) -> Unit) -> Account) {
        authBusy = true
        val attempt = ++authAttempt
        val previous = startup
        val previousDetail = stateLabel.text.toString()
        val previousAccount = account
        if (showChecking) render(Startup.CHECKING, if (activating) "正在启用设备" else Startup.CHECKING.label)
        authExecutor.execute {
            var candidate: Device? = null
            val result = runCatching { load { candidate = it } }
            val latestPending = runCatching { enrollment.pending() }.getOrNull()
            runOnUiThread {
                if (!active() || authAttempt != attempt) return@runOnUiThread
                authBusy = false
                pendingDevice = latestPending
                val changingDevice = activating || candidate != null
                result.fold({ verified -> ready(verified, reload || candidate != null) }, { error ->
                    when {
                        error is NoLocalDevice -> render(Startup.UNACTIVATED)
                        changingDevice && previousAccount != null -> {
                            ready(previousAccount, reload = false)
                            showNotice("新设备尚未启用，原工作台仍可使用")
                        }
                        changingDevice && candidate == null -> {
                            render(previous, previousDetail)
                            showNotice(when {
                                error is SSLException -> "邀请连接未通过证书校验，请重新扫码"
                                error is ServerFailure && error.status in setOf(400, 401, 403, 409, 410) ->
                                    "邀请无效、已过期或设备未通过验证"
                                else -> "暂时无法完成扫码，请检查网络后重试"
                            })
                        }
                        error is SSLException -> render(Startup.TLS_ERROR, "设备安全连接失败，可重试或重新扫码")
                        changingDevice && (error is CertificateExpiredException || error is ServerFailure && error.status in setOf(401, 403)) ->
                            render(Startup.UNREACHABLE, "新设备授权未通过核验，请重新扫码")
                        error is CertificateExpiredException -> render(Startup.REVOKED, "设备证书已过期，请重新扫描邀请")
                        error is ServerFailure && error.status in setOf(401, 403) -> render(Startup.REVOKED)
                        else -> render(Startup.UNREACHABLE)
                    }
                })
                waitingInvitation?.let { code ->
                    waitingInvitation = null
                    activate(code)
                }
            }
        }
    }

    private fun ready(verified: Account, reload: Boolean) {
        val renewed = storedDevice?.certificate != verified.device.certificate
        val webUser = verifiedOwner.getString("web-user", null)
            ?: verifiedOwner.getString("user", null) ?: storedDevice?.user
        val accountChanged = webUser != verified.device.user
        storedDevice = verified.device
        account = verified
        val identityChanged = webDevice != verified.device.id
        val replace = identityChanged || web == null || reload
        val previousUrl = web?.url?.takeIf { !identityChanged && isWorkbenchOrigin(Uri.parse(it)) }
        if (replace) resetWebView(verified.device.id, accountChanged)
        verifiedOwner.edit().putString("device", verified.device.id).putString("user", verified.device.user)
            .putString("web-user", verified.device.user).apply()
        render(Startup.READY, "正在打开工作台")
        if (replace || reload || renewed) {
            val view = web ?: return
            val destination = previousUrl ?: "$mobileOrigin$WORKBENCH_PATH"
            webReady = false
            WebView.clearClientCertPreferences {
                if (active() && web === view && account?.device?.id == verified.device.id) view.loadUrl(destination)
            }
        }
        shareExecutor.execute { queue.resume(verified.device) }
        maybeOfferUnassigned()
        dispatchReceipt()
    }

    private fun resetWebView(deviceId: String, clearPreviousData: Boolean) {
        web?.let { old ->
            fileCallback?.onReceiveValue(null)
            fileCallback = null
            content.removeView(old)
            old.destroy()
        }
        if (clearPreviousData) {
            WebStorage.getInstance().deleteAllData()
            CookieManager.getInstance().removeAllCookies(null)
        }
        webReady = false
        workbenchShown = false
        lateinit var view: WorkbenchWebView
        fun current() = active() && web === view
        view = WorkbenchWebView(this,
            currentDevice = { account?.device?.takeIf { current() } },
            openExternal = { uri -> if (current()) runCatching { startActivity(Intent(Intent.ACTION_VIEW, uri).addCategory(Intent.CATEGORY_BROWSABLE)) } },
            onUnauthorized = { if (current() && startup == Startup.READY) probe() },
            onFailure = { tls ->
                if (current() && startup == Startup.READY) {
                    webReady = false
                    workbenchShown = false
                    render(if (tls) Startup.TLS_ERROR else Startup.UNREACHABLE)
                }
            },
            onNavigation = { if (current()) webReady = false },
            onReady = {
                if (current() && startup == Startup.READY) {
                    webReady = true
                    workbenchShown = true
                    updateConnectionScreen()
                    dispatchReceipt()
                }
            },
            chooseFile = { callback, parameters -> current() && chooseFile(callback, parameters) })
        if (clearPreviousData) view.clearCache(true)
        web = view
        webDevice = deviceId
        content.addView(view, 0, FrameLayout.LayoutParams(-1, -1))
    }

    private fun chooseFile(callback: ValueCallback<Array<Uri>>, params: WebChromeClient.FileChooserParams): Boolean {
        fileCallback?.onReceiveValue(null)
        fileCallback = callback
        return try { chooser.launch(params.createIntent()); true }
        catch (_: Exception) { fileCallback = null; false }
    }

    private fun receiveShare(intent: Intent) {
        if (intent.action != Intent.ACTION_SEND && intent.action != Intent.ACTION_SEND_MULTIPLE) return
        val owner = storedDevice?.takeIf {
            startup != Startup.REVOKED && it.certificate.notAfter.time > System.currentTimeMillis() &&
                verifiedOwner.getString("device", null) == it.id && verifiedOwner.getString("user", null) == it.user
        }
        showNotice("正在保存分享内容")
        shareExecutor.execute {
            val saved = runCatching { queue.stage(intent, owner) }
            runOnUiThread {
                if (!active()) return@runOnUiThread
                showNotice(when {
                    saved.isFailure -> "分享未保存，请检查附件、大小或队列"
                    owner == null -> "分享已保存在本机，确认账号后可发送"
                    else -> "已加入上传队列"
                })
                saved.getOrNull()?.let { id -> if (owner != null) watchShare(id) else maybeOfferUnassigned() }
            }
        }
    }

    private fun maybeOfferUnassigned(requestedId: String? = null) {
        val verified = account ?: return
        if (!active() || readingUnassigned || confirmation != null || !lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)) return
        readingUnassigned = true
        shareExecutor.execute {
            val unassigned = queue.list().filter { it.optString("state") == "unassigned" }.map { it.getString("id") }
            val ids = unassigned.filter { requestedId == null || it == requestedId }
            runOnUiThread {
                readingUnassigned = false
                if (!active() || account?.device?.id != verified.device.id || !lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)) return@runOnUiThread
                if (ids.isEmpty()) { if (requestedId != null) showNotice("该分享已处理"); return@runOnUiThread }
                if (requestedId == null && ids.all(offered::contains)) return@runOnUiThread
                offered = offered.intersect(unassigned.toSet()) + ids
                sharePrompts.edit().putStringSet("offered", offered).apply()
                confirmation = AlertDialog.Builder(this).setTitle("发送本机待分配分享")
                    .setMessage("将 ${ids.size} 条分享发送到 ${verified.name} 的收件箱？")
                    .setNegativeButton("暂不发送", null)
                    .setPositiveButton("确认发送") { _, _ -> bindShares(verified, ids) }
                    .setOnDismissListener { confirmation = null }.show()
            }
        }
    }

    private fun bindShares(verified: Account, ids: List<String>) {
        if (account?.device?.id != verified.device.id) return
        shareExecutor.execute {
            val bound = runCatching { queue.bindUnassigned(verified.device, ids) }
            runOnUiThread {
                if (!active() || account?.device?.id != verified.device.id) return@runOnUiThread
                bound.fold({ rows ->
                    showNotice(if (rows.isEmpty()) "没有可发送的分享" else "${rows.size} 条分享已加入上传队列")
                    rows.forEach(::watchShare)
                }, { showNotice("本次分享未发送，请核对设备后重试") })
            }
        }
    }

    private fun watchShare(id: String) {
        val status = WorkManager.getInstance(this).getWorkInfosForUniqueWorkLiveData("share-$id")
        var reading = false
        status.observe(this) { rows ->
            if (reading || rows.isEmpty() || rows.any { !it.state.isFinished }) return@observe
            reading = true
            shareExecutor.execute {
                val row = runCatching { queue.read(id) }.getOrNull()
                runOnUiThread {
                    reading = false
                    if (row?.optString("state") == "pending") return@runOnUiThread
                    status.removeObservers(this)
                    if (row == null) return@runOnUiThread
                    val owner = storedDevice ?: return@runOnUiThread
                    if (!active() || !owned(row, owner)) return@runOnUiThread
                    showNotice(shareState(row.optString("state")))
                    val receipts = row.optJSONArray("receipts") ?: return@runOnUiThread
                    if (receipts.length() == 1) requestReceipt(row, receipts.getString(0), owner)
                }
            }
        }
    }

    private fun owned(row: JSONObject, device: Device) = row.optString("device") == device.id && row.optString("user") == device.user

    private fun requestReceipt(row: JSONObject, path: String, owner: Device? = account?.device) {
        if (owner == null || !owned(row, owner) || !RECEIPT_PATH.matches(path)) return
        pendingReceipt = Receipt(owner.id, owner.user, path)
        dispatchReceipt()
    }

    private fun dispatchReceipt() {
        val verified = account ?: return
        val receipt = pendingReceipt ?: return
        if (receipt.device != verified.device.id || receipt.user != verified.device.user) { pendingReceipt = null; return }
        if (!active() || !webReady || !lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)) return
        pendingReceipt = null
        web?.loadUrl("$mobileOrigin${receipt.path}")
    }

    private fun shareState(state: String) = when (state) {
        "unassigned" -> "保存在本机 · 待确认账号"
        "pending" -> "待上传 · 联网后继续"
        "received" -> "原件已保存 · 整理状态见回执"
        "authorization" -> "设备授权已失效 · 原分享未转移"
        "check" -> "结果待核对 · 请查看工作台"
        "connection" -> "服务器连接未通过验证"
        else -> "服务器拒绝了这次分享"
    }

    private fun showQueue() {
        shareExecutor.execute {
            val rows = queue.list()
            runOnUiThread {
                if (!active() || !lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)) return@runOnUiThread
                if (rows.isEmpty()) { showNotice("暂无分享记录"); return@runOnUiThread }
                AlertDialog.Builder(this).setTitle("分享记录")
                    .setItems(rows.map { shareState(it.optString("state")) }.toTypedArray()) { _, index -> showShare(rows[index]) }.show()
            }
        }
    }

    private fun showShare(row: JSONObject) {
        val state = row.optString("state")
        val verified = account?.takeIf { owned(row, it.device) }
        if (state == "received" && verified != null) {
            val receipts = row.getJSONArray("receipts")
            AlertDialog.Builder(this).setTitle("查看原件回执").setItems(Array(receipts.length()) { "原件 ${it + 1}" }) { _, index ->
                requestReceipt(row, receipts.getString(index))
            }.show()
            return
        }
        val dialog = AlertDialog.Builder(this).setMessage(shareState(state)).setNegativeButton("关闭", null)
        if (state != "pending") dialog.setNeutralButton("清除本机记录") { _, _ -> shareExecutor.execute { queue.discard(row.getString("id")) } }
        if (state == "unassigned" && account != null) dialog.setPositiveButton("确认账号并发送") { _, _ -> maybeOfferUnassigned(row.getString("id")) }
        if (state == "check" && verified != null) dialog.setPositiveButton("打开工作台") { _, _ ->
            if (account?.device?.id == verified.device.id && webReady) web?.loadUrl("$mobileOrigin$WORKBENCH_PATH")
        }
        if (state == "connection" && verified != null) dialog.setPositiveButton("重试本次分享") { _, _ ->
            retryShare(row.getString("id"), verified)
        }
        dialog.show()
    }

    private fun retryShare(id: String, verified: Account) {
        if (account?.device?.id != verified.device.id) return
        showNotice("正在重试本次分享")
        shareExecutor.execute {
            val queued = runCatching {
                queue.retryConnection(id, verified.device)
                queue.read(id).optString("state") in setOf("pending", "received")
            }.getOrDefault(false)
            runOnUiThread {
                if (!active() || account?.device?.id != verified.device.id) return@runOnUiThread
                showNotice(if (queued) "已加入上传队列" else "本次分享未重试，请查看分享记录")
                if (queued) watchShare(id)
            }
        }
    }

    override fun onDestroy() {
        ++authAttempt
        confirmation?.dismiss()
        fileCallback?.onReceiveValue(null)
        web?.destroy()
        super.onDestroy()
    }

    companion object {
        private val authExecutor = Executors.newSingleThreadExecutor()
        private val shareExecutor = Executors.newSingleThreadExecutor()
        private const val SHARE_HISTORY_ACTION = "app.incremind.mobile.SHOW_SHARE_HISTORY"
        private const val SHARE_HISTORY_SHORTCUT = "share-history"
        private const val WORKBENCH_PATH = "/#view=workbench&project_id=inbox"
        private val RECEIPT_PATH = Regex("/#view=workbench&project_id=${opaqueId.pattern}&thread_id=${opaqueId.pattern}&turn_id=${opaqueId.pattern}")
    }
}
