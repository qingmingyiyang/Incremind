package app.incremind.mobile

import android.content.Context
import android.net.Uri
import android.net.http.SslError
import android.graphics.Bitmap
import android.webkit.ClientCertRequest
import android.webkit.SslErrorHandler
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import java.io.ByteArrayInputStream

internal fun isWorkbenchOrigin(uri: Uri): Boolean = uri.scheme == "https" && uri.host == mobileOrigin.host &&
    uri.port == mobileOrigin.port && uri.userInfo == null

@Suppress("SetJavaScriptEnabled")
internal class WorkbenchWebView(
    context: Context,
    currentDevice: () -> Device?,
    openExternal: (Uri) -> Unit,
    onUnauthorized: () -> Unit,
    onFailure: (Boolean) -> Unit,
    onNavigation: () -> Unit,
    onReady: () -> Unit,
    chooseFile: (ValueCallback<Array<Uri>>, WebChromeClient.FileChooserParams) -> Boolean,
) : WebView(context) {
    init {
        settings.javaScriptEnabled = true
        settings.domStorageEnabled = true
        settings.allowFileAccess = false
        settings.allowContentAccess = false
        settings.mixedContentMode = android.webkit.WebSettings.MIXED_CONTENT_NEVER_ALLOW
        settings.userAgentString = settings.userAgentString + " IncremindAndroid/1"
        webViewClient = object : WebViewClient() {
            private var loadingUrl: String? = null
            private var mainFrameFailed = false

            private fun matchesLoad(url: String) = url.substringBefore('#') == loadingUrl

            override fun onPageStarted(view: WebView, url: String, favicon: Bitmap?) {
                loadingUrl = url.substringBefore('#')
                mainFrameFailed = false
                onNavigation()
            }

            override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? {
                if (isWorkbenchOrigin(request.url) || request.url.scheme in setOf("data", "blob", "about")) return null
                return WebResourceResponse("text/plain", "UTF-8", 403, "Forbidden", emptyMap(), ByteArrayInputStream(ByteArray(0)))
            }

            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                if (isWorkbenchOrigin(request.url)) return false
                if (request.isForMainFrame && request.hasGesture() && request.url.scheme == "https") openExternal(request.url)
                return true
            }

            override fun onReceivedClientCertRequest(view: WebView, request: ClientCertRequest) {
                val device = currentDevice()
                if (device != null && request.host == mobileOrigin.host && request.port == mobileOrigin.port) {
                    runCatching { request.proceed(device.privateKey(), arrayOf(device.certificate)) }.onFailure { request.ignore() }
                } else request.ignore()
            }

            override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
                handler.cancel()
                if (matchesLoad(error.url) && isWorkbenchOrigin(Uri.parse(error.url))) {
                    mainFrameFailed = true
                    onFailure(true)
                }
            }

            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame && matchesLoad(request.url.toString()) && isWorkbenchOrigin(request.url)) {
                    mainFrameFailed = true
                    onFailure(error.errorCode == ERROR_FAILED_SSL_HANDSHAKE)
                }
            }

            override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, errorResponse: WebResourceResponse) {
                if (!isWorkbenchOrigin(request.url)) return
                val status = errorResponse.statusCode
                val mainFailure = request.isForMainFrame && matchesLoad(request.url.toString())
                if (mainFailure) mainFrameFailed = true
                if (status == 401 || status == 403 && request.url.path == "/api/v2/devices") onUnauthorized()
                else if (mainFailure) onFailure(false)
            }

            override fun onPageCommitVisible(view: WebView, url: String) {
                if (!mainFrameFailed && matchesLoad(url) && isWorkbenchOrigin(Uri.parse(url))) onReady()
            }
        }
        webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(view: WebView, callback: ValueCallback<Array<Uri>>, params: FileChooserParams): Boolean =
                currentDevice() != null && isWorkbenchOrigin(Uri.parse(view.url.orEmpty())) && chooseFile(callback, params)
        }
    }
}
