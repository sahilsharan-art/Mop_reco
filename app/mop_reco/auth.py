"""Who may open the application: users, passwords, sessions and the audit trail (2026-11-23).

Pure engine - no Streamlit (CLAUDE.md 8). The page gate and the Users page (app/auth_gate.py, app/pages/9_Users.py)
are a thin view over this module.

Rules this module holds:
  * Passwords are never stored. Only a salted scrypt hash is (standard library, no new dependency).
  * A wrong username and a wrong password give the SAME answer, so the screen never tells a stranger which usernames exist.
  * After `max_failed_attempts` wrong passwords in a row the account is locked for `lockout_minutes`. An administrator can
    unlock it at once (Users page, or scripts/manage_users.py on the server).
  * A browser holds only a random session token. The database holds only its SHA-256, so a copy of the database cannot be
    used to sign in as anyone. A session ends after `idle_minutes` without use or `absolute_hours` in all, when the user signs
    out, is deactivated or changes / has their password reset.
  * The last active administrator can never be deactivated or demoted (nobody could then manage users).
  * Every sign-in, failure, lock-out, sign-out, password change and user change is appended to `auth_events`. Nothing is
    updated or deleted there (CLAUDE.md rule 4).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func

from .db import AppSession, AppUser, AuthEvent

ROLES = ("admin", "user")

DEFAULTS = {
    "min_password_length": 10,
    "max_failed_attempts": 5,
    "lockout_minutes": 15,
    "idle_minutes": 120,
    "absolute_hours": 12,
    "touch_seconds": 60,        # last_seen is written at most this often
}

# scrypt cost: 2**15 memory-hard. About 50 ms per check on a server - nothing for a person, expensive for a guesser.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 15, 8, 1
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,39}$")


def settings(config_raw: Optional[dict] = None) -> dict:
    """The auth settings: the defaults above, overridden by config.yaml's `auth:` block."""
    out = dict(DEFAULTS)
    out.update({k: v for k, v in ((config_raw or {}).get("auth") or {}).items() if k in DEFAULTS})
    return out


def auth_disabled() -> bool:
    """MOP_AUTH=off switches the login off for development and tests ONLY. It is refused in production
    (MOP_REQUIRE_POSTGRES=1), so a stray variable cannot open the live application."""
    if os.environ.get("MOP_AUTH", "").strip().lower() != "off":
        return False
    if os.environ.get("MOP_REQUIRE_POSTGRES") == "1":
        return False
    return True


class AuthError(ValueError):
    """A request that cannot be done, with a reason fit to show a person."""


# --- passwords ---------------------------------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, maxmem=2 ** 26, dklen=32)
    return "scrypt${}${}${}${}${}".format(_SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
                                          base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt, want = base64.b64decode(salt_b64), base64.b64decode(hash_b64)
        got = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), maxmem=2 ** 26, dklen=len(want))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


def password_problems(password: str, username: str = "", cfg: Optional[dict] = None) -> list:
    """Why a password is not acceptable (empty list = fine)."""
    cfg = cfg or DEFAULTS
    out = []
    if len(password) < int(cfg["min_password_length"]):
        out.append(f"at least {cfg['min_password_length']} characters")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        out.append("at least one letter and one digit")
    if username and username.lower() in password.lower():
        out.append("must not contain the username")
    return out


def _check_password(password: str, username: str, cfg: dict):
    problems = password_problems(password, username, cfg)
    if problems:
        raise AuthError("The password needs " + "; ".join(problems) + ".")


# --- audit -------------------------------------------------------------------------------------------------------

def log_event(session, event: str, username: str = "", detail: str = "", ip: str = "", commit: bool = True):
    session.add(AuthEvent(at=datetime.now(), username=(username or "")[:80], event=event, detail=(detail or "")[:500],
                          ip=(ip or "")[:64]))
    if commit:
        session.commit()


# --- users -------------------------------------------------------------------------------------------------------

def normalise_username(username: str) -> str:
    return (username or "").strip().lower()


def get_user(session, username: str) -> Optional[AppUser]:
    return session.query(AppUser).filter(AppUser.username == normalise_username(username)).one_or_none()


def _active_admins(session) -> int:
    return session.query(func.count(AppUser.id)).filter(AppUser.role == "admin", AppUser.active.is_(True)).scalar() or 0


def create_user(session, username: str, display_name: str, role: str, password: str, by: str,
                must_change: bool = True, cfg: Optional[dict] = None) -> AppUser:
    cfg = cfg or DEFAULTS
    username, display_name = normalise_username(username), (display_name or "").strip()
    if not _USERNAME_RE.match(username):
        raise AuthError("The username must be 3-40 characters: lower-case letters, digits, dot, dash or underscore.")
    if not display_name:
        raise AuthError("A name is required (it is recorded against everything the person does).")
    if role not in ROLES:
        raise AuthError(f"The role must be one of: {', '.join(ROLES)}.")
    if get_user(session, username):
        raise AuthError(f"The username '{username}' is already taken.")
    clash = session.query(AppUser).filter(func.lower(AppUser.display_name) == display_name.lower()).first()
    if clash:
        raise AuthError(f"The name '{display_name}' is already used by '{clash.username}'. Names are recorded in the audit trail, "
                        "so each must be unique - add a middle initial or the store/department.")
    _check_password(password, username, cfg)
    user = AppUser(username=username, display_name=display_name, role=role, password_hash=hash_password(password),
                   must_change_password=bool(must_change), active=True, failed_attempts=0, created_at=datetime.now(),
                   created_by=by or "")
    session.add(user)
    log_event(session, "user_created", username, f"role={role}; by {by}", commit=False)
    session.commit()
    return user


def list_users(session) -> list:
    return session.query(AppUser).order_by(AppUser.username).all()


def set_active(session, user_id: int, active: bool, by: str):
    user = session.get(AppUser, user_id)
    if user is None:
        raise AuthError("No such user.")
    if not active and user.role == "admin" and user.active and _active_admins(session) <= 1:
        raise AuthError("This is the last active administrator - deactivating them would leave nobody able to manage users.")
    user.active = bool(active)
    if not active:
        revoke_user_sessions(session, user.id, commit=False)
    log_event(session, "user_activated" if active else "user_deactivated", user.username, f"by {by}", commit=False)
    session.commit()


def set_role(session, user_id: int, role: str, by: str):
    user = session.get(AppUser, user_id)
    if user is None:
        raise AuthError("No such user.")
    if role not in ROLES:
        raise AuthError(f"The role must be one of: {', '.join(ROLES)}.")
    if user.role == "admin" and role != "admin" and user.active and _active_admins(session) <= 1:
        raise AuthError("This is the last active administrator - demoting them would leave nobody able to manage users.")
    user.role = role
    log_event(session, "role_changed", user.username, f"now {role}; by {by}", commit=False)
    session.commit()


def unlock(session, user_id: int, by: str):
    user = session.get(AppUser, user_id)
    if user is None:
        raise AuthError("No such user.")
    user.failed_attempts, user.locked_until = 0, None
    log_event(session, "user_unlocked", user.username, f"by {by}", commit=False)
    session.commit()


def admin_reset_password(session, user_id: int, temp_password: str, by: str, cfg: Optional[dict] = None):
    """An administrator sets a temporary password; the person must choose their own at the next sign-in."""
    cfg = cfg or DEFAULTS
    user = session.get(AppUser, user_id)
    if user is None:
        raise AuthError("No such user.")
    _check_password(temp_password, user.username, cfg)
    user.password_hash = hash_password(temp_password)
    user.must_change_password = True
    user.failed_attempts, user.locked_until = 0, None
    revoke_user_sessions(session, user.id, commit=False)
    log_event(session, "password_reset", user.username, f"by {by}", commit=False)
    session.commit()


def change_password(session, user: AppUser, old_password: str, new_password: str, cfg: Optional[dict] = None,
                    keep_token_hash: Optional[str] = None):
    """The person changes their own password. Every OTHER session of theirs ends."""
    cfg = cfg or DEFAULTS
    if not verify_password(old_password, user.password_hash):
        log_event(session, "password_change_failed", user.username, "wrong current password")
        raise AuthError("The current password is not correct.")
    if old_password == new_password:
        raise AuthError("The new password must be different from the current one.")
    _check_password(new_password, user.username, cfg)
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    revoke_user_sessions(session, user.id, except_token_hash=keep_token_hash, commit=False)
    log_event(session, "password_changed", user.username, "", commit=False)
    session.commit()


# --- signing in --------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SignIn:
    user: Optional[AppUser]
    message: str = ""           # fit to show a person; never says which part was wrong


GENERIC_FAILURE = "The username or password is not correct."


def authenticate(session, username: str, password: str, cfg: Optional[dict] = None, ip: str = "",
                 now: Optional[datetime] = None) -> SignIn:
    cfg = cfg or DEFAULTS
    now = now or datetime.now()
    user = get_user(session, username)
    if user is None:
        # Spend the same time as a real check so the response time does not reveal whether the username exists.
        verify_password(password, hash_password("x"))
        log_event(session, "login_failed", normalise_username(username), "unknown username", ip)
        return SignIn(None, GENERIC_FAILURE)
    if user.locked_until and user.locked_until > now:
        minutes = int((user.locked_until - now).total_seconds() // 60) + 1
        log_event(session, "login_blocked", user.username, "account locked", ip)
        return SignIn(None, f"This account is locked after too many wrong passwords. Try again in about {minutes} minute(s), "
                            "or ask an administrator to unlock it.")
    ok = verify_password(password, user.password_hash)
    if not ok:
        user.failed_attempts = (user.failed_attempts or 0) + 1
        detail = f"wrong password ({user.failed_attempts} in a row)"
        if user.failed_attempts >= int(cfg["max_failed_attempts"]):
            user.locked_until = now + timedelta(minutes=int(cfg["lockout_minutes"]))
            user.failed_attempts = 0
            detail += f"; locked for {cfg['lockout_minutes']} minutes"
        log_event(session, "login_failed", user.username, detail, ip)
        return SignIn(None, GENERIC_FAILURE)
    if not user.active:
        log_event(session, "login_blocked", user.username, "account deactivated", ip)
        return SignIn(None, "This account has been deactivated. Ask an administrator.")
    user.failed_attempts, user.locked_until, user.last_login_at = 0, None, now
    log_event(session, "login_ok", user.username, "", ip)
    return SignIn(user, "")


# --- sessions ----------------------------------------------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(session, user: AppUser, cfg: Optional[dict] = None, ip: str = "", now: Optional[datetime] = None) -> str:
    """Returns the raw token (given to the browser once, never stored)."""
    cfg = cfg or DEFAULTS
    now = now or datetime.now()
    token = secrets.token_urlsafe(32)
    session.add(AppSession(token_hash=_token_hash(token), user_id=user.id, created_at=now, last_seen_at=now,
                           expires_at=now + timedelta(hours=float(cfg["absolute_hours"])), revoked=False, ip=(ip or "")[:64]))
    session.commit()
    return token


def resolve_session(session, token: str, cfg: Optional[dict] = None, now: Optional[datetime] = None) -> Optional[AppUser]:
    """The signed-in user for a token, or None when it is unknown, ended, expired, idle too long or the user is deactivated."""
    cfg = cfg or DEFAULTS
    now = now or datetime.now()
    if not token:
        return None
    row = session.query(AppSession).filter(AppSession.token_hash == _token_hash(token)).one_or_none()
    if row is None or row.revoked or row.expires_at <= now:
        return None
    if now - row.last_seen_at > timedelta(minutes=float(cfg["idle_minutes"])):
        row.revoked = True
        session.commit()
        return None
    user = session.get(AppUser, row.user_id)
    if user is None or not user.active:
        return None
    if (now - row.last_seen_at).total_seconds() >= float(cfg["touch_seconds"]):
        row.last_seen_at = now
        session.commit()
    return user


def end_session(session, token: str, by_username: str = "", ip: str = ""):
    row = session.query(AppSession).filter(AppSession.token_hash == _token_hash(token or "")).one_or_none()
    if row is not None and not row.revoked:
        row.revoked = True
        log_event(session, "logout", by_username, "", ip, commit=False)
        session.commit()


def revoke_user_sessions(session, user_id: int, except_token_hash: Optional[str] = None, commit: bool = True):
    q = session.query(AppSession).filter(AppSession.user_id == user_id, AppSession.revoked.is_(False))
    if except_token_hash:
        q = q.filter(AppSession.token_hash != except_token_hash)
    for row in q.all():
        row.revoked = True
    if commit:
        session.commit()


def token_hash_of(token: str) -> str:
    return _token_hash(token)


def cookie_max_age_seconds(cfg: Optional[dict] = None) -> int:
    return int(float((cfg or DEFAULTS)["absolute_hours"]) * 3600)


def recent_events(session, limit: int = 200) -> list:
    return session.query(AuthEvent).order_by(AuthEvent.id.desc()).limit(limit).all()
