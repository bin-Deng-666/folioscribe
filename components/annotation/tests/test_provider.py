import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ocr_agent.errors import ProviderConfigError, ProviderTransientError
from ocr_agent.provider import ModelConfig, OpenAICompatibleProvider
from ocr_agent.task_provider import TaskProvider


def test_config_requires_explicit_values(monkeypatch) -> None:
    for prefix in ("OCR_AGENT", "DS_V4_FLASH"):
        for suffix in ("API_KEY", "BASE_URL", "MODEL"):
            monkeypatch.delenv(f"{prefix}_{suffix}", raising=False)
    with pytest.raises(ProviderConfigError):
        ModelConfig.from_env()


def test_generic_environment_takes_precedence_over_legacy(monkeypatch) -> None:
    monkeypatch.setenv("OCR_AGENT_API_KEY", "generic-key")
    monkeypatch.setenv("OCR_AGENT_BASE_URL", "https://generic.test/v1")
    monkeypatch.setenv("OCR_AGENT_MODEL", "generic-model")
    monkeypatch.setenv("DS_V4_FLASH_API_KEY", "legacy-key")
    monkeypatch.setenv("DS_V4_FLASH_BASE_URL", "https://legacy.test/v1")
    monkeypatch.setenv("DS_V4_FLASH_MODEL", "legacy-model")
    config = ModelConfig.from_env()
    assert (config.api_key, config.base_url, config.model) == ("generic-key", "https://generic.test/v1", "generic-model")


def test_endpoint_is_openai_compatible() -> None:
    provider = OpenAICompatibleProvider(ModelConfig("secret", "https://example.test/v1", "model"))
    assert provider._endpoint() == "https://example.test/v1/chat/completions"


def test_http_response_is_parsed_and_trace_is_redacted() -> None:
    """The semantic-task transport is the production path: same endpoint/auth/JSON contract."""
    captured = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers["content-length"])
            captured["path"] = self.path
            captured["authorization"] = self.headers.get("authorization")
            captured["payload"] = json.loads(self.rfile.read(size))
            decision = {
                "task_id": "sample.p1.semantic.1",
                "decisions": [{"block_id": "candidate.block.p1.0000", "type": "title", "level": 1, "evidence_refs": ["pdf.line.p1.0000"]}],
                "unresolved": [],
            }
            envelope = {
                "choices": [{"message": {"content": json.dumps(decision)}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5},
            }
            body = json.dumps(envelope).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.send_header("x-request-id", "local-test-request")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}/v1"
        provider = TaskProvider(OpenAICompatibleProvider(ModelConfig("test-secret", base_url, "test-model")))
        task = {
            "task_id": "sample.p1.semantic.1",
            "items": [{
                "block_id": "candidate.block.p1.0000",
                "allowed_types": ["title", "text"],
                "allowed_levels": [1],
                "allowed_parent_ids": [],
                "evidence": [{"evidence_id": "pdf.line.p1.0000"}],
            }],
        }
        response, trace = provider.complete_semantic(task)
    finally:
        server.shutdown()
        thread.join()

    assert response["decisions"][0]["block_id"] == "candidate.block.p1.0000"
    assert captured["path"] == "/v1/chat/completions"
    assert captured["authorization"] == "Bearer test-secret"
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert trace["request_id"] == "local-test-request"
    assert trace["usage"]["prompt_tokens"] == 12


def test_task_provider_retries_transient_errors() -> None:
    calls = 0

    class Base:
        config = ModelConfig("secret", "https://example.test/v1", "model")

        def _endpoint(self):
            return "https://example.test/v1/chat/completions"

        def _post(self, endpoint, payload):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ProviderTransientError("temporary upstream EOF")
            body = {"task_id": "task", "decisions": [{"block_id": "block", "type": "text", "evidence_refs": ["e1"]}], "unresolved": []}
            return {"choices": [{"message": {"content": json.dumps(body)}}]}, 200, "request", 0.01

        @staticmethod
        def _content_text(value):
            return value

        @staticmethod
        def _strip_fence(value):
            return value

    TaskProvider(Base()).complete_semantic({
        "task_id": "task",
        "items": [{"block_id": "block", "allowed_types": ["text"], "allowed_levels": [], "allowed_parent_ids": [], "evidence": [{"evidence_id": "e1"}]}],
    })
    assert calls == 3


def test_semantic_task_uses_extended_output_budget() -> None:
    captured = {}

    class Base:
        config = ModelConfig("secret", "https://example.test/v1", "model")

        def _endpoint(self):
            return "https://example.test/v1/chat/completions"

        def _post(self, endpoint, payload):
            captured["payload"] = payload
            body = {"task_id": "task", "decisions": [{"block_id": "block", "type": "text", "evidence_refs": ["e1"]}], "unresolved": []}
            return {"choices": [{"message": {"content": json.dumps(body)}}]}, 200, "request", 0.01

        @staticmethod
        def _content_text(value):
            return value

        @staticmethod
        def _strip_fence(value):
            return value

    TaskProvider(Base()).complete_semantic({
        "task_id": "task",
        "items": [{"block_id": "block", "allowed_types": ["text"], "evidence": [{"evidence_id": "e1"}]}],
    })
    assert captured["payload"]["max_tokens"] == 16384
