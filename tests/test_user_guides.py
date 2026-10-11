"""The guides have to describe the product, not a product.

A user guide rots the moment somebody renames a button, and a wrong
guide is worse than no guide - it sends a customer looking for a control
that is not there, and they conclude the thing is broken.

So these tests are mostly agreement tests. The strongest of them,
`test_every_control_a_guide_names_is_on_that_screen`, takes every one of
the controls named across the guides and looks for its words in the
template that renders that screen. Rename a button and the guide for it
fails, by name, with the words it could not find.
"""
import os
import re

import pytest

import app as app_module
import guides as guide_data

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Which template each guided endpoint renders. Written out rather than
# inferred, so a route that starts rendering something else is caught.
SCREEN_TEMPLATES = {
    'owner_login': 'owner_login.html',
    'agent_login': 'agent_login.html',
    'forgot_password': 'forgot_password.html',
    'reset_password': 'reset_password.html',
    'admin': 'admin.html',
    'agents_page': 'agents.html',
    'agent_detail': 'agent_detail.html',
    'appointments': 'appointments.html',
    'listings': 'listings.html',
    'analytics': 'analytics.html',
    'agency_profile': 'agency_profile.html',
    'agent_dashboard': 'agent_dashboard.html',
    'agent_profile': 'agent_profile.html',
}

# Words that are a description of a group of controls rather than one
# control with a label - a row of filter chips, a set of status buttons.
# Each still has to be findable, by the sample given here.
GROUP_SAMPLES = {
    'Quality filter row (All / Hot / High / Medium / Low / Cold)': 'filterByQuality',
    'Status filter row (All Status / New / Contacted / Meeting / Closed / Lost)': 'filterByStatus',
    'Type filter row (Everyone / Buyers / Sellers)': 'filterByType',
    'Status buttons inside a lead (New, Contacted, Meeting, Closed, Lost)': 'updateStatus',
    'Status buttons inside a lead': 'updateStatus',
    'Quality, Status and Type filter rows': 'filterLeads',
    'Filter row (All / Available / Pending / Sold)': 'filterListings',
    'Total / Pending / Confirmed / Completed / Cancelled tiles': 'Cancelled',
    'Total / Available / Pending / Sold tiles': 'stat-label',
    'Total Leads / Hot Leads (5⭐) / High Quality (4-5⭐) / Closed': 'Hot Leads',
    'Total Leads / Hot Leads (5⭐) / This Month / Avg Quality Score': 'Avg Quality',
    'My Leads / Hot Leads (5⭐) / My Appointments / Closed Deals': 'My Leads',
    'Select checkboxes, then 🗑️ Delete Selected': 'Delete Selected',
    '👁 View, or clicking the row': 'View',
    '👁 View, or clicking a row': 'View',
    '🗑️ (on a lead row)': 'deleteLead',
    '🗑️ Remove (on a note)': 'deleteNote',
    '+ Add (Notes)': 'addNote',
    'Notes': 'Notes',
    '🗑️ on a viewing': 'deleteAppt',
    '🗑️ on a listing': 'deleteListing',
    '📝 Add Note (on an appointment)': 'addApptNote',
    'Max viewings per slot (Save)': 'saveCapacity',
    '📋 Status dropdown on a viewing': 'updateApptStatus',
    '👤 Agent dropdown on a viewing': 'reassignAppt',
    'Search by name or email': 'searchAppts',
    'Filter row (All / Pending / Confirmed / Viewing done / Wants to buy / Other options / Not interested / Cancelled)': 'filterAppts',
    '📅 Book another viewing': 'Book another viewing',
    'Assign to Agent': 'Assign to Agent',
    '⏳ Pending / 🏷️ Sold / ✅ Available / ↩ Re-list': 'Re-list',
    'CRM Webhook Integration (Save Webhook)': 'Save Webhook',
    'Leads Per Day - Last 30 Days': 'Leads Per Day',
    'New Password / Confirm New Password': 'confirm_password',
    'Coverage area': 'Coverage area',
    'Assigned Leads': 'Assigned Leads',
    'Viewings': 'Viewings',
    'Email': 'Email',
    'Password': 'Password',
    'Status filter row': 'filterByStatus',
}


def template_text(name):
    with open(os.path.join(ROOT, 'templates', name), encoding='utf-8') as handle:
        return handle.read()


def searchable(text):
    """Templates escape & as &amp;, and a guide spells out what a reader
    sees. Normalise both sides before comparing."""
    text = text.replace('&amp;', '&').replace('&rarr;', '→')
    return re.sub(r'\s+', ' ', text)


# ── the guides cover the product, and only the product ──

def test_every_guided_screen_is_a_real_endpoint():
    endpoints = {rule.endpoint for rule in app_module.app.url_map.iter_rules()}
    for key in guide_data.GUIDES:
        assert key in endpoints, f"guides.py describes '{key}', which is not a route"


def test_every_screen_a_customer_can_reach_has_a_guide():
    """The list is explicit so that adding a customer-facing screen and
    forgetting its guide fails here rather than being noticed by a
    customer."""
    for key in SCREEN_TEMPLATES:
        assert key in guide_data.GUIDES, f"{key} has no guide"


@pytest.mark.parametrize('key', sorted(SCREEN_TEMPLATES))
def test_that_screen_actually_shows_its_help_button(key):
    markup = template_text(SCREEN_TEMPLATES[key])
    assert '_help_panel.html' in markup, \
        f"{SCREEN_TEMPLATES[key]} does not include the help panel"


@pytest.mark.parametrize('key', sorted(guide_data.GUIDES))
def test_a_guide_says_all_four_things(key):
    guide = guide_data.GUIDES[key]
    for field in ('role', 'title', 'where', 'purpose', 'steps', 'controls'):
        assert guide.get(field), f"{key} is missing {field}"
    assert guide['role'] in ('owner', 'agency', 'agent', 'anyone'), key
    assert len(guide['steps']) >= 1, key
    assert len(guide['controls']) >= 3, f"{key} explains too few controls"
    for control, effect in guide['controls']:
        assert len(effect) > 18, f"{key}: '{control}' is explained in too few words"


# ── the test that stops the guides going stale ──

@pytest.mark.parametrize('key', sorted(SCREEN_TEMPLATES))
def test_every_control_a_guide_names_is_on_that_screen(key):
    """Rename a button and this fails, naming the guide and the words it
    could not find. That is the whole point of it."""
    markup = searchable(template_text(SCREEN_TEMPLATES[key]))
    missing = []
    for control, _ in guide_data.GUIDES[key]['controls']:
        needle = GROUP_SAMPLES.get(control, control)
        if searchable(needle) not in markup:
            missing.append(f"{control!r} (looked for {needle!r})")
    assert missing == [], \
        f"{SCREEN_TEMPLATES[key]} no longer has: " + "; ".join(missing)


def test_no_guide_points_at_a_screen_that_no_longer_exists():
    for key, guide in guide_data.GUIDES.items():
        for other in guide.get('related', ()):
            assert other in guide_data.GUIDES, \
                f"{key} sends the reader to '{other}', which has no guide"


# ── the role walkthroughs ──

@pytest.mark.parametrize('role', sorted(guide_data.ROLE_GUIDES))
def test_a_role_guide_walks_real_screens(role):
    plan = guide_data.ROLE_GUIDES[role]
    assert plan['title'] and plan['who'] and plan['first_week']
    assert plan['screens'], f"{role} walks through nothing"
    for key in plan['screens']:
        assert key in guide_data.GUIDES, f"{role} includes '{key}', which has no guide"


def test_an_agent_guide_never_shows_an_agent_an_owner_screen():
    """An agent reading about Clear All Leads would go looking for a
    button they do not have."""
    owner_only = {key for key, guide in guide_data.GUIDES.items()
                  if guide['role'] in ('owner', 'agency')}
    for key in guide_data.ROLE_GUIDES['agent']['screens']:
        assert key not in owner_only, f"the agent guide includes {key}"


def test_a_solo_owner_is_not_shown_the_team_screens():
    """A solo account has no Agents page. Describing one is a support
    email waiting to happen."""
    agency_only = {key for key, guide in guide_data.GUIDES.items()
                   if guide['role'] == 'agency'}
    assert agency_only, 'nothing is marked Agency-only any more'
    for key in guide_data.ROLE_GUIDES['solo-owner']['screens']:
        assert key not in agency_only, f"the solo guide includes {key}"


def test_the_agency_owner_guide_covers_everything_an_owner_has():
    covered = set(guide_data.ROLE_GUIDES['agency-owner']['screens'])
    owner_screens = {key for key, guide in guide_data.GUIDES.items()
                     if guide['role'] in ('owner', 'agency')}
    assert owner_screens <= covered, \
        f"the agency owner guide skips {sorted(owner_screens - covered)}"


# ── the pages ──

def test_the_public_guide_lists_every_role(client):
    body = client.get('/guide').get_data(as_text=True)
    assert client.get('/guide').status_code == 200
    for role, plan in guide_data.ROLE_GUIDES.items():
        assert f'/guide/{role}' in body
        assert plan['title'] in body


@pytest.mark.parametrize('role', sorted(guide_data.ROLE_GUIDES))
def test_each_role_page_prints_every_screen_it_promises(client, role):
    body = client.get(f'/guide/{role}').get_data(as_text=True)
    assert client.get(f'/guide/{role}').status_code == 200
    for key in guide_data.ROLE_GUIDES[role]['screens']:
        guide = guide_data.GUIDES[key]
        assert guide['title'] in body, f"{role} page is missing {key}"
        for control, _ in guide['controls']:
            assert searchable(control) in searchable(body), \
                f"{role} page never mentions {control!r}"


def test_a_role_that_does_not_exist_is_a_404(client):
    assert client.get('/guide/managing-director').status_code == 404


def test_the_guide_pages_are_in_the_sitemap(client):
    body = client.get('/sitemap.xml').get_data(as_text=True)
    assert '/guide</loc>' in body
    for role in guide_data.ROLE_GUIDES:
        assert f'/guide/{role}</loc>' in body


def test_the_faqs_are_published_as_data_and_as_words(client):
    import html
    import json
    body = client.get('/guide').get_data(as_text=True)
    block = re.findall(r'<script type="application/ld\+json">(.*?)</script>',
                       body, re.S)
    faq = [json.loads(item) for item in block]
    faq = [item for item in faq if item.get('@type') == 'FAQPage'][0]
    readable = html.unescape(body)
    assert len(faq['mainEntity']) == len(guide_data.FAQS)
    for entry in faq['mainEntity']:
        assert entry['name'] in readable
        assert entry['acceptedAnswer']['text'] in readable


def test_no_faq_answer_is_a_shrug():
    for question, answer in guide_data.FAQS:
        assert question.endswith(('?', '.')), question
        assert len(answer) > 60, f"'{question}' is answered in too few words"


# ── the panel itself ──

def test_a_screen_with_no_guide_shows_no_button(client):
    """Better no button than a button that opens an empty panel."""
    body = client.get('/pricing').get_data(as_text=True)
    assert 'hp-open' not in body


@pytest.mark.parametrize('key', sorted(SCREEN_TEMPLATES))
def test_the_panel_can_be_closed_without_a_mouse(key):
    panel = template_text('_help_panel.html')
    assert 'Escape' in panel, 'the help panel cannot be dismissed from the keyboard'
    assert 'aria-modal' in panel and 'aria-label' in panel


def test_the_panel_does_not_print():
    """A printed role guide is the thing to keep; a floating ? button on
    a sheet of paper is not."""
    panel = template_text('_help_panel.html')
    assert '@media print' in panel


def test_the_printable_guide_is_actually_printable():
    markup = template_text('guide_role.html')
    assert '@media print' in markup
    assert 'page-break-inside' in markup


def test_the_panel_styles_cannot_collide_with_a_page():
    """It is injected into thirteen pages that each define their own CSS.
    Every class it introduces is prefixed - scanned inside the <style>
    block only, so prose and Jinja attribute access are not mistaken for
    selectors."""
    panel = template_text('_help_panel.html')
    css = panel.split('<style>')[1].split('</style>')[0]
    classes = set(re.findall(r'\.([a-zA-Z][\w-]*)', css))
    stray = {name for name in classes if not name.startswith('hp-')}
    assert stray == set(), f"unprefixed classes in the help panel: {stray}"


def test_the_panel_ids_are_prefixed_too():
    panel = template_text('_help_panel.html')
    ids = set(re.findall(r'\bid="([^"]+)"', panel))
    stray = {name for name in ids if not name.startswith('hp-')}
    assert stray == set(), f"unprefixed ids in the help panel: {stray}"


def test_the_panel_buttons_set_their_own_width():
    """Several of these pages style every button on them with
    width:100% for their own full-width submit button. Inherited, that
    made the panel's close button eat the whole header and squeezed the
    title into a two-character column. The panel has to say what its own
    buttons are."""
    panel = template_text('_help_panel.html')
    css = panel.split('<style>')[1].split('</style>')[0]
    close = re.search(r'\.hp-close\s*\{([^}]*)\}', css).group(1)
    assert 'width' in close, '.hp-close inherits whatever the page says about buttons'
    opener = re.search(r'\.hp-open\s*\{([^}]*)\}', css).group(1)
    assert 'width' in opener and 'min-width' in opener


def test_the_panel_does_not_inherit_a_page_that_centres_everything():
    """Most of these screens centre their own content. A guide read as
    centred prose is unreadable."""
    panel = template_text('_help_panel.html')
    css = panel.split('<style>')[1].split('</style>')[0]
    assert 'text-align: left' in css


def test_nothing_tells_the_reader_to_look_in_the_wrong_place():
    """The button is in the bottom corner. An FAQ that says 'header'
    sends somebody hunting, which is the one thing a guide must not do."""
    panel = template_text('_help_panel.html')
    css = panel.split('<style>')[1].split('</style>')[0]
    opener = re.search(r'\.hp-open\s*\{([^}]*)\}', css).group(1)
    assert 'bottom' in opener, 'the help button has moved - update the wording'
    for question, answer in guide_data.FAQS:
        if '?' in answer and 'button' in answer:
            assert 'header' not in answer.lower(), \
                f"'{question}' says the help button is in the header"
