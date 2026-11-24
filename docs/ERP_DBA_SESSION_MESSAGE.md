Hello. I am the Claude session building the MOP Reconciliation application (it runs on server 10.0.1.98). I need your help, as the session with
DBA access, to set up a READ-ONLY data link from the ERP / data-lake PostgreSQL. Please read the attached `ERP_INTEGRATION_BRIEF.md` first: it has the
full field list, the rules and the numbers to check against. This message is the short version plus what I have already found.

**Ground rules (please keep to them)**
- Investigate read-only. **Do not change anything** on the ERP / lake (no grants, views, pg_hba edits, role changes) until the human operator says OK for that specific change.
- **Never print, paste or log a password** - not in this chat, not in a file. If you need to show a connection string, mask the password.
- In anything you send back, show only a few MASKED sample rows (the terminal response column can hold masked card numbers; refund data holds customer names).

**What exists today**
- The login `ckpost` on PostgreSQL 10.0.0.82:5432 is already used by the Citykart Asset Management app (CKAM) from the same server 10.0.1.98. The operator wants MOP to use the same login, read-only.
- The server has NO SSL (a plain `host` rule is needed).
- From 10.0.1.98, `ckpost` was **refused** on database `postgres` and on database `ckpost`:
  `FATAL: no pg_hba.conf entry for host "10.0.1.98", user "ckpost", database "...", no encryption`
  (three attempts between 14:17 and 14:22 IST on 23/11/2026 - those were us, expected). So `ckpost` is allowed from 10.0.1.98 on some other database, which CKAM uses.
- What `ckpost` can see (from CKAM's side): schemas `gold_ckpost` (po_line, pi_line, vendor, article, site - companies CKSPL and CKVPL), `silver` (fact_grc_line, dim_company,
  dim_item), `meta` (v_freshness), `gold_ckam` (old fallback). That looks like **purchasing data only**.

**What MOP needs** (details in the brief, section 2)
1. **POS bill lines**: one row per bill x payment line, ~1.2 to 1.5 million rows a month, from April 2026. Bill number, bill date, bill time, store, payment-mode name, amount, amount
   tendered, change, till operator, and the **terminal reply text stored unchanged (JSON)** - the key used to match a bill to a Paytm / Axis / Bajaj payment.
2. **Petty-cash (PTC) vouchers**: ~22,000 rows a month. Store, date, voucher number, mode (PAYMENT / RECEIVE), head (e.g. "Cash/CMS Deposit"), ledger, amount, remarks.
3. Optional: the store list (names and short codes).

**Please find out, in this order, and answer each with the query and its result**
1. **Which database does `ckpost` use for CKAM**, and the exact `pg_hba.conf` line(s) that apply to `ckpost` from 10.0.1.98 (share the line, never a secret).
2. **Are POS bill lines and PTC vouchers anywhere in the ERP / lake?** Give database, schema, table or view names, row counts per month for April to November 2026, the date column and its
   index, and the column that holds the terminal reply text. If they are in another database or system, say which and who owns it.
3. **How is a deleted or duplicate bill recorded?** (IT deletes duplicates 7 to 15 days after the sale.) Hard delete, a flag, a status, a change log? Is there a change timestamp?
4. **How current is the data** (daily, hourly)? Is there a freshness view for those tables (like `meta.v_freshness`)? Is history kept from April 2026?
5. **Reproduce the check numbers in brief section 6** from the source tables (for example: 20 July 2026 = 40,242 POS rows, 36,889 distinct bills, 17,536 digital lines; July PTC = 21,690 rows
   with 4,490 "Cash/CMS Deposit" vouchers totalling 489,898,591.00) and report ours-versus-yours.

**If the data is there**: propose (do not apply yet) the SQL for two stable read-only views (for example `mop_export.pos_bill_lines` and `mop_export.ptc_vouchers`), `GRANT SELECT` on them to
`ckpost`, and the `pg_hba` entry `host <database> ckpost 10.0.1.98/32 <method>`. Wait for the operator's OK before applying anything. Note that this widens what the shared `ckpost` login can read,
which is for the operator and the CKAM owner to accept.

**How MOP will behave**: read-only, by date range, only from 10.0.1.98, `application_name = mop_reco`, 15-minute statement timeout, 2 to 4 connections, off-peak where possible. The application
forces every session read-only itself, and a catalogue-only probe (`scripts/erp_probe.py`, no data rows) will be run from 10.0.1.98 afterwards to confirm what `ckpost` can read.

**Please reply with one block, `ERP_FINDINGS`**: the database name; the schema.table or view names found; a column mapping (ERP column -> field in the brief, with type); monthly row counts;
any change that was made (the exact `pg_hba` line and the SQL, only if the operator approved it); the check-number comparison; and any open questions. No passwords.
