"""Manage application users from the server's command line - the way in for the FIRST administrator, and the way back in if
every administrator is locked out or has forgotten their password.

    python scripts/manage_users.py list
    python scripts/manage_users.py create   --username aditi.agarwal --name "Aditi Agarwal" --role admin
    python scripts/manage_users.py reset    --username aditi.agarwal
    python scripts/manage_users.py unlock   --username aditi.agarwal
    python scripts/manage_users.py deactivate|activate --username X

Passwords are typed at the prompt (hidden, twice) - never given on the command line, so they do not land in shell history,
process listings or logs. A password set here is TEMPORARY: the person must choose their own at their first sign-in.
On the production server run it through deploy\\run_in_app_env.ps1 so it reaches the production database.
"""
import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco import auth  # noqa: E402
from mop_reco.config import load_config  # noqa: E402
from mop_reco.db import get_engine, get_session, init_db  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("action", choices=["list", "create", "reset", "unlock", "deactivate", "activate"])
ap.add_argument("--username")
ap.add_argument("--name", help="full name (create)")
ap.add_argument("--role", choices=auth.ROLES, default="user")
ap.add_argument("--by", default="server console", help="recorded in the audit trail")
args = ap.parse_args()

cfg = auth.settings(load_config().raw)
session = get_session(init_db(get_engine()))


def ask_password() -> str:
    first = getpass.getpass("Temporary password (hidden): ")
    if first != getpass.getpass("Again: "):
        sys.exit("The two entries are not the same - nothing was changed.")
    return first


def user_or_exit():
    if not args.username:
        sys.exit("--username is required for this action")
    user = auth.get_user(session, args.username)
    if user is None:
        sys.exit(f"No user '{args.username}'.")
    return user


try:
    if args.action == "list":
        users = auth.list_users(session)
        for u in users:
            state = "active" if u.active else "DEACTIVATED"
            lock = f" locked until {u.locked_until:%d/%m/%Y %H:%M}" if u.locked_until else ""
            print(f"{u.username:24s} {u.role:6s} {state}{lock}  '{u.display_name}'"
                  f"{'  (must choose a new password)' if u.must_change_password else ''}")
        print(f"{len(users)} user(s)")
    elif args.action == "create":
        if not args.username or not args.name:
            sys.exit("--username and --name are required")
        user = auth.create_user(session, args.username, args.name, args.role, ask_password(), args.by, must_change=True, cfg=cfg)
        print(f"Created {user.username} ({user.role}). They must choose their own password at first sign-in.")
    elif args.action == "reset":
        user = user_or_exit()
        auth.admin_reset_password(session, user.id, ask_password(), args.by, cfg)
        print(f"Password reset for {user.username}; they are signed out everywhere and must choose a new one.")
    elif args.action == "unlock":
        user = user_or_exit()
        auth.unlock(session, user.id, args.by)
        print(f"Unlocked {user.username}.")
    elif args.action in ("deactivate", "activate"):
        user = user_or_exit()
        auth.set_active(session, user.id, args.action == "activate", args.by)
        print(f"{args.action.capitalize()}d {user.username}.")
except auth.AuthError as exc:
    sys.exit(f"Not done: {exc}")
