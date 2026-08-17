from __future__ import annotations

import asyncio
import json
import os
import statistics
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import MasterEvent, SystemFlag

AI_FLAG_SLUG = 'ai:master_strategy_intelligence'


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def _provider_models() -> dict[str, str]:
    return {
        'openai': os.getenv('OPENAI_MODEL', '').strip(),
        'anthropic': os.getenv('ANTHROPIC_MODEL', '').strip(),
        'deepseek': os.getenv('DEEPSEEK_MODEL', 'deepseek-v4-flash').strip(),
    }


def provider_chain() -> list[tuple[str, str]]:
    configured = _provider_models()
    raw: list[str] = []
    preferred = os.getenv('LLM_PREFERRED_MODEL', '').strip()
    if preferred:
        raw.append(preferred)
    raw.extend(x.strip() for x in os.getenv('LLM_FALLBACK_MODELS', '').split(',') if x.strip())
    for provider, model in configured.items():
        if model:
            raw.append(f'{provider}:{model}')

    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if ':' not in item:
            continue
        provider, model = item.split(':', 1)
        key = (provider.strip().lower(), model.strip())
        if key[0] not in {'openai', 'anthropic', 'deepseek'} or not key[1] or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _api_key(provider: str) -> str:
    return os.getenv(f'{provider.upper()}_API_KEY', '').strip()


def _json_from_text(text: str) -> dict:
    text = text.strip()
    if text.startswith('```'):
        text = text.strip('`').strip()
        if text.lower().startswith('json'):
            text = text[4:].strip()
    start = text.find('{')
    end = text.rfind('}')
    if start >= 0 and end > start:
        text = text[start:end + 1]
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError('LLM response must be a JSON object')
    return value


def _post_json(url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', **headers},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', errors='replace')[:500]
        raise RuntimeError(f'HTTP {exc.code}: {body}') from exc


def _openai_text(data: dict) -> str:
    parts: list[str] = []
    for item in data.get('output', []):
        if item.get('type') != 'message':
            continue
        for content in item.get('content', []):
            if content.get('type') in {'output_text', 'text'} and content.get('text'):
                parts.append(str(content['text']))
    return '\n'.join(parts)


def _call_provider_sync(provider: str, model: str, system: str, prompt: str, timeout: float) -> dict:
    key = _api_key(provider)
    if not key:
        raise RuntimeError('API key not configured')

    if provider == 'openai':
        data = _post_json(
            'https://api.openai.com/v1/responses',
            {'Authorization': f'Bearer {key}'},
            {'model': model, 'input': f'{system}\n\n{prompt}'},
            timeout,
        )
        text = _openai_text(data)
    elif provider == 'anthropic':
        data = _post_json(
            'https://api.anthropic.com/v1/messages',
            {'x-api-key': key, 'anthropic-version': '2023-06-01'},
            {'model': model, 'max_tokens': 1400, 'system': system, 'messages': [{'role': 'user', 'content': prompt}]},
            timeout,
        )
        text = '\n'.join(str(x.get('text', '')) for x in data.get('content', []) if x.get('type') == 'text')
    else:
        data = _post_json(
            'https://api.deepseek.com/chat/completions',
            {'Authorization': f'Bearer {key}'},
            {
                'model': model,
                'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': prompt}],
                'response_format': {'type': 'json_object'},
                'temperature': 0.1,
            },
            timeout,
        )
        text = str(data.get('choices', [{}])[0].get('message', {}).get('content', ''))
    if not text:
        raise RuntimeError('Provider returned no text')
    return _json_from_text(text)


async def call_llm_with_failover(system: str, prompt: str) -> tuple[dict, dict]:
    failures: list[dict] = []
    timeout = float(os.getenv('LLM_TIMEOUT_SECONDS', '25'))
    chain = provider_chain()
    if not chain:
        raise RuntimeError('No LLM model chain configured')

    for index, (provider, model) in enumerate(chain):
        try:
            result = await asyncio.to_thread(_call_provider_sync, provider, model, system, prompt, timeout)
            return result, {
                'provider': provider,
                'model': model,
                'fallback_index': index,
                'failures': failures,
            }
        except Exception as exc:
            failures.append({'provider': provider, 'model': model, 'error': f'{type(exc).__name__}: {exc}'[:600]})
    raise RuntimeError(json.dumps({'all_providers_failed': failures}))


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:
        return Decimal(0)


def learn_master_strategy(events: list[MasterEvent]) -> dict:
    by_asset: dict[str, list[MasterEvent]] = defaultdict(list)
    for event in events:
        by_asset[event.asset].append(event)

    assets: list[dict] = []
    total_notional = Decimal(0)
    total_micro = 0
    total_scale_ins = 0
    completed_holds: list[float] = []

    for asset, rows in by_asset.items():
        rows.sort(key=lambda x: x.event_ts)
        notionals: list[float] = []
        holds: list[float] = []
        opened_at: datetime | None = None
        scale_ins = 0
        reductions = 0
        reversals = 0
        for row in rows:
            start = _decimal(row.start_position)
            after = _decimal(row.position_after)
            notional = abs(_decimal(row.size) * _decimal(row.price))
            total_notional += notional
            notionals.append(float(notional))
            if notional < Decimal('10'):
                total_micro += 1
            if start == 0 and after != 0:
                opened_at = row.event_ts
            elif start != 0 and after == 0 and opened_at is not None:
                minutes = max((row.event_ts - opened_at).total_seconds() / 60, 0)
                holds.append(minutes)
                completed_holds.append(minutes)
                opened_at = None
            if start != 0 and after != 0 and (start > 0) == (after > 0):
                if abs(after) > abs(start):
                    scale_ins += 1
                    total_scale_ins += 1
                elif abs(after) < abs(start):
                    reductions += 1
            if start != 0 and after != 0 and (start > 0) != (after > 0):
                reversals += 1

        assets.append({
            'asset': asset,
            'events': len(rows),
            'median_fill_notional': round(statistics.median(notionals), 4) if notionals else 0,
            'median_hold_minutes': round(statistics.median(holds), 2) if holds else None,
            'scale_ins': scale_ins,
            'reductions': reductions,
            'reversals': reversals,
        })

    assets.sort(key=lambda x: x['events'], reverse=True)
    n = max(len(events), 1)
    return {
        'lookback_events': len(events),
        'assets_traded': len(by_asset),
        'gross_fill_notional': round(float(total_notional), 2),
        'micro_fill_pct': round(total_micro / n * 100, 2),
        'scale_in_event_pct': round(total_scale_ins / n * 100, 2),
        'median_completed_hold_minutes': round(statistics.median(completed_holds), 2) if completed_holds else None,
        'top_assets': assets[:20],
    }


def _validated_analysis(raw: dict) -> dict:
    policy = raw.get('capital_policy') if isinstance(raw.get('capital_policy'), dict) else {}
    def clamp(name: str, default: float, lo: float, hi: float) -> float:
        try:
            value = float(policy.get(name, default))
        except Exception:
            value = default
        return round(max(lo, min(value, hi)), 4)

    micro = str(policy.get('micro_position_policy', 'ignore_until_executable'))
    if micro not in {'aggregate', 'ignore_until_executable', 'exact'}:
        micro = 'ignore_until_executable'
    urgency = str(policy.get('rebalance_urgency', 'medium')).lower()
    if urgency not in {'low', 'medium', 'high'}:
        urgency = 'medium'

    try:
        confidence = max(0.0, min(float(raw.get('confidence', 0.5)), 1.0))
    except Exception:
        confidence = 0.5
    return {
        'summary': str(raw.get('summary', ''))[:1200],
        'observed_patterns': [str(x)[:300] for x in raw.get('observed_patterns', [])[:12]],
        'capital_policy': {
            'buffer_pct': clamp('buffer_pct', 0.10, 0.05, 0.30),
            'minimum_coverage_pct': clamp('minimum_coverage_pct', 0.75, 0.60, 0.95),
            'preferred_coverage_pct': clamp('preferred_coverage_pct', 0.90, 0.75, 0.99),
            'micro_position_policy': micro,
            'rebalance_urgency': urgency,
        },
        'confidence': round(confidence, 4),
    }


async def refresh_ai_intelligence(db: AsyncSession, *, force: bool = False) -> dict:
    enabled = _env_bool('LLM_ENABLED', False)
    mode = os.getenv('LLM_MODE', 'shadow').strip().lower() or 'shadow'
    row = await db.get(SystemFlag, AI_FLAG_SLUG)
    now = datetime.now(UTC)
    interval = max(int(os.getenv('LLM_ANALYSIS_INTERVAL_SECONDS', '900')), 60)
    if not force and row and row.updated_at and (now - row.updated_at).total_seconds() < interval:
        return row.value or {}

    if not enabled:
        value = {'status': 'disabled', 'mode': mode, 'updated_at': now.isoformat()}
    else:
        days = max(min(int(os.getenv('LLM_LOOKBACK_DAYS', '30')), 180), 1)
        cutoff = now - timedelta(days=days)
        events = (await db.execute(
            select(MasterEvent).where(MasterEvent.event_ts >= cutoff).order_by(MasterEvent.event_ts.desc()).limit(5000)
        )).scalars().all()
        events = list(reversed(events))
        latest_event = events[-1] if events else None
        profile = learn_master_strategy(events)
        preferred = os.getenv('LLM_PREFERRED_MODEL', '').strip()
        system = (
            'You are Traxion Capital Intelligence. Analyze an observed master copy-trading strategy. '
            'You are advisory only: never invent orders, leverage, prices, or risk overrides. '
            'Return JSON only with keys summary, observed_patterns, capital_policy, confidence. '
            'capital_policy must include buffer_pct, minimum_coverage_pct, preferred_coverage_pct, '
            'micro_position_policy, rebalance_urgency.'
        )
        prompt = json.dumps({'master_strategy_profile': profile, 'execution_mode': mode}, separators=(',', ':'))
        try:
            raw, runtime = await call_llm_with_failover(system, prompt)
            value = {
                'status': 'ok',
                'mode': mode,
                'preferred_model': preferred,
                'provider': runtime['provider'],
                'model': runtime['model'],
                'fallback_index': runtime['fallback_index'],
                'provider_failures': runtime['failures'],
                'strategy_profile': profile,
                'analysis': _validated_analysis(raw),
                'source_event_ts': latest_event.event_ts.isoformat() if latest_event else None,
                'source_event_id': latest_event.exchange_event_id if latest_event else None,
                'updated_at': now.isoformat(),
            }
        except Exception as exc:
            previous = (row.value or {}) if row else {}
            value = {
                **previous,
                'status': 'degraded',
                'mode': mode,
                'preferred_model': preferred,
                'last_error': f'{type(exc).__name__}: {exc}'[:1200],
                'strategy_profile': profile,
                'attempted_source_event_ts': latest_event.event_ts.isoformat() if latest_event else None,
                'attempted_source_event_id': latest_event.exchange_event_id if latest_event else None,
                'updated_at': now.isoformat(),
            }

    if not row:
        row = SystemFlag(slug=AI_FLAG_SLUG, enabled=enabled, value=value, reason='Traxion LLM capital intelligence')
        db.add(row)
    else:
        row.enabled = enabled
        row.value = value
    await db.commit()
    return value


async def read_ai_intelligence(db: AsyncSession) -> dict:
    row = await db.get(SystemFlag, AI_FLAG_SLUG)
    if row:
        return row.value or {}
    return {
        'status': 'pending' if _env_bool('LLM_ENABLED', False) else 'disabled',
        'mode': os.getenv('LLM_MODE', 'shadow'),
        'preferred_model': os.getenv('LLM_PREFERRED_MODEL', ''),
        'updated_at': None,
    }
