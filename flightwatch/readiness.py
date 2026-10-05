"""Configuration diagnostics for an unattended runner. No network requests or secret values."""
import os
from urllib.parse import urlsplit

from .core import aware


def diagnose(cfg, env=None, *, scheduled=True):
    env = os.environ if env is None else env
    issues = []
    provider = env.get('PROVIDER') or cfg['provider']
    if provider == 'website':
        issues.append('Website-адаптер не извлекает цены: живой контракт выдачи не подтверждён, '
                      'автоматического решения CAPTCHA нет. Браузерная сессия требует человека.')
    elif provider == 'partner':
        if not env.get('SKYSCANNER_API_KEY'):
            issues.append('Не задан GitHub Secret SKYSCANNER_API_KEY.')
    elif provider == 'ryanair':
        from playwright.sync_api import sync_playwright
        from pathlib import Path
        with sync_playwright() as pw:
            if not Path(pw.chromium.executable_path).exists():
                issues.append('Установите Chromium: python -m playwright install chromium.')
    else:
        issues.append('PROVIDER должен быть website, partner или ryanair.')
    if not env.get('SUPABASE_SERVICE_ROLE_KEY'):
        issues.append('Не задан GitHub Secret SUPABASE_SERVICE_ROLE_KEY.')
    url = urlsplit(env.get('SUPABASE_URL', ''))
    if url.scheme != 'https' or not url.hostname:
        issues.append('GitHub Secret SUPABASE_URL должен содержать HTTPS URL проекта.')
    if scheduled:
        start = env.get('CAMPAIGN_START') or cfg.get('campaign_start')
        if not start:
            issues.append('Задайте постоянный CAMPAIGN_START в Repository Variables или config.json.')
        else:
            try:
                aware(start)
            except (ValueError, TypeError):
                issues.append('CAMPAIGN_START должен быть ISO datetime с явным часовым поясом.')
    notes = []
    if provider == 'ryanair':
        notes.append('Охват: только базовые предложения сайта Ryanair; остальные авиакомпании не проверяются.')
    if provider == 'partner':
        notes.append('Наличие ключа не подтверждает разрешение мониторинга по расписанию: '
                     'стандартные Usage Guidelines Live Prices API исключают автоматические запросы '
                     'без действия пользователя. Условия вашего доступа нужно уточнить у Skyscanner.')
    return issues, notes
