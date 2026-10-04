"""Settings that can change without a deploy.

Mode, budgets, caps and kill switches live in the acq_setting table, not in
code, because the whole point of the mode idea is that moving from
Bootstrap to Growth is a setting change rather than a rewrite. Model prices
are here too: OpenAI changes them, and a price baked into code quietly
turns the cost page into fiction.
"""
import json

from . import models

BOOTSTRAP = 'bootstrap'
GROWTH = 'growth'
SCALE = 'scale'
MODES = (BOOTSTRAP, GROWTH, SCALE)
MODE_LABELS = {
    BOOTSTRAP: 'Bootstrap - you press the buttons and send the emails',
    GROWTH: 'Growth - a worker runs jobs, sending still needs approval',
    SCALE: 'Scale - trusted templates send themselves',
}

# Dollars per million tokens, September 2026. Checked on the pricing page;
# change them here (or on the Settings screen) when OpenAI changes them.
DEFAULT_MODEL_PRICES = {
    'gpt-4o-mini': {'input': 0.15, 'output': 0.60},
    'gpt-5-nano': {'input': 0.05, 'output': 0.40},
}
# $10 per 1,000 calls, plus the fixed input tokens the tool adds.
DEFAULT_WEB_SEARCH_USD = 0.01

DEFAULTS = {
    'mode': BOOTSTRAP,
    'ai_budget_usd_month': '5',
    'ai_budget_action': 'pause',      # pause non-essential jobs at 100%
    'research_cost_cap_usd': '0.02',  # per prospect
    'web_search_cap_month': '50',
    'pilot_cap': '5',
    'pilot_days': '30',
    'daily_send_cap': '10',
    'discovery_enabled': 'manual,osm',   # manual / openai / osm  (comma separated)
    'discovery_limit_default': '25',
    'web_search_model': 'gpt-4o-mini',
    # 'auto' tries the current tool name first and remembers which one the
    # API accepted, so a rename at OpenAI costs a setting, not a deploy.
    'web_search_tool_type': 'auto',
    'osm_nominatim_url': '',          # blank = the public OpenStreetMap service
    'osm_overpass_url': '',           # blank = the public Overpass service
    'kill_switch_all': 'off',
    'kill_switch_outreach': 'off',
    'retention_days_uncontacted': '180',
    'retention_days_rejected': '365',
    'model_extract': 'gpt-4o-mini',
    'model_draft': 'gpt-4o-mini',
    'model_classify': 'gpt-4o-mini',
    'model_prices_json': json.dumps(DEFAULT_MODEL_PRICES),
    'web_search_usd': str(DEFAULT_WEB_SEARCH_USD),
    'fixed_costs_usd_month': '0',
    'operator_email': '',
    'postal_address': '',
}

# Settings a person can edit on the Settings screen, in the order shown.
EDITABLE = (
    ('mode', 'Mode'),
    ('ai_budget_usd_month', 'AI budget for this month (USD)'),
    ('research_cost_cap_usd', 'Most AI spend per prospect (USD)'),
    ('web_search_cap_month', 'Most web searches per month'),
    ('pilot_cap', 'Most pilots running at once'),
    ('pilot_days', 'Pilot length (days)'),
    ('daily_send_cap', 'Most emails to send in a day'),
    ('discovery_enabled', 'Discovery sources switched on (manual, osm, openai)'),
    ('discovery_limit_default', 'How many agencies to look for in one run'),
    ('web_search_model', 'Model used for AI web search'),
    ('kill_switch_all', 'Stop everything (on/off)'),
    ('kill_switch_outreach', 'Stop outreach only (on/off)'),
    ('retention_days_uncontacted', 'Delete contact details of never-contacted prospects after (days)'),
    ('retention_days_rejected', 'Delete contact details after a rejection (days)'),
    ('fixed_costs_usd_month', 'Fixed monthly costs, for cost per client (USD)'),
    ('postal_address', 'Postal address for the email footer'),
    ('operator_email', 'Your email, for engine notices'),
)

_db = None


def init(db):
    global _db
    _db = db


def get(key, default=None):
    row = _db.session.get(models.Setting, key)
    if row is not None and row.value is not None:
        return row.value
    if default is not None:
        return default
    return DEFAULTS.get(key)


def set(key, value):                 # noqa: A001 - reads better than set_value
    row = _db.session.get(models.Setting, key)
    if row is None:
        row = models.Setting(key=key, value=str(value))
        _db.session.add(row)
    else:
        row.value = str(value)
    _db.session.commit()
    return row


def get_float(key, default=0.0):
    try:
        return float(get(key))
    except (TypeError, ValueError):
        return default


def get_int(key, default=0):
    try:
        return int(float(get(key)))
    except (TypeError, ValueError):
        return default


def get_bool(key):
    return str(get(key) or '').strip().lower() in ('on', 'true', 'yes', '1')


def get_json(key, default=None):
    try:
        return json.loads(get(key) or '')
    except (TypeError, ValueError):
        return default if default is not None else {}


def get_list(key):
    return [part.strip() for part in (get(key) or '').split(',') if part.strip()]


def mode():
    value = (get('mode') or BOOTSTRAP).strip().lower()
    return value if value in MODES else BOOTSTRAP


def model_prices():
    prices = get_json('model_prices_json', DEFAULT_MODEL_PRICES)
    return prices or DEFAULT_MODEL_PRICES


def everything_stopped():
    """The big red button. Checked before any job runs."""
    return get_bool('kill_switch_all')


def outreach_stopped():
    return get_bool('kill_switch_all') or get_bool('kill_switch_outreach')


def all_settings():
    """Every setting with its current value, for the Settings screen."""
    stored = {row.key: row.value for row in models.Setting.query.all()}
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in stored.items() if v is not None})
    return merged
