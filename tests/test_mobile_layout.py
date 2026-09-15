"""Layout guards from Moaz's mobile testing (Sept 15, 2026).

These are cheap structural checks, not visual ones — they catch the exact
mistakes that made the dashboards unusable on a phone:

  - a wide table with no scroll container drags the WHOLE PAGE sideways,
    so every other control on the page becomes hard to reach. The agent
    dashboard already did it right (.table-wrap with overflow-x); the
    super admin agency table and the agency leads table did not.
  - a modal that puts max-height + its own scrollbar on the panel and
    centres it clips content on a short screen. The agent dashboard
    scrolls the OVERLAY and starts the panel at the top instead.
  - a missing viewport meta tag makes a phone render at ~980px and shrink,
    which no amount of CSS can fix.
"""
import glob
import os
import re

TEMPLATES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'templates')


def _read(name):
    with open(os.path.join(TEMPLATES, name), encoding='utf-8') as fh:
        return fh.read()


def _style_of(src):
    return "\n".join(re.findall(r'<style>(.*?)</style>', src, re.S))


def _all_templates():
    return sorted(glob.glob(os.path.join(TEMPLATES, '*.html')))


# ── Horizontal scrolling lives in the box, never the page ────────────

def test_every_table_sits_inside_a_horizontal_scroll_box():
    offenders = []
    for path in _all_templates():
        src = _read(os.path.basename(path))
        if '<table' not in src:
            continue
        style = _style_of(src)
        if 'overflow-x: auto' not in style and 'overflow-x:auto' not in style:
            offenders.append(f"{os.path.basename(path)}: no overflow-x container defined")
            continue
        # every <table> must be preceded by a table-wrap opening tag
        if not re.search(r'class="[^"]*table-wrap[^"]*"[^>]*>\s*(?:<[^>]+>\s*)?<table', src, re.S):
            offenders.append(f"{os.path.basename(path)}: <table> not inside .table-wrap")
    assert offenders == [], offenders


def test_wide_tables_declare_a_min_width_so_columns_do_not_crush():
    """Without a min-width the table squeezes into the viewport instead of
    scrolling, and the columns become unreadable slivers."""
    for name in ('admin.html', 'owner.html'):
        style = _style_of(_read(name))
        assert re.search(r'min-width:\s*\d+px', style), f"{name} has no table min-width"


def test_super_admin_toggles_the_scroll_box_not_the_table_itself():
    """Hiding/showing the <table> with display:table while the wrapper
    stayed hidden was how the box got bypassed."""
    src = _read('owner.html')
    assert 'agencyTableWrap' in src
    assert 'getElementById("agencyTableWrap")' in src
    # the table element itself must no longer be the thing toggled
    assert 'table.style.display = "block"' in src


# ── The lead modal matches the agent dashboard ───────────────────────

def test_agency_and_agent_dashboards_share_the_same_modal_behaviour():
    """Moaz's instruction was 'exactly as in agent dashboard'. The three
    properties that matter for a phone: the overlay scrolls, the panel
    starts at the top, and the panel has no competing inner scrollbar."""
    for name in ('admin.html', 'agent_dashboard.html'):
        style = _style_of(_read(name))
        overlay = re.search(r'\.modal-overlay\s*\{(.*?)\}', style, re.S)
        assert overlay, f"{name}: no .modal-overlay rule"
        body = overlay.group(1)
        assert 'align-items: flex-start' in body, f"{name}: modal is still centred"
        assert 'overflow-y: auto' in body, f"{name}: overlay does not scroll"

        panel = re.search(r'\n\s*\.modal\s*\{(.*?)\}', style, re.S)
        assert panel, f"{name}: no .modal rule"
        assert 'max-height' not in panel.group(1), \
            f"{name}: panel still has its own max-height, which clips on a short screen"


def test_both_dashboards_collapse_the_lead_info_grid_on_a_phone():
    for name in ('admin.html', 'agent_dashboard.html'):
        style = _style_of(_read(name))
        mobile = re.findall(r'@media[^{]*\{(.*?)\n    \}', style, re.S)
        joined = "\n".join(mobile)
        assert 'info-grid' in joined, f"{name}: info grid never collapses to one column"


# ── Topbar controls ──────────────────────────────────────────────────

def test_agent_dashboard_topbar_buttons_are_a_wrapping_flex_row():
    """They used to be loose inline-blocks held apart by a margin, which
    piled up raggedly once the row ran out of width."""
    src = _read('agent_dashboard.html')
    style = _style_of(src)
    assert 'topbar-actions' in src, "buttons are not in a named container"
    rule = re.search(r'\.topbar-actions\s*\{(.*?)\}', style, re.S)
    assert rule, "no .topbar-actions rule"
    assert 'display: flex' in rule.group(1)
    assert 'flex-wrap: wrap' in rule.group(1)
    assert 'gap:' in rule.group(1)
    # and no leftover margin-based spacing on the buttons
    changepw = re.search(r'\.btn-changepw\s*\{(.*?)\}', style, re.S)
    if changepw:
        assert 'margin-right' not in changepw.group(1)


def test_dashboard_topbars_wrap_instead_of_overflowing():
    for name in ('admin.html', 'agent_dashboard.html'):
        style = _style_of(_read(name))
        rule = re.search(r'\.topbar\s*\{(.*?)\}', style, re.S)
        assert rule, f"{name}: no .topbar rule"
        assert 'flex-wrap: wrap' in rule.group(1), f"{name}: topbar cannot wrap"


# ── Stat cards are the same compact size on all three dashboards ─────

def _rule(style, selector):
    m = re.search(r'\.' + selector + r'\s*\{([^}]*)\}', style)
    return " ".join(m.group(1).split()) if m else None


# every page that shows stat cards
STAT_PAGES = ('owner.html', 'admin.html', 'agent_dashboard.html',
              'listings.html', 'analytics.html', 'appointments.html')


def test_every_page_with_stat_cards_uses_the_same_compact_sizing():
    """The super admin dashboard's sizing is the reference. The others ran
    from 180px-200px columns with 20-24px padding and 32-36px numbers,
    which filled most of a phone screen before any content appeared."""
    expected = {
        'stats-grid': ('minmax(160px, 1fr)', 'gap: 16px'),
        'stat-card': ('padding: 18px 20px',),
        'stat-label': ('font-size: 12px',),
        'stat-value': ('font-size: 28px',),
    }
    for name in STAT_PAGES:
        style = _style_of(_read(name))
        for selector, needles in expected.items():
            body = _rule(style, selector)
            assert body, f"{name}: no .{selector} rule"
            for needle in needles:
                assert needle in body, \
                    f"{name} .{selector} should contain '{needle}', got: {body}"


def test_stat_cards_step_down_again_on_a_phone():
    for name in STAT_PAGES:
        style = _style_of(_read(name))
        mobile = "\n".join(re.findall(r'@media[^{]*\{(.*?)\n\s{0,8}\}\s*\n', style, re.S))
        assert 'minmax(140px' in mobile, f"{name}: stat grid does not narrow on mobile"
        assert 'font-size: 24px' in mobile, f"{name}: stat value does not shrink on mobile"


def test_topbar_buttons_are_compact_everywhere():
    """Every page's nav buttons should be the same small size, so moving
    between pages doesn't jump between chunky and compact controls."""
    for name in ('admin.html', 'agent_dashboard.html', 'listings.html',
                 'analytics.html', 'agents.html', 'appointments.html'):
        style = _style_of(_read(name))
        # whichever selector this page uses for its nav controls
        for selector in (r'\.actions a, \.actions button',
                         r'\.topbar-actions a, \.topbar-actions button',
                         r'\.btn-back',
                         r'\.btn-logout, \.btn-changepw'):
            m = re.search(selector + r'\s*\{([^}]*)\}', style)
            if not m:
                continue
            body = " ".join(m.group(1).split())
            size = re.search(r'font-size:\s*([\d.]+)px', body)
            if size:
                assert float(size.group(1)) <= 13, \
                    f"{name}: nav buttons still {size.group(1)}px ({selector})"
            break
        else:
            raise AssertionError(f"{name}: no recognised nav-button rule found")


# ── Viewport (kept from the Sept 14 fix) ─────────────────────────────

def test_every_template_still_declares_a_mobile_viewport():
    missing = [os.path.basename(p) for p in _all_templates()
               if 'name="viewport"' not in _read(os.path.basename(p))]
    assert missing == [], f"templates with no viewport meta tag: {missing}"
