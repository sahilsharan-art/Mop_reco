"""Users - who may sign in. Administrators only (2026-11-23).

Add a person, give them a temporary password (they must choose their own at first sign-in), unlock, deactivate, change
role, reset a forgotten password, and read the sign-in trail. The rules are in mop_reco/auth.py. Passwords are never shown
after they are set; a temporary one is shown once, at the moment it is created or reset.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import auth_gate
from dashboard_common import db_session, page
from mop_reco import auth
from mop_reco.config import load_config

page("Users", "Who may sign in. Administrators only.")

if not auth_gate.is_admin():
    if auth.auth_disabled():
        st.info("The login is switched off in this development session (MOP_AUTH=off), so there are no users to manage here.")
    else:
        st.error("Only an administrator may manage users.")
    st.stop()

cfg = auth.settings(load_config().raw)
session = db_session()
me = auth_gate.current_user()


def _fmt(ts) -> str:
    return ts.strftime("%d/%m/%Y %H:%M") if ts else ""


def _status(u) -> str:
    if not u.active:
        return "Deactivated"
    if u.locked_until and u.locked_until > pd.Timestamp.now().to_pydatetime():
        return "Locked"
    return "Active"


users = auth.list_users(session)
st.markdown(f"##### People ({len(users)})")
st.dataframe(pd.DataFrame([{
    "Username": u.username, "Name": u.display_name, "Role": u.role, "Status": _status(u),
    "Must choose a new password": "yes" if u.must_change_password else "",
    "Last sign-in": _fmt(u.last_login_at), "Added": _fmt(u.created_at), "Added by": u.created_by or "",
} for u in users]), hide_index=True, width="stretch")

# --- add a person ------------------------------------------------------------------------------------------------
st.markdown("##### Add a person")
if "new_temp_pw" not in st.session_state:
    st.session_state["new_temp_pw"] = auth_gate.generate_temp_password()
with st.form("add_user", clear_on_submit=False):
    c1, c2, c3 = st.columns(3)
    new_user = c1.text_input("Username", help="Lower-case letters, digits, dot, dash or underscore. For example: aditi.agarwal")
    new_name = c2.text_input("Full name", help="Recorded against everything this person does, so it must be unique.")
    new_role = c3.selectbox("Role", auth.ROLES, index=1, help="admin = can manage users; user = everything else in the application.")
    temp = st.text_input("Temporary password", value=st.session_state["new_temp_pw"],
                         help="Give this to the person. They must replace it at their first sign-in.")
    go = st.form_submit_button("Add person", type="primary")
if go:
    try:
        auth.create_user(session, new_user, new_name, new_role, temp, me["display_name"], must_change=True, cfg=cfg)
        st.success(f"Added {new_user.strip().lower()}. Temporary password: **{temp}** - shown now only; hand it over "
                   "and they will be asked to choose their own at first sign-in.")
        st.session_state["new_temp_pw"] = auth_gate.generate_temp_password()
    except auth.AuthError as exc:
        st.error(str(exc))

# --- manage one person -------------------------------------------------------------------------------------------
st.markdown("##### Manage a person")
if users:
    labels = {f"{u.username} - {u.display_name}": u.id for u in users}
    chosen = st.selectbox("Person", list(labels), key="users_pick")
    target = session.get(auth.AppUser, labels[chosen])
    st.caption(f"{_status(target)} · {target.role}"
               + (f" · locked until {_fmt(target.locked_until)}" if _status(target) == "Locked" else ""))
    a, b, c, d = st.columns(4)
    if a.button("Unlock", key="u_unlock", disabled=_status(target) != "Locked", width="stretch"):
        auth.unlock(session, target.id, me["display_name"])
        st.rerun()
    if target.active:
        if b.button("Deactivate", key="u_deact", width="stretch"):
            try:
                auth.set_active(session, target.id, False, me["display_name"])
                st.rerun()
            except auth.AuthError as exc:
                st.error(str(exc))
    elif b.button("Reactivate", key="u_react", width="stretch"):
        auth.set_active(session, target.id, True, me["display_name"])
        st.rerun()
    other_role = "user" if target.role == "admin" else "admin"
    if c.button(f"Make {other_role}", key="u_role", width="stretch"):
        try:
            auth.set_role(session, target.id, other_role, me["display_name"])
            st.rerun()
        except auth.AuthError as exc:
            st.error(str(exc))
    with st.form("reset_pw"):
        tmp = st.text_input("New temporary password", value=auth_gate.generate_temp_password(), key="reset_tmp",
                            help="For a forgotten password. Signs them out everywhere; they choose their own at next sign-in.")
        if st.form_submit_button("Reset password"):
            try:
                auth.admin_reset_password(session, target.id, tmp, me["display_name"], cfg)
                st.success(f"Password reset. Temporary password: **{tmp}** - shown now only.")
            except auth.AuthError as exc:
                st.error(str(exc))

# --- the trail ---------------------------------------------------------------------------------------------------
with st.expander("Sign-in and user activity (latest 200)"):
    events = auth.recent_events(session, 200)
    st.dataframe(pd.DataFrame([{"When": _fmt(e.at), "Username": e.username, "Event": e.event, "Detail": e.detail,
                                "From": e.ip} for e in events]), hide_index=True, width="stretch")
