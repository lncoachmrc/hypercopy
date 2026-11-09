from decimal import Decimal as D
import importlib
import pytest


def quote(**changes):
    try:
        module = importlib.import_module('app.services.risex_profit_exit_collector')
    except ModuleNotFoundError:
        module = None
    fn = getattr(module, 'quote_risex_shadow', None)
    assert callable(fn), 'missing RISEx economic adapter'
    values = dict(position=D('-2'), entry=D('100'), bid=D('89'), ask=D('90'),
                  tick=D('0.01'), slippage_bps=50, fee_bps=D('10'))
    return fn(**{**values, **changes})


def test_short_exit_uses_ask_and_adverse_slippage_with_both_fees():
    q = quote()
    assert q['exit_price'] == D('90.45')
    assert q['gross'] == D('19.10')
    assert q['net'] == D('18.71910')
    assert q['entry_fee'] == D('0.2')


def test_long_exit_uses_bid_and_rounds_down_not_up():
    q = quote(position=D('1'), bid=D('110.01'), ask=D('111'), tick=D('0.1'))
    assert q['exit_price'] == D('109.4')
    assert q['net'] == D('9.1906')


def test_loss_is_measured_not_clamped_to_zero():
    assert quote(ask=D('110'), bid=D('109'))['net'] < 0


@pytest.mark.parametrize('change', [
    {'fee_bps':None}, {'fee_bps':D('-1')}, {'fee_bps':D('NaN')},
    {'position':D('0')}, {'entry':D('0')}, {'bid':D('91')},
    {'tick':D('0')}, {'slippage_bps':True}, {'slippage_bps':10000},
    {'ask':D('Infinity')},
])
def test_unknown_or_invalid_prices_and_fees_fail_closed(change):
    with pytest.raises(ValueError):
        quote(**change)
