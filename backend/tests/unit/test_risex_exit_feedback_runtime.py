"""Behavioral tests: real evaluator, mocked only provider/database boundaries."""
from decimal import Decimal as D
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import uuid

import pytest

from app.models.entities import AIProfitExitDecision, CopyState, MasterEvent, UserState
from app.services.ai_profit_exit import ProfitExitAction, ProfitExitFeatureMode
from app.services.ai_profit_exit_decision import _decide_with_ai, _decision_inputs
from app.services import ai_profit_exit_decision as shared
from app.services import risex_profit_exit_shadow as pilot
from app.services.risex_profit_exit_collector import collect_risex_shadow_profit_exit


class PublicAPI:
    public_read_only = True
    def __init__(self):
        self.calls = []
    async def get_json(self, path, *, params=None):
        self.calls.append((path,params))
        if path == '/v1/markets':
            return {'markets':[{'market_id':1,'base_asset_symbol':'BTC','active':True,
                'config':{'step_size':'0.001','step_price':'0.01','min_order_size':'0.001',
                          'unlocked':True,'max_leverage':'20'}}]}
        if path == '/v1/orderbook':
            return {'bids':[{'price':'89'}],'asks':[{'price':'90'}]}
        raise AssertionError('Unexpected provider endpoint')
    async def post_json(self, *args, **kwargs):
        raise AssertionError('No provider write is permitted')


@pytest.mark.asyncio
async def test_collector_uses_real_public_read_parsers_without_signer(monkeypatch):
    monkeypatch.setenv('ENABLE_LIVE_TRADING','false')
    monkeypatch.setenv('RISEX_SHADOW_TAKER_FEE_BPS','10')
    api = PublicAPI()
    obs = await collect_risex_shadow_profit_exit(
        api=api, network='testnet', asset='BTC', shadow_position=D('-2'),
        master_perp_state={'assetPositions':[{'position':{'coin':'BTC','szi':'-4','entryPx':'100'}}]},
        slippage_bps=50,
    )
    assert obs.complete and obs.eligible
    assert obs.economics.net_pnl == D('18.71910')
    assert obs.pnl_complete is False
    assert obs.economics.residual_funding is None
    assert obs.basis == 'risex_copy_shadow_estimate'
    assert [p for p,_ in api.calls] == ['/v1/markets','/v1/orderbook']


@pytest.mark.parametrize('network,live,fee', [('mainnet','false','10'),('testnet','true','10'),('testnet','false',None)])
@pytest.mark.asyncio
async def test_collector_rejects_mainnet_live_stack_and_unknown_fees(monkeypatch,network,live,fee):
    monkeypatch.setenv('ENABLE_LIVE_TRADING',live)
    if fee is None:
        monkeypatch.delenv('RISEX_SHADOW_TAKER_FEE_BPS',raising=False)
    else:
        monkeypatch.setenv('RISEX_SHADOW_TAKER_FEE_BPS',fee)
    api=PublicAPI()
    obs=await collect_risex_shadow_profit_exit(api=api,network=network,asset='BTC',shadow_position=D('-1'),
        master_perp_state={'assetPositions':[{'position':{'coin':'BTC','szi':'-1','entryPx':'100'}}]},slippage_bps=50)
    assert obs.eligible is False and obs.economics is None
    assert api.calls == []


def fixture(monkeypatch,net='5'):
    monkeypatch.setenv('ENABLE_LIVE_TRADING','false')
    user_id,epoch_id=uuid.uuid4(),uuid.uuid4()
    account_address='0x'+'1'*40
    user=NS(id=user_id,state=UserState.ACTIVE,copy_state=CopyState.SHADOW,
            execution_provider='risex',execution_network='testnet',active_execution_epoch_id=epoch_id)
    ledger=NS(user_id=user_id,managed=True,target_size=D('-1'),asset='BTC')
    account=NS(user_id=user_id,account_address=account_address)
    epoch=NS(id=epoch_id,user_id=user_id,provider='risex',network='testnet',ended_at=None,account_address=account_address)
    risk=NS(max_slippage_bps=50)
    cycle=NS(open_event_id=uuid.uuid4(),source_cycle_id='testnet:BTC:current',state_version=42,master_position=D('-1'))
    economics=NS(net_pnl=D(net),gross_price_pnl=D(net),residual_entry_fees=D('0.1'),
                 residual_funding=None,estimated_exit_fee=D('0.1'))
    obs=NS(complete=True,eligible=D(net)>0,current_position=D('-1'),economics=economics,
           entry_price=D('100'),mark_price=D('90'),executable_exit_price=D('91'),
           taker_fee_rate=D('0.001'),pnl_complete=False,basis='risex_copy_shadow_estimate',funding_model='not_modeled_fee_10bps')
    class DB:
        added=[]
        def __init__(self): self.added=[]; self.commits=0; self.rollbacks=0
        async def execute(self,stmt): return NS(all=lambda:[(user,ledger,account,risk,epoch)])
        async def get(self,model,key): return NS() if model is MasterEvent else None
        def add(self,obj): self.added.append(obj)
        async def commit(self): self.commits+=1
        async def rollback(self): self.rollbacks+=1
    monkeypatch.setattr(pilot,'read_current_source_cycle',AsyncMock(return_value=cycle))
    monkeypatch.setattr(pilot,'is_master_source_user',lambda u:False)
    monkeypatch.setattr(pilot,'collect_risex_shadow_profit_exit',AsyncMock(return_value=obs))
    monkeypatch.setattr(pilot,'record_shadow_observation',AsyncMock())
    monkeypatch.setattr(pilot,'read_retention_context',AsyncMock(return_value={'status':'observed'}))
    monkeypatch.setattr(pilot,'read_exit_feedback',AsyncMock(return_value={'status':'warming_up'}))
    monkeypatch.setattr(pilot,'user_destination_state',AsyncMock(return_value=NS(epoch_id=epoch_id,provider='risex',network='testnet')))
    monkeypatch.setattr(pilot,'read_profit_exit_mode',AsyncMock(return_value=ProfitExitFeatureMode.SHADOW))
    decide=AsyncMock(return_value=(ProfitExitAction.CLOSE_PROFIT,'fixture',{'provider':'test','model':'test'}))
    args=dict(mode=ProfitExitFeatureMode.SHADOW,master_snapshot=NS(perp_state={'assetPositions':[{'position':{'coin':'BTC','szi':'-1'}}]}),
              snapshot_started_order=43,intelligence={},evaluation_slot=100,decide=decide,inputs_builder=_decision_inputs,api=PublicAPI())
    return DB(),user,epoch,args,decide


@pytest.mark.asyncio
async def test_risex_account_without_hl_account_gets_record_only_decision(monkeypatch):
    db,user,epoch,args,decide=fixture(monkeypatch)
    stats=await pilot.evaluate_risex_shadow_portfolio(db,**args)
    assert stats['errors']==0 and stats['decisions']==1 and stats['queued']==0
    assert len(db.added)==1 and isinstance(db.added[0],AIProfitExitDecision)
    assert db.added[0].copy_job_id is None and db.added[0].intent_state is None
    assert db.added[0].execution_provider=='risex' and db.added[0].pnl_complete is False
    assert db.added[0].decision_inputs['decision_mode']=='SHADOW'
    decide.assert_awaited_once()


@pytest.mark.parametrize('net',['0','-1'])
@pytest.mark.asyncio
async def test_losses_still_recorded_without_calling_model(monkeypatch,net):
    db,_,_,args,decide=fixture(monkeypatch,net)
    stats=await pilot.evaluate_risex_shadow_portfolio(db,**args)
    assert stats['observations']==1 and stats['decisions']==0 and not db.added
    decide.assert_not_awaited()


@pytest.mark.parametrize('mode',[ProfitExitFeatureMode.ON,ProfitExitFeatureMode.OFF])
@pytest.mark.asyncio
async def test_on_off_never_enable_risex_pilot(monkeypatch,mode):
    db,_,_,args,decide=fixture(monkeypatch)
    args['mode']=mode
    stats=await pilot.evaluate_risex_shadow_portfolio(db,**args)
    assert stats['evaluated']==0 and not db.added
    decide.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_destination_during_model_call_discards_decision(monkeypatch):
    db,_,_,args,decide=fixture(monkeypatch)
    monkeypatch.setattr(pilot,'user_destination_state',AsyncMock(return_value=NS(epoch_id=uuid.uuid4(),provider='risex',network='testnet')))
    stats=await pilot.evaluate_risex_shadow_portfolio(db,**args)
    assert stats['decisions']==0 and not db.added and stats['queued']==0


@pytest.mark.asyncio
async def test_wrong_account_binding_never_reads_or_calls_ai(monkeypatch):
    db,_,epoch,args,decide=fixture(monkeypatch)
    epoch.account_address='0x'+'2'*40
    stats=await pilot.evaluate_risex_shadow_portfolio(db,**args)
    assert stats['decisions']==0 and stats['observations']==0
    decide.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_prompt_accepts_mature_feedback_only_in_shadow(monkeypatch):
    monkeypatch.setenv('LLM_ENABLED','true')
    provider=AsyncMock(return_value=({'action':'HOLD','reason':'test'},{}))
    monkeypatch.setattr(shared,'call_llm_with_failover',provider)
    payload={'exit_feedback':{'version':'exit_feedback_30m_v1','status':'observed'},'decision_mode':'ON'}
    await _decide_with_ai(payload)
    assert 'EXIT FEEDBACK:' not in provider.call_args.args[0]
    payload['decision_mode']='SHADOW'
    await _decide_with_ai(payload)
    assert 'EXIT FEEDBACK:' in provider.call_args.args[0]
    assert 'NOT realized returns' in provider.call_args.args[0]
