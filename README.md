# Dentify Demo — AI Voice Receptionist + Practice Hub (+ social-media booking)

A working demo of the proposal: one calendar shared by an **AI receptionist** (voice or chat) and a
**Practice Hub** (calendar, patients, billing, reminders, reports). 100% free / open source:
Python, FastAPI, SQLite, FullCalendar (bundled, works offline), browser speech APIs.
All data is **fake sample data** generated on first run.

## 1. Set up (5 minutes)

Requires Python 3.10+ (`python --version`).

```bash
cd dentify-demo
python -m venv .venv
# Windows:  .venv\Scripts\activate          Mac/Linux:  source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Run the tests

```bash
python -m pytest -q          # expect: 43 passed
```

## 3. Start the demo

```bash
uvicorn app.main:app --port 8000
```
Open **http://localhost:8000** in **Chrome or Edge** (needed for voice). First start creates `dentify.db`
with sample patients and appointments around today's date. Delete `dentify.db` to reset.
The demo uses your computer's clock and timezone for "today" and office hours.

## 4. Demo script for your client (about 6 minutes)

1. **Dashboard** – point at the cost card: $700 → $299/month, $4,812 saved per year.
2. **Calendar** – drag an appointment to a new time; try dropping it where both doctors are busy (it refuses).
3. **Start voice call** (right panel) and say: *"Hi, I'd like to book a cleaning tomorrow."* Pick a time,
   give any phone number, then a name. Watch the booking appear on the **Calendar** within seconds.
   This is the key selling point: the AI books into the *real* calendar.
4. Call again: *"How much is a whitening?"*, *"Do you take Delta Dental?"* (answers come from the practice FAQ).
5. Call again as an existing patient: *"I need to cancel my appointment"* with phone `555 010 0103`.
   Also try *"move my appointment to next Friday at 2 pm"* and *"I have a toothache"*.
6. **AI Calls** tab – every call has a transcript and summary. **Messages** tab – the SMS confirmations it sent.
7. **Messages** → *Run reminders* (48h/2h) and *Run recall campaign* (lapsed patients). Mark a visit
   **completed** on the calendar → a Google-review invite is queued.
8. **Billing** – create an invoice, open its **Pay link**, pay it.

No microphone? Use **Text chat** — same brain, same calendar (this is also the social-media chatbot concept).

## 5. Make it the client's practice

| Change | Where |
|---|---|
| Practice name | `set PRACTICE_NAME=Their Dental` (Windows) / `export PRACTICE_NAME=...` before starting |
| FAQ answers (hours, prices, insurance...) | `app/data/faq.json` (also update the opening hours sentence) |
| Office hours / appointment types & lengths | `HOURS` and `TYPES` in `app/scheduling.py` |
| Doctors | `providers` in `app/seed.py` (then delete `dentify.db`) |
| Google review link | `REVIEW_LINK` environment variable |

## 6. Optional: smarter answers with a free LLM
Booking never uses an LLM (so it can't invent times). The LLM only rewords answers to open questions.
- **Local & free:** install Ollama, run `ollama pull llama3.2:3b`, then start the server with `LLM_BACKEND=ollama`.
- **Free hosted tier:** `LLM_BACKEND=groq` and `GROQ_API_KEY=...`.
If the LLM is unreachable, it silently falls back to the built-in answers.

## 7. Show it remotely
Easiest: screen-share or record a video. To let the client click around live:
`cloudflared tunnel --url http://localhost:8000` (free Cloudflare quick tunnel) gives a temporary https link.
Keep your laptop on and the server running while they test.

## 8. Demo vs. production (be upfront with the client)

| In this demo | In the real build |
|---|---|
| Browser mic/speaker (Chrome speech API) | Real phone number + streaming speech (e.g. Twilio/Telnyx + Whisper/Deepgram + TTS) |
| SMS saved to an outbox table; confirmation **emails really sent via Gmail** when configured | Real SMS provider (Twilio); optionally a transactional email provider (SendGrid) |
| SQLite, no login | PostgreSQL, user accounts and roles, audit log, encrypted backups |
| Cancel/reschedule acts on the caller's *next* appointment | Disambiguate when a patient has several |
| Fake data only | HIPAA: signed BAAs with every vendor that touches PHI; Curve data migration |

**Do not enter real patient data** into this demo: Chrome's speech recognition and any hosted LLM are not covered by a BAA.


---

# 9. Social-media booking (Instagram, Facebook, LinkedIn) + public booking form

Anyone who messages your Instagram or Facebook page is answered by the same receptionist brain that
handles calls, and bookings land on the **same Calendar**. Staff see every chat in the **Social Inbox**
tab, can **take over** (the AI goes silent) and hand back. Bookings, cancellations and reschedules also
drop a note into **Messages**, and the Dashboard shows how many bookings came from social/web.

| Channel | How it works | Needs |
|---|---|---|
| Instagram DMs | Meta webhook `POST /webhooks/meta` (object `instagram`) | Meta app, Instagram professional account linked to a Facebook Page |
| Facebook Messenger | same webhook (object `page`) | Meta app + Facebook Page |
| LinkedIn | LinkedIn does **not** offer a public API for reading/answering Page DMs. Use the booking link as the Page's custom button / profile link, or feed leads through Zapier/Make into `POST /webhooks/inbound` | Booking link, or automation tool |
| WhatsApp, ManyChat, Zapier, Make, anything with webhooks | `POST /webhooks/inbound` with header `X-Inbound-Secret`; the reply is returned in the JSON response | `SOCIAL_INBOUND_SECRET` |
| Anyone, anywhere | Public mobile-friendly form at `/book?src=instagram` (also `facebook`, `linkedin`, `google`) | Nothing. Paste the link in your bios |

## Set up Instagram + Facebook (about 30 minutes)

1. Run the app on a public **https** URL (a server, or `cloudflared tunnel --url http://localhost:8000` for testing).
2. At developers.facebook.com create an app (type Business), add the **Messenger** and **Instagram** products,
   and connect your Facebook Page / Instagram professional account.
3. Set environment variables, then restart:
   ```
   META_APP_SECRET=<App settings > Basic > App secret>
   META_VERIFY_TOKEN=<any random string you invent>
   META_PAGE_TOKEN=<Page access token with pages_messaging / instagram_manage_messages>
   PUBLIC_URL=https://your-domain.com
   ADMIN_PASSWORD=<strong password for your staff login>
   ```
4. In the Meta app's webhook settings use callback URL `https://your-domain.com/webhooks/meta` and your verify
   token, and subscribe to the **messages** field for both Page and Instagram.
5. Message your page from another account. The chat appears in **Social Inbox** within seconds.
   Until `META_PAGE_TOKEN` is set the chats still land in the inbox but replies are not sent (shown as "not delivered").

Meta rules to know: while the app is in Development mode only accounts with a role on the app can message it;
for the public you must complete Meta **App Review** and Business Verification for the messaging permissions.
Replies are only allowed within 24 hours of the customer's last message. Exact permission names and API
versions change, so check Meta's current Messenger / Instagram Messaging docs (`META_API_VERSION` sets the version).

## Generic endpoint (LinkedIn via automation, WhatsApp bridges...)
```bash
curl -X POST https://your-domain.com/webhooks/inbound \
  -H "X-Inbound-Secret: $SOCIAL_INBOUND_SECRET" -H "Content-Type: application/json" \
  -d '{"channel":"linkedin","sender_id":"li-42","sender_name":"Pat","text":"I need a cleaning tomorrow"}'
# -> {"reply":"Sure, a cleaning. ...","handoff":false}
```
Your automation sends `reply` back to the person on that platform.

## Security notes (read before going live)
- Set `ADMIN_PASSWORD`. Without it the whole staff app is open. Only `/book`, `/api/public/*`, `/webhooks/*` and
  static files are public. Use HTTPS.
- Webhook POSTs are rejected unless the Meta signature matches `META_APP_SECRET`
  (`SOCIAL_ALLOW_UNSIGNED=1` is for local testing only).
- Social chats cannot cancel or move appointments unless the chat created that patient. Anyone can type any
  phone number, so this stops strangers cancelling real patients' visits. Existing patients are told to call.
- The public form has a honeypot field, a per-IP rate limit and a cap of 3 upcoming visits per phone.
- The "Pay now" page and receipts are still the demo (no real card processing). Use Stripe Checkout in production.
- Still HIPAA territory: Meta, hosting and any LLM vendor need BAAs before real patient data flows through them.

## New in this version
- **Clinical:** tooth chart (Universal 1-32, 8 conditions) and treatment plans per patient (Patients tab, click a
  patient). Marking a treatment *Done* creates the invoice automatically.
- **Staff login:** optional `ADMIN_PASSWORD` / `ADMIN_USER`.
- **Reports:** bookings by source, social/web bookings, unread chats.

## Not built yet (compared with Curve)
Insurance eligibility/claims, X-ray/imaging integration, e-prescribing, patient intake/consent forms with
e-signature, real card payments, multi-user roles and an audit log, multi-location support.


## Confirmation emails (Gmail)

Every new booking (staff Hub, AI receptionist, public `/book` form) emails the patient a confirmation with a
calendar (.ics) attachment, if the patient has an email address.

Email is collected automatically: the public `/book` form requires it, and the AI receptionist (voice, chat,
Instagram/Facebook) asks for it after the name, accepts typed or spoken addresses ("sara at gmail dot com"), and
lets callers say "skip". Returning patients with no email on file get it added to their record.

1. In the Google account that will send the mail, turn on **2-Step Verification**, then create an
   **App password** (Google Account > Security > App passwords).
2. Set `GMAIL_USER=you@gmail.com` and `GMAIL_APP_PASSWORD=<16-character app password>`.
   Optional: `EMAIL_FROM_NAME`, `PRACTICE_PHONE`, `PRACTICE_ADDRESS` (shown in the email).
3. Put them in a `.env` file next to this README (copy `.env.example`), or export them in your shell / hosting
   dashboard (Railway, Render...). Restart the server; its first line should say `Confirmation emails: ON via Gmail`.
   Check the settings with `python -m app.email_check you@example.com`.
4. Sent emails show in the **Messages** tab (channel `email`); a failed send is logged there as
   `email_failed` and never blocks the booking. Without these variables, emails are only written to the outbox.

Note: a normal Gmail account allows roughly 500 emails/day, fine for a single clinic.

## Practice-management features (Curve-style)

What a dentist expects from systems like Curve Dental, all in this demo with sample data:

| Feature | Where to see it |
|---|---|
| **Morning huddle**: today's patients with medical alerts, insurance, balances and accepted treatment | Dashboard |
| **KPIs**: production, collections, case acceptance, accepted-but-unscheduled revenue, A/R aging, production by provider | Dashboard |
| **Tabbed patient chart**: tooth chart, treatment plan, medical & insurance, SOAP notes, account | Patients > click a patient |
| **Fee schedule with procedure codes** (23 common CDT codes); picking a code fills the name and fee | Patient > Treatment plan |
| **Insurance estimates**: coverage % per class, deductible, annual maximum, remaining benefits | Patient > Treatment plan |
| **Claims workflow**: *Done + bill* raises a claim for the insurance share and invoices only the patient share. Submit, then mark paid (a short payment bills the patient the difference) or denied (the insurance share moves to the patient) | Insurance tab |
| **Medical alerts**: allergies, blood thinners, premed shown in red on the chart, calendar popup and huddle | Patient > Medical & insurance |
| **SOAP clinical notes** with draft, edit, sign and lock | Patient > Notes |
| **Treatment plan the patient can accept online**: printable estimate, private signed link, emailed through Gmail | Patient > Treatment plan > Email plan |
| **Patient statements** (printable) | Patient > Account |

Plus everything from before: AI receptionist (voice/chat), social inbox, online booking, reminders, recall.

### Suggested 10-minute demo for the dentist
1. **Dashboard**: walk through the huddle (red allergy and blood-thinner alerts, who owes money, who has accepted treatment but isn't booked) and the KPIs.
2. **Calendar**: click an appointment, note the medical alert, open the chart.
3. **Patient chart > Treatment plan**: add *D2740 porcelain crown*, see the insurance estimate; click **Email plan to patient**; open the link and accept as the patient.
4. Click **Done + bill**, then go to **Insurance**: submit the claim, mark it paid for a little less than expected, and show the patient's balance pick up the difference.
5. **Notes**: write a SOAP note, sign it, and show that it locks.
6. Finish with the **AI receptionist** taking a booking by voice and the new appointment landing on the calendar.

Emailed plan links need `PUBLIC_URL` set to a public https address (otherwise they point at your own computer).
Sample data (carriers, claims in every stage, 90-day-old balances, allergies, notes) is fake. An existing demo database is upgraded automatically on the next start.

### Before using real patient data
This is a demo. Clinical notes and medical history are protected health information. A real clinic needs, at minimum:
HTTPS, `ADMIN_PASSWORD` (or real user accounts with roles), encrypted backups, an audit log, and a signed BAA with the hosting provider.
Consumer Gmail is not a HIPAA-compliant channel: use Google Workspace with a BAA (or a healthcare email provider) and keep emails free of clinical detail.
Not included yet: X-ray/image storage, e-prescribing, perio charting, electronic claim transmission (claims are tracked, not sent to carriers) and per-user permissions.
