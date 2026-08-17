import asyncio

import app.adapters.llm as llm_module
from app.adapters.llm import LLMRouter
from app.core.config import settings


def test_preferred_model_is_attempted_first_and_invalid_output_fails_over(monkeypatch):
    monkeypatch.setattr(settings, 'OPENAI_API_KEY', 'openai-test-key')
    monkeypatch.setattr(settings, 'ANTHROPIC_API_KEY', 'anthropic-test-key')
    monkeypatch.setattr(settings, 'DEEPSEEK_API_KEY', 'deepseek-test-key')
    monkeypatch.setattr(settings, 'OPENAI_MODEL', 'openai-model')
    monkeypatch.setattr(settings, 'ANTHROPIC_MODEL', 'anthropic-model')
    monkeypatch.setattr(settings, 'DEEPSEEK_MODEL', 'deepseek-model')
    monkeypatch.setattr(settings, 'LLM_PROVIDER_ORDER', 'openai,anthropic,deepseek')
    monkeypatch.setattr(settings, 'LLM_PREFERRED_MODEL', 'anthropic-model')
    monkeypatch.setattr(settings, 'LLM_TIMEOUT_SECONDS', 1.0)

    calls = []

    def fake_provider(provider, model, api_key, system_prompt, user_prompt):
        calls.append((provider, model))
        if provider == 'anthropic':
            return {'candidate_id': 'invented', 'confidence': 0.9}
        return {'candidate_id': 'exact', 'confidence': 0.8, 'summary': 'safe'}

    monkeypatch.setattr(llm_module, '_provider_sync', fake_provider)
    result = asyncio.run(LLMRouter().complete_json(
        system_prompt='system', user_prompt='user', allowed_candidate_ids={'exact'},
    ))

    assert calls[:2] == [('anthropic', 'anthropic-model'), ('openai', 'openai-model')]
    assert result.provider == 'openai'
    assert result.model == 'openai-model'
    assert result.data['candidate_id'] == 'exact'
    assert result.attempts[0]['status'] == 'failed'
    assert result.attempts[1]['status'] == 'ok'


def test_unconfigured_providers_are_skipped(monkeypatch):
    monkeypatch.setattr(settings, 'OPENAI_API_KEY', '')
    monkeypatch.setattr(settings, 'ANTHROPIC_API_KEY', '')
    monkeypatch.setattr(settings, 'DEEPSEEK_API_KEY', 'deepseek-test-key')
    monkeypatch.setattr(settings, 'DEEPSEEK_MODEL', 'deepseek-model')
    monkeypatch.setattr(settings, 'LLM_PROVIDER_ORDER', 'openai,anthropic,deepseek')
    monkeypatch.setattr(settings, 'LLM_PREFERRED_MODEL', '')

    calls = []

    def fake_provider(provider, model, api_key, system_prompt, user_prompt):
        calls.append(provider)
        return {'candidate_id': 'smart_balanced'}

    monkeypatch.setattr(llm_module, '_provider_sync', fake_provider)
    result = asyncio.run(LLMRouter().complete_json(
        system_prompt='system', user_prompt='user', allowed_candidate_ids={'smart_balanced'},
    ))

    assert calls == ['deepseek']
    assert result.provider == 'deepseek'
