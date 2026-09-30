"""The Growth-mode worker. Built now, left idle until it is needed.

Today the dashboard tab drives the queue. When clicking Run becomes a
chore, add a Render background worker whose start command is:

    python -m acquisition.jobs.worker

It calls the same run_next() the browser calls, in a loop, and nothing else
about the engine changes. That is the whole upgrade.
"""
import os
import sys
import time


def main(poll_seconds=5):
    # Importing app.py here (and nowhere else in the engine) is deliberate:
    # this file is a program, not part of the engine, so it is allowed to
    # know how the app is put together.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    import app as saas_app                      # noqa: WPS433
    from acquisition.jobs import runner

    print("acquisition worker: started")
    while True:
        with saas_app.app.app_context():
            outcome = runner.run_for(seconds=25)
        if not outcome.get('ran'):
            time.sleep(poll_seconds)


if __name__ == '__main__':
    main()
