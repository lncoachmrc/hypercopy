from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from app.core.config import settings


@dataclass(slots=True)
class LLMResult:
    provider: str
    model: str
    data: dict
    latency_ms: int
    attempts: list[dict]


class LLMUnavailable(RuntimeError):
    def __init__(self, attempts: list[dict]):
        super().__init__('All configured LLM providers failed')
        self.attempts = attempts


def _extract_json(text: str) -> dict:
    raw = (text or '').strip()
    if raw.startswith('```'):
        raw = raw.strip('`').strip()
        if raw.lower().startswith('json'):
            raw = raw[4:].strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find('{')
        end = raw.rfind('}')
        if start < 0 or end <= start:
            raise ValueError('LLM response did not contain a JSON object')
        value = json.loads(raw[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError('LLM response must be a JSON object')
    return value


def _post_json(url: str, *, headers: dict[str, str], payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(',', ':')).encode('utf-8'),
        headers={'Content-Type': 'application/json', **headers},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode('utf-8', errors='replace')[:300]
        except Exception:
            detail = ''
        raise RuntimeError(f'HTTP {exc.code}: {detail}') from exc


def _openai_text(response: dict) -> str:
    if isinstance(response.get('output_text'), str):
        return response['output_text']
    for item in response.get('output') or []:
        if item.get('type') != 'message':
            continue
        for content in item.get('content') or []:
            if content.get('type') in {'output_text', 'text'} and isinstance(content.get('text'), str):
                return content['text']
    raise ValueError('OpenAI response contained no output text')


def _provider_sync(provider: str, model: str, api_key: str, system_prompt: str, user_prompt: str) -> dict:
    timeout = settings.LLM_TIMEOUT_SECONDS
    if provider == 'openai':
        response = _post_json(
            'https://api.openai.com/v1/responses',
            headers={'Authorization': f'Bearer {api_key}'},
            payload={
                'model': model,
                'input': [
                    {'role': 'system', 'content': [{'type': 'input_text', 'text': system_prompt}]},
                    {'role': 'user', 'content': [{'type': 'input_text', 'text': user_prompt}]},
                ],
                'max_output_tokens': settings.LLM_MAX_OUTPUT_TOKENS,
            },
            timeout=timeout,
        )
        return _extract_json(_openai_text(response))

    if provider == 'anthropic':
        response = _post_json(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
            },
            payload={
                'model': model,
                'system': system_prompt,
                'messages': [{'role': 'user', 'content': user_prompt}],
                'max_tokens': settings.LLM_MAX_OUTPUT_TOKENS,
            },
            timeout=timeout,
        )
        for item in response.get('content') or []:
            if item.get('type') == 'text' and isinstance(item.get('text'), str):
                return _extract_json(item['text'])
        raise ValueError('Anthropic response contained no text')

    if provider == 'deepseek':
        response = _post_json(
            'https://api.deepseek.com/chat/completions',
            headers={'Authorization': f'Bearer {api_key}'},
            payload={
                'model': model,
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_prompt},
                ],
                'response_format': {'type': 'json_object'},
                'thinking': {'type': 'disabled'},
                'temperature': 0.1,
                'max_tokens': settings.LLM_MAX_OUTPUT_TOKENS,
                'stream': False,
            },
            timeout=timeout,
        )
        choices = response.get('choices') or []
        if not choices:
            raise ValueError('DeepSeek response contained no choices')
        return _extract_json((choices[0].get('message') or {}).get('content') or '')

    raise ValueError(f'Unsupported LLM provider: {provider}')


class LLMRouter:
    """Provider failover with no trading authority.

    The preferred model is attempted first when it matches a configured provider.
    Any timeout, credit/rate-limit error, provider outage, malformed/invalid
    policy response, or authentication failure advances to the next configured
    provider. API secrets never leave this backend adapter.
    """

    def _configured(self) -> list[tuple[str, str, str]]:
        providers = {
            'openai': (settings.OPENAI_MODEL, settings.OPENAI_API_KEY),
            'anthropic': (settings.ANTHROPIC_MODEL, settings.ANTHROPIC_API_KEY),
            'deepseek': (settings.DEEPSEEK_MODEL, settings.DEEPSEEK_API_KEY),
        }
        order = [x.strip().lower() for x in settings.LLM_PROVIDER_ORDER.split(',') if x.strip()]
        for name in providers:
            if name not in order:
                order.append(name)
        rows = [(name, providers[name][0], providers[name][1]) for name in order if name in providers and providers[name][1]]
        preferred = settings.LLM_PREFERRED_MODEL.strip()
        if preferred:
            rows.sort(key=lambda row: 0 if row[1] == preferred else 1)
        return rows

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        allowed_candidate_ids: set[str] | None = None,
    ) -> LLMResult:
        attempts: list[dict] = []
        for provider, model, key in self._configured():
            started = time.perf_counter()
            try:
                data = await asyncio.wait_for(
                    asyncio.to_thread(_provider_sync, provider, model, key, system_prompt, user_prompt),
                    timeout=settings.LLM_TIMEOUT_SECONDS + 2,
                )
                if allowed_candidate_ids is not None:
                    candidate_id = str(data.get('candidate_id') or '')
                    if candidate_id not in allowed_candidate_ids:
                        raise ValueError(f'Unknown candidate_id {candidate_id!r}')
                latency = int((time.perf_counter() - started) * 1000)
                attempts.append({'provider': provider, 'model': model, 'status': 'ok', 'latency_ms': latency})
                return LLMResult(provider=provider, model=model, data=data, latency_ms=latency, attempts=attempts)
            except Exception as exc:
                attempts.append({
                    'provider': provider,
                    'model': model,
                    'status': 'failed',
                    'error': f'{type(exc).__name__}: {str(exc)[:240]}',
                    'latency_ms': int((time.perf_counter() - started) * 1000),
                })
        raise LLMUnavailable(attempts)
