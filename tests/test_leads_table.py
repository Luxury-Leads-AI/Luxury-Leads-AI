"""Lead rows stay one line tall, on every screen that lists leads.

16 November 2026: "the width of the lead rows has become excessive due to
the volume of data. Since the lead rows can be scrolled horizontally, it
would be better to increase their length" - everywhere leads are shown,
owner and agent, and on a phone.

The tables already scrolled sideways inside .table-wrap, but every cell
was allowed to wrap. Measured with six realistic leads before the change:
owner dashboard rows 69 to 129px tall on a laptop, agent dashboard 63 to
114px. After: 46px and 49px, every row, the table simply longer.
"""
import re
from pathlib import Path

import pytest

import app as app_module

from tests.test_route_guards import make_agency, make_agent, make_lead, login_as_owner, login_as_agent

ROOT = Path(__file__).resolve().parent.parent
THEME = re.sub(r"/\*.*?\*/", "", (ROOT / "static" / "theme.css").read_text(encoding="utf-8"),
               flags=re.S)


def rule_for(selector):
    """The declarations of the first rule whose selector list names this."""
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", THEME):
        if selector in [s.strip() for s in selectors.split(",")]:
            return body
    return ""


def test_no_cell_in_a_lead_table_wraps():
    assert "white-space: nowrap" in rule_for("table.leads-table td")
    assert "white-space: nowrap" in rule_for("table.leads-table th")


def test_the_table_grows_longer_instead():
    body = rule_for("table.leads-table")
    assert "width: max-content" in body
    assert "min-width: 100%" in body


def test_the_same_holds_on_a_phone():
    phone = THEME[THEME.index("table.leads-table"):]
    assert "@media (max-width: 768px)" in phone


@pytest.mark.parametrize("template", ["admin.html", "agent_dashboard.html", "agent_detail.html"])
def test_every_screen_that_lists_leads_uses_it(template):
    source = (ROOT / "templates" / template).read_text(encoding="utf-8")
    assert 'class="leads-table"' in source
    assert "/static/theme.css" in source


@pytest.fixture()
def agency_with_a_lead():
    agency = make_agency()
    agent = make_agent(agency.id)
    lead = make_lead(agency.id, agent_id=agent.id, lead_type="buyer",
                     budget="8 million USD (based on properties discussed)",
                     whatsapp_number="+65824778886", timeline="immediate")
    yield agency, agent, lead
    for row in (lead, agent, agency):
        app_module.db.session.delete(row)
    app_module.db.session.commit()


def test_the_owner_dashboard_renders_it(client, agency_with_a_lead):
    agency, _agent, _lead = agency_with_a_lead
    login_as_owner(client, agency.id)
    page = client.get(f"/admin?agency_id={agency.id}").get_data(as_text=True)
    assert '<table id="leadsTable" class="leads-table">' in page
    assert "8 million USD (based on properties discussed)" in page


def test_the_agent_dashboard_renders_it(client, agency_with_a_lead):
    _agency, agent, _lead = agency_with_a_lead
    login_as_agent(client, agent.id)
    page = client.get(f"/agent-dashboard/{agent.id}").get_data(as_text=True)
    assert 'class="leads-table"' in page
