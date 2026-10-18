"""Tallisor web app.  Run:  streamlit run app.py"""
from __future__ import annotations

import json
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from tallisor.agent import Copilot
from tallisor.financing import DISCLAIMER, readiness_pack
from tallisor import settings, store
from tallisor.ingest import InputRejected, UnknownFormat
from tallisor.mapping import FIELDS, LABELS, build_adapter, distinct, kind_suggestions, save_adapter, suggest
from tallisor.report import build_workbook
from tallisor.llm import from_env, remove_api_key, save_api_key
from tallisor.money import fmt
from tallisor.reconcile import CATEGORY_LABELS, TONE
from tallisor.security.audit import verify_events
from tallisor.security.policy import ACTIONS, PermissionDenied
from tallisor.session import Session

ROOT = Path(__file__).parent
SAMPLE = ROOT / "sample_data" / "northwind"

BRAND = ROOT / "assets" / "brand"
st.set_page_config(page_title="Tallisor", page_icon=str(BRAND / "tallisor-mark-64.png"), layout="wide")

def md(text: str, **kw):
    """Markdown with literal dollar signs (Streamlit treats $...$ as maths)."""
    st.markdown(str(text).replace("$", "\\$"), **kw)


def html_md(text: str):
    """HTML block with literal dollar signs."""
    st.markdown(str(text).replace("$", "&#36;"), unsafe_allow_html=True)


def cap(text: str):
    st.caption(str(text).replace("$", "\\$"))


def alert_html(a: dict) -> str:
    import html
    cls = {"critical": "md-crit", "warning": "md-warn", "info": "md-info"}[a["severity"]]
    title = html.escape(a["title"]).replace("$", "&#36;")
    detail = html.escape(a["detail"]).replace("$", "&#36;")
    return (f"<div style='margin:.35rem 0 .7rem'><span class='md-pill {cls}'>{a['severity'].title()}</span> "
            f"<b>{title}</b><br><span class='md-muted'>{detail}</span></div>")


st.markdown("""
<style>
@import url("https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap");
:root { --accent:#0074FB; --ink:#031230; --short:#A8281F; --over:#0B8F84; --wait:#7D5200; }
html, body, [class*="css"], .stMarkdown, .stButton button { font-family: "Plus Jakarta Sans", system-ui, sans-serif; }
h1, h2, h3, h4 { color: var(--ink); }
[data-testid="stSidebarUserContent"] { padding-top: .5rem; }
.block-container { padding-top: 3.2rem; max-width: 1200px; }
h1, h2, h3 { letter-spacing: -0.01em; }
.md-big { font-size: 1.55rem; line-height: 1.3; font-weight: 600; margin: .2rem 0 .6rem; }
.md-num { font-family: ui-monospace, "SFMono-Regular", Menlo, monospace; }
.md-short { color: var(--short); } .md-over { color: var(--over); } .md-wait { color: var(--wait); }
.md-pill { display:inline-block; padding:2px 9px; border-radius:999px; font-size:.78rem; font-weight:600; }
.md-crit { background:#F5DFDC; color:#8A1F18; } .md-warn { background:#F3E6C6; color:#6B4700; } .md-info { background:#DDEBFF; color:#0B4FB3; }
.md-muted { color: #56655F; font-size: .9rem; }
</style>
""", unsafe_allow_html=True)


# ------------------------------------------------------------------ session setup
SAMPLES = {
    "Sample company (Northwind Market)": SAMPLE,
    "Sample: Stripe, PayPal and MT940 files (Kestrel Goods)": ROOT / "sample_data" / "realformats",
}
UPLOAD = "Upload my files"
ROLE_KEY = {"processor": "processor", "books": "books", "bank": "bank"}
KINDS = ["charge", "refund", "dispute", "dispute_fee", "failed", "payout", "fee", "adjustment"]
NONE = "(not in this file)"

TEMPLATES = {
    "Sales export (books)": ("books_template.csv",
                             "invoice_no,date,customer,paid_via,currency,amount,status\n"
                             "INV-1001,2026-10-09,Example customer,Stripe,USD,120.00,paid\n"),
    "Open customer invoices": ("invoices_template.csv",
                               "invoice,customer,amount,issued,due,status\n"
                               "INV-2001,Example customer,1500.00,2026-10-01,2026-10-31,open\n"),
    "Weekly sales history": ("sales_history_template.csv",
                             "week_start,card_net,wallet_net,charges,disputes\n"
                             "2026-09-28,12000.00,800.00,240,1\n"),
}


def user_profile() -> dict:
    return settings.load() or settings.default_profile()


def persist():
    """Keep the saved copy of an uploaded run in step with decisions."""
    sess, run_id = st.session_state.get("s"), st.session_state.get("run_id")
    if sess is not None and run_id:
        store.save_run(sess, run_id)


def start_session(files: dict | None, sample_dir: Path | None = None):
    llm = from_env()
    existing = store.find_by_inputs(files) if files is not None else None
    if existing:
        # Same files as a saved run: continue it, with its decisions, instead of starting over.
        s, report = store.open_run(existing, profile=user_profile(), llm=llm)
        store.save_run(s, existing)
        skipped = len(report["skipped"])
        st.session_state.update(s=s, chat=[], files=files, run_id=existing,
                                notice=f"These files match saved run {existing}. Continuing it with your current "
                                       f"settings: {report['applied']} earlier decision(s) re-applied"
                                       + (f", {skipped} need review again because the fix changed." if skipped else "."))
        return
    if files is None:
        s = Session.from_dir(sample_dir or SAMPLE, llm=llm)
    else:
        s = Session(books=files["books"], processors=files["processors"], bank=files["bank"],
                    history=files.get("history"), receivables=files.get("receivables"),
                    profile=user_profile(), llm=llm)
    s.run()
    auto = s.apply_rules(person=st.session_state.get("person") or "Owner")
    st.session_state.s = s
    st.session_state.chat = []
    st.session_state.files = files
    st.session_state.run_id = store.save_run(s) if files is not None else None
    st.session_state.notice = (f"Your auto-approval rule approved {len(auto)} fix(es). They are marked in the "
                               f"audit log with the rule and who set it.") if auto else ""


def try_start(files, sample_dir=None):
    try:
        start_session(files, sample_dir)
        st.session_state.pop("pending_map", None)
    except UnknownFormat as exc:
        st.session_state.pending_map = {"name": exc.name, "role": exc.role, "header": exc.header,
                                        "sample": exc.sample, "files": files}
    except InputRejected as exc:
        st.session_state.upload_error = f"File refused: {exc}"


with st.sidebar:
    import base64
    _logo = base64.b64encode((BRAND / "tallisor-logo.png").read_bytes()).decode()
    st.markdown(f"<img src='data:image/png;base64,{_logo}' alt='Tallisor' "
                f"style='width:210px;height:auto;display:block;margin:.2rem 0 .4rem;image-rendering:auto'>",
                unsafe_allow_html=True)
    cap("Every payment, tallied to the cent.")
    source = st.radio("Data", [*SAMPLES, UPLOAD], key="source")
    uploaded = None
    if source == UPLOAD:
        b = st.file_uploader("Sales export from your books (CSV)", type=["csv"], key="up_books")
        p = st.file_uploader("Payment provider reports (CSV)", type=["csv"], accept_multiple_files=True,
                             key="up_psp", help="Stripe, PayPal or any other provider. Unknown layouts can be "
                                                "mapped once and are recognised after that.")
        k = st.file_uploader("Bank statement (CAMT.053, MT940 or CSV)", type=["xml", "csv", "sta", "mt940", "txt"],
                             key="up_bank")
        with st.expander("Optional: invoices and sales history"):
            inv = st.file_uploader("Open customer invoices (CSV)", type=["csv"], key="up_inv")
            hist = st.file_uploader("Weekly sales history (CSV)", type=["csv"], key="up_hist",
                                    help="Without history the forecast uses your weekly sales estimate in Settings.")
            for label, (fname, body) in TEMPLATES.items():
                st.download_button(f"Template: {label}", body, file_name=fname, mime="text/csv",
                                   key=f"tpl_{fname}", use_container_width=True)
        cap("Fee contracts, payments and safety floor come from the Settings tab.")
        if b and p and k:
            uploaded = {"books": (b.name, b.getvalue()), "processors": [(f.name, f.getvalue()) for f in p],
                        "bank": (k.name, k.getvalue()),
                        "receivables": (inv.name, inv.getvalue()) if inv else None,
                        "history": (hist.name, hist.getvalue()) if hist else None}
    ready = source != UPLOAD or uploaded is not None
    if st.button("Run reconciliation", type="primary", use_container_width=True, key="run_btn", disabled=not ready):
        try_start(uploaded if source == UPLOAD else None, SAMPLES.get(source))
    if st.session_state.get("upload_error"):
        st.error(st.session_state.pop("upload_error"))
    st.divider()
    _owner = (settings.load() or {}).get("business", {}).get("owner_name", "")
    person = st.text_input("Your name", value=_owner, key="person", placeholder="Shown on every approval",
                           help="Recorded in the audit log next to every decision you make.")
    role = st.selectbox("Your role", ["owner", "reviewer", "viewer"], key="role",
                        format_func=lambda r: {"owner": "Owner", "reviewer": "Reviewer (accountant)",
                                               "viewer": "Viewer (read only)"}[r],
                        help="Owners and reviewers can approve fixes. Viewers can only read. "
                             "Only owners can change settings and auto-approval rules.")
    _ai = from_env()
    if _ai.name == "none":
        cap("AI explanations: off. Turn them on in Settings.")
    else:
        cap(f"AI explanations: on ({ {'gemini': 'Google Gemini', 'anthropic': 'Anthropic Claude', 'openai': 'OpenAI'}[_ai.name] }).")

person = person.strip() or "Owner"


# ------------------------------------------------------------------ column mapping for unknown files
def mapping_screen(pm: dict):
    role_key = ROLE_KEY[pm["role"]]
    what = {"processor": "payment provider report", "books": "sales export", "bank": "bank statement"}[role_key]
    st.subheader(f"Teach Tallisor this {what}")
    md(f"**{pm['name']}** has a layout we have not seen before. Tell us which column holds what, once. "
       f"Next time a file with these columns arrives it is read automatically.")
    st.dataframe(pd.DataFrame(pm["sample"][:5]), hide_index=True, use_container_width=True)
    guess = suggest(pm["header"], role_key, pm["sample"])
    options = [NONE, *pm["header"]]
    req, opt = FIELDS[role_key]["required"], FIELDS[role_key]["optional"]
    mapping = {}
    cols = st.columns(3)
    for i, field in enumerate(req + opt):
        label = LABELS.get(field, field) + (" (required)" if field in req else "")
        current = guess["mapping"].get(field)
        mapping[field] = cols[i % 3].selectbox(label, options, index=options.index(current) if current else 0,
                                               key=f"mapcol_{pm['role']}_{field}")
    mapping = {k: v for k, v in mapping.items() if v != NONE}
    c1, c2, c3 = st.columns(3)
    name = c1.text_input("Name this format", value=f"{Path(pm['name']).stem} format", key="map_name")
    provider = ""
    if role_key in ("processor", "books"):
        provider = c2.text_input("Provider name" if role_key == "processor" else "Paid via (if there is no column)",
                                 value=Path(pm["name"]).stem.split("_")[0].title() if role_key == "processor" else "",
                                 key="map_provider",
                                 help="Must match the 'paid via' value in your sales export and your fee contract.")
    day_first = c3.radio("Dates are written", ["day first (25/10/2026)", "month first (10/25/2026)"],
                         index=0 if guess["options"]["day_first"] else 1, key="map_dates").startswith("day")
    fee_sign, kind_map, arrival = "positive", {}, None
    if role_key == "processor":
        fee_sign = st.radio("Fees appear as", ["positive numbers (7.50)", "negative numbers (-7.50)"],
                            index=0 if guess["options"]["fee_sign"] == "positive" else 1, horizontal=True,
                            key="map_fee").split()[0]
        if mapping.get("kind"):
            values = distinct(pm["sample"], mapping["kind"])
            kinds = kind_suggestions(values)
            md("**What each transaction type means**")
            edited = st.data_editor(pd.DataFrame({"Value in file": list(kinds), "Means": list(kinds.values())}),
                                    column_config={"Value in file": st.column_config.TextColumn(disabled=True),
                                                   "Means": st.column_config.SelectboxColumn(options=KINDS,
                                                                                             required=True)},
                                    hide_index=True, use_container_width=True, key="map_kinds")
            kind_map = dict(zip(edited["Value in file"], edited["Means"]))
        else:
            cap("No type column: positive amounts are read as sales and negative amounts as refunds.")
        if not mapping.get("arrival_date"):
            arrival = st.number_input("Days for a payout to reach the bank", 0, 14, 2, key="map_days")
    b1, b2 = st.columns([1, 4])
    if role == "viewer":
        st.info("A viewer cannot save a new file format. Switch to owner or reviewer.")
    if b1.button("Save and continue", type="primary", key="map_save", disabled=role == "viewer"):
        try:
            cfg = build_adapter(name=name, role=role_key, mapping=mapping, provider=provider,
                                fee_sign="negative" if fee_sign == "negative" else "positive",
                                day_first=day_first, kind_map=kind_map, payout_arrival_days=arrival)
            save_adapter(cfg)
        except ValueError as exc:
            st.error(str(exc))
            return
        try_start(pm["files"])
        st.rerun()
    if b2.button("Cancel", key="map_cancel"):
        st.session_state.pop("pending_map", None)
        st.rerun()


if st.session_state.get("pending_map"):
    mapping_screen(st.session_state.pending_map)
    st.stop()

if "s" not in st.session_state:
    try:
        start_session(None)
    except InputRejected as exc:
        st.error(str(exc))
        st.stop()

s: Session = st.session_state.s
r, fc = s.recon, s.forecast
halted = s.policy.halted
CCY = s.profile["business"].get("reporting_currency", "USD")

# ------------------------------------------------------------------ header
top_l, top_r = st.columns([3, 1])
with top_l:
    html_md(f"<div class='md-muted'>{(s.profile['business']['name'] if s.profile['business']['name'] not in ('', 'My business') else 'Your business')} · sales of {r.sales_date or 'the period'} · "
                f"bank statement {r.statement.date} · providers: {', '.join(r.providers)}"
                f"{' · saved as run ' + st.session_state.run_id if st.session_state.get('run_id') else ''}</div>")
with top_r:
    if halted:
        st.error("Agent stopped (kill switch)")
    st.download_button("Excel report", build_workbook(s), key="dl_xlsx", use_container_width=True,
                       file_name=f"tallisor_{r.statement.date}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
if st.session_state.get("notice"):
    st.info(st.session_state.notice)
missing = settings.missing_fee_contracts(s.profile, r.providers)
if missing:
    st.warning(f"No fee contract entered for {', '.join(missing)}, so their fees are accepted as charged and not "
               f"checked. Add your rates in Settings to catch overcharges.")

tabs = st.tabs(["Overview", "Review differences", "Cash flow", "Financing", "Ask the copilot", "Audit and security",
                "Settings", "History"])

# ------------------------------------------------------------------ overview
with tabs[0]:
    lowest = fc.lowest
    if fc.breach_weeks:
        html_md(f"<div class='md-big'>Your bank shows <span class='md-num'>{fmt(fc.bank_balance)}</span>, but "
                    f"cash will drop to <span class='md-num md-short'>{fmt(lowest.closing)}</span> in the week of "
                    f"{lowest.label}. You need about <span class='md-num'>{fmt(fc.shortfall)}</span> to stay above your "
                    f"safety floor.</div>")
    else:
        html_md(f"<div class='md-big'>Verified cash is <span class='md-num'>{fmt(fc.verified_cash)}</span> and stays "
                    f"above your safety floor for the next {len(fc.weeks)} weeks.</div>")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Bank balance", fmt(fc.bank_balance))
    c2.metric("Verified cash", fmt(fc.verified_cash), fmt(fc.verified_cash - fc.bank_balance, signed=True))
    c3.metric("Orders matched automatically", f"{len(r.matched)} of {r.order_count}")
    c4.metric("Differences to review", f"{sum(c.status == 'open' for c in r.cases)} open")

    st.subheader("Business financial analysis")
    md(s.gap_sentence())
    rows = [{"Cause": CATEGORY_LABELS[c.category], "Reference": c.ref, "Amount": -c.impact / 100,
             "Type": {"short": "Money short", "over": "Extra money", "timing": "On its way"}[TONE[c.category]]}
            for c in r.cases if c.impact]
    df = pd.DataFrame(rows)
    if not df.empty:
        chart = alt.Chart(df).mark_bar().encode(
            x=alt.X("Amount:Q", title=f"Effect on cash received ({CCY})"),
            y=alt.Y("Cause:N", sort="x", title=None, axis=alt.Axis(labelLimit=300)),
            color=alt.Color("Type:N", scale=alt.Scale(domain=["Money short", "Extra money", "On its way"],
                                                      range=["#A8281F", "#0CB7A6", "#D9A441"]),
                            legend=alt.Legend(orient="bottom", title=None)),
            tooltip=["Cause", "Reference", alt.Tooltip("Amount:Q", format=",.2f")],
        ).properties(height=40 * len(df) + 40)
        st.altair_chart(chart, use_container_width=True)
    cap(f"Explained {fmt(r.explained)} of {fmt(r.gap)}. Unexplained remainder {fmt(r.residual)}.")

    st.subheader("Cash-flow alerts")
    for a in s.alerts[:6]:
        st.markdown(alert_html(a), unsafe_allow_html=True)

# ------------------------------------------------------------------ review
with tabs[1]:
    md("Each difference was investigated. Check the evidence and approve the fix. Nothing reaches your books "
             "until a person approves it, and Tallisor can never move money.")
    if role == "viewer":
        st.info("You are signed in as a viewer and can read only.")
    for c in r.cases:
        tone = TONE[c.category]
        label = f"{CATEGORY_LABELS[c.category]} · {c.ref} · {fmt(c.variance, signed=True)} · {c.status}"
        with st.expander(label, expanded=False):
            md(f"**{c.explanation}**")
            src = "AI-generated, numbers verified by code" if c.explanation_source.startswith("ai:") else "Generated from the records by fixed rules"
            cap(f"{src}. Confidence: {c.confidence}.")
            for f in c.flags:
                if f["type"] == "injection_suspected":
                    st.warning("This file contained text that looked like instructions. It was treated as data and changed nothing.")
            ev = pd.DataFrame([{"Source": e.source, "Reference": e.ref, "What it shows": e.note,
                                "Amount": fmt(e.amount) if e.amount is not None else ""} for e in c.evidence])
            st.dataframe(ev, hide_index=True, use_container_width=True)
            md("**What was checked**")
            for x in c.checks:
                md(f"- {x}")
            p = c.proposal
            if p:
                md(f"**Proposed fix:** {p.summary}")
                if p.lines:
                    st.dataframe(pd.DataFrame([{"Account": l.account, "Debit": fmt(l.debit) if l.debit else "",
                                                "Credit": fmt(l.credit) if l.credit else ""} for l in p.lines]),
                                 hide_index=True, use_container_width=True)
                    cap(f"Debits equal credits: {fmt(p.total_debit)}. Fix ID {p.id}.")
                for t in p.tasks:
                    md(f"- Task for a person: {t}")
            else:
                md("No automatic fix is safe here. It has been sent to a person.")
            cols = st.columns([1.3, 1, 1, 3])
            if c.status == "open" and p:
                confirm = cols[3].checkbox("I have checked this entry", key=f"chk_{c.id}")
                if cols[0].button("Approve fix", key=f"ap_{c.id}", type="primary", disabled=not confirm or role == "viewer"):
                    try:
                        s.approve(c.id, role=role, proposal_id=p.id, person=person)
                        persist()
                        st.rerun()
                    except PermissionDenied as exc:
                        st.error(str(exc))
                if cols[1].button("Reject", key=f"rj_{c.id}", disabled=role == "viewer"):
                    try:
                        s.reject(c.id, role=role, proposal_id=p.id, person=person)
                        persist()
                        st.rerun()
                    except PermissionDenied as exc:
                        st.error(str(exc))
                if cols[2].button("Send to a person", key=f"es_{c.id}", disabled=role == "viewer"):
                    s.escalate(c.id, role=role if role != "viewer" else "reviewer", person=person)
                    persist()
                    st.rerun()
            elif c.status in ("approved", "rejected"):
                cols[0].success(c.status.title())
                if cols[1].button("Undo", key=f"un_{c.id}", disabled=role == "viewer"):
                    s.undo(c.id, role=role, person=person)
                    persist()
                    st.rerun()
    approved = sum(c.status == "approved" for c in r.cases)
    st.divider()
    if approved and role != "viewer":
        st.download_button(f"Download journal file ({approved} approved)", s.export_journal(role),
                           file_name=f"journal_{r.statement.date}.csv", mime="text/csv", key="dl_journal")
    else:
        cap("Approve at least one fix to download the journal file for your accounting system.")

# ------------------------------------------------------------------ cash flow
with tabs[2]:
    st.subheader("Cash-flow management with alerts")
    md(f"The forecast starts from **verified cash of {fmt(fc.verified_cash)}**: bank {fmt(fc.bank_balance)} "
             f"plus {fmt(fc.in_transit)} in transit, minus {fmt(fc.refunds_owed)} owed back to customers. "
             f"Weekly sales receipts assumed: {fmt(fc.weekly_sales)} ({fc.sales_basis}).")
    wdf = pd.DataFrame([{"Week of": w.start.isoformat(), "Cash at week end": w.closing / 100} for w in fc.weeks])
    line = alt.Chart(wdf).mark_line(point=True, color="#0074FB").encode(
        x=alt.X("Week of:T", title=None), y=alt.Y("Cash at week end:Q", title=CCY),
        tooltip=[alt.Tooltip("Week of:T"), alt.Tooltip("Cash at week end:Q", format=",.2f")])
    floor = alt.Chart(pd.DataFrame({"y": [fc.floor / 100]})).mark_rule(color="#A8281F", strokeDash=[6, 4]).encode(y="y:Q")
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="#8a8a8a").encode(y="y:Q")
    st.altair_chart((line + floor + zero).properties(height=320), use_container_width=True)
    cap(f"Dashed line: your safety floor of {fmt(fc.floor)}.")
    table = pd.DataFrame([{"Week of": w.label, "Opening": fmt(w.opening), "Sales in": fmt(w.sales_in),
                           "Invoices paid": fmt(w.receivables_in), "Payments out": fmt(w.outflows),
                           "Closing": fmt(w.closing), "Below floor": "yes" if w.closing < fc.floor else "",
                           "Payments": ", ".join(i["name"] for i in w.items)} for w in fc.weeks])
    st.dataframe(table, hide_index=True, use_container_width=True)
    st.subheader("All alerts")
    for a in s.alerts:
        st.markdown(alert_html(a), unsafe_allow_html=True)

# ------------------------------------------------------------------ financing
with tabs[3]:
    st.subheader("Financing-option matching")
    m = s.metrics
    md(f"Matched against verified records, not self-reported figures: revenue about **{fmt(m.monthly_revenue)} a month**, "
             f"week-to-week variation {m.revenue_volatility:.0%}, dispute rate {m.chargeback_rate:.2%}, "
             f"**{fmt(m.eligible_receivables)}** in open customer invoices. Forecast need: **{fmt(m.need)}** for "
             f"{m.need_weeks} week(s), starting in {m.weeks_until_need} week(s).")
    for x in s.matches:
        with st.container(border=True):
            head = st.columns([3, 1])
            head[0].markdown(f"#### {x.product['name']}")
            head[1].markdown(f"**Fit {x.score}/100**" if x.eligible else "**Not a fit**")
            md(f"{x.product['description']} Cost: {x.product['cost_text']}. Term: {x.product['term_text']}.")
            if x.eligible:
                for reason in x.reasons:
                    md(f"- {reason}")
            for w in x.why_not:
                html_md(f"<div style='margin-left:1.2rem' class='md-muted'>Why not: {w}</div>")
    cap(DISCLAIMER)
    top = next((x for x in s.matches if x.eligible), None)
    pack = readiness_pack(fc, m, top, s.profile["business"]["name"])
    st.download_button("Download financing readiness summary (draft)", pack, file_name="financing_readiness.md",
                       key="dl_pack")

# ------------------------------------------------------------------ chat
with tabs[4]:
    md("Ask in plain words. The copilot plans its steps, runs only permitted tools, and answers from verified records.")
    examples = ["Why is my bank short?", "Can I make payroll?", "What financing fits?", "What needs my attention?",
                *([f"Explain {r.cases[0].ref}"] if r.cases else []), "Approve all fixes",
                "Pay my supplier $5,000"]
    ex_cols = st.columns(4)
    clicked = None
    for i, q in enumerate(examples):
        if ex_cols[i % 4].button(q, key=f"ex_{i}", use_container_width=True):
            clicked = q
    prompt = st.chat_input("Ask about your cash, differences or financing", key="chat_in")
    question = prompt or clicked
    if question:
        reply = Copilot(s).ask(question)
        st.session_state.chat.append((question, reply))
    for q, rep in reversed(st.session_state.chat[-8:]):
        with st.chat_message("user"):
            md(q)
        with st.chat_message("assistant"):
            cap(f"Plan: {' → '.join(rep.plan) or 'answer only'}" + (" · refused by policy" if rep.refused else ""))
            md(rep.answer)

# ------------------------------------------------------------------ audit
with tabs[5]:
    st.subheader("Permission tiers")
    tiers = pd.DataFrame([
        {"Tier": "T0 Read", "Who": "Agent, people", "Examples": "read files, reconcile, forecast, match financing"},
        {"Tier": "T1 Propose", "Who": "Agent, people", "Examples": "investigate, draft fixes, draft readiness summary"},
        {"Tier": "T2 Approve", "Who": "People only", "Examples": "approve or reject a fix, bound to its exact content"},
        {"Tier": "T3 Not available", "Who": "Nobody", "Examples": ", ".join(a for a, t in ACTIONS.items() if int(t) == 3)},
    ])
    st.dataframe(tiers, hide_index=True, use_container_width=True)
    st.subheader("Safety checks this session")
    stt = s.stats
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Amounts checked against evidence", stt["numbers_checked"])
    k2.metric("AI amounts rejected", stt["numbers_rejected"])
    k3.metric("Suspicious inputs flagged", stt["injection_flags"])
    k4.metric("Unsafe fixes refused", stt["proposals_refused"])
    st.subheader("Audit log")
    ok, line, why = verify_events(s.log.events)
    (st.success if ok else st.error)(f"Hash chain: {why}")
    log_df = pd.DataFrame([{"#": e["seq"], "Time": e["ts"], "Actor": e["actor"], "Action": e["action"],
                            "Tier": e["tier"] or "", "Outcome": e["outcome"],
                            "Details": json.dumps(e["details"])[:160], "Hash": e["hash"][:12]}
                           for e in reversed(s.log.events)])
    cap("Newest first.")
    st.dataframe(log_df, hide_index=True, use_container_width=True, height=360)
    st.download_button("Download audit log (JSONL)", "\n".join(json.dumps(e, sort_keys=True) for e in s.log.events) + "\n",
                       file_name="audit_log.jsonl", key="dl_log")
    st.subheader("Kill switch")
    md("Stops the agent for this session. People can still read and decide.")
    if st.button("Stop the agent", disabled=halted, key="halt_btn"):
        s.halt(role if role != "viewer" else "owner", person=person)
        st.rerun()

# ------------------------------------------------------------------ settings
SAFE_AUTO = ["fee_variance", "fx_difference", "refund_unrecorded", "provider_fee"]
CURRENCIES = ["USD", "EUR", "GBP", "CAD", "AUD", "SGD", "INR", "AED", "HKD", "JPY", "CHF", "SEK", "NZD", "ZAR",
              "BRL", "MXN", "BDT", "PKR", "IDR", "MYR", "PHP", "NGN", "KES"]

with tabs[6]:
    prof = user_profile()
    owner = role == "owner"
    st.subheader("Business settings")
    cap("These settings are used when you upload your own files. The sample companies keep their own settings. "
        f"Saved on this computer in {settings.path()}.")
    if not owner:
        st.info("Only the owner can change settings.")
    biz, cash = prof["business"], prof["cash"]
    a1, a2, a3 = st.columns(3)
    biz["name"] = a1.text_input("Business name", "" if biz["name"] == "My business" else biz["name"],
                                disabled=not owner, key="set_name", placeholder="Your company name")
    biz["owner_name"] = a1.text_input("Your name", biz.get("owner_name", ""), disabled=not owner,
                                      key="set_owner", placeholder="Shown on approvals and reports")
    cur = biz.get("reporting_currency", "USD")
    biz["reporting_currency"] = a2.selectbox("Reporting currency", CURRENCIES,
                                             index=CURRENCIES.index(cur) if cur in CURRENCIES else 0,
                                             disabled=not owner, key="set_ccy")
    biz["months_trading"] = int(a3.number_input("Months trading", 0, 600, int(biz.get("months_trading", 12)),
                                                disabled=not owner, key="set_months"))

    md("**Cash plan**")
    b1, b2, b3 = st.columns(3)
    cash["floor"] = round(b1.number_input("Safety floor (lowest cash you are comfortable with)", 0.0, 1e9,
                                          cash.get("floor", 0) / 100, step=100.0, disabled=not owner,
                                          key="set_floor") * 100)
    cash["weekly_sales"] = round(b2.number_input("Typical weekly sales received (used without history)", 0.0, 1e9,
                                                 cash.get("weekly_sales", 0) / 100, step=100.0, disabled=not owner,
                                                 key="set_weekly") * 100)
    cash["weeks"] = int(b3.number_input("Forecast weeks", 4, 26, int(cash.get("weeks", 13)), disabled=not owner,
                                        key="set_weeks"))
    md("**Regular payments** (payroll, rent, suppliers, loan repayments, tax)")
    cap("Repeats: weekly, every 2 weeks, monthly or once. Next date as 2026-11-01; for monthly you can write day 1.")
    pay_df = pd.DataFrame(settings.payment_rows(prof) or [{"name": "", "amount": 0.0, "repeats": "monthly",
                                                           "next_date": ""}])
    pay_edit = st.data_editor(pay_df, num_rows="dynamic", hide_index=True, use_container_width=True,
                              disabled=not owner, key="set_pay",
                              column_config={"name": st.column_config.TextColumn("Payment"),
                                             "amount": st.column_config.NumberColumn("Amount", min_value=0.0,
                                                                                     format="%.2f"),
                                             "repeats": st.column_config.SelectboxColumn(
                                                 "Repeats", options=list(settings.PAYMENT_KINDS)),
                                             "next_date": st.column_config.TextColumn("Next date")})

    md("**Fee contracts** (what each provider agreed to charge you)")
    providers = sorted(set(prof["fee_schedules"]) | (set(r.providers) if st.session_state.get("files") else set()))
    new_provider = st.text_input("Add a provider", "", disabled=not owner, key="set_newprov",
                                 placeholder="Name exactly as in your sales export, for example Stripe")
    if new_provider.strip():
        providers = sorted(set(providers) | {new_provider.strip()})
    schedules = {}
    for prov in providers:
        sched = prof["fee_schedules"].get(prov, {})
        dom, intl = sched.get("domestic", {}), sched.get("international")
        with st.container(border=True):
            f = st.columns([1.4, 1, 1, 1, 1, 1])
            f[0].markdown(f"**{prov}**" + ("" if sched else "  \n:orange[no contract yet]"))
            d_pct = f[1].number_input("Domestic %", 0.0, 20.0, dom.get("bp", 0) / 100, step=0.05, format="%.2f",
                                      key=f"fee_dp_{prov}", disabled=not owner)
            d_fix = f[2].number_input("Domestic fixed", 0.0, 100.0, dom.get("fixed", 0) / 100, step=0.05,
                                      format="%.2f", key=f"fee_df_{prov}", disabled=not owner)
            i_pct = f[3].number_input("International %", 0.0, 20.0, (intl or {}).get("bp", 0) / 100, step=0.05,
                                      format="%.2f", key=f"fee_ip_{prov}", disabled=not owner,
                                      help="Leave 0 if the provider charges one rate everywhere.")
            i_fix = f[4].number_input("International fixed", 0.0, 100.0, (intl or {}).get("fixed", 0) / 100,
                                      step=0.05, format="%.2f", key=f"fee_if_{prov}", disabled=not owner)
            home = f[5].text_input("Home country", sched.get("home_country", ""), max_chars=2, key=f"fee_hc_{prov}",
                                   disabled=not owner, help="Two-letter code. Cards from elsewhere get the "
                                                            "international rate.")
            if d_pct or d_fix or sched:
                has_intl = bool(i_pct or i_fix)
                schedules[prov] = settings.fee_schedule(d_pct, d_fix, i_pct if has_intl else None,
                                                        i_fix if has_intl else None, home if has_intl else "")

    md("**Estimates used when no sales history is uploaded**")
    e1, e2, e3 = st.columns(3)
    biz["revenue_variation_estimate"] = e1.slider("Week-to-week sales variation", 0.0, 1.0,
                                                  float(biz.get("revenue_variation_estimate", 0.3)), 0.05,
                                                  disabled=not owner, key="set_cv")
    biz["dispute_rate_estimate"] = e2.slider("Share of payments disputed", 0.0, 0.05,
                                             float(biz.get("dispute_rate_estimate", 0.005)), 0.001, format="%.3f",
                                             disabled=not owner, key="set_disp")
    biz["card_share_estimate"] = e3.slider("Share of sales paid by card", 0.0, 1.0,
                                           float(biz.get("card_share_estimate", 1.0)), 0.05, disabled=not owner,
                                           key="set_card")

    md("**Auto-approval rule** (off unless you turn it on)")
    cap("Approves only high-confidence fixes in the chosen categories, at or under the amount, with no security "
        "flag. It acts in your name, is written to the audit log with who set it, and stops with the kill switch. "
        "It can never move money.")
    rule = prof.get("auto_approve") or {"enabled": False}
    g1, g2, g3 = st.columns([1, 2, 1])
    enabled = g1.toggle("Turn on", bool(rule.get("enabled")), disabled=not owner, key="set_auto")
    cats = g2.multiselect("Categories", SAFE_AUTO, [c for c in rule.get("categories", []) if c in SAFE_AUTO],
                          format_func=lambda c: CATEGORY_LABELS[c], disabled=not owner, key="set_auto_cats")
    limit = g3.number_input("Up to amount", 0.0, 1000.0, rule.get("max_amount", 2000) / 100, step=5.0,
                            disabled=not owner, key="set_auto_max")

    md("**AI explanations** (optional)")
    cap("An AI model writes the explanations in plain language and understands more ways of asking the copilot. "
        "It never calculates: every amount it writes is checked against your records, and it cannot approve "
        "fixes or move money. Without it, explanations come from fixed rules.")
    _cur = from_env()
    _labels = {"gemini": "Google Gemini (free tier available)", "anthropic": "Anthropic Claude",
               "openai": "OpenAI"}
    _links = {"gemini": "https://aistudio.google.com/apikey", "anthropic": "https://console.anthropic.com/settings/keys",
              "openai": "https://platform.openai.com/api-keys"}
    h1, h2 = st.columns([1, 2])
    ai_provider = h1.selectbox("Provider", list(_labels), format_func=_labels.get, disabled=not owner,
                               index=list(_labels).index(_cur.name) if _cur.name in _labels else 0, key="ai_prov")
    ai_key = h2.text_input("API key", type="password", disabled=not owner, key="ai_key",
                           placeholder="Key saved" if _cur.name != "none" else "Paste your key",
                           help="Stored only on this computer, readable by your user account only.")
    cap(f"Get a key at {_links[ai_provider]}.")
    if ai_provider == "gemini":
        cap("On Gemini's free tier Google may use what is sent to improve its products and people may review it. "
            "Fine for trying Tallisor; use a paid key for real customer files.")
    k1, k2, k3, _ = st.columns([1, 1, 1, 2])
    if k1.button("Save key", disabled=not owner, key="ai_save", use_container_width=True):
        try:
            if not ai_key.strip():
                raise ValueError("Paste your API key first.")
            save_api_key(ai_provider, ai_key)
            ok, why = from_env().check()
            st.session_state.ai_msg = (ok, why if ok else f"Key saved, but the test failed: {why}")
        except ValueError as exc:
            st.session_state.ai_msg = (False, str(exc))
        st.rerun()
    if k2.button("Test connection", disabled=_cur.name == "none", key="ai_test", use_container_width=True):
        st.session_state.ai_msg = from_env().check()
    if k3.button("Remove key", disabled=not owner or _cur.name == "none", key="ai_remove", use_container_width=True):
        remove_api_key()
        st.session_state.ai_msg = (True, "Key removed. Explanations now come from fixed rules.")
        st.rerun()
    if st.session_state.get("ai_msg"):
        ok, why = st.session_state.ai_msg
        (st.success if ok else st.error)(why)

    if st.button("Save settings", type="primary", disabled=not owner, key="set_save"):
        try:
            prof["cash"]["payments"] = settings.payments_from_rows(pay_edit.to_dict("records"))
            prof["fee_schedules"] = schedules
            prof["auto_approve"] = {"enabled": enabled and bool(cats), "categories": cats,
                                    "max_amount": round(limit * 100), "set_by": person}
            settings.save(prof)
            s.log.record(role, "change_settings", "ok", 2, person=person, auto_approve=prof["auto_approve"]["enabled"],
                         providers=sorted(schedules))
            st.success("Saved. Run reconciliation again to use the new settings.")
        except ValueError as exc:
            st.error(str(exc))
    if st.session_state.get("files") and st.button("Rerun my files with these settings", key="set_rerun"):
        try_start(st.session_state.files)
        st.rerun()

# ------------------------------------------------------------------ history
with tabs[7]:
    st.subheader("Saved runs")
    cap("Every run on your own files is saved with the exact files, settings, decisions and audit log. Opening a "
        "run recalculates it and re-applies your decisions only where the fix is unchanged.")
    runs = store.list_runs()
    if not runs:
        md("No saved runs yet. Upload your files and run a reconciliation.")
    else:
        hist_df = pd.DataFrame([{"Run": x["id"], "Business": x["business"], "Bank statement": x["statement_date"],
                                 "Providers": x["providers"], "Gap": fmt(x["gap"]), "Differences": x["cases"],
                                 "Open": x["open"], "Approved": x["approved"],
                                 "Shortfall": fmt(x["shortfall"]), "Saved": x["updated"][:16].replace("T", " ")}
                                for x in runs])
        st.dataframe(hist_df, hide_index=True, use_container_width=True)
        h1, h2 = st.columns([3, 1])
        pick = h1.selectbox("Open a run", [x["id"] for x in runs], key="hist_pick")
        if h2.button("Open", key="hist_open", use_container_width=True):
            try:
                again, report = store.open_run(pick, llm=from_env())
                st.session_state.update(s=again, run_id=pick, chat=[], files=store.stored_inputs(pick),
                                        notice=f"Opened run {pick}. Re-applied {report['applied']} decision(s)"
                                               + (f"; {len(report['skipped'])} need review again because the fix "
                                                  f"changed." if report["skipped"] else "."))
                st.rerun()
            except (KeyError, InputRejected) as exc:
                st.error(f"Could not open this run: {exc}")
