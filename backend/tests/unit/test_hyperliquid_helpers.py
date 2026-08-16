from app.adapters.hyperliquid import deterministic_cloid, fill_event_id, parse_order_response


def test_cloid_is_exactly_128_bits_and_deterministic():
    a=deterministic_cloid('job-1','o')
    assert a==deterministic_cloid('job-1','o')
    assert a.startswith('0x') and len(a)==34
    int(a[2:],16)


def test_parse_filled_order_response():
    r=parse_order_response({'response':{'data':{'statuses':[{'filled':{'oid':7,'totalSz':'0.1','avgPx':'60000'}}]}}})
    assert r.state=='FILLED' and str(r.filled_size)=='0.1'


def test_parse_rejection():
    r=parse_order_response({'response':{'data':{'statuses':[{'error':'Insufficient margin'}]}}})
    assert r.state=='REJECTED'
