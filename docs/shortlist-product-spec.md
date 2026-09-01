# Shortlist — Product Spec v0.1

*Working name. An AI chief of staff for your dating life.*

---

## 1. Thesis

**Dating apps optimize for matching. Nobody owns what happens after the match.**

The match is not the bottleneck. The bottleneck is the middle of the funnel: four apps,
twelve matches, three good conversations, and no system. The best thread dies from a
three-day reply gap. The ones that survive turn into a fourteen-message negotiation about
Thursday. You forget which app someone lives in.

Shortlist is a CRM and scheduler for the people you're actually talking to. One portal,
every app, every conversation, every date.

> **The reframe that matters:** this is not an AI that dates for you. It is an AI that
> makes sure you don't drop the ball. That distinction drives every decision below — the
> integration strategy, the ethics line, and the reason people will pay.

---

## 2. The problem, concretely

The funnel as users actually live it:

```
  Swipe          Match           Talk            Ask            Date
   ~200    ->     ~12      ->     ~4      ->     ~1.5    ->     ~1
                                   ^              ^              ^
                              you lose 2      you lose 1     you double-book,
                              to silence      to logistics   forget her name,
                                                             never follow up
```

Everything to the left of `Match` is a solved, saturated, capital-intensive market.
Everything to the right is unowned.

Four failure modes we are targeting, in order of pain:

1. **Thread rot.** A good conversation goes quiet because you were busy for two days.
   No app tells you which silence actually costs you something.
2. **Context collapse.** You cannot remember who mentioned the Iceland trip, who's the
   nurse, and which app either of them is on.
3. **The ask.** People stall at the transition from chat to plan. It is a specific,
   learnable move and most people are bad at it.
4. **Logistics.** Availability, a venue that works for both neighborhoods, no
   double-booking, reschedules, reminders. Pure calendar work that nobody has automated.

---

## 3. Who it's for

**Primary:** 27–40, employed, busy, dating with intent. Three to eight live conversations
at a time, not forty. They are not bad at dating — they are bad at *follow-through*,
because dating is competing with a job.

They already pay $20–30/mo for Hinge+ and Tinder Gold. Willingness to pay is proven.
Their complaint is never "I need more matches," it is "I let a good one go cold."

**Not the primary user:** the high-volume 22-year-old optimizing for match count. That
user wants a rizz generator, which is a commodity toy with no retention.

---

## 4. The core loop

```
  Capture  ->  Organize  ->  Nudge  ->  Draft  ->  Ask  ->  Schedule  ->  Log
     ^                                                                     |
     +---------------------------------------------------------------------+
```

1. **Capture** — a conversation enters Shortlist (see §6 for how).
2. **Organize** — it becomes a Person card with context, source app, and state.
3. **Nudge** — Shortlist tells you which thread is decaying and worth saving.
4. **Draft** — three replies in your voice. You edit. **You** press send.
5. **Ask** — Shortlist detects a ripe thread and proposes a concrete ask with a real
   time and a real place drawn from your actual calendar.
6. **Schedule** — calendar hold, venue, travel buffer, reminders, reschedule handling.
7. **Log** — 20-second post-date note that feeds the next nudge.

---

## 5. Feature spec

### P0 — the MVP is not real without these

**Pipeline board.** Every person you're talking to as a card: name, photo, source app,
last message, days since contact, stage (`Talking` / `Ripe` / `Planned` / `Met` /
`Faded`). This is the home screen. It is the product.

**Person cards.** Everything you know: what they do, what they mentioned, where they live,
what you already told them. Facts are extracted from the conversation automatically and
editable by hand. Solves context collapse.

**Decay nudges.** The engine ranks threads by `(quality of conversation) × (time since
last message) × (risk of loss)` and surfaces at most two per day. *At most two.* A nudge
app that fires ten times a day gets deleted in a week.

**Calendar-aware scheduler.** Two-way Google/Apple Calendar sync. Knows your real
availability, holds a tentative slot the moment you propose a time, converts it on
confirmation, and releases it on a no. Hard constraints:
- Never propose a slot you can't make
- Never two first dates in one day
- Never the same venue twice in a week (this happens, it is mortifying)
- Always a travel buffer on both sides

### P1 — the reason they stay

**Draft assist.** Three suggested replies, in the user's own voice, learned from their
sent messages. Bias hard toward short, specific, and unclever. Generic LLM flirting is
instantly detectable and actively repels people — this is the single hardest quality
problem in the app and it is worth over-investing in.

**Ask detection.** Flags a thread as `Ripe` when the signals are there (message cadence,
reciprocal questions, a shared specific interest, thread length) and drafts the actual ask
with a time and a place: *"Are you free Thursday at 7? There's a place near you I've been
meaning to try."* Vague asks are why people don't get dates.

**Venue engine.** Midpoint between two neighborhoods, filtered by vibe, price, noise, and
whether it works for a first date. Not a Yelp dump — two options and a reason.

**Safety layer.** One tap to share the venue, time, name, and live location with a chosen
friend, plus an automatic check-in ping. Non-negotiable and a genuine differentiator for
women users. Ship it in v1, not v3.

### P2 — later

Post-date logging and pattern feedback ("you consistently do better on weeknight
walk-and-coffee than Saturday dinner"). Multi-date arc management. Reschedule negotiation.
Read-receipt-style analytics on your own behavior.

---

## 6. The hard part: getting the data in

**There are no public APIs.** Tinder, Hinge, and Bumble all prohibit automated access in
their terms of service. Match Group in particular enforces aggressively. This is not a
"move fast" risk we absorb ourselves — **if our integration is detected, our users' dating
accounts get banned.** That is a product-killing trust event, not a legal footnote.

So the integration strategy *is* the product strategy.

| Approach | Mechanism | Verdict |
|---|---|---|
| **Share-sheet bridge** | User shares a screenshot or copied thread into Shortlist; we OCR/parse it into a Person card | **Ship this first.** Zero ToS exposure. ~2 taps per conversation. Fully defensible publicly and legally. |
| **Desktop browser extension** | Runs locally as the user on `tinder.com` / `bumble.com` / `okcupid.com`, reads a session they're already logged into | Real unified inbox — but a gray zone, brittle across redesigns, and **Hinge has no web client at all**, which is a fatal gap for our persona. |
| **Notification ingestion** | Parse push/email notifications | Low friction, but yields fragments, not threads. Useful as a decay signal only. |
| **Official partnership** | Integrate as a sanctioned scheduling layer | The endgame, not the start. Requires leverage we don't have yet. |

**Decision: bridge-first.** Friction is survivable when the payoff is high. It lets us
launch publicly, charge money, and get press with no legal cloud. The share-sheet path
also happens to be the only version that works on Hinge, which is where our persona lives.

The calendar half of the product has clean, documented, permissioned APIs. **The half that
is legally clean is also the half that is defensible.** That is not a coincidence — it is
the reason to build the scheduler as the heart of the product rather than the messaging.

---

## 7. Lines we don't cross

These are product decisions, not disclaimers.

- **The user always presses send.** We draft, they approve. No autopilot, ever, even
  when it becomes trivially easy to ship.
- **We never impersonate the user at scale.** No bulk outreach, no auto-replies, no
  running conversations while they sleep.
- **We never fabricate biography.** Draft assist works from the user's real life and real
  profile. It writes in their voice; it does not invent a better person.
- **No scraping of other users' data beyond what our user already legitimately sees**
  in their own inbox. No cross-user profile database. Ever.

The ethics line and the quality line point the same direction here: autopilot also
produces *worse dates*. Someone who shows up to a date they didn't write themselves has
already lost. Good constraints are rarely a tax.

**Open question worth deciding deliberately:** what happens when both people on a thread
are running Shortlist? Two agents negotiating Thursday in four seconds is either the
killer feature or the most dystopian thing on the internet. Decide before we stumble into
it.

---

## 8. Architecture sketch

```
  iOS app (SwiftUI)
    |
    +-- Share extension ------> ingest: screenshot -> OCR -> thread parse
    +-- EventKit ------------->  local calendar read/write
    |
  API (one service, boring stack)
    |
    +-- Person / Thread / Event store
    +-- Nudge engine (scheduled job, ranks decay)
    +-- LLM layer (Claude): fact extraction, voice-matched drafts,
    |                       ripeness classification, ask generation
    +-- Google Calendar OAuth (for non-Apple users)
    +-- Venue lookup (Places)
```

**Privacy posture, which is a feature and not overhead:** this is among the most sensitive
data a person owns. Encrypt at rest, keep raw message content out of logs and telemetry,
never train on user content, and offer one-tap full delete. Say all of this loudly on the
landing page. For a nervous user, the privacy policy *is* the pitch.

---

## 9. Business model

$12–15/month subscription, free tier capped at three active people.

The pitch is not "get more matches." It is **"stop losing the good ones."** Concrete,
measurable, and something the incumbent apps structurally cannot say — their revenue
depends on you *not* leaving.

That structural conflict is the strategic center of the whole idea: **we are aligned with
the user leaving the apps. The apps are not.** Every dating app makes money when you stay
single. Shortlist makes money when you go on dates. Lead with that.

---

## 10. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Ban risk to user accounts | **Critical** | Bridge-first ingestion; no automated access to dating platforms |
| "AI wrote your messages" reads as catfishing | High | Draft-assist framing, user-always-sends, voice matching from real messages |
| Generic AI voice repels matches | High | Over-invest in voice cloning; short and specific over clever; ship a kill switch for drafts |
| Bridge friction kills retention | High | The nudge and the scheduler must deliver value from a *single* pasted thread |
| Creepy-factor press cycle | Medium | Publish the §7 lines publicly, before anyone asks |
| Incumbents ship it | Medium | They won't — it's cross-app by definition, and it's counter to their retention model |
| Data breach | Medium | Encryption, minimal retention, no message content in logs |

---

## 11. MVP scope

Ruthlessly cut to what one developer can ship. Everything here is buildable solo on iOS.

**Milestone 1 — the spine (weeks 1–2)**
Pipeline board, Person cards, manual entry. No AI, no ingestion. Prove the CRM alone is
worth opening. If a hand-typed version isn't useful, no amount of AI fixes it.

**Milestone 2 — the scheduler (weeks 3–4)**
EventKit integration, propose/hold/confirm/release flow, the four hard constraints,
reminders. This is the defensible core and the strongest demo.

**Milestone 3 — intelligence (weeks 5–6)**
Share-extension ingestion with OCR, LLM fact extraction into Person cards, decay nudges,
draft assist.

**Milestone 4 — trust (week 7)**
Safety sharing, privacy controls, delete-everything.

**Cut from v1:** venue engine (hardcode a shortlist by neighborhood), post-date analytics,
Android, any browser extension, multi-user anything.

**The demo that sells it:** paste one Hinge conversation → a Person card populates itself
→ two days pass → Shortlist says *"Maya was your best thread this week and she's been
waiting"* → tap → a drafted ask with a real Thursday from your real calendar → confirm →
it's on the calendar with a reminder and a venue.

That is 90 seconds and it lands.

---

## 12. Decisions made in this draft

Recorded so they can be argued with rather than silently inherited:

1. **The scheduler/CRM is the heart; messaging is a feature.** The messaging half is
   commoditized, legally exposed, and reputationally risky. The pipeline and calendar half
   is unowned, defensible, and API-clean.
2. **Persona is the busy 27–40 professional**, not the high-volume swiper. Better
   willingness to pay, better retention, better story.
3. **iOS-first, share-sheet ingestion.** Not a browser extension — Hinge has no web
   client, and the extension path carries ban risk we shouldn't hand to users.
4. **Draft assist, never autopilot** — as a permanent product constraint, not a v1
   limitation to be relaxed later.

---

## 13. Open questions

- Does a hand-entered pipeline board hold attention for two weeks with no AI at all?
  **Test this before building anything else.** It is the load-bearing assumption.
- How much sent-message history is needed before voice matching stops sounding like a
  chatbot? Guess: 200 messages. Unverified.
- What is the honest ceiling on OCR-parsing a Hinge screenshot into a clean thread?
- Both-sides-using-it: feature or horror? (§7)
- Is "Shortlist" the name? It's clean and describes the product, but it's provisional.
