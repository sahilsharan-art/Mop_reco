"""The login gate: nothing on any page is drawn or read until a person has signed in (2026-11-23).

`dashboard_common.page()` calls `require_login()` first, before the navigation bar, the filters or any query, and every
page calls `page()` before touching data (a test checks that). The rules live in mop_reco/auth.py; this module is only the
screens: the sign-in form, "choose your own password", and the account menu with Sign out.

A signed-in browser holds one cookie, `mop_session`, whose value is a random token; the database holds only its hash.
Streamlit cannot set a cookie from the server, so a one-line script does it in the browser; the cookie is read back on the
next page load (st.context.cookies), which is what keeps a person signed in through a browser refresh. It is SameSite=Strict
and lasts at most `absolute_hours`. (It cannot be HttpOnly, because a script sets it - the token is revocable and expires.)

The signed-in person's display name becomes the operator recorded against every remark, match, upload and run, so the
"who is doing this?" prompts no longer appear.
"""

from __future__ import annotations

import html
import json
import logging
import secrets

import streamlit as st

from mop_reco import auth
from mop_reco.config import load_config

log = logging.getLogger(__name__)

COOKIE = "mop_session"
K_OPERATOR = "operator"          # must equal dashboard_common.K_OPERATOR (a test asserts it)
K_TOKEN = "_auth_token"
K_USER = "_auth_user"


def _cfg() -> dict:
    return auth.settings(load_config().raw)


def _ip() -> str:
    try:
        return st.context.ip_address or ""
    except Exception:                                           # not available outside a browser session
        return ""


def current_user() -> dict | None:
    """The signed-in user ({'id','username','display_name','role'}), or None (also None when the login is switched off)."""
    return st.session_state.get(K_USER)


def is_admin() -> bool:
    u = current_user()
    return bool(u and u["role"] == "admin")


def _remember(user, token: str):
    st.session_state[K_TOKEN] = token
    st.session_state[K_USER] = {"id": user.id, "username": user.username, "display_name": user.display_name, "role": user.role,
                                "must_change": bool(user.must_change_password)}
    st.session_state[K_OPERATOR] = user.display_name            # what every record is stamped with


def _forget():
    for k in (K_TOKEN, K_USER, K_OPERATOR):
        st.session_state.pop(k, None)


def _run_script(js: str):
    st.html(f"<script>{js}</script>", unsafe_allow_javascript=True)


def _set_cookie_script(token: str, max_age: int):
    _run_script(f"document.cookie = 'mop_session=' + {json.dumps(token)} + '; Max-Age={int(max_age)}; Path=/; SameSite=Strict';")


def _clear_cookie_script():
    _run_script("document.cookie = 'mop_session=; Max-Age=0; Path=/; SameSite=Strict';")


def _cookie_token() -> str:
    try:
        return st.context.cookies.get(COOKIE, "") or ""
    except Exception:
        return ""


# --- the screens -------------------------------------------------------------------------------------------------

def _login_screen(session, cfg: dict, title_html: str):
    if _cookie_token():
        _clear_cookie_script()                                  # an old or ended token must not linger in the browser
    with st.container(key="login-card"):
        st.markdown(title_html, unsafe_allow_html=True)
        st.markdown("<div class='mop-login-sub'>Sign in to continue</div>", unsafe_allow_html=True)
        with st.form("login_form", clear_on_submit=False):
            username = st.text_input("Username", key="login_user", autocomplete="username")
            password = st.text_input("Password", type="password", key="login_pw", autocomplete="current-password")
            go = st.form_submit_button("Sign in", type="primary", width="stretch")
        if go:
            if not username.strip() or not password:
                st.error("Enter your username and password.")
            else:
                result = auth.authenticate(session, username, password, cfg, ip=_ip())
                if result.user is None:
                    st.error(result.message)
                else:
                    token = auth.create_session(session, result.user, cfg, ip=_ip())
                    _remember(result.user, token)
                    st.rerun()
        st.caption("Access is recorded. Forgotten your password? Ask an administrator to reset it.")
    st.stop()


def _change_password_screen(session, cfg: dict, user, title_html: str, forced: bool):
    with st.container(key="login-card"):
        st.markdown(title_html, unsafe_allow_html=True)
        st.markdown("<div class='mop-login-sub'>"
                    + ("Choose your own password before you continue" if forced else "Change your password") + "</div>",
                    unsafe_allow_html=True)
        st.caption(f"At least {cfg['min_password_length']} characters, with a letter and a digit, not containing your username.")
        with st.form("change_pw_form"):
            old = st.text_input("Current (temporary) password" if forced else "Current password", type="password",
                                key="cp_old", autocomplete="current-password")
            new = st.text_input("New password", type="password", key="cp_new", autocomplete="new-password")
            again = st.text_input("New password again", type="password", key="cp_again", autocomplete="new-password")
            go = st.form_submit_button("Save password", type="primary", width="stretch")
        if go:
            if new != again:
                st.error("The two new passwords are not the same.")
            else:
                try:
                    db_user = session.get(auth.AppUser, user["id"])
                    auth.change_password(session, db_user, old, new, cfg,
                                         keep_token_hash=auth.token_hash_of(st.session_state[K_TOKEN]))
                    st.session_state[K_USER]["must_change"] = False
                    st.success("Password changed.")
                    st.rerun()
                except auth.AuthError as exc:
                    st.error(str(exc))
        if st.button("Sign out", key="cp_signout"):
            sign_out(session)
    st.stop()


def require_login(session_factory, title_html: str):
    """Call first on every page. Returns the signed-in user dict, or None when the login is switched off (development);
    otherwise it draws the sign-in screen and stops the page until someone has signed in."""
    if auth.auth_disabled():
        return None
    cfg = _cfg()
    session = session_factory()
    try:
        token = st.session_state.get(K_TOKEN) or _cookie_token()
        user = auth.resolve_session(session, token, cfg) if token else None
        if user is None:
            _forget()
            _login_screen(session, cfg, title_html)             # stops
        _remember(user, token)
        if _cookie_token() != token:
            _set_cookie_script(token, auth.cookie_max_age_seconds(cfg))
        if user.must_change_password:
            _change_password_screen(session, cfg, st.session_state[K_USER], title_html, forced=True)   # stops
        return st.session_state[K_USER]
    finally:
        session.close()


def sign_out(session=None):
    token = st.session_state.get(K_TOKEN, "")
    user = st.session_state.get(K_USER) or {}
    own = session is None
    if own:
        from mop_reco.db import get_session
        import dashboard_common as dc
        session = get_session(dc._engine())
    try:
        auth.end_session(session, token, user.get("username", ""), _ip())
    finally:
        if own:
            session.close()
    _forget()
    st.rerun()


def account_menu():
    """The signed-in person's name in the top bar, with Change password and Sign out. Nothing when the login is off."""
    u = current_user()
    if not u:
        return
    with st.popover(u["display_name"], width="stretch"):
        st.caption(f"Signed in as **{html.escape(u['username'])}** · {u['role']}")
        if st.button("Sign out", key="signout", width="stretch"):
            sign_out()
        with st.expander("Change my password"):
            with st.form("menu_change_pw", clear_on_submit=True):
                old = st.text_input("Current password", type="password", key="mp_old", autocomplete="current-password")
                new = st.text_input("New password", type="password", key="mp_new", autocomplete="new-password")
                go = st.form_submit_button("Change password", width="stretch")
            if go:
                from mop_reco.db import get_session
                import dashboard_common as dc
                session = get_session(dc._engine())
                try:
                    cfg = _cfg()
                    auth.change_password(session, session.get(auth.AppUser, u["id"]), old, new, cfg,
                                         keep_token_hash=auth.token_hash_of(st.session_state[K_TOKEN]))
                    st.success("Password changed.")
                except auth.AuthError as exc:
                    st.error(str(exc))
                finally:
                    session.close()


def generate_temp_password() -> str:
    """A readable one-time password for an administrator to hand over (12 characters, always a letter and a digit)."""
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(9)) + "".join(secrets.choice("23456789") for _ in range(3))
        pw = "".join(secrets.SystemRandom().sample(pw, len(pw)))
        if not auth.password_problems(pw):
            return pw
