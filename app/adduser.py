"""Create a console user with a username and password from the command line, or give an existing
username a new password. For the first sign-in, and for a forgotten password.

    python -m app.adduser <username> [admin|viewer]        (asks for the password, hidden)
"""
from __future__ import annotations

import getpass
import re
import secrets
import sys

from . import crypto
from .auth import hash_password
from .db import AdminPassword, AdminTotp, AdminUser, Audit, init_db, session_scope


def main(argv: list) -> int:
    if not argv or not re.fullmatch(r"[a-zA-Z0-9._-]{3,60}", argv[0]) or (len(argv) > 1 and argv[1] not in ("admin", "viewer")):
        print(__doc__)
        return 2
    username, role = argv[0].lower(), (argv[1] if len(argv) > 1 else "admin")
    password = getpass.getpass("Password (10 or more characters; nothing shows as you type): ")
    if len(password) < 10 or password != getpass.getpass("Again: "):
        print("The two did not match, or it is shorter than 10 characters. Nothing was changed.")
        return 1
    init_db()
    with session_scope() as db:
        row = db.get(AdminPassword, username)
        if row is not None:
            row.password_hash = hash_password(password)
            second = db.get(AdminTotp, row.user_id)
            if second is not None:  # the way back in for someone who lost their phone
                db.delete(second)
                print("Two-step sign-in was on for this name and is now off; turn it on again in the console.")
            user = db.get(AdminUser, row.user_id)
            user.active = True
            db.add(Audit(action="user.password_set", target=f"{user.id} {user.name} as {username} (by command line)"))
            print(f"New password set for {username} ({user.role}).")
        else:
            user = AdminUser(id="usr_" + secrets.token_hex(8), name=username, role=role,
                             token_hash=crypto.sha256_hex("oa_" + secrets.token_urlsafe(32)))  # a token nobody holds
            db.add(user)
            db.flush()
            db.add(AdminPassword(username=username, user_id=user.id, password_hash=hash_password(password)))
            db.add(Audit(action="user.created", target=f"{user.id} {username} ({role}) as {username} (by command line)"))
            print(f"Created {username} ({role}). Sign in at /admin with that username and password.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
