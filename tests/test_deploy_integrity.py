"""Catch a half-deployed commit before Render does.

On 2026-09-20, commit 28be70a shipped all 24 templates and all 4 test
files but left out `app.py` and `static/theme.css`. Both failures were
silent in the sense that nothing *said* what was wrong:

  - Every template now asks for `var(--bg)`, `var(--surface)` and so on.
    With /static/theme.css returning 404 those variables are undefined,
    and an undefined var() voids the entire declaration - so backgrounds
    fell back to transparent and text to black. The product looked like
    the theme had simply been done badly.
  - `timeline_label` is registered on the Jinja environment by app.py.
    Without it, /agent-dashboard/<id> returned a 500 on every request.

The test suite would have caught the missing stylesheet (test_theme.py
reads it), but nothing caught the missing app.py. These checks close
that gap: they assert that the templates and app.py in a working tree
are actually a matching pair, and that everything a template links to
exists.

Run before every push. `pytest tests/ -q` is the pre-push check.
"""
import glob
import os
import re

import app as app_module

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, 'templates')
PAGES = sorted(glob.glob(os.path.join(TEMPLATES, '*.html')))

# Jinja's own builtins and the filters/tests it ships with - a template
# calling these needs nothing from app.py.
JINJA_BUILTINS = {
    # filters
    'abs', 'attr', 'batch', 'capitalize', 'center', 'default', 'dictsort',
    'escape', 'filesizeformat', 'first', 'float', 'forceescape', 'format',
    'groupby', 'indent', 'int', 'join', 'last', 'length', 'list', 'lower',
    'map', 'max', 'min', 'pprint', 'random', 'reject', 'rejectattr',
    'replace', 'reverse', 'round', 'safe', 'select', 'selectattr', 'slice',
    'sort', 'string', 'striptags', 'sum', 'title', 'tojson', 'trim',
    'truncate', 'unique', 'upper', 'urlencode', 'urlize', 'wordcount',
    'wordwrap', 'xmlattr', 'items',
    # globals
    'range', 'dict', 'lipsum', 'cycler', 'joiner', 'namespace',
    # flask
    'url_for', 'get_flashed_messages', 'config', 'request', 'session', 'g',
    # python methods reached on a value, not a template function
    'get', 'strftime', 'lstrip', 'rstrip', 'strip', 'split', 'startswith',
    'endswith', 'keys', 'values', 'append', 'isdigit', 'isupper', 'islower',
    'total_seconds', 'date', 'time', 'isoformat', 'timestamp',
}


def read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def jinja_expressions(src):
    """Everything inside {{ }} and {% %}, which is where a template can
    reach for something app.py has to provide."""
    return re.findall(r'\{\{(.*?)\}\}', src, re.S) + \
           re.findall(r'\{%(.*?)%\}', src, re.S)


def route_context_keywords():
    """Every keyword passed to a render_template() call in app.py. A name
    supplied per-route is just as valid as a registered global."""
    src = read(os.path.join(ROOT, 'app.py'))
    names = set()
    for call in re.finditer(r'render_template\((.*?)\)\s*$', src, re.M | re.S):
        for kw in re.findall(r'(\w+)\s*=', call.group(1)):
            names.add(kw)
    # the same, for multi-line calls the regex above may clip
    for kw in re.findall(r'^\s+(\w+)\s*=\s*\w', src, re.M):
        names.add(kw)
    return names


def test_every_template_function_is_something_app_py_provides():
    """This is the check that was missing on 2026-09-20.

    A template calling `timeline_label(...)` is a hard dependency on
    app.py registering it. If the templates ship and app.py doesn't, the
    page 500s for every visitor - and the traceback only shows up in the
    server log, not anywhere a person looking at the site would see."""
    registered = (set(app_module.app.jinja_env.globals)
                  | set(app_module.app.jinja_env.filters)
                  | set(app_module.app.jinja_env.tests))
    from_routes = route_context_keywords()
    known = registered | from_routes | JINJA_BUILTINS

    missing = {}
    for path in PAGES:
        for expr in jinja_expressions(read(path)):
            for name in re.findall(r'\b([a-z_][a-z0-9_]*)\s*\(', expr):
                if name not in known:
                    missing.setdefault(os.path.basename(path), set()).add(name)

    assert missing == {}, (
        "templates call something app.py does not provide - these pages "
        f"will 500 in production: { {k: sorted(v) for k, v in missing.items()} }")


def test_timeline_label_is_registered_on_the_jinja_environment():
    """Named explicitly, because it is the one that broke, and because
    five templates call it in a lead row - the hot path of the product."""
    assert 'timeline_label' in app_module.app.jinja_env.globals
    assert app_module.app.jinja_env.globals['timeline_label']('immediate') \
        == 'Immediately / within a month'
    assert app_module.app.jinja_env.globals['timeline_label'](None) == 'Not given'

    callers = [os.path.basename(p) for p in PAGES if 'timeline_label(' in read(p)]
    assert 'admin.html' in callers and 'agent_dashboard.html' in callers, callers


def test_every_static_file_a_template_links_actually_exists():
    """A 404 on a stylesheet is not a 404 anyone sees - it is a product
    that renders with no palette and looks like bad design."""
    missing = {}
    for path in PAGES:
        for ref in re.findall(r'["\'](/static/[^"\']+)["\']', read(path)):
            local = os.path.join(ROOT, ref.lstrip('/').replace('/', os.sep))
            if not os.path.isfile(local):
                missing.setdefault(os.path.basename(path), set()).add(ref)
    assert missing == {}, f"templates link static files that are not here: {missing}"


def test_the_theme_stylesheet_is_present_and_not_empty():
    theme = os.path.join(ROOT, 'static', 'theme.css')
    assert os.path.isfile(theme), \
        "static/theme.css is missing - every template's colours resolve to nothing"
    assert os.path.getsize(theme) > 2000, "theme.css looks truncated"
    assert ':root' in read(theme), "theme.css defines no variables"


def test_the_models_the_templates_read_actually_have_those_columns():
    """The other half of the same coupling: a template reading
    lead.timeline against an app.py whose Lead has no such column is an
    UndefinedError at render time, not an import error at boot."""
    lead_columns = {c.name for c in app_module.Lead.__table__.columns}
    for column in ('timeline', 'timeline_raw', 'quality_reasons', 'lead_type'):
        assert column in lead_columns, f"Lead.{column} is missing"

    assert hasattr(app_module, 'ActivityEvent'), \
        "the activity feed model is missing but the dashboards render a feed"
    event_columns = {c.name for c in app_module.ActivityEvent.__table__.columns}
    for column in ('agency_id', 'agent_id', 'action', 'summary',
                   'seen_by_owner', 'seen_by_agent'):
        assert column in event_columns, f"ActivityEvent.{column} is missing"


def test_the_routes_the_templates_post_to_exist():
    """A feed panel whose mark-as-read URL 404s looks like it works and
    silently never clears."""
    rules = {r.rule for r in app_module.app.url_map.iter_rules()}

    def has(pattern):
        return any(re.fullmatch(pattern, rule) for rule in rules)

    assert has(r'/mark-activity-seen/<int:agency_id>')
    assert has(r'/agent-mark-activity-seen/<int:agent_id>')
    assert has(r'/get-lead-detail/<int:lead_id>')


def test_every_new_column_has_a_startup_migration():
    """Render runs an existing database, so a new column that only exists
    in the model is a column that does not exist in production."""
    src = read(os.path.join(ROOT, 'app.py'))
    migrations = src[src.index('# ── LEAD TIMELINE'):]
    for column in ('timeline', 'timeline_raw', 'quality_reasons'):
        assert f"'{column}'" in migrations, \
            f"lead.{column} is in the model with no ALTER TABLE behind it"
    assert 'activity_event' in src, "no migration creates the activity_event table"
