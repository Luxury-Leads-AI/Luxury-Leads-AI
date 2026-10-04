"""Plugs: the outside services the engine can be pointed at.

Each kind of outside service has one small interface, so today's free or
manual way of doing a thing and tomorrow's paid API are swapped in Settings
rather than in code:

    discovery.py   where candidate agencies come from
                   (manual, OpenAI web search, OpenStreetMap; Places later)

Email sending, inbox reading, address checking and billing join them in
phases 5 to 8.
"""
