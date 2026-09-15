"""Guards for the dashboard filters (Moaz's Sept 15 testing).

Three real bugs were live when he reported "lead filters are not working
properly":

  1. AGENCY DASHBOARD quality filter: the code read the tab's visible TEXT
     and parseInt()'d it. Every label starts with an emoji ("🔥 Hot (5⭐)"),
     so parseInt returned NaN and `rowQuality === NaN` was false for every
     row - clicking any quality tab hid all leads.
  2. LISTINGS page: the filter looped over ALL .listing-card elements and
     called .toLowerCase() on card.dataset.search. The owner-submitted
     properties awaiting approval (added with the seller flow) carry no
     data-search, so that threw a TypeError, the loop aborted, and
     filtering plus search silently stopped working for the whole page
     as soon as one seller submission was pending.
  3. APPOINTMENTS page: cards are grouped per agent now, but filtering
     hid only the cards - leaving agent headings with counts above empty
     space.

Plus the missing feature: the AGENT dashboard had no lead filters at all.

These checks are structural (no browser needed). The behaviour itself was
driven through a real DOM with jsdom during development; what's pinned
here are the specific mistakes, so they can't come back.
"""
import glob
import os
import re

TEMPLATES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'templates')

# every page that filters a list of things
FILTER_PAGES = ('admin.html', 'agent_dashboard.html', 'listings.html',
                'appointments.html')


def _read(name):
    with open(os.path.join(TEMPLATES, name), encoding='utf-8') as fh:
        return fh.read()


def _script_of(src):
    return "\n".join(re.findall(r'<script[^>]*>(.*?)</script>', src, re.S))


# ── The specific mistakes, pinned ────────────────────────────────────

def test_no_filter_reads_its_value_from_a_tabs_label_text():
    """This is bug 1. A tab's label is for humans; its value belongs in a
    data attribute. parseInt on an emoji-prefixed label is always NaN."""
    for name in FILTER_PAGES:
        js = _script_of(_read(name))
        assert 'parseInt(quality)' not in js, f"{name}: still parseInt()s a label"
        assert '.textContent : ' not in js, \
            f"{name}: still derives a filter value from tab text"


def test_no_filter_scrapes_its_value_out_of_an_onclick_attribute():
    """Reading state back out of the onclick string breaks silently the
    moment anyone reformats the markup."""
    for name in FILTER_PAGES:
        js = _script_of(_read(name))
        assert "getAttribute('onclick')" not in js, \
            f"{name}: still parses the onclick attribute for filter state"


def test_every_filter_tab_carries_its_own_value():
    for name in FILTER_PAGES:
        src = _read(name)
        # the tab itself, not the "filter-tabs" container around them
        tabs = re.findall(r'<div class="filter-tab(?: active)?"[^>]*>', src)
        assert tabs, f"{name}: no filter tabs found"
        missing = [t for t in tabs if 'data-filter=' not in t]
        assert missing == [], f"{name}: tabs with no data-filter: {missing}"


def test_filters_never_use_innertext():
    """innerText is layout-dependent (and absent in jsdom). textContent is
    what a filter wants."""
    for name in FILTER_PAGES:
        js = _script_of(_read(name))
        assert 'innerText' not in js, f"{name}: filter still relies on innerText"


def test_listings_filter_is_scoped_to_the_live_grid():
    """Bug 2. The review section holds owner submissions awaiting approval:
    it must never be filtered away, and its cards must never be read for
    attributes they don't have."""
    js = _script_of(_read('listings.html'))
    assert "#listings-grid .listing-card" in js, \
        "listings filter still walks every .listing-card, review cards included"
    assert ".getAttribute('data-search').toLowerCase()" not in js, \
        "listings filter still calls .toLowerCase() on a possibly-null attribute"


def test_appointment_filter_hides_groups_that_end_up_empty():
    """Bug 3."""
    js = _script_of(_read('appointments.html'))
    assert '.agent-group' in js, \
        "appointments filter never touches the per-agent groups"


def test_appointment_filter_matches_the_stage_the_card_displays():
    src = _read('appointments.html')
    assert 'data-stage=' in src, "appointment cards do not expose their stage"
    js = _script_of(src)
    assert 'dataset.stage' in js, "appointment filter ignores the unified stage"


# ── The agent dashboard's new filters ────────────────────────────────

def test_agent_dashboard_has_lead_filters():
    """They were missing entirely - an agent working a full pipeline needs
    to find their hot leads the same way the owner does."""
    src = _read('agent_dashboard.html')
    assert 'id="quality-tabs"' in src, "no quality filter on the agent dashboard"
    assert 'id="status-tabs"' in src, "no status filter on the agent dashboard"
    assert 'id="lead-search"' in src, "no search box on the agent dashboard"
    js = _script_of(src)
    assert 'function applyLeadFilters' in js


def test_agent_dashboard_lead_rows_carry_what_the_filter_reads():
    src = _read('agent_dashboard.html')
    row = re.search(r'<tr class="lead-row".*?>', src, re.S)
    assert row, "no lead row found"
    markup = row.group(0)
    for attr in ('data-quality', 'data-status', 'data-search'):
        assert attr in markup, f"agent lead row is missing {attr}"


def test_both_dashboards_offer_the_same_lead_filters():
    """An owner and an agent should be filtering by the same things."""
    owner = _read('admin.html')
    agent = _read('agent_dashboard.html')
    for value in ('"all"', '"5"', '"4"', '"3"', '"2"', '"1"'):
        assert f'data-filter={value}' in owner, f"agency dashboard lost quality {value}"
        assert f'data-filter={value}' in agent, f"agent dashboard lost quality {value}"
    for status in ('new', 'contacted', 'meeting', 'closed', 'lost'):
        assert f'data-filter="{status}"' in owner, f"agency dashboard lost status {status}"
        assert f'data-filter="{status}"' in agent, f"agent dashboard lost status {status}"


def test_search_boxes_are_addressed_by_id_not_by_attribute_guess():
    """document.querySelector('input[oninput]') grabs whichever such input
    happens to come first in the document."""
    for name in FILTER_PAGES:
        js = _script_of(_read(name))
        assert "querySelector('input[oninput]')" not in js, \
            f"{name}: still finds its search box by guessing"


def test_lead_rows_expose_searchable_fields_server_side():
    """Search should cover the real lead fields, not just whatever text
    the row happens to render."""
    for name in ('admin.html', 'agent_dashboard.html'):
        src = _read(name)
        row = re.search(r'<tr class="lead-row".*?>', src, re.S)
        assert row and 'data-search' in row.group(0), f"{name}: no data-search on rows"
        assert 'lead.email' in row.group(0), f"{name}: search data omits the email"
        assert 'lead.budget' in row.group(0), f"{name}: search data omits the budget"
