"""Mismatch Desk web app.  Run:  streamlit run app.py"""
from __future__ import annotations

import json
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from mismatchdesk.agent import Copilot
from mismatchdesk.financing import DISCLAIMER, readiness_pack
from mismatchdesk.ingest import InputRejected
from mismatchdesk.llm import from_env
from mismatchdesk.money import fmt
from mismatchdesk.reconcile import CATEGORY_LABELS, TONE
from mismatchdesk.security.audit import verify_events
from mismatchdesk.security.policy import ACTIONS, PermissionDenied
from mismatchdesk.session import Session

ROOT = Path(__file__).parent
SAMPLE = ROOT / "sample_data" / "northwind"

st.set_page_config(page_title="Mismatch Desk", page_icon=None, layout="wide")

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
:root { --accent:#0A5C4F; --short:#A8281F; --over:#1D4F8F; --wait:#7D5200; }
.block-container { padding-top: 3.2rem; max-width: 1200px; }
h1, h2, h3 { letter-spacing: -0.01em; }
.md-big { font-size: 1.55rem; line-height: 1.3; font-weight: 600; margin: .2rem 0 .6rem; }
.md-num { font-family: ui-monospace, "SFMono-Regular", Menlo, monospace; }
.md-short { color: var(--short); } .md-over { color: var(--over); } .md-wait { color: var(--wait); }
.md-pill { display:inline-block; padding:2px 9px; border-radius:999px; font-size:.78rem; font-weight:600; }
.md-crit { background:#F5DFDC; color:#8A1F18; } .md-warn { background:#F3E6C6; color:#6B4700; } .md-info { background:#DCE7F6; color:#1D4F8F; }
.md-muted { color: #56655F; font-size: .9rem; }
</style>
""", unsafe_allow_html=True)


# ------------------------------------------------------------------ session setup
def start_session(files: dict | None):
    llm = from_env()
    if files is None:
        s = Session.from_dir(SAMPLE, llm=llm)
    else:
        profile = json.loads((SAMPLE / "profile.json").read_text())
        s = Session(books=files["books"], processors=files["processors"], bank=files["bank"], profile=profile,
                    history=SAMPLE / "history.csv", receivables=SAMPLE / "receivables.csv", llm=llm)
    s.run()
    st.session_state.s = s
    st.session_state.chat = []


with st.sidebar:
    md("### Mismatch Desk")
    cap("Governed finance copilot for small businesses")
    source = st.radio("Data", ["Sample company (Northwind Market)", "Upload my files"], key="source")
    uploaded = None
    if source == "Upload my files":
        b = st.file_uploader("Books / sales export (CSV)", type=["csv"], key="up_books")
        p = st.file_uploader("Payment provider reports (CSV)", type=["csv"], accept_multiple_files=True, key="up_psp")
        k = st.file_uploader("Bank statement (CAMT.053 XML or CSV)", type=["xml", "csv"], key="up_bank")
        cap("Cash plan, invoices and fee contracts use the sample company's settings in this demo.")
        if b and p and k:
            uploaded = {"books": (b.name, b.getvalue()), "processors": [(f.name, f.getvalue()) for f in p],
                        "bank": (k.name, k.getvalue())}
    if st.button("Run reconciliation", type="primary", use_container_width=True, key="run_btn"):
        try:
            start_session(uploaded if source == "Upload my files" else None)
        except InputRejected as exc:
            st.error(f"File refused: {exc}")
    st.divider()
    person = st.text_input("Your name (for the audit log)", value="Maya Chen", key="person")
    role = st.selectbox("Your role", ["owner", "reviewer", "viewer"], key="role",
                        help="Owners and reviewers can approve fixes. Viewers can only read.")
    llm_name = from_env().name
    cap(f"AI model: {'not configured, deterministic explanations' if llm_name == 'none' else llm_name}")

if "s" not in st.session_state:
    try:
        start_session(None)
    except InputRejected as exc:
        st.error(str(exc))
        st.stop()

s: Session = st.session_state.s
r, fc = s.recon, s.forecast
halted = s.policy.halted

# ------------------------------------------------------------------ header
top_l, top_r = st.columns([3, 1])
with top_l:
    html_md(f"<div class='md-muted'>{s.profile['business']['name']} · sales of {r.sales_date} · "
                f"bank statement {r.statement.date} · providers: {', '.join(r.providers)}</div>")
with top_r:
    if halted:
        st.error("Agent stopped (kill switch)")

tabs = st.tabs(["Overview", "Review differences", "Cash flow", "Financing", "Ask the copilot", "Audit and security"])

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
            x=alt.X("Amount:Q", title="Effect on cash received (USD)"),
            y=alt.Y("Cause:N", sort="x", title=None, axis=alt.Axis(labelLimit=300)),
            color=alt.Color("Type:N", scale=alt.Scale(domain=["Money short", "Extra money", "On its way"],
                                                      range=["#A8281F", "#3B7BD1", "#D9A441"]),
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
             "until a person approves it, and Mismatch Desk can never move money.")
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
                        st.rerun()
                    except PermissionDenied as exc:
                        st.error(str(exc))
                if cols[1].button("Reject", key=f"rj_{c.id}", disabled=role == "viewer"):
                    try:
                        s.reject(c.id, role=role, proposal_id=p.id, person=person)
                        st.rerun()
                    except PermissionDenied as exc:
                        st.error(str(exc))
                if cols[2].button("Send to a person", key=f"es_{c.id}", disabled=role == "viewer"):
                    s.escalate(c.id, role=role if role != "viewer" else "reviewer", person=person)
                    st.rerun()
            elif c.status in ("approved", "rejected"):
                cols[0].success(c.status.title())
                if cols[1].button("Undo", key=f"un_{c.id}", disabled=role == "viewer"):
                    s.undo(c.id, role=role, person=person)
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
             f"Weekly sales receipts assumed: {fmt(fc.weekly_sales)} (average of the last 8 weeks).")
    wdf = pd.DataFrame([{"Week of": w.start.isoformat(), "Cash at week end": w.closing / 100} for w in fc.weeks])
    line = alt.Chart(wdf).mark_line(point=True, color="#0A5C4F").encode(
        x=alt.X("Week of:T", title=None), y=alt.Y("Cash at week end:Q", title="USD"),
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
                "Explain NW-48242", "Approve all fixes", "Transfer $5,000 to my supplier"]
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
