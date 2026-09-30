"""Every table the acquisition engine owns.

Nineteen tables, all named acq_*, living in the same database as the SaaS.
The engine reads the SaaS's own tables (lead, appointment, conversation
session) to measure how a pilot is going, but it never writes to them: the
only way in is provision_agency().

Why the classes are built inside define(db) instead of at import time: a
model class needs db.Model when it is defined, and the engine must never
import app.py (that would be a circular import, and under `python app.py`
it would load the whole app a second time). So app.py hands its db in
through acquisition.init_app(), and this module builds the classes then.
After that they are ordinary module attributes - acquisition.models.Market
and so on - because define() puts them there.
"""
from datetime import datetime

# Filled in by define(). Named here so editors and readers can see what
# exists, and so a typo in a later import fails loudly rather than quietly.
Market = Prospect = Fact = Contact = Suppression = None
ScoreModel = Score = Campaign = Template = Message = Reply = None
Demo = Onboarding = Pilot = Feedback = Job = Cost = Audit = Setting = None

ALL = []

# ── the small fixed vocabularies, kept here so screens, jobs and tests all
# read the same list ──
LEGAL_STATUSES = ('verified', 'needs_verification', 'unknown')
LEGAL_STATUS_LABELS = {
    'verified': 'Verified',
    'needs_verification': 'Needs verification',
    'unknown': 'Unknown',
}
OUTREACH_METHODS = ('email', 'founder_led', 'none')
OUTREACH_METHOD_LABELS = {
    'email': 'Email, sent by you',
    'founder_led': 'Founder-led only',
    'none': 'No outreach yet',
}
PROSPECT_STAGES = ('new', 'researching', 'researched', 'qualified', 'queued',
                   'contacted', 'replied', 'interested', 'onboarding',
                   'pilot', 'client', 'rejected', 'parked')
PROSPECT_STAGE_LABELS = {
    'new': 'New',
    'researching': 'Researching',
    'researched': 'Researched',
    'qualified': 'Qualified',
    'queued': 'Queued for outreach',
    'contacted': 'Contacted',
    'replied': 'Replied',
    'interested': 'Interested',
    'onboarding': 'Onboarding',
    'pilot': 'Pilot',
    'client': 'Client',
    'rejected': 'Not interested',
    'parked': 'Parked',
}
CONFIDENCE_LEVELS = ('verified', 'inferred', 'estimated')
JOB_STATUSES = ('queued', 'running', 'done', 'failed', 'cancelled')


def define(db):
    """Build every model against this SQLAlchemy instance, once."""
    global Market, Prospect, Fact, Contact, Suppression, ScoreModel, Score
    global Campaign, Template, Message, Reply, Demo, Onboarding, Pilot
    global Feedback, Job, Cost, Audit, Setting, ALL

    if Market is not None:          # already built (app.py imported twice)
        return ALL

    class Market(db.Model):
        """A city we may prospect in, and what the law there allows.

        legal_status is the gate: the engine researches and scores prospects
        anywhere, but will not draft outreach for a market that is not
        Verified until a person records a decision.
        """
        __tablename__ = 'acq_market'
        id = db.Column(db.Integer, primary_key=True)
        country = db.Column(db.String(80), nullable=False)
        country_code = db.Column(db.String(4))
        city = db.Column(db.String(80), nullable=False)
        language = db.Column(db.String(12), default='en')
        timezone = db.Column(db.String(60))
        target_type = db.Column(db.String(80), default='real estate agency')
        luxury_focus = db.Column(db.Boolean, default=True)
        outreach_method = db.Column(db.String(20), default='none')
        legal_status = db.Column(db.String(24), default='unknown')
        legal_note = db.Column(db.Text)
        company_check_needed = db.Column(db.Boolean, default=False)
        postal_address_needed = db.Column(db.Boolean, default=False)
        national_opt_out_list = db.Column(db.String(200))
        human_approval_required = db.Column(db.Boolean, default=True)
        status = db.Column(db.String(20), default='proposed')  # proposed/active/paused
        notes = db.Column(db.Text)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)

        @property
        def name(self):
            return f"{self.city}, {self.country}"

        @property
        def may_send_email(self):
            return self.legal_status == 'verified' and self.outreach_method == 'email'

    class Prospect(db.Model):
        """One agency we might sell to. canonical_domain is the identity:
        two rows for the same website is the mistake that gets somebody two
        cold emails from us."""
        __tablename__ = 'acq_prospect'
        id = db.Column(db.Integer, primary_key=True)
        market_id = db.Column(db.Integer, db.ForeignKey('acq_market.id'), index=True)
        canonical_domain = db.Column(db.String(200), unique=True, nullable=False)
        name = db.Column(db.String(200))
        website = db.Column(db.String(300))
        phone = db.Column(db.String(60))
        address = db.Column(db.String(300))
        source = db.Column(db.String(40), default='manual')
        source_ref = db.Column(db.String(300))
        stage = db.Column(db.String(20), default='new', index=True)
        priority = db.Column(db.Integer, default=0)
        fit_score = db.Column(db.Integer)
        reachability = db.Column(db.String(20))
        confidence = db.Column(db.String(20))
        do_not_contact = db.Column(db.Boolean, default=False)
        do_not_contact_reason = db.Column(db.String(200))
        needs_human = db.Column(db.Boolean, default=False)
        needs_human_reason = db.Column(db.String(200))
        next_action_at = db.Column(db.DateTime, index=True)
        agency_id = db.Column(db.Integer)      # set only when it becomes a client
        notes = db.Column(db.Text)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                               onupdate=datetime.utcnow)

        market = db.relationship('Market', backref='prospects')

    class Fact(db.Model):
        """One researched thing about a prospect, with where it came from.

        Every fact carries its source page and how sure we are. A draft may
        only use facts marked verified, which is what stops the AI inventing
        flattering details about someone's business.
        """
        __tablename__ = 'acq_fact'
        id = db.Column(db.Integer, primary_key=True)
        prospect_id = db.Column(db.Integer, db.ForeignKey('acq_prospect.id'),
                                index=True, nullable=False)
        field = db.Column(db.String(60), nullable=False)
        value = db.Column(db.Text)
        confidence = db.Column(db.String(20), default='inferred')
        source_url = db.Column(db.String(500))
        extractor = db.Column(db.String(40))   # 'rules' or a model name
        created_at = db.Column(db.DateTime, default=datetime.utcnow)

        prospect = db.relationship('Prospect', backref='facts')

    class Contact(db.Model):
        """A person or inbox at a prospect. personal_data_expires_at is what
        the retention job reads."""
        __tablename__ = 'acq_contact'
        id = db.Column(db.Integer, primary_key=True)
        prospect_id = db.Column(db.Integer, db.ForeignKey('acq_prospect.id'),
                                index=True, nullable=False)
        email = db.Column(db.String(200), index=True)
        name = db.Column(db.String(120))
        role = db.Column(db.String(120))
        is_generic = db.Column(db.Boolean, default=True)   # info@ vs a person
        email_check = db.Column(db.String(20))             # ok / no_mx / bad_format
        email_checked_at = db.Column(db.DateTime)
        source_url = db.Column(db.String(500))
        personal_data_expires_at = db.Column(db.DateTime)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)

        prospect = db.relationship('Prospect', backref='contacts')

    class Suppression(db.Model):
        """Never contact this address or domain again.

        Deliberately has no foreign key: an opt-out must outlive the
        prospect row it came from, including when that row is deleted.
        """
        __tablename__ = 'acq_suppression'
        id = db.Column(db.Integer, primary_key=True)
        email = db.Column(db.String(200), index=True)
        domain = db.Column(db.String(200), index=True)
        reason = db.Column(db.String(200))
        created_at = db.Column(db.DateTime, default=datetime.utcnow)

    class ScoreModel(db.Model):
        """The weights behind a score. Editing them makes a new version, so
        an old score still says what it meant when it was given."""
        __tablename__ = 'acq_score_model'
        id = db.Column(db.Integer, primary_key=True)
        version = db.Column(db.Integer, default=1)
        weights = db.Column(db.Text)            # JSON
        thresholds = db.Column(db.Text)         # JSON
        active = db.Column(db.Boolean, default=False)
        notes = db.Column(db.Text)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)

    class Score(db.Model):
        __tablename__ = 'acq_score'
        id = db.Column(db.Integer, primary_key=True)
        prospect_id = db.Column(db.Integer, db.ForeignKey('acq_prospect.id'),
                                index=True, nullable=False)
        model_version = db.Column(db.Integer)
        fit = db.Column(db.Integer)
        reachability = db.Column(db.String(20))
        confidence = db.Column(db.String(20))
        breakdown = db.Column(db.Text)          # JSON: why, line by line
        created_at = db.Column(db.DateTime, default=datetime.utcnow)

        prospect = db.relationship('Prospect', backref='scores')

    class Campaign(db.Model):
        __tablename__ = 'acq_campaign'
        id = db.Column(db.Integer, primary_key=True)
        market_id = db.Column(db.Integer, db.ForeignKey('acq_market.id'))
        name = db.Column(db.String(120))
        status = db.Column(db.String(20), default='draft')
        sequence_days = db.Column(db.String(60), default='0,3,7,14')
        daily_limit = db.Column(db.Integer, default=10)
        approval_mode = db.Column(db.String(20), default='manual')
        created_at = db.Column(db.DateTime, default=datetime.utcnow)

    class Template(db.Model):
        __tablename__ = 'acq_template'
        id = db.Column(db.Integer, primary_key=True)
        kind = db.Column(db.String(40))         # initial / follow_up_1 / demo / pilot ...
        language = db.Column(db.String(12), default='en')
        subject = db.Column(db.String(300))
        body = db.Column(db.Text)
        version = db.Column(db.Integer, default=1)
        active = db.Column(db.Boolean, default=False)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)

    class Message(db.Model):
        """A drafted email. In Bootstrap mode it is sent by hand, so 'sent'
        means "he pressed Mark as sent"."""
        __tablename__ = 'acq_message'
        id = db.Column(db.Integer, primary_key=True)
        prospect_id = db.Column(db.Integer, db.ForeignKey('acq_prospect.id'),
                                index=True, nullable=False)
        contact_id = db.Column(db.Integer, db.ForeignKey('acq_contact.id'))
        campaign_id = db.Column(db.Integer, db.ForeignKey('acq_campaign.id'))
        template_id = db.Column(db.Integer, db.ForeignKey('acq_template.id'))
        step = db.Column(db.Integer, default=0)
        status = db.Column(db.String(24), default='draft', index=True)
        subject = db.Column(db.String(300))
        body = db.Column(db.Text)
        facts_used = db.Column(db.Text)         # JSON list of acq_fact ids
        check_results = db.Column(db.Text)      # JSON: claim check, compliance
        approved_at = db.Column(db.DateTime)
        sent_at = db.Column(db.DateTime)
        provider = db.Column(db.String(40), default='manual')
        created_at = db.Column(db.DateTime, default=datetime.utcnow)

        prospect = db.relationship('Prospect', backref='messages')

    class Reply(db.Model):
        __tablename__ = 'acq_reply'
        id = db.Column(db.Integer, primary_key=True)
        prospect_id = db.Column(db.Integer, db.ForeignKey('acq_prospect.id'),
                                index=True, nullable=False)
        message_id = db.Column(db.Integer, db.ForeignKey('acq_message.id'))
        raw_text = db.Column(db.Text)
        received_at = db.Column(db.DateTime, default=datetime.utcnow)
        classification = db.Column(db.String(40))
        confidence = db.Column(db.Float)
        classified_by = db.Column(db.String(40))
        handled = db.Column(db.Boolean, default=False)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)

        prospect = db.relationship('Prospect', backref='replies')

    class Demo(db.Model):
        __tablename__ = 'acq_demo'
        id = db.Column(db.Integer, primary_key=True)
        prospect_id = db.Column(db.Integer, db.ForeignKey('acq_prospect.id'),
                                index=True, nullable=False)
        token = db.Column(db.String(80), unique=True)
        sandbox_agency_id = db.Column(db.Integer)
        expires_at = db.Column(db.DateTime)
        views = db.Column(db.Integer, default=0)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)

    class Onboarding(db.Model):
        __tablename__ = 'acq_onboarding'
        id = db.Column(db.Integer, primary_key=True)
        prospect_id = db.Column(db.Integer, db.ForeignKey('acq_prospect.id'),
                                index=True, nullable=False)
        token_hash = db.Column(db.String(200))
        expires_at = db.Column(db.DateTime)
        answers = db.Column(db.Text)            # JSON
        status = db.Column(db.String(24), default='sent')
        agency_id = db.Column(db.Integer)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)

    class Pilot(db.Model):
        __tablename__ = 'acq_pilot'
        id = db.Column(db.Integer, primary_key=True)
        prospect_id = db.Column(db.Integer, db.ForeignKey('acq_prospect.id'), index=True)
        agency_id = db.Column(db.Integer, index=True)
        starts_at = db.Column(db.DateTime)
        ends_at = db.Column(db.DateTime)
        success_criteria = db.Column(db.Text)
        status = db.Column(db.String(24), default='active')
        outcome = db.Column(db.String(24))
        outcome_reason = db.Column(db.Text)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)

    class Feedback(db.Model):
        __tablename__ = 'acq_feedback'
        id = db.Column(db.Integer, primary_key=True)
        pilot_id = db.Column(db.Integer, db.ForeignKey('acq_pilot.id'), index=True)
        agency_id = db.Column(db.Integer)
        category = db.Column(db.String(60))
        question = db.Column(db.Text)
        text = db.Column(db.Text)
        rating = db.Column(db.Integer)
        severity = db.Column(db.String(20))
        source = db.Column(db.String(40))       # survey / call / email
        created_at = db.Column(db.DateTime, default=datetime.utcnow)

    class Job(db.Model):
        """One small unit of work. The browser runs these today and a worker
        runs them later; the row is the same either way.

        idempotency_key is what stops the same work being queued twice - for
        example, researching one prospect from two different screens.
        """
        __tablename__ = 'acq_job'
        id = db.Column(db.Integer, primary_key=True)
        type = db.Column(db.String(40), nullable=False, index=True)
        prospect_id = db.Column(db.Integer, index=True)
        payload = db.Column(db.Text)            # JSON
        status = db.Column(db.String(20), default='queued', index=True)
        attempts = db.Column(db.Integer, default=0)
        max_attempts = db.Column(db.Integer, default=3)
        run_after = db.Column(db.DateTime, default=datetime.utcnow, index=True)
        locked_until = db.Column(db.DateTime)
        idempotency_key = db.Column(db.String(200), unique=True)
        last_error = db.Column(db.Text)
        result = db.Column(db.Text)             # JSON
        duration_ms = db.Column(db.Integer)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        finished_at = db.Column(db.DateTime)

    class Cost(db.Model):
        """Every penny the engine spends, with what it bought."""
        __tablename__ = 'acq_cost'
        id = db.Column(db.Integer, primary_key=True)
        provider = db.Column(db.String(40), default='openai')
        model = db.Column(db.String(60))
        purpose = db.Column(db.String(60))
        input_tokens = db.Column(db.Integer, default=0)
        output_tokens = db.Column(db.Integer, default=0)
        units = db.Column(db.Integer, default=0)     # e.g. web searches
        usd = db.Column(db.Float, default=0.0)
        job_id = db.Column(db.Integer)
        prospect_id = db.Column(db.Integer)
        created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    class Audit(db.Model):
        """What the engine and its operator did, in order."""
        __tablename__ = 'acq_audit'
        id = db.Column(db.Integer, primary_key=True)
        actor = db.Column(db.String(40), default='system')
        action = db.Column(db.String(60), nullable=False)
        entity = db.Column(db.String(60))
        entity_id = db.Column(db.Integer)
        before = db.Column(db.Text)
        after = db.Column(db.Text)
        ip = db.Column(db.String(60))
        created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    class Setting(db.Model):
        """Mode, budgets, caps and kill switches - editable without a deploy."""
        __tablename__ = 'acq_setting'
        key = db.Column(db.String(60), primary_key=True)
        value = db.Column(db.Text)
        updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                               onupdate=datetime.utcnow)

    ALL = [Market, Prospect, Fact, Contact, Suppression, ScoreModel, Score,
           Campaign, Template, Message, Reply, Demo, Onboarding, Pilot,
           Feedback, Job, Cost, Audit, Setting]

    globals().update({model.__name__: model for model in ALL})
    globals()['ALL'] = ALL
    return ALL
