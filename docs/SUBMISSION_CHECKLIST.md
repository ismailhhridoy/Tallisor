# Submission checklist

Deadline: **20 October 2026, 23:59 Beijing time = 21:59 Dhaka time.** Registration and submission share the deadline. You can resend any time before it; the latest version counts. Aim to send a complete version by Monday evening and an improved one on Tuesday afternoon.

Send to: wangyr@pbcsf.tsinghua.edu.cn (address taken from the official guidelines PDF).

## Required by the rules

| # | Deliverable | File | Status |
|---|---|---|---|
| 1 | Technical documentation (architecture, core algorithm, security design) | `docs/TECHNICAL_DOCUMENTATION.md` (export to PDF) | Ready |
| 2 | Presentation deck, 10 minutes or less | Slides artifact "Mismatch Desk FinTechathon deck" (download as PDF and PPTX) | Ready; fill in [Team name], members, university, GitHub link |
| 3 | Demo video, 5 minutes or less (optional, but also execution evidence) | record with `docs/DEMO_VIDEO_SCRIPT.md` | To record |
| 4 | Source code with README, GitHub link, deployment instructions, test cases | this repository; `README.md`; `tests/` | Ready; push to a public GitHub repository |
| 5 | Security self-assessment (permission tiers, known-risk list) | `docs/SECURITY_SELF_ASSESSMENT.md` (export to PDF) | Ready |
| 6 | Execution evidence (sandbox operation logs and demo video) | `evidence/` folder plus the video | Ready except the video |

Also needed: the official registration form and both members' passport bio pages.

## Before sending

- [ ] Teammate confirmed as a full-time student at a university outside China
- [ ] Registration form filled for both members; passports scanned
- [ ] Repository pushed to GitHub as public; link added to README, deck cover and closing slide
- [ ] `python -m pytest -q` and `python -m mismatchdesk eval` run once more after any change
- [ ] Deck placeholders replaced: [Team name], [Member 1], [Member 2], [University], [contact email], [your-account]
- [ ] Video recorded, under 5 minutes, uploaded (YouTube unlisted or Google Drive with "anyone with the link"), link tested in a private window
- [ ] PDFs exported: technical documentation, security self-assessment, deck
- [ ] Total attachment size checked; if over about 20 MB, send links instead of files

## Draft email: registration (send first, Monday)

Subject: FinTechathon 2026 International Track registration: [Team name], Topic E

Dear Ms Wang,

We would like to register for the 2026 Shenzhen International FinTech Competition (FinTechathon), International Track, Topic E: SME Finance Copilot.

Team name: [Team name]
Members: [Member 1], [university, programme, year]; [Member 2], [university, programme, year]
Country: Bangladesh

Please find attached the completed registration form and both members' passport bio pages. We would be grateful if you could confirm our slot.

Kind regards,
[Your name]
[Phone, including +880]

## Draft email: submission (Monday evening, update Tuesday if needed)

Subject: FinTechathon 2026 International Track submission: [Team name], Topic E, Mismatch Desk

Dear Ms Wang,

Please find our submission for Topic E: SME Finance Copilot. Our entry, Mismatch Desk, is an AI agent that reconciles a small business's books, payment-provider reports and bank statement into a verified cash position, forecasts cash flow with alerts, and matches financing options to the verified need, under strict permission tiers.

1. Technical documentation: attached (PDF)
2. Presentation deck: attached (PDF)
3. Demo video: [link]
4. Source code: [GitHub link] (README with deployment instructions; 43 automated tests)
5. Security self-assessment: attached (PDF)
6. Execution evidence: the `evidence/` folder in the repository (scored evaluation report, hash-chained sandbox operation logs, copilot transcript) and the demo video

To reproduce our results: `pip install -r requirements.txt`, then `python -m mismatchdesk eval`.

Kind regards,
[Your name], on behalf of [Team name]
