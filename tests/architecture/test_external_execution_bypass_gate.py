from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


ROOT = Path(__file__).resolve().parents[2]
PYTHON_SOURCE_ROOT = ROOT / "src"
ELECTRON_MAIN_ROOT = ROOT / "apps" / "desktop-electron" / "src"

# Every key includes a stable callee ordinal, so a second same-family wire in
# one function is detected by this freeze gate.
Sink = tuple[str, str, str, int, str]


@dataclass(frozen=True)
class BypassDebt:
    """Temporary debt record. This is a freeze gate, never migration proof."""

    debt_id: str
    target_effect_kind: str
    target_handler: str
    exit_condition: str


def _dotted_name(node: ast.AST, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value, aliases)
        return f"{parent}.{node.attr}" if parent else node.attr
    if isinstance(node, ast.Call):
        return _dotted_name(node.func, aliases)
    return ""


class _PythonWireVisitor(ast.NodeVisitor):
    _HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "request", "stream", "send"})
    _SUBPROCESS_CALLS = frozenset({"Popen", "run", "check_call", "check_output", "call"})

    def __init__(self, relative_path: str, tree: ast.Module) -> None:
        self.relative_path = relative_path
        self.aliases = self._aliases(tree)
        self.alias_scopes = [self.aliases]
        self.scope: list[str] = []
        self.http_clients: list[set[str]] = [set()]
        self.socket_values: list[set[str]] = [set()]
        self.ordinals: list[Counter[tuple[str, str]]] = [Counter()]
        self.sinks: set[Sink] = set()

    @staticmethod
    def _aliases(tree: ast.Module) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.Import):
                for item in node.names:
                    # ``import urllib.request`` binds ``urllib``, whereas an
                    # explicit ``as`` binds the dotted module itself.
                    local_name = item.asname or item.name.split(".")[0]
                    aliases[local_name] = item.name if item.asname else item.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.module:
                for item in node.names:
                    aliases[item.asname or item.name] = f"{node.module}.{item.name}"
        return aliases

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._push(node.name)
        self.generic_visit(node)
        self._pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        http_names = {
            arg.arg
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            if arg.annotation is not None
            and _dotted_name(arg.annotation, self.aliases).startswith(("httpx.", "requests."))
        }
        aliases = dict(self.aliases)
        local_nodes = []
        def collect(item):
            local_nodes.append(item)
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                return
            for child in ast.iter_child_nodes(item):
                collect(child)
        for statement in node.body:
            collect(statement)
        for item in local_nodes:
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store):
                if aliases.get(item.id, "").split(".")[0] == "requests":
                    aliases.pop(item.id, None)
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            if aliases.get(arg.arg, "").split(".")[0] == "requests":
                aliases.pop(arg.arg, None)
        self._push(node.name, http_names)
        self.aliases = aliases
        self.alias_scopes[-1] = aliases
        self.generic_visit(node)
        self._pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _push(self, name: str, http_names: set[str] | None = None) -> None:
        self.scope.append(name)
        self.alias_scopes.append(dict(self.aliases))
        self.aliases = self.alias_scopes[-1]
        self.http_clients.append(http_names or set())
        self.socket_values.append(set())
        self.ordinals.append(Counter())

    def _pop(self) -> None:
        self.scope.pop()
        self.alias_scopes.pop()
        self.aliases = self.alias_scopes[-1]
        self.http_clients.pop()
        self.socket_values.pop()
        self.ordinals.pop()

    def visit_Import(self, node: ast.Import) -> None:
        imported = self._aliases(ast.Module(body=[node], type_ignores=[]))
        self.aliases.update({name: target for name, target in imported.items() if target.split(".")[0] == "requests"})

    visit_ImportFrom = visit_Import

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and _dotted_name(node.annotation, self.aliases).startswith(("httpx.", "requests.")):
            self.http_clients[-1].add(node.target.id)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        names = {target.id for target in node.targets if isinstance(target, ast.Name)}
        if self._contains_constructor(node.value, {"httpx.Client", "httpx.AsyncClient", "requests.Session"}):
            self.http_clients[-1].update(names)
        if self._contains_constructor(node.value, {"socket.create_connection", "socket.socket"}):
            self.socket_values[-1].update(names)
        elif self._contains_socket_value(node.value):
            self.socket_values[-1].update(names)
        self.generic_visit(node)
        for name in names:
            if self.aliases.get(name, "").split(".")[0] == "requests":
                self.aliases.pop(name, None)

    def visit_With(self, node: ast.With) -> None:
        self._register_http_context_managers(node.items)
        self.generic_visit(node)

    visit_AsyncWith = visit_With

    def _register_http_context_managers(self, items: list[ast.withitem]) -> None:
        for item in items:
            if not isinstance(item.optional_vars, ast.Name):
                continue
            if self._contains_constructor(item.context_expr, {"httpx.Client", "httpx.AsyncClient", "requests.Session"}):
                self.http_clients[-1].add(item.optional_vars.id)

    def _contains_constructor(self, node: ast.AST, names: set[str]) -> bool:
        return any(isinstance(item, ast.Call) and _dotted_name(item.func, self.aliases) in names for item in ast.walk(node))

    def _contains_socket_value(self, node: ast.AST) -> bool:
        return any(
            isinstance(item, ast.Name) and any(item.id in values for values in self.socket_values)
            for item in ast.walk(node)
        )

    def visit_Call(self, node: ast.Call) -> None:
        detected = self._wire(node)
        if detected is not None:
            family, callee = detected
            self.ordinals[-1][(family, callee)] += 1
            self.sinks.add((self.relative_path, ".".join(self.scope) or "<module>", family, self.ordinals[-1][(family, callee)], callee))
        self.generic_visit(node)

    def _wire(self, node: ast.Call) -> tuple[str, str] | None:
        dotted = _dotted_name(node.func, self.aliases)
        method = node.func.attr if isinstance(node.func, ast.Attribute) else ""
        receiver = dotted.rsplit(".", 1)[0].lower()
        first = node.args[0] if node.args else None
        is_model_request = isinstance(first, ast.Call) and _dotted_name(first.func, self.aliases).split(".")[-1] in {"ModelRequest", "ImageGenerationRequest"}
        if method in {"invoke", "generate", "complete_text", "complete_text_with_usage", "create_text_completion", "create_structured_completion"} and (is_model_request or "gateway" in receiver):
            return "model_gateway_call", dotted
        if dotted in {"self._completion", "self._acompletion"}:
            return "litellm_completion", dotted
        if dotted in {"websockets.asyncio.client.connect", "websockets.connect"}:
            return "websocket_connect", dotted
        if dotted.startswith("httpx.") and dotted.rsplit(".", 1)[-1] in self._HTTP_METHODS:
            return "httpx_request", dotted
        request_root = node.func
        while isinstance(request_root, ast.Attribute):
            request_root = request_root.value
        imported_requests = isinstance(request_root, ast.Name) and self.aliases.get(request_root.id, "").split(".")[0] == "requests"
        if imported_requests and dotted.startswith("requests.") and dotted.rsplit(".", 1)[-1] in self._HTTP_METHODS:
            return "requests_request", dotted
        if method in self._HTTP_METHODS and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and any(node.func.value.id in values for values in self.http_clients):
            return "http_client_request", dotted
        if dotted in {"urllib.request.urlopen", "urlopen"}:
            return "urllib_request", dotted
        if dotted.endswith("build_opener.open"):
            return "urllib_opener_request", dotted
        if dotted.startswith("subprocess.") and dotted.rsplit(".", 1)[-1] in self._SUBPROCESS_CALLS:
            return "subprocess", dotted
        if dotted in {"asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell", "create_subprocess_exec", "create_subprocess_shell"}:
            return "async_subprocess", dotted
        if dotted in {"yt_dlp.YoutubeDL", "YoutubeDL"}:
            return "yt_dlp_constructor", dotted
        if method == "extract_info":
            return "yt_dlp_extract_info", dotted
        if dotted == "socket.create_connection":
            return "socket_connect", dotted
        if method in {"connect", "send", "sendall"} and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and any(node.func.value.id in values for values in self.socket_values):
            return f"socket_{method}", dotted
        if self.relative_path == "src/core/mcp_host/streamable_http_transport.py" and dotted == "self._requester.request":
            return "mcp_requester_wire", dotted
        return None


def _find_python_wires() -> set[Sink]:
    found: set[Sink] = set()
    for path in PYTHON_SOURCE_ROOT.rglob("*.py"):
        if "node_modules" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _PythonWireVisitor(path.relative_to(ROOT).as_posix(), tree)
        visitor.visit(tree)
        found.update(visitor.sinks)
    return found


_JS_CALL = re.compile(r"(?<![\w$#.])(?P<callee>this\.(?:fetch|fetchFn|fetchImpl|net\.request)|this\.\#fetch|spawnSync|spawn|execFileSync|execFile|execSync|exec|fetchFn|fetch|axios\.(?:get|post|put|patch|delete|request)|(?:net|http|https)\.(?:connect|request|get))\s*\(")
_JS_INJECTED_SPAWN = re.compile(r"(?P<callee>(?:this\.)?spawnChild|spawnProcess)\s*\(")
_CONTROLLED_BARE_FETCHFN_SCOPES = frozenset({
    ("apps/desktop-electron/src/companion/interaction-recorder.cjs", "recordCompanionInteraction"),
    ("apps/desktop-electron/src/companion/voice-call-controller.cjs", "revokeGrantBestEffort"),
    ("apps/desktop-electron/src/companion/screen-vision-controller.cjs", "postJson"),
    ("apps/desktop-electron/src/companion/screen-vision-controller.cjs", "getJson"),
    ("apps/desktop-electron/src/companion/screen-vision-controller.cjs", "revokeGrantBestEffort"),
})


def _find_electron_main_wires() -> set[Sink]:
    """Production Electron main scanner. Scripts and tests are intentionally excluded."""
    found: set[Sink] = set()
    for path in ELECTRON_MAIN_ROOT.rglob("*.cjs"):
        relative = path.relative_to(ROOT).as_posix()
        current_class: str | None = None
        scope = "<module>"
        counts: Counter[tuple[str, str, str]] = Counter()
        for line in path.read_text(encoding="utf-8").splitlines():
            class_match = re.match(r"\s*class\s+(\w+)", line)
            function_match = re.match(r"\s*(?:async\s+)?(?:function\s+)?([#\w]+)\s*\(", line)
            if class_match:
                current_class = class_match.group(1)
                scope = current_class
            elif function_match and not line.lstrip().startswith(("if", "for", "while", "switch", "catch")):
                is_top_level_function = line.lstrip().startswith(("function ", "async function "))
                scope = function_match.group(1) if is_top_level_function else f"{current_class}.{function_match.group(1)}" if current_class else function_match.group(1)
            for match in (*_JS_CALL.finditer(line), *_JS_INJECTED_SPAWN.finditer(line)):
                callee = match.group("callee")
                if callee == "fetchFn" and (relative, scope) not in _CONTROLLED_BARE_FETCHFN_SCOPES:
                    continue
                if "spawn" in callee or callee.startswith("exec"):
                    family = "electron_process_spawn"
                elif path.name == "network-health-adapter.cjs" and callee == "this.net.request":
                    family = "electron_https_health_probe"
                else:
                    family = "electron_loopback_request"
                counts[(scope, family, callee)] += 1
                found.add((relative, scope, family, counts[(scope, family, callee)], callee))
    return found


def _find_external_execution_wires() -> set[Sink]:
    return _find_python_wires() | _find_electron_main_wires()


_FAMILY_TARGETS: dict[str, tuple[str, str, str]] = {
    "model_gateway_call": ("model_call", "ModelEffectHandler", "remove after Gate → EffectRunner → Model Handler has crash and recovery evidence"),
    "litellm_completion": ("model_call", "ModelEffectHandler", "remove after LiteLLM wire is reachable only through the Model Effect Handler"),
    "httpx_request": ("transport_dispatch", "HttpTransportHandler", "remove after EffectRunner owns intent, receipt and recovery"),
    "http_client_request": ("transport_dispatch", "HttpTransportHandler", "remove after EffectRunner owns intent, receipt and recovery"),
    "requests_request": ("transport_dispatch", "HttpTransportHandler", "remove after EffectRunner owns intent, receipt and recovery"),
    "urllib_request": ("transport_dispatch", "HttpTransportHandler", "remove after the request is Handler-owned"),
    "urllib_opener_request": ("transport_dispatch", "HttpTransportHandler", "remove after the opener wire is Handler-owned"),
    "subprocess": ("local_process", "SubprocessTransportHandler", "remove after Core Reaper is the only process recovery scheduler"),
    "async_subprocess": ("local_process", "SubprocessTransportHandler", "remove after Core Reaper is the only process recovery scheduler"),
    "yt_dlp_constructor": ("media_fetch", "MediaFetchHandler", "remove after Effect owns media wire, receipt and UNKNOWN semantics"),
    "yt_dlp_extract_info": ("media_fetch", "MediaFetchHandler", "remove after Effect owns media wire, receipt and UNKNOWN semantics"),
    "socket_connect": ("transport_dispatch", "PinnedSocketTransportHandler", "remove after pinned socket dispatch is Handler-owned"),
    "socket_send": ("transport_dispatch", "PinnedSocketTransportHandler", "remove after pinned socket dispatch is Handler-owned"),
    "socket_sendall": ("transport_dispatch", "PinnedSocketTransportHandler", "remove after pinned socket dispatch is Handler-owned"),
    "websocket_connect": ("transport_dispatch", "WebSocketTransportHandler", "remove after WebSocket dispatch is Handler-owned"),
    "mcp_requester_wire": ("mcp_tool_call", "McpTransportHandler", "remove after MCP transport has no independent wire/recovery authority"),
    "electron_loopback_request": ("desktop_coordination", "DesktopLoopbackHandler", "retain only while the request remains authenticated local coordination, otherwise migrate to an Effect Handler"),
    "electron_process_spawn": ("desktop_process", "DesktopProcessHandler", "sidecar-supervisor is coordination, not an external-effect completion claim; migrate other process effects to Handler ownership"),
    "electron_https_health_probe": ("network_health_probe", "DesktopNetworkHealthProbe", "remove or migrate if the HTTPS probe gains side effects, credentials, or retry authority"),
}


# No directory-level whitelist: every item is an exact path/scope/family/ordinal/callee key.
FROZEN_KEYS: tuple[Sink, ...] = (
    ('apps/desktop-electron/src/authenticated-local-gateway.cjs', 'AuthenticatedLocalGateway.requestJson', 'electron_loopback_request', 1, 'this.#fetch'),
    ('apps/desktop-electron/src/companion/launcher-controller.cjs', 'CompanionLauncherController.launch', 'electron_process_spawn', 1, 'spawnProcess'),
    ('apps/desktop-electron/src/companion/media-session-adapter.cjs', 'WindowsMediaSessionAdapter.run', 'electron_process_spawn', 1, 'spawnProcess'),
    ('apps/desktop-electron/src/companion/data-ipc-controller.cjs', 'CompanionDataIpcController.requestData', 'electron_loopback_request', 1, 'this.fetchImpl'),
    ('apps/desktop-electron/src/companion/interaction-recorder.cjs', 'recordCompanionInteraction', 'electron_loopback_request', 1, 'fetchFn'),
    ('apps/desktop-electron/src/companion/network-health-adapter.cjs', 'CompanionNetworkHealthAdapter.#head', 'electron_https_health_probe', 1, 'this.net.request'),
    ('apps/desktop-electron/src/companion/voice-call-controller.cjs', 'CompanionVoiceCallController.transcribe', 'electron_loopback_request', 1, 'this.fetchFn'),
    ('apps/desktop-electron/src/companion/voice-call-controller.cjs', 'revokeGrantBestEffort', 'electron_loopback_request', 1, 'fetchFn'),
    ('apps/desktop-electron/src/companion/voice-controller.cjs', 'CompanionVoiceController.speak', 'electron_loopback_request', 1, 'this.fetchFn'),
    ('apps/desktop-electron/src/companion/screen-vision-controller.cjs', 'getJson', 'electron_loopback_request', 1, 'fetchFn'),
    ('apps/desktop-electron/src/companion/screen-vision-controller.cjs', 'postJson', 'electron_loopback_request', 1, 'fetchFn'),
    ('apps/desktop-electron/src/companion/screen-vision-controller.cjs', 'revokeGrantBestEffort', 'electron_loopback_request', 1, 'fetchFn'),
    ('apps/desktop-electron/src/doctor.cjs', 'checkPreloadEntry', 'electron_process_spawn', 1, 'exec'),
    ('apps/desktop-electron/src/doctor.cjs', 'checkPreloadEntry', 'electron_process_spawn', 1, 'spawn'),
    ('apps/desktop-electron/src/document-pdf-ipc-controller.cjs', 'DocumentPdfIpcController.#json', 'electron_loopback_request', 1, 'this.fetch'),
    ('apps/desktop-electron/src/file-grant.cjs', 'streamRequest', 'electron_loopback_request', 1, 'http.request'),
    ('apps/desktop-electron/src/memory-transfer-ipc-controller.cjs', 'MemoryTransferIpcController.exportMemoryAssets', 'electron_loopback_request', 1, 'this.fetch'),
    ('apps/desktop-electron/src/memory-transfer-ipc-controller.cjs', 'MemoryTransferIpcController.importMemoryAssets', 'electron_loopback_request', 1, 'this.fetch'),
    ('apps/desktop-electron/src/memory-transfer-ipc-controller.cjs', 'MemoryTransferIpcController.saveMemoryExport', 'electron_loopback_request', 1, 'this.fetch'),
    ('apps/desktop-electron/src/original-asset-ipc-controller.cjs', 'OriginalAssetIpcController.open', 'electron_loopback_request', 1, 'this.fetch'),
    ('apps/desktop-electron/src/sidecar-supervisor.cjs', 'SidecarSupervisor.start', 'electron_process_spawn', 1, 'this.spawnChild'),
    # Existing session-bound loopback helper also performs the authenticated
    # runtime-root POST probe; no renderer URL or external Provider is accepted.
    ('apps/desktop-electron/src/sidecar-supervisor.cjs', 'requestJson', 'electron_loopback_request', 1, 'http.request'),
    ('apps/desktop-electron/src/sidecar-supervisor.cjs', 'resolve', 'electron_process_spawn', 1, 'spawnProcess'),
    ('apps/desktop-electron/src/vault-recovery-controller.cjs', 'VaultRecoveryController.#run', 'electron_process_spawn', 1, 'this.spawnChild'),
    ('apps/desktop-electron/src/vault-restore-ipc-controller.cjs', 'VaultRestoreIpcController.restore', 'electron_loopback_request', 1, 'this.fetch'),
    ('src/backend/agent/infrastructure/chat_gateway.py', 'LiteLLMChatGateway.create_text_completion', 'model_gateway_call', 1, 'self._gateway.complete_text'),
    ('src/backend/api/companion_chat_ai_runtime.py', 'CompanionChatMessageWriteCapability.invoke', 'model_gateway_call', 1, 'self._gateway.invoke'),
    ('src/backend/api/companion_vision_ai_runtime.py', 'CompanionVisionAnalyzeCapability.invoke', 'model_gateway_call', 1, 'self._gateway.invoke'),
    ('src/backend/api/developer_studio_test_lab_ai_runtime.py', 'DeveloperStudioTestLabCapability._invoke_model', 'model_gateway_call', 1, 'self._gateway.invoke'),
    ('src/backend/api/four_layer_memory_candidate_ai_runtime.py', '_BoundModelGateway.invoke', 'model_gateway_call', 1, 'self.gateway.invoke'),
    ('src/backend/api/four_layer_memory_candidate_ai_runtime.py', '_GatewayProposalGenerator.__call__', 'model_gateway_call', 1, 'gateway.invoke'),
    ('src/backend/api/governed_local_asr.py', '_run_fixed_command', 'subprocess', 1, 'subprocess.Popen'),
    ('src/backend/api/governed_local_ocr.py', '_run_governed_command', 'subprocess', 1, 'subprocess.Popen'),
    ('src/backend/api/governed_staged_video.py', '_run_governed_ffmpeg', 'subprocess', 1, 'subprocess.Popen'),
    ('src/backend/api/image_generation_ai_runtime.py', 'ImageGenerationCapability.invoke', 'model_gateway_call', 1, 'gateway.generate'),
    ('src/backend/api/ppt_master_capability_runtime.py', '_run_process', 'subprocess', 1, 'subprocess.run'),
    ('src/backend/api/project_skill_ai_runtime.py', 'ProjectSkillDraftPlanner.plan', 'model_gateway_call', 1, 'self._gateway.invoke'),
    ('src/backend/api/routes/settings.py', 'register_provider_model_discovery_handler.handle', 'httpx_request', 1, 'httpx.get'),
    ('src/backend/api/series_intake_ai_runtime.py', 'SeriesIntakeOrganizeCommitCapability.invoke', 'model_gateway_call', 1, 'self._gateway.invoke'),
    ('src/backend/api/source_document_ai_runtime.py', 'SourceDocumentDraftPlanner.plan', 'model_gateway_call', 1, 'self._gateway.invoke'),
    ('src/backend/api/workbench_ai_runtime.py', 'WorkbenchQuestionPlanner.plan', 'model_gateway_call', 1, 'self._gateway.invoke'),
    ('src/backend/api/local_audio_chunking.py', 'split_local_audio_chunk', 'subprocess', 1, 'subprocess.run'),
    ('src/backend/api/workbench_input_classifier_ai_runtime.py', '_GatewayJsonProvider.complete_json', 'model_gateway_call', 1, 'self.gateway.invoke'),
    ('src/backend/bilibili/authorized_public_download.py', '_media_has_audio', 'subprocess', 1, 'subprocess.run'),
    ('src/backend/bilibili/authorized_public_download.py', '_resolve_cid', 'http_client_request', 1, 'client.get'),
    ('src/backend/bilibili/authorized_public_download.py', '_resolve_public_media', 'http_client_request', 1, 'client.get'),
    ('src/backend/bilibili/authorized_public_download.py', '_run_yt_dlp', 'subprocess', 1, 'subprocess.run'),
    ('src/backend/bilibili/authorized_public_download.py', '_stream_atomic_media', 'http_client_request', 1, 'client.stream'),
    ('src/backend/bilibili/ytdlp_bilibili.py', 'BilibiliDownloader.download', 'subprocess', 1, 'subprocess.Popen'),
    ('src/backend/bilibili/ytdlp_bilibili.py', '_extract_info', 'yt_dlp_constructor', 1, 'YoutubeDL'),
    ('src/backend/bilibili/ytdlp_bilibili.py', '_extract_info', 'yt_dlp_extract_info', 1, 'ydl.extract_info'),
    ('src/backend/bilibili/ytdlp_bilibili.py', '_extract_view_info', 'httpx_request', 1, 'httpx.get'),
    ('src/backend/companion_provider_runtime.py', 'CompanionLiteLLMProvider.generate', 'model_gateway_call', 1, 'self.gateway.invoke'),
    ('src/backend/model_runtime.py', 'LiteLLMModelGatewayAdapter.invoke', 'model_gateway_call', 1, 'self._gateway.complete_text'),
    ('src/backend/model_runtime.py', 'TieredModelGatewayAdapter.invoke', 'model_gateway_call', 1, 'resolution.gateway.invoke'),
    ('src/backend/security/network_adapter.py', '_open_loopback_connect_tunnel', 'socket_connect', 1, 'socket.create_connection'),
    ('src/backend/security/network_adapter.py', '_open_loopback_connect_tunnel', 'socket_sendall', 1, 'connection.sendall'),
    ('src/backend/security/network_adapter.py', '_perform_pinned_binary_download', 'socket_connect', 1, 'socket.create_connection'),
    ('src/backend/security/network_adapter.py', '_perform_pinned_binary_download', 'socket_sendall', 1, 'connection.sendall'),
    ('src/backend/security/network_adapter.py', '_perform_pinned_request', 'socket_connect', 1, 'socket.create_connection'),
    ('src/backend/security/network_adapter.py', '_perform_pinned_request', 'socket_sendall', 1, 'connection.sendall'),
    ('src/backend/security/network_adapter.py', '_perform_pinned_request', 'socket_sendall', 2, 'connection.sendall'),
    ('src/backend/shared/llm/image_generation_gateway.py', '_post_openai_compatible_image_generation', 'urllib_opener_request', 1, 'urllib.request.build_opener.open'),
    ('src/backend/shared/llm/litellm_gateway.py', 'LiteLLMCompletionGateway.acomplete_text', 'litellm_completion', 1, 'self._acompletion'),
    ('src/backend/shared/llm/litellm_gateway.py', 'LiteLLMCompletionGateway.astream_text', 'litellm_completion', 1, 'self._acompletion'),
    ('src/backend/shared/llm/litellm_gateway.py', 'LiteLLMCompletionGateway.complete_text_with_usage.handle_wire', 'litellm_completion', 1, 'self._completion'),
    ('src/backend/shared/llm/litellm_gateway.py', 'LiteLLMCompletionGateway.stream_text', 'litellm_completion', 1, 'self._completion'),
    ('src/backend/shared/llm/litellm_gateway.py', 'LiteLLMCompletionGateway.stream_text_with_metadata', 'litellm_completion', 1, 'self._completion'),
    ('src/backend/shared/llm/litellm_gateway.py', '_anonymous_openai_compatible_completion', 'urllib_opener_request', 1, 'urllib.request.build_opener.open'),
    ('src/backend/team_memory.py', '_request_json', 'http_client_request', 1, 'client.request'),
    ('src/backend/video_intake/bilibili.py', 'BilibiliClient._download_sync', 'yt_dlp_constructor', 1, 'YoutubeDL'),
    ('src/backend/video_intake/bilibili.py', 'BilibiliClient._download_visual_probe_sync', 'yt_dlp_constructor', 1, 'YoutubeDL'),
    ('src/backend/video_intake/bilibili.py', 'BilibiliClient._resolve_sync', 'yt_dlp_constructor', 1, 'YoutubeDL'),
    ('src/backend/video_intake/bilibili.py', 'BilibiliClient._resolve_sync', 'yt_dlp_extract_info', 1, 'ydl.extract_info'),
    ('src/backend/video_intake/vision.py', 'OpenAICompatibleVisionProvider.analyze', 'http_client_request', 1, 'client.post'),
    ('src/backend/video_intake/visual.py', '_run_ffmpeg', 'subprocess', 1, 'subprocess.run'),
    ('src/backend/video_summary/infrastructure/faster_whisper_transcriber.py', '_is_nvidia_gpu_available', 'subprocess', 1, 'subprocess.run'),
    ('src/backend/video_summary/infrastructure/litellm_web_search.py', 'LiteLLMNativeWebSearchGateway.search', 'litellm_completion', 1, 'self._completion'),
    ('src/backend/video_summary/infrastructure/media_tools.py', 'FfmpegMediaProcessor.extract_audio', 'subprocess', 1, 'subprocess.Popen'),
    ('src/backend/video_summary/infrastructure/media_tools.py', 'FfmpegMediaProcessor.probe_duration', 'subprocess', 1, 'subprocess.run'),
    ('src/core/ai_kernel/model_planner.py', 'ModelGatewayAgentPlanner.plan', 'model_gateway_call', 1, 'self._gateway.invoke'),
    ('src/core/mcp_host/stdio_transport.py', 'MCPStdioTransport._spawn_if_needed', 'subprocess', 1, 'subprocess.Popen'),
    ('src/core/mcp_host/streamable_http_transport.py', 'MCPStreamableHTTPTransport._cancel_and_terminate', 'mcp_requester_wire', 1, 'self._requester.request'),
    ('src/core/mcp_host/streamable_http_transport.py', 'MCPStreamableHTTPTransport._cancel_and_terminate', 'mcp_requester_wire', 2, 'self._requester.request'),
    ('src/core/mcp_host/streamable_http_transport.py', 'MCPStreamableHTTPTransport._resume_sse', 'mcp_requester_wire', 1, 'self._requester.request'),
    ('src/core/mcp_host/streamable_http_transport.py', 'MCPStreamableHTTPTransport._send', 'mcp_requester_wire', 1, 'self._requester.request'),
    ('src/core/mcp_host/streamable_http_transport.py', 'MCPStreamableHTTPTransport.close', 'mcp_requester_wire', 1, 'self._requester.request'),
    ('src/core/plugin_hands/stdio_runner.py', 'PluginHandsStdioRunner._spawn', 'subprocess', 1, 'subprocess.Popen'),
    ('src/core/product_core/audio_asset_transcriber.py', '_run_command', 'subprocess', 1, 'subprocess.Popen'),
    ('src/core/product_core/local_asr_provider.py', 'LocalCommandAudioTranscriptionAdapter.transcribe', 'subprocess', 1, 'subprocess.run'),
    ('src/core/product_core/local_asr_provider.py', '_run_cancellable_command', 'subprocess', 1, 'subprocess.Popen'),
    ('src/core/product_core/local_document_text_extractor.py', 'LocalCommandDocumentTextExtractor.extract', 'subprocess', 1, 'subprocess.run'),
    ('src/core/product_core/local_ocr_provider.py', 'LocalCommandImageOcrAdapter.extract_text', 'subprocess', 1, 'subprocess.run'),
    ('src/core/product_core/local_video_provider.py', 'LocalCommandVideoFrameExtractionAdapter.extract_frames', 'subprocess', 1, 'subprocess.run'),
    ('src/core/product_core/openai_compatible_four_layer_provider.py', '_urllib_post_json', 'urllib_request', 1, 'urllib.request.urlopen'),
    ('src/core/product_core/transcript_summary_adapter.py', '_run_command', 'subprocess', 1, 'subprocess.run'),
    ('src/core/product_core/video_audio_extractor.py', '_run_command', 'subprocess', 1, 'subprocess.run'),
    ('src/core/product_core/video_link_adapter.py', '_run_subprocess', 'subprocess', 1, 'subprocess.run'),
    ('src/backend/api/tokenhub_asr_provider.py', '_http_call', 'urllib_request', 1, 'urllib.request.urlopen'),
)


EXPECTED_FROZEN_KEY_COUNT = 99


def _stable_debt_id(key: Sink) -> str:
    # Derived from the full stable key, so source-list reordering cannot remap
    # a debt id to another wire.  Uniqueness is asserted below.
    normalized = re.sub(r"[^A-Z0-9]+", "-", "-".join((key[0], key[1], key[2], str(key[3]), key[4])).upper()).strip("-")
    return f"D-SCHEME-D-WIRE-{normalized}"


def _build_inventory() -> MappingProxyType:
    assert len(FROZEN_KEYS) == len(set(FROZEN_KEYS))
    inventory: dict[Sink, BypassDebt] = {}
    for key in FROZEN_KEYS:
        kind, handler, exit_condition = _FAMILY_TARGETS[key[2]]
        inventory[key] = BypassDebt(_stable_debt_id(key), kind, handler, exit_condition)
    return MappingProxyType(inventory)


FROZEN_EXISTING_EXTERNAL_SINKS = _build_inventory()

# This is a newly-approved, exact Handler-owned sink, not historical bypass
# debt.  Keep it separate from FROZEN_EXISTING_EXTERNAL_SINKS so it cannot
# silently inherit a migration-debt exemption.
APPROVED_EFFECT_HANDLER_SINKS = frozenset({
    (
        "src/core/external_extension_runtime/outbound_fetch.py",
        "_PinnedHTTPSConnection.connect",
        "socket_connect",
        1,
        "socket.create_connection",
    ),
})

# Realtime microphone audio is deliberately ephemeral and cannot be replayed
# by a durable Effect. Its single wire is owned by the broker-backed connector;
# the session service records one bounded execution fact and terminal receipt.
APPROVED_INTERACTIVE_SESSION_SINKS = frozenset({
    (
        "src/backend/api/qwen_realtime_asr_secure_connector.py",
        "_wire_connect",
        "websocket_connect",
        1,
        "websockets.asyncio.client.connect",
    ),
})

# Exact, authenticated desktop-to-sidecar coordination is not an external
# Effect completion claim.  Keep these sinks separate from both historical
# bypass debt and Handler-owned effects.  A sink must leave this set if its
# target is no longer session-authenticated loopback, if renderer-controlled
# paths reach the request, if it performs Provider/MCP/formal-object work, or
# if it gains retry or recovery authority.
APPROVED_LOCAL_COORDINATION_SINKS = frozenset({
    (
        "apps/desktop-electron/src/context-graph-import-ipc-controller.cjs",
        "ContextGraphImportIpcController.stage",
        "electron_loopback_request",
        1,
        "this.fetch",
    ),
    (
        "apps/desktop-electron/src/session-placement-transfer-ipc-controller.cjs",
        "SessionPlacementTransferIpcController.save",
        "electron_loopback_request",
        1,
        "this.fetch",
    ),
    (
        "apps/desktop-electron/src/session-placement-transfer-ipc-controller.cjs",
        "SessionPlacementTransferIpcController.import",
        "electron_loopback_request",
        1,
        "this.fetch",
    ),
    (
        "apps/desktop-electron/src/session-placement-transfer-ipc-controller.cjs",
        "SessionPlacementTransferIpcController.reconcile",
        "electron_loopback_request",
        1,
        "this.fetch",
    ),
})


# T9.3 explicitly authorizes this planner wire through the existing governed gateway.
APPROVED_STEWARD_DECOMPOSITION_SINKS = frozenset({
    ("src/backend/api/agent_steward_decomposition.py", "StewardDecompositionPlanner.plan",
     "model_gateway_call", 1, "self._gateway.invoke"),
})


def test_approved_steward_sink_is_exact_and_separate_from_historical_debt() -> None:
    assert APPROVED_STEWARD_DECOMPOSITION_SINKS == {
        ("src/backend/api/agent_steward_decomposition.py", "StewardDecompositionPlanner.plan",
         "model_gateway_call", 1, "self._gateway.invoke"),
    }
    assert APPROVED_STEWARD_DECOMPOSITION_SINKS.isdisjoint(FROZEN_EXISTING_EXTERNAL_SINKS)
    assert APPROVED_STEWARD_DECOMPOSITION_SINKS.isdisjoint(APPROVED_EFFECT_HANDLER_SINKS)


# Verified memory-app adapters, deliberately separate from legacy migration
# debt and EffectHandler sinks. Exact behavior lives in
# test_runtime_security_behavior (real configuration/source stores and gateway,
# only the terminal HTTP/completion wire is replaced).
APPROVED_MEMORY_APP_ADAPTER_SINKS = frozenset({
    # 823622e77 / 1c9ce336b: global policy and frozen-source checks at wire time.
    ("src/backend/memory_app/model_config.py", "ModelConfiguration.complete", "model_gateway_call", 1, "gateway.complete_text_with_usage"),
    ("src/backend/memory_app/model_config.py", "ModelConfiguration.complete_governed", "model_gateway_call", 1, "gateway.complete_text_with_usage"),
    # 823622e77: real configured transport rechecks source/config after response.
    ("src/backend/memory_app/retrieval_models.py", "ConfiguredTransport.post_json", "http_client_request", 1, "client.stream"),
    # 8156e871e: native subscription transport remains behind the same gateway.
    ("src/backend/shared/llm/openai_responses.py", "ResponsesCompletion._stream", "http_client_request", 1, "client.stream"),
})

APPROVED_USER_DOWNLOAD_SINKS = frozenset({
    # a4c50cf6d / 6cf0d7a07; d6518f511f T0.7 ruling:
    # User-initiated download, not model egress. SSRF, DNS pinning, redirect/size
    # bounds and credential-free GETs have behavioral witnesses in
    # test_runtime_security_behavior; processing retains model authorization.
    ("src/backend/memory_app/workspace_links.py", "_fetch_url", "socket_connect", 1, "socket.create_connection"),
    ("src/backend/memory_app/workspace_media_url.py", "_redirect_location", "socket_connect", 1, "socket.create_connection"),
})


def _function_named(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in tree.body if isinstance(tree, ast.Module) else ():
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function was not found: {name}")


def test_external_execution_bypass_freeze_gate_matches_explicit_inventory() -> None:
    """Freeze new wires; passing this test never proves EffectRunner completion."""
    assert _find_external_execution_wires() == (
        set(FROZEN_EXISTING_EXTERNAL_SINKS)
        | APPROVED_EFFECT_HANDLER_SINKS
        | APPROVED_INTERACTIVE_SESSION_SINKS
        | APPROVED_LOCAL_COORDINATION_SINKS
        | APPROVED_STEWARD_DECOMPOSITION_SINKS
        | APPROVED_MEMORY_APP_ADAPTER_SINKS
        | APPROVED_USER_DOWNLOAD_SINKS
    )
    assert not (set(FROZEN_EXISTING_EXTERNAL_SINKS) & APPROVED_EFFECT_HANDLER_SINKS)
    assert not (set(FROZEN_EXISTING_EXTERNAL_SINKS) & APPROVED_LOCAL_COORDINATION_SINKS)
    assert APPROVED_EFFECT_HANDLER_SINKS.isdisjoint(APPROVED_LOCAL_COORDINATION_SINKS)
    assert APPROVED_INTERACTIVE_SESSION_SINKS.isdisjoint(FROZEN_EXISTING_EXTERNAL_SINKS)
    assert len(FROZEN_EXISTING_EXTERNAL_SINKS) == EXPECTED_FROZEN_KEY_COUNT


def test_approved_local_coordination_sinks_are_authenticated_and_main_owned() -> None:
    """Keep desktop-to-sidecar wires constrained to authenticated local control."""
    assert APPROVED_LOCAL_COORDINATION_SINKS == {
        (
            "apps/desktop-electron/src/context-graph-import-ipc-controller.cjs",
            "ContextGraphImportIpcController.stage",
            "electron_loopback_request",
            1,
            "this.fetch",
        ),
        (
            "apps/desktop-electron/src/session-placement-transfer-ipc-controller.cjs",
            "SessionPlacementTransferIpcController.save",
            "electron_loopback_request",
            1,
            "this.fetch",
        ),
        (
            "apps/desktop-electron/src/session-placement-transfer-ipc-controller.cjs",
            "SessionPlacementTransferIpcController.import",
            "electron_loopback_request",
            1,
            "this.fetch",
        ),
        (
            "apps/desktop-electron/src/session-placement-transfer-ipc-controller.cjs",
            "SessionPlacementTransferIpcController.reconcile",
            "electron_loopback_request",
            1,
            "this.fetch",
        ),
    }
    source = (
        ROOT / "apps/desktop-electron/src/context-graph-import-ipc-controller.cjs"
    ).read_text(encoding="utf-8")
    stage = source.split("async stage(event, request = {}) {", 1)[1].split(
        "\n  dispose()", 1,
    )[0]
    request_body = stage.split("body: JSON.stringify({", 1)[1].split("}),", 1)[0]

    assert "this.requireMainRenderer(event);" in stage
    assert 'new URL("/api/rebuild/context-graph-import-selections", requestSession.origin)' in stage
    assert "[this.sessionHeader]: requestSession.secret" in stage
    assert "asset_id: assetId" in request_body
    assert "filePath" not in request_body

    transfer = (
        ROOT / "apps/desktop-electron/src/session-placement-transfer-ipc-controller.cjs"
    ).read_text(encoding="utf-8")
    assert "this.requireMainRenderer(event);" in transfer
    assert "/api/rebuild/session-placement/" in transfer
    assert "[SESSION_HEADER]: session.secret" in transfer
    assert "DESKTOP_CONTROL_HEADER" in transfer
    assert "session.origin" in transfer


def test_approved_extension_socket_sink_is_unique_and_factory_owned() -> None:
    """Approve one pinned socket only through the extension Effect Handler port."""

    relative = "src/core/external_extension_runtime/outbound_fetch.py"
    path = ROOT / relative
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    visitor = _PythonWireVisitor(relative, tree)
    visitor.visit(tree)
    socket_sinks = {sink for sink in visitor.sinks if sink[2].startswith("socket_")}
    assert socket_sinks == APPROVED_EFFECT_HANDLER_SINKS
    assert APPROVED_EFFECT_HANDLER_SINKS.isdisjoint(FROZEN_EXISTING_EXTERNAL_SINKS)

    factory = _function_named(tree, "production_extension_acquisition_fetcher")
    factory_calls = [
        node for node in ast.walk(factory)
        if isinstance(node, ast.Call) and _dotted_name(node.func, visitor.aliases) == "PinnedStdlibHttpsTransport"
    ]
    assert len(factory_calls) == 1
    constructors = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _dotted_name(node.func, visitor.aliases) == "PinnedStdlibHttpsTransport"
    ]
    assert constructors == factory_calls

    # The test-only constructor seam must not appear in production callers.
    seam_users = [
        source.relative_to(ROOT).as_posix()
        for source in PYTHON_SOURCE_ROOT.rglob("*.py")
        if source != path and "_fetcher_for_test" in source.read_text(encoding="utf-8")
    ]
    assert seam_users == []


def test_approved_extension_socket_sink_is_live_effect_runtime_only() -> None:
    """An approved sink must be live only behind the application's v2 Runtime."""

    startup_relative = "src/backend/api/external_extension_runtime_startup.py"
    startup_path = ROOT / startup_relative
    startup_tree = ast.parse(
        startup_path.read_text(encoding="utf-8"),
        filename=str(startup_path),
    )
    startup_factory_calls = [
        node
        for node in ast.walk(startup_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "production_extension_acquisition_fetcher"
    ]
    assert len(startup_factory_calls) == 1

    factory_callers: list[str] = []
    governed_constructor_callers = {
        "ExternalExtensionResolveHandler": [],
        "ExternalExtensionAcquireHandler": [],
        "GitHubSourceResolver": [],
        "GitHubArtifactAcquirer": [],
    }
    for source in PYTHON_SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        relative = source.relative_to(ROOT).as_posix()
        if any(
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "production_extension_acquisition_fetcher")
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "production_extension_acquisition_fetcher"
                )
            )
            for node in ast.walk(tree)
        ):
            factory_callers.append(relative)
        for constructor in governed_constructor_callers:
            if any(
                isinstance(node, ast.Call)
                and (
                    (isinstance(node.func, ast.Name) and node.func.id == constructor)
                    or (isinstance(node.func, ast.Attribute) and node.func.attr == constructor)
                )
                for node in ast.walk(tree)
            ):
                governed_constructor_callers[constructor].append(relative)
    assert factory_callers == [startup_relative]
    assert governed_constructor_callers == {
        "ExternalExtensionResolveHandler": [startup_relative],
        "ExternalExtensionAcquireHandler": [startup_relative],
        "GitHubSourceResolver": [startup_relative],
        "GitHubArtifactAcquirer": [startup_relative],
    }

    # Startup may return the immutable fact entry point, but never raw execution
    # objects that a caller could invoke outside EffectRuntime.execute().
    for node in startup_tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "ExternalExtensionRuntimeStartupHandles":
            continue
        public_fields = {
            field.target.id
            for field in node.body
            if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name)
        }
        assert public_fields.isdisjoint({
            "resolve_handler",
            "acquire_handler",
            "fetcher",
            "transport",
            "resolver",
            "acquirer",
        })

    app_path = ROOT / "src/backend/api/app.py"
    app_source = app_path.read_text(encoding="utf-8")
    assert "from backend.api.external_extension_runtime_startup import" in app_source
    assert "register_external_extension_runtime(" in app_source


def test_external_execution_bypass_freeze_gate_requires_complete_debt_metadata() -> None:
    """Each frozen key must keep complete migration debt, not only an allowlisted key."""
    debts = tuple(FROZEN_EXISTING_EXTERNAL_SINKS.values())
    assert debts
    debt_ids = [debt.debt_id for debt in debts]
    assert len(debt_ids) == len(set(debt_ids))
    assert all(re.fullmatch(r"D-SCHEME-D-[A-Z0-9-]+", debt_id) for debt_id in debt_ids)
    assert all(debt.target_effect_kind and debt.target_handler and debt.exit_condition for debt in debts)


def test_external_execution_bypass_freeze_gate_recognizes_import_aliases_and_typed_context_clients() -> None:
    """Keep alias and context-manager coverage from silently regressing."""
    source = """
import httpx as hx
from subprocess import run as launch
from socket import create_connection as dial
from urllib.request import build_opener as opener
from yt_dlp import YoutubeDL as YDL

def wires(client: hx.Client):
    with hx.Client() as local:
        local.get('https://example.invalid')
    client.post('https://example.invalid')
    launch(['tool'])
    connection = dial(('127.0.0.1', 1))
    connection.sendall(b'x')
    opener().open('https://example.invalid')
    YDL({})
"""
    tree = ast.parse(source)
    visitor = _PythonWireVisitor("synthetic.py", tree)
    visitor.visit(tree)
    assert {entry[2] for entry in visitor.sinks} >= {
        "http_client_request", "subprocess", "socket_connect", "socket_sendall",
        "urllib_opener_request", "yt_dlp_constructor",
    }


def test_external_execution_bypass_freeze_gate_recognizes_dotted_urllib_import() -> None:
    """`import urllib.request` must retain the urllib package namespace."""
    tree = ast.parse("import urllib.request\n\ndef wire():\n    return urllib.request.urlopen('https://example.invalid')\n")
    visitor = _PythonWireVisitor("synthetic.py", tree)
    visitor.visit(tree)
    assert ("synthetic.py", "wire", "urllib_request", 1, "urllib.request.urlopen") in visitor.sinks


def test_external_execution_bypass_gate_recognizes_websocket_connect_aliases() -> None:
    """A provider WebSocket is an external wire even when imported as ``connect``."""
    tree = ast.parse(
        "from websockets.asyncio.client import connect\n\n"
        "def wire():\n    return connect('wss://example.invalid')\n"
    )
    visitor = _PythonWireVisitor("synthetic.py", tree)
    visitor.visit(tree)
    assert (
        "synthetic.py", "wire", "websocket_connect", 1,
        "websockets.asyncio.client.connect",
    ) in visitor.sinks


def test_requests_wires_require_actual_imports_and_respect_local_shadowing():
    source = """
import requests
import requests as http
from requests import post as send

def wire():
    requests.get('https://example.invalid')
    http.post('https://example.invalid')
    send('https://example.invalid')

def local_import():
    import requests as client
    client.get('https://example.invalid')

def mapping():
    requests = {key: value for key, value in []}
    return requests.get('key', {})

def argument(requests):
    return requests.get('key', {})

def rebound():
    import requests as client
    client.post('https://example.invalid')
    client = {}
    return client.get('key', {})
"""
    visitor = _PythonWireVisitor("synthetic.py", ast.parse(source))
    visitor.visit(ast.parse(source))
    wires = {entry[1:3] for entry in visitor.sinks}
    assert wires == {("wire", "requests_request"), ("local_import", "requests_request"), ("rebound", "requests_request")}
    assert len(visitor.sinks) == 5


def test_unimported_requests_mapping_is_not_a_wire():
    source = "def read():\n    requests = {}\n    return requests.get('key', {})\n"
    visitor = _PythonWireVisitor("synthetic.py", ast.parse(source))
    visitor.visit(ast.parse(source))
    assert visitor.sinks == set()
