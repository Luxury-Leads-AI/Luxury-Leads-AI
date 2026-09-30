"""Services: the small pieces of thinking the engine does.

Each one is the only code allowed to do its job, so there is one place to
test and one place to fix:

    ai.py         every OpenAI call, every cost line, the budget ceiling
    prospects.py  adding a prospect, and what counts as the same company

Later phases add fetch.py (the only code that opens a stranger's website),
extract.py, scoring.py, drafting.py and retention.py.
"""
