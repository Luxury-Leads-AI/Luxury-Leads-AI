"""The dark premium theme (Moaz's Sept 20 request).

"I would like this 'SaaS' website to be modified a bit. It should be
converted to a dark theme... and given a more premium look by adding
shadows in boxes."

Before this, the product had three unrelated looks: the marketing site was
indigo-on-white, the login and signup pages were near-black with cyan, and
the dashboards were slate with purple. A visitor moving from the pricing
page to their dashboard passed through two different products.

Everything now reads from static/theme.css. These checks are structural,
because the things that actually go wrong with a palette conversion are
mechanical: a page that never got the stylesheet, a hex that survived the
sweep, or a colour that was a background on the light theme and became a
text colour on the dark one (which is how you get dark-on-dark).

The look itself was verified by rendering all 22 pages in Chromium at
desktop and phone widths and measuring every text node's contrast ratio
against its computed background - that pass is what found --text-faint
being too dark for body text, white on the pale brand fill at 2.7:1, and
two light callout panels on the refunds page that had kept their white
backgrounds under near-white text.
"""
import glob
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, 'templates')
THEME = os.path.join(ROOT, 'static', 'theme.css')

PAGES = sorted(glob.glob(os.path.join(TEMPLATES, '*.html')))

# Palettes the three old looks were built from. Any of these surviving
# means a rule was missed and that page is off-theme.
RETIRED = [
    # dashboard slate
    '#0f172a', '#1e293b', '#334155', '#26334a', '#94a3b8', '#64748b',
    '#e2e8f0', '#cbd5e1', '#f1f5f9',
    # marketing indigo
    '#667eea', '#764ba2', '#f7fafc', '#edf2f7', '#2d3748', '#718096',
    '#4a5568',
    # auth near-black
    '#1e1e1e', '#1c1c1c',
    # light panels that only work on a white page
    '#ebf4ff', '#fffaf0',
]


def read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def test_every_page_loads_the_shared_stylesheet():
    for path in PAGES:
        src = read(path)
        assert '/static/theme.css' in src, f"{os.path.basename(path)} is off-theme"
        # before its own <style>, so a page can still override
        if '<style' in src:
            assert src.index('/static/theme.css') < src.index('<style'), \
                f"{os.path.basename(path)}: its own styles load before the theme"


def test_the_theme_defines_the_palette_as_variables():
    css = read(THEME)
    for name in ('--bg', '--bg-elev', '--surface', '--surface-2', '--border',
                 '--text', '--text-dim', '--text-faint', '--brand', '--brand-2',
                 '--brand-dark', '--grad-brand', '--glow', '--glow-strong'):
        assert re.search(re.escape(name) + r'\s*:', css), f"{name} is not defined"


def test_no_page_still_carries_a_retired_palette_colour():
    offenders = {}
    for path in PAGES:
        src = read(path).lower()
        found = [c for c in RETIRED if c in src]
        if found:
            offenders[os.path.basename(path)] = found
    assert offenders == {}, f"off-theme colours left behind: {offenders}"


def test_the_body_never_keeps_a_light_background():
    """The marketing and legal pages were white; a missed body rule is a
    white page with near-white text on it."""
    for path in PAGES:
        src = read(path)
        for m in re.finditer(r'\bbody\s*\{([^}]*)\}', src, re.S):
            block = m.group(1)
            bad = re.search(r'background(?:-color)?\s*:\s*(#fff|#ffffff|white|#f[0-9a-f]{5})\b',
                            block, re.I)
            assert not bad, f"{os.path.basename(path)}: body is still light ({bad.group(0)})"


def test_brand_dark_is_never_used_as_a_text_colour():
    """This is the specific mistake the conversion made and the contrast
    pass caught: #667eea was a fill in some places and a text colour in
    others, and mapping both to the darker brand purple left 2.4:1 text."""
    for path in PAGES:
        src = read(path)
        bad = re.findall(r'(?<!-)color:\s*var\(--brand-dark\)', src)
        assert not bad, f"{os.path.basename(path)}: --brand-dark used as text colour"


def test_the_brand_gradient_is_dark_enough_for_white_text():
    """White on #a78bfa is 2.7:1. Every primary button used to sit on it."""
    css = read(THEME)
    grad = re.search(r'--grad-brand:\s*([^;]+);', css).group(1)
    assert '#a78bfa' not in grad, "the primary button gradient is too pale for white text"
    assert '#7c3aed' in grad or '#6d28d9' in grad or '#5b21b6' in grad


def test_cards_and_panels_get_the_glow():
    """"a more premium look by adding shadows in boxes" - the shadow is
    declared once, for every class that is a box."""
    css = read(THEME)
    shadow_block = css[css.index('The premium treatment'):]
    for cls in ('.stat-card', '.meta-card', '.feed-panel', '.appt-card',
                '.listing-card', '.table-wrap', '.modal', '.box'):
        assert cls in shadow_block, f"{cls} has no shadow"
    assert 'box-shadow: var(--glow)' in shadow_block
    assert 'var(--glow-strong)' in shadow_block, "nothing lifts on hover"


def test_the_glow_is_a_coloured_shadow_not_a_grey_one():
    """A grey drop shadow on a dark page just looks like dirt. The brand
    hairline plus a violet cast below is what reads as premium."""
    css = read(THEME)
    glow = re.search(r'--glow:\s*([^;]+);', css).group(1)
    assert 'rgba(88, 40, 220' in glow or 'rgba(139, 92, 246' in glow, glow
    ring = re.search(r'--ring:\s*([^;]+);', css).group(1)
    assert 'rgba(139, 92, 246' in ring, ring


def test_reduced_motion_is_respected():
    css = read(THEME)
    assert 'prefers-reduced-motion' in css, \
        "the hover lifts ignore a reader who asked for less movement"


def test_the_theme_does_not_reach_for_a_blocked_cdn():
    """Artifact pages and the widget aside, the app serves its own CSS;
    a webfont from a third party is a render-blocking request on someone
    else's uptime."""
    css = read(THEME)
    assert '@import' not in css
    assert 'http' not in css


def test_mobile_rules_survived_the_conversion():
    """Moaz signed off on mobile before the theme landed; a colour sweep
    that ate a media query would silently undo that."""
    for name in ('admin.html', 'agent_dashboard.html', 'owner.html',
                 'listings.html', 'appointments.html', 'analytics.html',
                 'agents.html', 'agent_detail.html'):
        src = read(os.path.join(TEMPLATES, name))
        assert '@media' in src, f"{name} lost its media queries"
        assert 'max-width' in src, f"{name} lost its breakpoints"


def test_wide_tables_still_scroll_inside_their_own_box():
    """The exact bug Moaz reported on mobile: the page scrolled sideways
    instead of the table. Pinned here so the theme can't undo it."""
    for name, wrapper in (('admin.html', 'table-wrap'),
                          ('agent_dashboard.html', 'table-wrap'),
                          ('owner.html', 'agencyTableWrap'),
                          ('agent_detail.html', 'table-wrap')):
        src = read(os.path.join(TEMPLATES, name))
        assert wrapper in src, f"{name}: no scroll container"
    for name in ('admin.html', 'agent_dashboard.html', 'agent_detail.html'):
        src = read(os.path.join(TEMPLATES, name))
        assert re.search(r'\.table-wrap\s*\{[^}]*overflow-x\s*:\s*auto', src, re.S), \
            f"{name}: .table-wrap no longer scrolls horizontally"


def test_the_top_bars_are_unified_in_one_place():
    css = read(THEME)
    assert '.topbar' in css and '.header' in css
    bar = css[css.index('Top bars'):]
    assert 'linear-gradient' in bar


def test_the_empty_state_table_colspan_matches_the_column_count():
    """A drifted colspan leaves the "no leads" row not spanning the table -
    the kind of thing a Timeline column quietly causes."""
    for name in ('admin.html',):
        src = read(os.path.join(TEMPLATES, name))
        header = re.search(r'<thead>(.*?)</thead>', src, re.S).group(1)
        columns = len(re.findall(r'<th[ >]', header))
        for span in re.findall(r'colspan="(\d+)"', src):
            assert int(span) == columns, \
                f"{name}: colspan {span} but {columns} columns"
