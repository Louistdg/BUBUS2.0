"""Small local-only provider contract; no tools, credentials, or remote hosts."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class GenerationRequest:
    model: str
    system: str
    prompt: str
    schema: dict[str, Any]
    num_ctx: int = 4096
    num_predict: int = 512
    temperature: float = 0.0
    seed: int = 17


@dataclass(frozen=True)
class GenerationResult:
    model: str
    text: str
    elapsed_seconds: float
    total_duration_ns: int | None
    load_duration_ns: int | None
    eval_duration_ns: int | None
    prompt_tokens: int | None
    generated_tokens: int | None
    done_reason: str | None


class LLMProvider(Protocol):
    def generate(self, request: GenerationRequest) -> GenerationResult: ...


class OllamaLocalProvider:
    """Use only an explicitly configured loopback Ollama endpoint."""

    def __init__(self, base_url: str = 'http://127.0.0.1:11435', timeout: float = 300.0):
        parsed = urlsplit(base_url)
        if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1'}:
            raise ValueError('LLM benchmark endpoint must be plain HTTP on loopback only.')
        if parsed.port != 11435:
            raise ValueError('LLM benchmark only accepts the dedicated INVEST-AI port 11435.')
        if parsed.path not in {'', '/'}:
            raise ValueError('LLM benchmark endpoint must be the root of the dedicated local server.')
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Endpoint credentials, query strings, and fragments are forbidden.')
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout

    def list_models(self) -> list[dict[str, Any]]:
        """Read installed model tags from the dedicated local endpoint."""
        request = Request(self.base_url + '/api/tags', headers={'Accept': 'application/json'}, method='GET')
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode('utf-8'))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise ProviderError(f'Cannot read installed models from local Ollama: {exc}') from exc
        models = payload.get('models') if isinstance(payload, dict) else None
        if not isinstance(models, list):
            raise ProviderError('Local Ollama /api/tags returned an invalid response.')
        return [item for item in models if isinstance(item, dict)]

    def generate(self, request: GenerationRequest) -> GenerationResult:
        payload = {
            'model': request.model,
            'messages': [
                {'role': 'system', 'content': request.system},
                {'role': 'user', 'content': request.prompt},
            ],
            'format': request.schema,
            'stream': False,
            'keep_alive': '30s',
            'options': {
                'num_ctx': request.num_ctx,
                'num_predict': request.num_predict,
                'temperature': request.temperature,
                'seed': request.seed,
            },
        }
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        http_request = Request(self.base_url + '/api/chat', data=body,
                               headers={'Content-Type': 'application/json'}, method='POST')
        started = time.perf_counter()
        try:
            with urlopen(http_request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode('utf-8'))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise ProviderError(f'Local Ollama request failed: {exc}') from exc
        elapsed = time.perf_counter() - started
        message = result.get('message') or {}
        if not isinstance(message.get('content'), str):
            raise ProviderError('Local Ollama response has no text content.')
        return GenerationResult(
            model=str(result.get('model', request.model)),
            text=message['content'],
            elapsed_seconds=elapsed,
            total_duration_ns=_optional_int(result.get('total_duration')),
            load_duration_ns=_optional_int(result.get('load_duration')),
            eval_duration_ns=_optional_int(result.get('eval_duration')),
            prompt_tokens=_optional_int(result.get('prompt_eval_count')),
            generated_tokens=_optional_int(result.get('eval_count')),
            done_reason=str(result['done_reason']) if result.get('done_reason') is not None else None,
        )


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None
