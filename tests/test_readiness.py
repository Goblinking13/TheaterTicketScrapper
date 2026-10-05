from flightwatch.readiness import diagnose


def configured():
    return {'PROVIDER':'partner','SKYSCANNER_API_KEY':'private-test-key',
            'SUPABASE_SERVICE_ROLE_KEY':'private-db-key','SUPABASE_URL':'https://example.supabase.co',
            'CAMPAIGN_START':'2027-01-20T00:00:00Z'}


def test_readiness_fails_for_unimplemented_website(cfg):
    env=configured()
    env['PROVIDER']='website'
    issues,_=diagnose(cfg,env)
    assert len(issues)==1 and 'CAPTCHA' in issues[0]


def test_missing_key_is_not_ready(cfg):
    env=configured()
    env.pop('SKYSCANNER_API_KEY')
    issues,notes=diagnose(cfg,env)
    assert len(issues)==1 and 'SKYSCANNER_API_KEY' in issues[0]
    assert notes


def test_readiness_does_not_leak_secrets_or_claim_live_access(cfg):
    env=configured()
    issues,notes=diagnose(cfg,env)
    assert not issues
    assert 'private-test-key' not in str(notes)
    assert 'private-db-key' not in str(notes)
    assert 'действия пользователя' in notes[0]


def test_manual_test_does_not_need_campaign_start(cfg):
    env=configured()
    env.pop('CAMPAIGN_START')
    assert diagnose(cfg,env,scheduled=False)[0]==[]
    assert diagnose(cfg,env,scheduled=True)[0]
    env['CAMPAIGN_START']='2027-01-20T00:00:00'
    assert diagnose(cfg,env)[0]
