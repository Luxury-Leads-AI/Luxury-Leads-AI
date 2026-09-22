"""Turn your super admin password into a hash you can paste into Render.

Run it on your own computer:

    python tools/make_admin_hash.py

It asks for the password twice (nothing is shown as you type), prints one
long line, and saves nothing. Copy that line into Render -> Environment as
SUPER_ADMIN_PASSWORD_HASH, then redeploy. After that the password itself is
not stored anywhere: the app only ever compares hashes.

The old SUPER_ADMIN_PASSWORD setting keeps working until the hash is set, so
you cannot lock yourself out. Once the hash works, delete it in Render.
"""
import getpass
import sys

try:
    from werkzeug.security import generate_password_hash
except ImportError:
    sys.exit("Run this inside the project's venv: pip install -r requirements.txt")

MIN_LENGTH = 12


def ask(label):
    """Ask for a password without showing it.

    getpass() reads from the console, and on Windows it reads ONLY from the
    console - so when this script is run by the test suite, which feeds it
    text through a pipe, getpass would wait for a keyboard that isn't there
    until the test gave up. When there is no console attached we read the
    line normally instead; nothing is echoed either way, because in that
    case the text is coming from a pipe rather than from someone typing.
    """
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass(label)
    print(label, end="", file=sys.stderr, flush=True)
    line = sys.stdin.readline()
    if not line:
        sys.exit("No password given.")
    return line.rstrip("\r\n")


def main():
    print(__doc__.strip().split("\n\n")[0])
    print()
    password = ask("New super admin password: ")
    if len(password) < MIN_LENGTH:
        sys.exit(f"Too short - use at least {MIN_LENGTH} characters.")
    again = ask("Type it again: ")
    if password != again:
        sys.exit("They did not match. Nothing was changed - run it again.")

    print()
    print("Paste this whole line into Render as SUPER_ADMIN_PASSWORD_HASH:")
    print()
    print(generate_password_hash(password))
    print()
    print("Then redeploy, log in with the password to check it works, and")
    print("delete the old SUPER_ADMIN_PASSWORD setting in Render.")


if __name__ == "__main__":
    main()
