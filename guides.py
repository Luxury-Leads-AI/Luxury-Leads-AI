"""The user guides, as data.

Every screen a customer can reach gets an entry here, keyed by the Flask
endpoint that renders it. `app.py` looks the current endpoint up and the
shared `_help_panel.html` partial draws it behind the ? button, so a
screen's guide travels with the screen instead of living in a document
somebody forgets to update.

This is a content file, not a second backend. It imports nothing, holds
no logic and touches no database - app.py is still the only place with
routes, models and AI. Keeping the prose out of a 320 KB app.py is the
only reason it is separate.

Each entry:
    role      'owner', 'agency', 'agent' or 'anyone' - who sees this
              screen. 'agency' means the Agency plan only.
    title     the screen's name, as the guide calls it
    where     how you got here, so somebody lost can place themselves
    purpose   one sentence: what this screen is for
    steps     the ordinary job, in order
    controls  (control, what it does AND what changes as a result)
    notes     the things that surprise people
    related   endpoints to read next

A rule for everything written here: describe what the screen does, not
what it ought to do. A guide that is wrong is worse than no guide, so
`tests/test_user_guides.py` checks every control named below against the
template it claims to be on.
"""

GUIDES = {

    # ── getting in ───────────────────────────────────────────────────

    'owner_login': {
        'role': 'owner',
        'title': 'Agency login',
        'where': 'The page at /owner-login. This is where an agency owner '
                 'signs in.',
        'purpose': 'Get the owner into their dashboard.',
        'steps': (
            'Type your Agency ID. It is the number we emailed you when your '
            'account was created, and it never changes.',
            'Type your password.',
            'Press Login to Dashboard.',
        ),
        'controls': (
            ('Agency ID',
             'Your account number, not your email. If you have lost it, it '
             'is in the welcome email and in every lead alert we send you.'),
            ('Password',
             'Set when your account was created. Change it later from the '
             'dashboard.'),
            ('Login to Dashboard',
             'Signs you in and takes you to your leads. A wrong ID or '
             'password sends you back here with a message above the form.'),
            ('Forgot password?',
             'Emails you a link that lets you set a new one. The link works '
             'once and expires.'),
            ('Create Agency',
             'For somebody who does not have an account yet. If you already '
             'have one, do not use this - it starts a second, separate '
             'agency.'),
        ),
        'notes': (
            'Agents do not sign in here. They have their own page at '
            '/agent-login, with an email instead of an Agency ID.',
        ),
        'related': ('admin', 'agent_login'),
    },

    'agent_login': {
        'role': 'agent',
        'title': 'Agent login',
        'where': 'The page at /agent-login. This is where an agent signs in.',
        'purpose': 'Get an agent into their own dashboard.',
        'steps': (
            'Type the email address your agency added you with.',
            'Type your password - the one in your welcome email, unless you '
            'have changed it.',
            'Press Sign In.',
        ),
        'controls': (
            ('Email',
             'The address your owner used when they added you. Not your '
             'personal address, unless they are the same.'),
            ('Password',
             'Emailed to you when you were added. Change it from your '
             'dashboard once you are in.'),
            ('Sign In',
             'Takes you to your dashboard: your leads, your viewings, '
             'nobody else\'s.'),
            ('Forgot password?',
             'Emails you a one-time link to set a new one.'),
            ('Agency owner? Login here',
             'Switches to the owner login. If you are an agent, you do not '
             'need it.'),
        ),
        'notes': (
            'If sign-in is refused and the password is right, ask your owner '
            'whether your account is still enabled - a disabled agent keeps '
            'their leads but cannot sign in.',
        ),
        'related': ('agent_dashboard',),
    },

    'forgot_password': {
        'role': 'anyone',
        'title': 'Reset your password',
        'where': 'The page at /forgot-password, reached from either login '
                 'screen.',
        'purpose': 'Send you a link that lets you set a new password.',
        'steps': (
            'Type the email address on your account.',
            'Press Send Reset Link.',
            'Open the email and follow the link.',
        ),
        'controls': (
            ('Email Address',
             'Owners: the address on your agency. Agents: the address you '
             'sign in with.'),
            ('Send Reset Link',
             'Emails a one-time link. The page says the same thing whether '
             'or not that address has an account - deliberately, so nobody '
             'can use this page to find out who has one.'),
            ('Agency owner login',
             'Back to the owner sign-in page, if you landed here by mistake.'),
            ('Agent login',
             'Back to the agent sign-in page.'),
        ),
        'notes': (
            'The link works once and then expires. Request another if you '
            'run out of time.',
            'Nothing changes until you set the new password - your old one '
            'keeps working until then.',
        ),
        'related': ('reset_password',),
    },

    'reset_password': {
        'role': 'anyone',
        'title': 'Set a new password',
        'where': 'The page the emailed reset link opens.',
        'purpose': 'Replace your password with one you choose.',
        'steps': (
            'Type the new password.',
            'Type it again to confirm.',
            'Press Reset Password, then sign in with it.',
        ),
        'controls': (
            ('New Password / Confirm New Password',
             'Both have to match. The page says so before it will save.'),
            ('Reset Password',
             'Saves the new password and spends the link, so the same email '
             'cannot be used again.'),
            ('Request a new link',
             'Shown when a link has expired or has already been used.'),
        ),
        'notes': (
            'Your leads, agents and appointments are untouched by this - '
            'only the password changes.',
        ),
        'related': ('owner_login', 'agent_login'),
    },

    # ── the owner's screens ──────────────────────────────────────────

    'admin': {
        'role': 'owner',
        'title': 'Leads dashboard',
        'where': 'The first screen after you sign in, at '
                 '/admin?agency_id=<your id>. Everything else is reached '
                 'from the row of buttons at the top.',
        'purpose': 'Every lead your assistant has captured, newest first, '
                   'with everything you need to decide who to call.',
        'steps': (
            'Read the four tiles across the top for the shape of things: '
            'Total Leads, Hot Leads, High Quality and Closed.',
            'Open Latest updates if it is showing a badge - that is what has '
            'changed since you last looked.',
            'Narrow the list with the filter rows if you want only hot '
            'leads, only new ones, or only sellers.',
            'Click a row, or its View button, to open the full lead.',
            'Set the status as you work it, and leave a note for whoever '
            'picks it up next.',
        ),
        'controls': (
            ('Total Leads / Hot Leads (5⭐) / High Quality (4-5⭐) / Closed',
             'Counts, not buttons. They describe the whole list, not '
             'whatever filter you have on.'),
            ('🔔 Latest updates',
             'Opens the feed of what has happened since you last opened it - '
             'new leads, bookings, agent activity. Opening it is what marks '
             'this side as caught up; your agents\' unread counts are their '
             'own.'),
            ('Quality filter row (All / Hot / High / Medium / Low / Cold)',
             'Shows only leads with that star rating. It filters the table '
             'in front of you; it does not delete or archive anything.'),
            ('Status filter row (All Status / New / Contacted / Meeting / '
             'Closed / Lost)',
             'Shows only leads at that stage. Combines with the other two '
             'rows - Hot plus New plus Buyers is a valid view.'),
            ('Type filter row (Everyone / Buyers / Sellers)',
             'A seller is somebody who offered you a property through the '
             'chat rather than asking about one.'),
            ('Search name, email, budget',
             'Filters the table as you type, alongside whatever filters are '
             'already on. This is how you find somebody who has just rung '
             'you, without scrolling.'),
            ('👁 View, or clicking the row',
             'Opens the lead: their details, the star rating with the reason '
             'for it, what the assistant learned, their appointments, and '
             'the notes.'),
            ('Status buttons inside a lead (New, Contacted, Meeting, Closed, '
             'Lost)',
             'Set where the lead has got to. Saved immediately - there is no '
             'separate save - and the dashboard counts and your agent\'s '
             'view update with it.'),
            ('+ Add (Notes)',
             'Adds a note to that lead, visible to you and to the agent it '
             'is assigned to. Use it for what was said on the phone.'),
            ('🗑️ Remove (on a note)',
             'Deletes that note only. The lead is untouched.'),
            ('🗑️ (on a lead row)',
             'Deletes that one lead and everything on it - notes, '
             'transcript, rating. It cannot be undone. Export first if you '
             'might want it.'),
            ('Select checkboxes, then 🗑️ Delete Selected',
             'Deletes several leads at once. Also permanent. ✕ Cancel clears '
             'the selection without deleting anything.'),
            ('🗑️ Clear All Leads',
             'Empties the whole list. Nothing survives it. It asks first, '
             'and you should Export Excel before saying yes.'),
            ('📥 Export Excel',
             'Downloads every lead as a spreadsheet, including the ones a '
             'filter is currently hiding.'),
            ('📈 Analytics',
             'Leads over time and the spread of quality scores.'),
            ('📅 Appointments',
             'The viewings calendar - everything the assistant booked and '
             'everything you added by hand.'),
            ('👥 Agents',
             'Your team. Agency plan only.'),
            ('🏠 Listings',
             'The properties the assistant can talk about.'),
            ('⚙️ Profile',
             'Your agency\'s name, contact details and the assistant\'s name.'),
            ('🔑 Change Password',
             'Changes your own owner password. Agents change theirs on their '
             'own dashboard.'),
            ('🚪 Logout',
             'Signs you out. Your Agency ID is still needed to get back in.'),
        ),
        'notes': (
            'When the list is empty the page shows you the one line of code '
            'to paste into your website. Nothing arrives here until that '
            'line is on your site.',
            'The star rating comes with "Why this rating" inside the lead. '
            'If you disagree with a score, that panel tells you what it was '
            'reading.',
            'Deleting is the only irreversible thing on this screen. '
            'Everything else can be set back.',
        ),
        'related': ('analytics', 'appointments', 'agents_page', 'listings',
                    'agency_profile'),
    },

    'agents_page': {
        'role': 'agency',
        'title': 'Agents',
        'where': 'Reached from 👥 Agents on the leads dashboard. Agency plan '
                 'only - a solo account has no team to manage.',
        'purpose': 'Add the people on your team, give them their own logins, '
                   'and see how each is doing.',
        'steps': (
            'Press + Add Agent and fill in their name, email and coverage '
            'area.',
            'They are emailed their login details.',
            'New leads start being assigned to them automatically.',
            'Use View leads & viewings on any agent to see their work.',
        ),
        'controls': (
            ('+ Add Agent',
             'Opens the form. Name and email are required; coverage area and '
             'password are optional. Leave the password blank and one is '
             'generated and emailed to them.'),
            ('Coverage Area',
             'The part of town or the kind of property they cover. It is '
             'used when deciding who a new lead should go to, so it is worth '
             'filling in.'),
            ('✏️ Edit',
             'Changes their name, email or coverage area. It does not change '
             'their password and does not move leads they already have.'),
            ('⏸ Disable',
             'Stops them signing in. Their leads and viewings stay exactly '
             'where they are, still assigned to them, still visible to you. '
             'Use this when somebody leaves.'),
            ('▶ Enable',
             'Lets a disabled agent back in. Nothing was lost while they '
             'were off.'),
            ('🗑️',
             'Deletes the agent. Use Disable instead unless you are certain - '
             'deleting is permanent.'),
            ('View leads & viewings →',
             'Opens that agent\'s page: the leads assigned to them and the '
             'viewings they are taking.'),
            ('← Back to Dashboard',
             'Returns to your leads dashboard.'),
        ),
        'notes': (
            'Your plan sets how many agents you can have. The Agency plan is '
            'up to ten.',
            'An agent only ever sees their own leads. You see everybody\'s.',
        ),
        'related': ('agent_detail', 'admin'),
    },

    'agent_detail': {
        'role': 'agency',
        'title': 'One agent',
        'where': 'Reached from View leads & viewings on the Agents page.',
        'purpose': 'See what one agent is carrying, without signing in as '
                   'them.',
        'steps': (
            'Read the coverage area and the count of assigned leads.',
            'Look down Assigned Leads for anything sitting still.',
            'Look down Viewings for what they have booked.',
        ),
        'controls': (
            ('Coverage area',
             'What you set for them on the Agents page. Change it there.'),
            ('Assigned Leads',
             'Every lead currently theirs, with its status. Read-only here - '
             'open the lead on your own dashboard to change anything.'),
            ('Viewings',
             'The appointments they are taking. Reassign one from the '
             'Appointments calendar, not from here.'),
            ('← All agents',
             'Returns to the list of everybody on your team.'),
        ),
        'notes': (
            'This is the owner\'s view of an agent. It is not the screen the '
            'agent sees - theirs has the buttons to actually work a lead.',
        ),
        'related': ('agents_page', 'agent_dashboard'),
    },

    'appointments': {
        'role': 'owner',
        'title': 'Appointments',
        'where': 'Reached from 📅 Appointments on the leads dashboard.',
        'purpose': 'Every viewing the assistant has booked and every one you '
                   'have added by hand, with what happened at each.',
        'steps': (
            'Read the five tiles: Total, Pending, Confirmed, Completed, '
            'Cancelled.',
            'Filter to Pending to see what still needs confirming.',
            'Open a viewing to change its status, move it to another agent, '
            'or record how it went.',
            'Add anything booked over the phone with + New Appointment.',
        ),
        'controls': (
            ('Total / Pending / Confirmed / Completed / Cancelled tiles',
             'Counts across every viewing, not just the filtered ones.'),
            ('Filter row (All / Pending / Confirmed / Viewing done / Wants '
             'to buy / Other options / Not interested / Cancelled)',
             'Narrows the list on screen. The last three are outcomes - how '
             'a viewing that has happened actually went. Nothing is deleted '
             'or hidden permanently.'),
            ('Search by name or email',
             'Filters the list as you type. Useful when a customer rings and '
             'you need their viewing in front of you.'),
            ('+ New Appointment',
             'Books one yourself: customer name and email, date, time slot, '
             'which agent, which property, and a note. Use it for anything '
             'arranged over the phone so the calendar stays true.'),
            ('Assign to Agent',
             'On the booking form. Sets who is taking the viewing; they are '
             'emailed about it.'),
            ('👤 Agent dropdown on a viewing',
             'Moves that viewing to a different agent, or to Unassigned. It '
             'saves as soon as you pick - there is no save button. If that '
             'agent already has a booking at the same time it is refused and '
             'says so, so you cannot double-book somebody by accident. Both '
             'agents are told: the one who gained the viewing and the one '
             'who lost it, each in their own Latest updates. Agency plan '
             'only - a solo account has nobody to reassign to.'),
            ('Max viewings per slot (Save)',
             'How many viewings may share one time slot before the assistant '
             'stops offering it. Raise it if you are turning people away, '
             'lower it if you are double-booked.'),
            ('📋 Status dropdown on a viewing',
             'The one control that moves a viewing through its life: '
             'pending, confirmed, viewing done, and then how it went - wants '
             'to buy, other options, not interested - or cancelled. It saves '
             'the moment you pick, and the tiles at the top follow.'),
            ('📅 Book another viewing',
             'Books the next viewing for the same customer without retyping '
             'their details.'),
            ('🗑️ on a viewing',
             'Deletes it. Permanent, and the customer is not told. Set the '
             'status to cancelled instead if they should know.'),
            ('View this agent →',
             'Opens that agent\'s own page, with everything they are '
             'carrying.'),
            ('📊 Leads Dashboard',
             'Takes you back to your leads.'),
        ),
        'notes': (
            'A viewing the assistant booked and one you typed in look and '
            'behave the same.',
            'Cancelling and deleting are different. Cancelling keeps the '
            'record; deleting removes it.',
        ),
        'related': ('admin', 'agent_detail'),
    },

    'listings': {
        'role': 'owner',
        'title': 'Listings',
        'where': 'Reached from 🏠 Listings on the leads dashboard.',
        'purpose': 'The properties your assistant is allowed to talk about, '
                   'and the ones sellers have offered you through the chat.',
        'steps': (
            'Add your properties - one at a time, or a spreadsheet at once.',
            'Anything marked Available is what the assistant will offer.',
            'Check Pending for properties a seller submitted, and approve or '
            'reject each.',
            'Mark a property Sold when it goes, so the assistant stops '
            'offering it.',
        ),
        'controls': (
            ('Total / Available / Pending / Sold tiles',
             'Counts. Available is the only status the assistant reads from.'),
            ('Filter row (All / Available / Pending / Sold)',
             'Narrows the list on screen. It changes nothing about the '
             'properties themselves.'),
            ('Search listings',
             'Filters the list as you type, and works alongside whichever '
             'status filter is on. Quicker than scrolling once you have more '
             'than a screenful of stock.'),
            ('+ Add Listing',
             'Title is required; price, type, location, bedrooms, bathrooms, '
             'features and description are optional but every one you fill '
             'in is something the assistant can answer on.'),
            ('📂 Upload CSV',
             'Adds many at once from a spreadsheet. Faster than typing if '
             'you already have your stock in a file.'),
            ('✅ Approve & publish',
             'On a property a seller submitted. Approving is what makes it '
             'visible to buyers - until you press it the assistant will not '
             'mention it to anybody.'),
            ('✕ Reject',
             'Turns down a seller\'s submission. It never reaches a buyer.'),
            ('⏳ Pending / 🏷️ Sold / ✅ Available / ↩ Re-list',
             'Move a property between states. Sold takes it out of the '
             'assistant\'s mouth without deleting it; Re-list puts it back.'),
            ('🗑️ on a listing',
             'Deletes that property permanently.'),
            ('🗑️ Clear All',
             'Deletes every listing. It asks first. There is no undo.'),
            ('📊 Leads Dashboard',
             'Back to your leads.'),
        ),
        'notes': (
            'Nothing a seller types through the chat reaches a buyer until '
            'you approve it. That is deliberate.',
            'An assistant with no listings still captures leads - it just '
            'cannot answer "what have you got in that area".',
        ),
        'related': ('admin',),
    },

    'analytics': {
        'role': 'owner',
        'title': 'Analytics',
        'where': 'Reached from 📈 Analytics on the leads dashboard.',
        'purpose': 'Whether the assistant is earning its keep, as numbers '
                   'rather than a feeling.',
        'steps': (
            'Read the four tiles: Total Leads, Hot Leads, This Month, '
            'Average Quality Score.',
            'Look at Leads Per Day for the trend over the last 30 days.',
            'Look at Quality Distribution to see whether more traffic is '
            'bringing better enquiries or just more of them.',
        ),
        'controls': (
            ('Total Leads / Hot Leads (5⭐) / This Month / Avg Quality Score',
             'Counts and an average. This Month resets on the first.'),
            ('Leads Per Day - Last 30 Days',
             'A line per day. A flat stretch usually means a quiet week on '
             'the website, not a broken assistant - check Total Leads is '
             'still rising.'),
            ('Quality Distribution',
             'How your leads spread across one to five stars.'),
            ('CRM Webhook Integration (Save Webhook)',
             'Paste a webhook address and every qualifying lead is also '
             'posted to it as JSON, the moment it is captured. Works with '
             'HubSpot, Follow Up Boss, Zapier, Make.com and anything else '
             'that accepts a webhook. Leave it blank and nothing is sent.'),
            ('← Back to Leads',
             'Returns to your leads dashboard.'),
        ),
        'notes': (
            'The webhook is in addition to the email alert, not instead of '
            'it.',
            'Charts count leads, not conversations. Somebody who chatted and '
            'left without giving an email is not here.',
        ),
        'related': ('admin',),
    },

    'agency_profile': {
        'role': 'owner',
        'title': 'Agency profile',
        'where': 'Reached from ⚙️ Profile on the leads dashboard.',
        'purpose': 'Your agency\'s details and your assistant\'s name - the '
                   'things visitors and your emails see.',
        'steps': (
            'Change what needs changing.',
            'Press Save Changes.',
            'The next conversation uses the new settings.',
        ),
        'controls': (
            ('Business Name',
             'Used in the chat and in the emails we send you.'),
            ('Owner Name',
             'Who we address when we write to you.'),
            ('Email Address',
             'Where every lead alert goes. Change it here and the next alert '
             'follows. Get it wrong and you stop hearing about leads.'),
            ('WhatsApp Number',
             'Your own number, for your records.'),
            ('AI Assistant Name',
             'What the assistant calls itself to visitors. Changing it takes '
             'effect on the next conversation, not on ones already open.'),
            ('Max Viewings Per Time Slot',
             'How many bookings may share a slot when you have no agents set '
             'up. With agents, their own availability governs instead.'),
            ('💾 Save Changes',
             'Writes all of it at once. Nothing is saved until you press it.'),
            ('← Back to Dashboard',
             'Leaves without saving if you have not pressed Save.'),
        ),
        'notes': (
            'Your Agency ID is not on this page because it cannot be '
            'changed - it is what you sign in with.',
        ),
        'related': ('admin',),
    },

    # ── the agent's screens ──────────────────────────────────────────

    'agent_dashboard': {
        'role': 'agent',
        'title': 'My dashboard',
        'where': 'The screen you land on after signing in at /agent-login.',
        'purpose': 'Your leads and your viewings. Nobody else\'s.',
        'steps': (
            'Check the four tiles: My Leads, Hot Leads, My Appointments, '
            'Closed Deals.',
            'Open Latest updates if it has a badge.',
            'Filter to Hot or to New to decide who to ring first.',
            'Open a lead, call them, then set the status and leave a note.',
            'Book the next viewing from the lead itself.',
        ),
        'controls': (
            ('My Leads / Hot Leads (5⭐) / My Appointments / Closed Deals',
             'Counts of your own work only - they do not include anything '
             'assigned to another agent.'),
            ('🔔 Latest updates',
             'What has changed on your leads since you last opened it. '
             'Opening it marks you caught up; it does not affect your '
             'owner\'s unread count.'),
            ('Quality, Status and Type filter rows',
             'Narrow the list in front of you. They combine, and they change '
             'nothing about the leads themselves.'),
            ('Search name, email, budget',
             'Filters your leads as you type, alongside whichever filters are '
             'on. The quickest way to find somebody who has just rung you.'),
            ('👁 View, or clicking a row',
             'Opens the lead: contact details, budget, timeline, the star '
             'rating and why, the transcript, and the notes.'),
            ('Status buttons inside a lead',
             'New, Contacted, Meeting, Closed, Lost. Saved as you press '
             'them. Your owner sees the change.'),
            ('Notes',
             'What you learned on the phone. Your owner can read them, which '
             'is the point - it is how a lead survives your day off.'),
            ('📅 Book Another Viewing',
             'Books the next viewing for a customer you have already seen, '
             'without retyping their details.'),
            ('Status dropdown on a viewing',
             'The one control that carries a viewing through its life: '
             'pending, confirmed, viewing done, then how it went - wants to '
             'buy, other options, not interested - or cancelled. It saves the '
             'moment you pick, and your owner sees it.'),
            ('📝 Add Note (on an appointment)',
             'Records what happened at that viewing.'),
            ('📋 Notes from your owner',
             'Appears on a viewing whose lead your owner has written notes '
             'on. Opens them. Worth a look before you go - it is where '
             'anything they learned on the phone will be.'),
            ('⚙️ Profile',
             'Your name, email and coverage area.'),
            ('🔑 Password',
             'Changes your own password.'),
            ('🚪 Logout',
             'Signs you out. You will need your email and password to get '
             'back in.'),
        ),
        'notes': (
            'You cannot see other agents\' leads, and they cannot see yours.',
            'There is no delete button here. Only the owner can delete a '
            'lead.',
            'A lead appears here because it was assigned to you '
            'automatically when it was captured.',
        ),
        'related': ('agent_profile',),
    },

    'agent_profile': {
        'role': 'agent',
        'title': 'My profile',
        'where': 'Reached from ⚙️ Profile on your dashboard.',
        'purpose': 'Your own details.',
        'steps': (
            'Change what needs changing and save.',
        ),
        'controls': (
            ('Your Name',
             'How you appear to your owner and on your viewings.'),
            ('Email Address',
             'What you sign in with, and where your notifications go. '
             'Changing it changes your login.'),
            ('Coverage Area',
             'The area or property type you cover. It is read when a new '
             'lead is being assigned, so keeping it right means getting '
             'leads that suit you.'),
            ('← Back to Dashboard',
             'Returns to your leads dashboard.'),
        ),
        'notes': (
            'Your password is changed from the dashboard, not here.',
        ),
        'related': ('agent_dashboard',),
    },
}


# The order the printable role guides walk through the screens. A reader
# who follows one of these from the top has met every screen they have.
ROLE_GUIDES = {
    'solo-owner': {
        'title': 'Solo Agent guide',
        'who': 'You are the whole agency: you own the account and you work '
               'every lead yourself.',
        'screens': ('owner_login', 'admin', 'listings', 'appointments',
                    'analytics', 'agency_profile'),
        'first_week': (
            'Paste the one line of code into your website. Your dashboard '
            'shows it, already carrying your agency number. Nothing happens '
            'until this is done.',
            'Open your own website and talk to the assistant as a visitor '
            'would. Give it an email and a budget and see the lead arrive.',
            'Add your properties on Listings, so it can answer questions '
            'about real stock rather than generalities.',
            'Check Profile: the email address there is where every lead '
            'alert goes.',
            'Leave it a week, then open Analytics.',
        ),
    },
    'agency-owner': {
        'title': 'Agency owner guide',
        'who': 'You own the account and you have a team. You see everything; '
               'each agent sees only their own.',
        'screens': ('owner_login', 'admin', 'agents_page', 'agent_detail',
                    'listings', 'appointments', 'analytics', 'agency_profile'),
        'first_week': (
            'Paste the one line of code into your website. Your dashboard '
            'shows it. Nothing arrives until this is done.',
            'Add your agents on the Agents page, with a coverage area each - '
            'that is what new leads are matched against.',
            'Tell your team to sign in at /agent-login with the password '
            'they were emailed, and to change it.',
            'Add your properties on Listings.',
            'Test it yourself from your own website, and watch the lead '
            'land with one of your agents.',
            'After a week, open Analytics and compare it with what your '
            'website used to produce.',
        ),
    },
    'agent': {
        'title': 'Agent guide',
        'who': 'You work leads your agency assigns you. You cannot see '
               'anybody else\'s, and they cannot see yours.',
        'screens': ('agent_login', 'agent_dashboard', 'agent_profile'),
        'first_week': (
            'Sign in at /agent-login with your work email and the password '
            'in your welcome email.',
            'Change that password from the 🔑 Password button.',
            'Set your coverage area on Profile - it decides which leads come '
            'to you.',
            'Work the Hot filter first. A five-star lead gave a budget and a '
            'timeline, which means they are ready to talk.',
            'Set a status and leave a note every time you speak to somebody. '
            'That note is what your owner reads instead of asking you.',
        ),
    },
}


# The questions people ask before they have an account, and the ones they
# ask in the first week. Published on the public guide page and as FAQ
# structured data. Keep every answer true of the product as it stands.
FAQS = (
    ('How do I put the assistant on my website?',
     'Paste one line of code before the closing body tag. Your dashboard '
     'shows the exact line, already carrying your agency number. It works on '
     'WordPress, Wix, Squarespace or a bespoke site - anything that lets you '
     'add a script tag.'),
    ('How long does setup take?',
     'Pasting the line takes a minute. Adding your properties and your '
     'agents takes as long as you have stock and staff. The assistant '
     'captures leads from the moment the line is live.'),
    ('What happens when somebody chats on my site?',
     'The assistant answers their questions, and while it does, it works out '
     'their budget, the area they want, the kind of property and how soon '
     'they are moving. When it has enough, it saves the lead and emails you '
     'straight away.'),
    ('What makes a lead "qualified"?',
     'A name, a contact address and a budget, gathered over a real '
     'conversation rather than a single message. Half a chat does not land '
     'in your inbox as a lead.'),
    ('How are leads scored?',
     'One to five stars, from how complete the information is and how urgent '
     'the person sounds. Every lead also carries a "Why this rating" panel '
     'saying what the score was based on.'),
    ('Can visitors book a viewing themselves?',
     'Yes. The assistant takes the booking inside the chat and it appears on '
     'your Appointments calendar.'),
    ('Does each of my agents get their own login?',
     'On the Agency plan, yes. Each agent sees only the leads assigned to '
     'them and the viewings they are taking. You see everybody.'),
    ('How are leads shared out between agents?',
     'Automatically, as each lead is captured, using the coverage area you '
     'set for each agent. You can reassign afterwards.'),
    ('What happens to an agent\'s leads if they leave?',
     'Disable the agent rather than deleting them. They can no longer sign '
     'in, and every lead and viewing stays exactly where it is.'),
    ('Can the assistant talk about my actual properties?',
     'Yes, once you have added them on the Listings page. It answers on '
     'price, area and property type from your own list.'),
    ('A visitor wants to sell me a property. What happens?',
     'They can submit it through the chat. It sits as Pending and is '
     'invisible to buyers until you approve it on the Listings page.'),
    ('What languages does it speak?',
     'Nine: English, French, Spanish, Portuguese, Brazilian Portuguese, '
     'German, Italian, Dutch and Arabic. The visitor does not have to '
     'choose - it reads their browser.'),
    ('Does it tell people they are talking to an AI?',
     'Yes, in the chat header and as the first line of every conversation, '
     'in the visitor\'s own language. That is EU AI Act Article 50, in force '
     'since 2 August 2026.'),
    ('Can I get my leads into my CRM?',
     'Yes. Put your webhook address on the Analytics page and every '
     'qualifying lead is posted to it as JSON as it is captured. HubSpot, '
     'Follow Up Boss, Zapier and Make.com all accept one.'),
    ('Can I export my leads?',
     'Yes, to a spreadsheet, from the Export Excel button on your dashboard. '
     'It includes leads a filter is currently hiding.'),
    ('I deleted a lead by mistake. Can I get it back?',
     'No. Deleting a lead removes it, its notes and its transcript '
     'permanently. Export before any bulk delete.'),
    ('Is there a free trial?',
     'Yes - 14 days, with no card required. You are not charged anything '
     'during it.'),
    ('Who can see my leads?',
     'Only your agency. They are not shared with anybody else, and you can '
     'export them or ask for them to be deleted at any time.'),
    ('I have lost my Agency ID.',
     'It is in your welcome email, and at the top of every lead alert we '
     'send you. Agents sign in with an email address instead and do not need '
     'it.'),
    ('How do I get help?',
     'Every screen in the product has a ? button in its bottom corner that '
     'explains that screen - what it is for, and what every button on it '
     'does. For anything else, email support and a person will answer.'),
)
