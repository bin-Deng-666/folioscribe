from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .errors import ProviderConfigError, ProviderPermanentError, ProviderTransientError


@dataclass(frozen=True)
class ModelConfig:
    api_key: str
    base_url: str
    model: str
    timeout_seconds: int = 90
    max_output_tokens: int = 16384
    temperature: float = 0.0
    vision_enabled: bool = False
    reasoning_effort: str | None = "low"

    @classmethod
    def from_env(cls) -> "ModelConfig":
        def setting(name: str, legacy: str, default: str = "") -> str:
            return os.environ.get(name, os.environ.get(legacy, default)).strip()

        key = setting("OCR_AGENT_API_KEY", "DS_V4_FLASH_API_KEY")
        base = setting("OCR_AGENT_BASE_URL", "DS_V4_FLASH_BASE_URL")
        model = setting("OCR_AGENT_MODEL", "DS_V4_FLASH_MODEL")
        missing = [name for name, value in (("OCR_AGENT_API_KEY", key), ("OCR_AGENT_BASE_URL", base), ("OCR_AGENT_MODEL", model)) if not value]
        if missing:
            raise ProviderConfigError(f"missing environment variables: {', '.join(missing)}")
        return cls(api_key=key, base_url=base, model=model,
            timeout_seconds=int(setting("OCR_AGENT_TIMEOUT_SECONDS", "DS_V4_FLASH_TIMEOUT_SECONDS", "90")),
            max_output_tokens=int(setting("OCR_AGENT_MAX_OUTPUT_TOKENS", "DS_V4_FLASH_MAX_OUTPUT_TOKENS", "16384")),
            vision_enabled=setting("OCR_AGENT_VISION", "DS_V4_FLASH_VISION", "0").lower() in {"1", "true", "yes"},
            reasoning_effort=(None if setting("OCR_AGENT_REASONING_EFFORT", "DS_V4_FLASH_REASONING_EFFORT", "low").lower() in {"", "off", "none"} else setting("OCR_AGENT_REASONING_EFFORT", "DS_V4_FLASH_REASONING_EFFORT", "low")))


class OpenAICompatibleProvider:
    def __init__(self, config: ModelConfig) -> None:
        self.config = config

    def _post(self, endpoint: str, payload: dict[str, Any]) -> tuple[dict[str, Any], int, str | None, float]:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=encoded,
            method="POST",
            headers={"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"},
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw_bytes = response.read()
                status_code = response.status
                request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
        except urllib.error.HTTPError as exc:
            body = exc.read(2048).decode("utf-8", errors="replace")
            if exc.code == 429 or 500 <= exc.code < 600:
                raise ProviderTransientError(f"HTTP {exc.code}: {body}") from exc
            if "not a multimodal model" in body.lower():
                raise ProviderPermanentError(f"HTTP {exc.code}: configured model does not support page images") from exc
            raise ProviderPermanentError(f"HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise ProviderTransientError(f"request failed: {exc}") from exc
        elapsed = round(time.monotonic() - started, 3)
        try:
            envelope = json.loads(raw_bytes)
        except json.JSONDecodeError as exc:
            raise ProviderPermanentError(f"HTTP {status_code} returned non-JSON response") from exc
        if not isinstance(envelope, dict):
            raise ProviderPermanentError(f"HTTP {status_code} returned non-object JSON")
        return envelope, status_code, request_id, elapsed

    def _endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        return base if base.endswith("/chat/completions") else f"{base}/chat/completions"

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(part.get("text", "") for part in content if isinstance(part, dict))
        raise TypeError("message content is neither string nor list")

    @staticmethod
    def _strip_fence(content: str) -> str:
        text = content.strip()
        if text.startswith("```"):
            first_newline = text.find("\n")
            text = text[first_newline + 1 :] if first_newline >= 0 else text[3:]
            if text.endswith("```"):
                text = text[:-3]
        return text.strip()



# Backward-compatible aliases for existing callers.
DSConfig = ModelConfig
DSOpenAICompatibleProvider = OpenAICompatibleProvider
