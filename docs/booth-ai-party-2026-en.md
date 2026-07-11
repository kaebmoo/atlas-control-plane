# Atlas @ Chiang Mai AI Party 2026 — booth + 5-minute talk script

Event: **Chiang Mai AI Party 2026** · theme *"AI for Money"* · July 18, 2026 · Maker Asia, Chiang Mai
Format: show & tell — people come to *try real things*, not sit through lectures.

Tracks Atlas fits: **AI Agent** (multi-agent orchestration) + **Local AI** (runs on your own hardware).
Audience: makers / devs / creators / local-model tinkerers who want to make money with AI.
**Not** government executives → keep the NT business plan off this stage.

---

## Opening hook (first 15 seconds)

> "You're all playing with a single AI agent, right? Let me show you what happens when you put
> **many** agents to work in a pipeline — on your own machine, data never leaves, budget capped
> so it can't run away."

Booth sign (one line):
> **Orchestrate many AI agents — on your own hardware, budget-capped, fully auditable**

---

## 5-minute talk script

**0:00–0:30 — Hook + what Atlas is**
Say the opening hook, then:
Atlas is a **control plane** — it gives orders, it doesn't run the AI itself. It talks to many
workers (thClaws) across many machines from one dashboard, over plain HTTP APIs — routing, live
result streaming, history, and chaining agents into workflows.

**0:30–3:00 — Live demo (the star)** — News Desk, from `demo-script.md`
1. Show 2 workers online (reporter + anchor).
2. Send the reporter: *"Find one tech news item and summarize the facts."* → **let people watch it stream live**.
3. Turn on `Hand off after success` → the anchor turns the reporter's output into a broadcast script
   → point out the linked parent/child jobs.
4. (If time) run the full News Desk workflow + human approval gate:
   it pauses for a human to click Approve before publishing → this is what separates it from a chatbot.

**3:00–4:00 — Why this = money** (tie to the event theme)
- **Cheaper:** run on your own machine / a local model → no data leakage, no per-token cloud bill.
- **Won't blow up on you:** budget caps + human approval + full audit log → no surprise runaway bills.
  (Gartner: 40%+ of agentic projects will be canceled — because they can't be *controlled*, not
  because the models aren't good enough.)
- **Makes money:** because governance is built in, you can actually **sell the workflow as a service**
  (complaint intake, document drafting, summarize→produce content) — not a toy that dies in the demo.

**4:00–4:30 — Close + CTA**
- Open source, runs on **Python stdlib + SQLite** — no separate database, works air-gapped.
- Model-agnostic — plug in local / open-source / commercial models.
- "Come drive a workflow yourself at the booth / scan the QR for the repo."

**Closing line:**
> "A chatbot can answer. Atlas makes AI **work as a team** — on your machine, budget-capped, auditable."

---

## Booth setup checklist

- [ ] Big screen showing the dashboard with a live stream running = crowd magnet.
- [ ] 2 workers + Atlas started ahead of time per `demo-script.md` (Terminals 1/2/3).
- [ ] **Bad-wifi fallback:** wire a worker to a **local model** so a cloud call can't hang mid-demo —
      and record a backup demo GIF to play if the live run breaks.
- [ ] Rehearse to finish in 3 minutes (booth visitors won't wait).
- [ ] QR code → GitHub repo on the sign.
- [ ] One rehearsed sentence answering "what is it?" (= the sign) you can repeat all day.

## What NOT to do

- Don't open with architecture / node types / policy — open with the live demo.
- Don't push the NT sovereign gov deck (฿1.2bn, GDCC, procurement) — wrong audience.
  One bonus sentence is enough: *"this also extends to selling into orgs/gov that need audit."*
- Don't over-claim sovereignty — if asked, say "reduces jurisdiction exposure + you keep control,"
  not "zero dependencies."
