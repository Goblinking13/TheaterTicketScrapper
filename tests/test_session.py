from flightwatch.session import json_shape, safe_endpoint


def test_research_shapes_never_save_scalar_secrets():
    payload={'sessionToken':'sensitive-test-token','price':{'amount':'123.45'},
             'Authorization':'Bearer test-credential','offers':[{'deepLink':'https://test/?token=secret'}]}
    shape=json_shape(payload)
    assert shape['sessionToken']=='string'
    assert 'sensitive-test-token' not in str(shape)
    assert 'test-credential' not in str(shape)
    assert '123.45' not in str(shape)
    assert 'https://' not in str(shape)


def test_research_endpoints_strip_session_and_query():
    value=safe_endpoint('https://example.com/search/poll/01234567-89ab-cdef-0123-456789abcdef?token=secret')
    assert value=={'host':'example.com','path':'/search/poll/<id>'}
