from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable, Mapping
from typing import Protocol

import httpx

from backend.security import ProviderEgressPolicyStore, SecretStore, build_secret_store

from backend.shared.filesystem import atomic_write_text


@dataclass(frozen=True)
class VisionSettings:
    mode: str = "mock"
    provider: str = "openai_compatible"
    base_url: str = ""
    model: str = ""
    has_api_key: bool = False
    max_frames: int = 12
    timeout_seconds: int = 60

    @property
    def real_configured(self) -> bool:
        return bool(self.base_url and self.model and self.has_api_key)


class VisionProvider(Protocol):
    async def analyze(
        self,
        *,
        record_dir: Path,
        frames: list[dict[str, object]],
        nearby_context: dict[str, str],
    ) -> dict[str, object]: ...


class MockVisionProvider:
    async def analyze(
        self,
        *,
        record_dir: Path,
        frames: list[dict[str, object]],
        nearby_context: dict[str, str],
    ) -> dict[str, object]:
        del record_dir
        visual_claims = []
        for frame in frames[:3]:
            frame_id = str(frame.get("frame_id", ""))
            visual_claims.append(
                {
                    "claim": "Mock Vision 联调占位，不代表对画面内容的真实识别。",
                    "evidence_text": nearby_context.get(frame_id, ""),
                    "frame_id": frame_id,
                    "timestamp": str(frame.get("timestamp_text", "")),
                    "confidence": "mock",
                    "is_mock": True,
                }
            )
        return {
            "mode": "mock",
            "provider": "mock",
            "model": "deterministic-schema-v1",
            "is_mock": True,
            "analyzed_frame_count": len(frames),
            "tables": [],
            "charts": [],
            "visual_claims": visual_claims,
            "uncertainties": [
                "当前结果由 Mock Vision Provider 生成，只验证数据流和界面，不执行 OCR、表格、图表或画面语义识别。"
            ],
        }


class OpenAICompatibleVisionProvider:
    def __init__(
        self,
        settings: VisionSettings,
        *,
        client: httpx.AsyncClient | None = None,
        egress_guard: Callable[[int], object] | None = None,
        authorization_header_provider: Callable[[str], Mapping[str, str]] | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._egress_guard = egress_guard
        self._authorization_header_provider = authorization_header_provider

    async def analyze(
        self,
        *,
        record_dir: Path,
        frames: list[dict[str, object]],
        nearby_context: dict[str, str],
    ) -> dict[str, object]:
        privacy_notice = f"仅发送筛选后的 {len(frames)} 张关键帧与附近转写，不发送完整视频。"
        if not self._settings.real_configured:
            return _real_failure_result(
                self._settings,
                "真实视觉模式尚未完整配置 base_url、model 和受控密钥。",
                privacy_notice,
            )
        content: list[dict[str, object]] = [
            {"type": "text", "text": _vision_prompt(frames, nearby_context)}
        ]
        available_frames: list[dict[str, object]] = []
        for frame in frames:
            relative_path = str(frame.get("file") or "")
            path = (record_dir / relative_path).resolve()
            try:
                path.relative_to(record_dir.resolve())
            except ValueError:
                continue
            if not path.exists():
                continue
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{encoded}", "detail": "low"},
                }
            )
            available_frames.append(frame)
        if not available_frames:
            return _real_failure_result(self._settings, "没有可发送的关键帧。", privacy_notice)
        payload = {
            "model": self._settings.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        if self._egress_guard is None:
            return _real_failure_result(self._settings, "真实视觉外发尚未授权。", privacy_notice)
        payload_bytes = len(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        try:
            lease = self._egress_guard(payload_bytes)
        except Exception:
            return _real_failure_result(self._settings, "真实视觉外发尚未授权。", privacy_notice)
        endpoint = _chat_completions_url(self._settings.base_url)
        if self._authorization_header_provider is None:
            return _real_failure_result(self._settings, "真实视觉密钥注入不可用。", privacy_notice)
        try:
            headers = {
                **dict(self._authorization_header_provider(endpoint)),
                "Content-Type": "application/json",
            }
        except Exception:
            return _real_failure_result(self._settings, "真实视觉密钥注入失败。", privacy_notice)
        last_error: Exception | None = None
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._settings.timeout_seconds)
        try:
            for attempt in range(2):
                try:
                    response = await client.post(endpoint, headers=headers, json=payload)
                    response.raise_for_status()
                    parsed = _parse_provider_response(response.json())
                    lease.finish("succeeded")
                    return _normalize_real_result(
                        parsed,
                        settings=self._settings,
                        frames=available_frames,
                        privacy_notice=privacy_notice,
                    )
                except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as error:
                    last_error = error
                    if attempt == 1:
                        break
        finally:
            if owns_client:
                await client.aclose()
        lease.finish("failed", error_code="provider_request_failed")
        return _real_failure_result(
            self._settings,
            f"真实视觉调用失败：{type(last_error).__name__}: {_safe_error(last_error)}",
            privacy_notice,
        )


async def analyze_and_merge_visuals(
    *,
    record_dir: Path,
    structured: dict[str, object],
    settings: VisionSettings,
    requested: bool,
    egress_guard: Callable[[int], object] | None = None,
    authorization_header_provider: Callable[[str], Mapping[str, str]] | None = None,
) -> dict[str, object]:
    visual = structured.get("visual_analysis")
    if not isinstance(visual, dict):
        visual = {}
        structured["visual_analysis"] = visual
    frames = _dict_list(visual.get("keyframes"))[: settings.max_frames]
    if not requested or settings.mode == "disabled":
        visual.update(
            {
                "cloud_vision_used": False,
                "cloud_vision_mode": "disabled",
                "cloud_vision_provider": "",
                "cloud_vision_model": "",
            }
        )
        _save_outputs(record_dir, structured, visual)
        return structured

    nearby_context = _nearby_context(structured, frames)
    try:
        if settings.mode == "mock":
            result = await MockVisionProvider().analyze(
                record_dir=record_dir,
                frames=frames,
                nearby_context=nearby_context,
            )
        else:
            result = await OpenAICompatibleVisionProvider(
                settings, egress_guard=egress_guard,
                authorization_header_provider=authorization_header_provider,
            ).analyze(
                record_dir=record_dir,
                frames=frames,
                nearby_context=nearby_context,
            )
    except Exception as error:
        result = {
            "mode": settings.mode,
            "provider": "mock" if settings.mode == "mock" else settings.provider,
            "model": "deterministic-schema-v1" if settings.mode == "mock" else settings.model,
            "is_mock": settings.mode == "mock",
            "analyzed_frame_count": 0,
            "tables": [],
            "charts": [],
            "visual_claims": [],
            "uncertainties": [
                f"视觉 Provider 失败：{type(error).__name__}: {_safe_error(error)}",
                "视觉失败未阻塞音频、转写和总结主流程。",
            ],
        }
    _merge_provider_result(structured, visual, result)
    _save_outputs(record_dir, structured, visual)
    return structured


def load_vision_settings(root_dir: Path, secret_store: SecretStore | None = None) -> VisionSettings:
    values = _load_dotenv(root_dir / ".env")
    store = secret_store or build_secret_store(root_dir)
    legacy_key = values.get("VISION_API_KEY", "").strip()
    if legacy_key and not store.has_secret("provider:vision"):
        store.set("provider:vision", legacy_key)
    mode = values.get("VISION_MODE", "mock").strip().lower()
    if mode not in {"disabled", "mock", "real"}:
        mode = "mock"
    settings = VisionSettings(
        mode=mode,
        provider=values.get("VISION_PROVIDER", "openai_compatible").strip() or "openai_compatible",
        base_url=values.get("VISION_BASE_URL", "").strip().rstrip("/"),
        model=values.get("VISION_MODEL", "").strip(),
        has_api_key=store.has_secret("provider:vision"),
        max_frames=_bounded_int(values.get("VISION_MAX_FRAMES"), default=12, minimum=8, maximum=20),
        timeout_seconds=_bounded_int(values.get("VISION_TIMEOUT_SECONDS"), default=60, minimum=10, maximum=180),
    )
    if legacy_key:
        save_vision_settings(root_dir, settings, store)
    return settings


def save_vision_settings(
    root_dir: Path,
    settings: VisionSettings,
    secret_store: SecretStore | None = None,
    *,
    api_key: str | None = None,
) -> None:
    store = secret_store or build_secret_store(root_dir)
    if isinstance(api_key, str) and api_key.strip():
        store.set("provider:vision", api_key.strip())
    path = root_dir / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replacements = {
        "VISION_MODE": settings.mode,
        "VISION_PROVIDER": settings.provider,
        "VISION_BASE_URL": settings.base_url,
        "VISION_MODEL": settings.model,
        "VISION_API_KEY": "",
        "VISION_MAX_FRAMES": str(settings.max_frames),
        "VISION_TIMEOUT_SECONDS": str(settings.timeout_seconds),
    }
    output: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if "=" not in line or line.lstrip().startswith("#"):
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in replacements:
            output.append(f"{key}={replacements[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key, value in replacements.items():
        if key not in seen:
            output.append(f"{key}={value}")
    atomic_write_text(path, "\n".join(output).rstrip() + "\n")


def render_visual_markdown(structured: dict[str, object]) -> str:
    visual = structured.get("visual_analysis")
    if not isinstance(visual, dict):
        return ""
    mode = str(visual.get("cloud_vision_mode") or "disabled")
    lines = ["## 画面分析", ""]
    lines.append(f"- 画面重要性：{visual.get('visual_importance') or 'unknown'}")
    lines.append(f"- 本地采样：{visual.get('sampled_frame_count', 0)} 张，保留 {visual.get('important_frame_count', 0)} 张")
    lines.append(f"- 视觉模式：{mode}")
    provider = str(visual.get("cloud_vision_provider") or "")
    model = str(visual.get("cloud_vision_model") or "")
    if provider or model:
        lines.append(f"- Provider：{provider or '未配置'} / {model or '未配置'}")
    lines.append("")
    if mode == "mock":
        lines.extend(["> 当前视觉语义结果为 Mock 联调占位，不代表真实画面识别。", ""])
    frames = _dict_list(visual.get("keyframes"))
    if frames:
        lines.extend(["### 关键帧", ""])
        for frame in frames:
            lines.append(
                f"- {frame.get('frame_id', '')} · {frame.get('timestamp_text', '')} · "
                f"评分 {frame.get('score', 0)} · `{frame.get('file', '')}`"
            )
        lines.append("")
    claims = _dict_list(visual.get("visual_claims"))
    if claims:
        lines.extend(["### 视觉结论", ""])
        for claim in claims:
            label = "Mock 占位" if claim.get("is_mock") else "视觉证据"
            lines.append(
                f"- [{label}] {claim.get('claim', '')} "
                f"({claim.get('frame_id', '')} · {claim.get('timestamp', '')})"
            )
        lines.append("")
    uncertainties = [str(item) for item in visual.get("uncertainties", [])] if isinstance(visual.get("uncertainties"), list) else []
    if uncertainties:
        lines.extend(["### 不确定项", ""])
        lines.extend(f"- {item}" for item in uncertainties)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _merge_provider_result(
    structured: dict[str, object],
    visual: dict[str, object],
    result: dict[str, object],
) -> None:
    mode = str(result.get("mode") or "disabled")
    visual.update(
        {
            "cloud_vision_used": mode == "real" and int(result.get("analyzed_frame_count") or 0) > 0,
            "cloud_vision_mode": mode,
            "cloud_vision_provider": str(result.get("provider") or ""),
            "cloud_vision_model": str(result.get("model") or ""),
            "tables": _dict_list(result.get("tables")),
            "charts": _dict_list(result.get("charts")),
            "visual_claims": _dict_list(result.get("visual_claims")),
            "uncertainties": [str(item) for item in result.get("uncertainties", [])]
            if isinstance(result.get("uncertainties"), list)
            else [],
            "is_mock": bool(result.get("is_mock")),
            "privacy_notice": str(result.get("privacy_notice") or ""),
        }
    )
    timeline = _dict_list(structured.get("timeline"))
    for frame in _dict_list(visual.get("keyframes")):
        timestamp = _number(frame.get("timestamp"))
        for chapter in timeline:
            if _number(chapter.get("start")) <= timestamp <= _number(chapter.get("end")):
                frame_ids = chapter.setdefault("frame_ids", [])
                if isinstance(frame_ids, list) and frame.get("frame_id") not in frame_ids:
                    frame_ids.append(frame.get("frame_id"))
                break
    claims = structured.setdefault("claims", [])
    if isinstance(claims, list):
        for item in _dict_list(result.get("visual_claims")):
            claims.append(
                {
                    "type": "visual_mock" if item.get("is_mock") else "visual",
                    "source": str(result.get("provider") or "vision"),
                    "claim": str(item.get("claim") or ""),
                    "evidence_text": str(item.get("evidence_text") or ""),
                    "timestamp": str(item.get("timestamp") or ""),
                    "chunk_id": "",
                    "frame_id": str(item.get("frame_id") or ""),
                    "confidence": str(item.get("confidence") or "medium"),
                    "is_mock": bool(item.get("is_mock")),
                }
            )


def _nearby_context(
    structured: dict[str, object], frames: list[dict[str, object]]
) -> dict[str, str]:
    contexts: dict[str, str] = {}
    chunks = _dict_list(structured.get("chunks"))
    for frame in frames:
        frame_id = str(frame.get("frame_id", ""))
        timestamp = _number(frame.get("timestamp"))
        overlapping = [
            chunk
            for chunk in chunks
            if _number(chunk.get("start")) - 30 <= timestamp <= _number(chunk.get("end")) + 30
        ]
        if not overlapping and chunks:
            overlapping = [
                min(
                    chunks,
                    key=lambda chunk: min(
                        abs(_number(chunk.get("start")) - timestamp),
                        abs(_number(chunk.get("end")) - timestamp),
                    ),
                )
            ]
        contexts[frame_id] = " ".join(str(chunk.get("text", "")) for chunk in overlapping)[:1200]
    return contexts


def _save_outputs(record_dir: Path, structured: dict[str, object], visual: dict[str, object]) -> None:
    data_dir = record_dir / "data"
    atomic_write_text(data_dir / "structured.json", json.dumps(structured, ensure_ascii=False, indent=2))
    atomic_write_text(data_dir / "visual-analysis.json", json.dumps(visual, ensure_ascii=False, indent=2))


def _vision_prompt(frames: list[dict[str, object]], nearby_context: dict[str, str]) -> str:
    frame_context = [
        {
            "frame_id": str(frame.get("frame_id") or ""),
            "timestamp": str(frame.get("timestamp_text") or ""),
            "nearby_transcript": nearby_context.get(str(frame.get("frame_id") or ""), ""),
        }
        for frame in frames
    ]
    return (
        "分析这些视频关键帧。只输出 JSON 对象，不要 Markdown。识别 PPT、表格、图表、代码、软件 UI、"
        "画面中的关键文字与能够由画面直接支持的结论。无法确认时写入 uncertainties，禁止根据常识补全。\n"
        "返回结构：{\"tables\":[{\"frame_id\":\"\",\"title\":\"\",\"content\":\"\",\"timestamp\":\"\",\"confidence\":\"high/medium/low\"}],"
        "\"charts\":[{\"frame_id\":\"\",\"title\":\"\",\"description\":\"\",\"timestamp\":\"\",\"confidence\":\"high/medium/low\"}],"
        "\"visual_claims\":[{\"claim\":\"\",\"evidence_text\":\"\",\"frame_id\":\"\",\"timestamp\":\"\",\"confidence\":\"high/medium/low\"}],"
        "\"uncertainties\":[\"\"]}\n"
        "帧与附近转写：" + json.dumps(frame_context, ensure_ascii=False)
    )


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    if normalized.endswith("/v1"):
        return normalized + "/chat/completions"
    return normalized + "/v1/chat/completions"


def _parse_provider_response(payload: dict[str, object]) -> dict[str, object]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("响应缺少 choices。")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ValueError("响应缺少 message。")
    content = message.get("content")
    if not isinstance(content, str):
        raise ValueError("响应 content 不是文本。")
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("视觉响应不是 JSON 对象。")
    return parsed


def _normalize_real_result(
    payload: dict[str, object],
    *,
    settings: VisionSettings,
    frames: list[dict[str, object]],
    privacy_notice: str,
) -> dict[str, object]:
    allowed_frames = {str(frame.get("frame_id") or "") for frame in frames}
    tables = _normalize_evidence_items(payload.get("tables"), allowed_frames)
    charts = _normalize_evidence_items(payload.get("charts"), allowed_frames)
    claims = _normalize_evidence_items(payload.get("visual_claims"), allowed_frames)
    for claim in claims:
        claim["is_mock"] = False
    return {
        "mode": "real",
        "provider": settings.provider,
        "model": settings.model,
        "is_mock": False,
        "analyzed_frame_count": len(frames),
        "tables": tables,
        "charts": charts,
        "visual_claims": claims,
        "uncertainties": [str(item) for item in payload.get("uncertainties", [])]
        if isinstance(payload.get("uncertainties"), list)
        else [],
        "privacy_notice": privacy_notice,
    }


def _normalize_evidence_items(value: object, allowed_frames: set[str]) -> list[dict[str, object]]:
    result = []
    for item in _dict_list(value):
        normalized = {str(key): value for key, value in item.items()}
        frame_id = str(normalized.get("frame_id") or "")
        if frame_id not in allowed_frames:
            normalized["frame_id"] = ""
        normalized["is_mock"] = False
        result.append(normalized)
    return result


def _real_failure_result(settings: VisionSettings, message: str, privacy_notice: str) -> dict[str, object]:
    return {
        "mode": "real",
        "provider": settings.provider,
        "model": settings.model,
        "is_mock": False,
        "analyzed_frame_count": 0,
        "tables": [],
        "charts": [],
        "visual_claims": [],
        "uncertainties": [message, "视觉失败未阻塞音频、转写和总结主流程。"],
        "privacy_notice": privacy_notice,
    }


def _safe_error(error: Exception | None) -> str:
    if error is None:
        return "未知错误"
    return " ".join(str(error).split())[:240]


def _load_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _dict_list(value: object) -> list[dict[str, object]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _number(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    return float(value) if isinstance(value, (int, float)) else 0.0
