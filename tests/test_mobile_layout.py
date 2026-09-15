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


# ── Viewport (kept from the Sept 14 fix) ─────────────────────────────

def test_every_template_still_declares_a_mobile_viewport():
    missing = [os.path.basename(p) for p in _all_templates()
               if 'name="viewport"' not in _read(os.path.basename(p))]
    assert missing == [], f"templates with no viewport meta tag: {missing}"
