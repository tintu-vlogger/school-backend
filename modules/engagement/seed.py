"""Defaults every company gets (kudos values, a reception kiosk), and lively demo content
for the development demo company (people, posts with pictures, reactions, comments,
kudos, celebrations, an announcement to acknowledge and an eNPS survey with answers).

`setup_company` and `seed_demo` are found by seed.seed_data (`_module_setup` on sign-up
and every start, `_module_demo` for the development demo company). Demo content is
added once and marked in the engagement settings so it never repeats.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.config import get_settings

DEMO_SLUG = "nextgenhrms-admin"


def setup_company(db: Session, company_id: int) -> None:
    from modules.engagement import kudos
    from modules.engagement.models import Kiosk
    kudos.ensure_default_values(db, company_id)
    if not db.query(Kiosk.id).filter(Kiosk.company_id == company_id).first():
        from modules.engagement import kiosk
        kiosk.create(db, company_id, {"name": "Reception TV", "welcome_text": "Welcome! Have a great day."})
    db.flush()


# ── Demo content ────────────────────────────────────────────────────

def _demo_people(db: Session, company, today: date) -> dict[str, object]:
    """The demo company's people (created by the organisation module's demo seed), keyed
    by first name. A few birthdays are moved to this week so Buzz has celebrations to show."""
    from modules.employees.models import Employee
    from modules.engagement import people
    try:
        from modules.organization.seed import seed_demo_people
        seed_demo_people(db)
        db.info["company_id"] = company.id
    except ImportError:
        pass
    rows = db.query(Employee).filter(Employee.company_id == company.id, Employee.active.is_(True),
                                     Employee.email.like("%@example.com")).all()
    out = {}
    for e in rows:
        if "+" not in e.email and e.first_name not in out:
            out[e.first_name] = e
    for name, offset in (("Ananya", 0), ("Siddharth", -1), ("Karthik", 2), ("Neha", 4), ("Ganesh", 11)):
        emp = out.get(name)
        if emp is None:
            continue
        born = today + timedelta(days=offset)
        year = random.Random(name).randint(1986, 1999)
        try:
            people.set_fallback_dates(db, company.id, emp.id, birth=born.replace(year=year))
        except ValueError:  # 29 February
            people.set_fallback_dates(db, company.id, emp.id, birth=born.replace(year=year, day=28))
    db.flush()
    return out


def _actor(emp, company_id: int):
    from modules.engagement.people import Actor, full_name
    if emp is None:
        return Actor(company_id=company_id, employee_id=None, role="admin", name="Tintu")
    return Actor(company_id=company_id, employee_id=emp.id, role=emp.role, name=full_name(emp))


def _m(emp) -> str:
    return f"@[{emp.first_name} {emp.last_name}]({emp.uuid})"


def _picture(db: Session, company_id: int, key: str, by) -> str:
    from modules.engagement import content, demo_art
    f = content.store_upload(db, company_id, by.id if by else None, f"{key}.png", "image/png", demo_art.PICTURES[key]())
    return str(f.uuid)


def _at(days_ago: float, hour: int | None = None) -> datetime:
    """A moment `days_ago` days before now (never in the future)."""
    return datetime.now(timezone.utc) - timedelta(days=max(days_ago, 0.02))


def seed_demo(db: Session, company_id: int) -> None:
    """Called by seed.seed_data for the demo company (development only)."""
    from modules.company.models import Company
    company = db.query(Company).filter(Company.id == company_id).first()
    if company is None or company.slug != DEMO_SLUG or get_settings().is_production:
        return
    _seed_demo(db, company)


def _seed_demo(db: Session, company) -> None:  # noqa: C901  (one long, linear script)
    from modules.engagement import announcements, celebrations, feed, kudos, surveys
    from modules.engagement import settings as cfg
    from modules.engagement.models import BuzzPost, SurveyAnswer, SurveyParticipation, SurveyResponse, SurveyRun
    from core.timeutil import today_for, tz_for

    row = cfg.get_row(db, company.id, create=True)
    if (row.config or {}).get("demo", {}).get("seeded"):
        return
    rnd = random.Random(42)
    cid = company.id
    today = today_for(company)
    p = _demo_people(db, company, today)
    needed = ["Priya", "Rahul", "Rohan", "Neha", "Ananya", "Lakshmi", "Farah", "Deepak", "Manish", "Siddharth", "Shalini"]
    if any(n not in p for n in needed):
        print("[seed] Engagement demo waits for the demo people")
        return
    A = lambda name: _actor(p.get(name), cid)  # noqa: E731
    owner = _actor(None, cid)
    everyone = list(p.values())

    def backdate(post, days_ago):
        post.created_at = _at(days_ago)
        db.flush()
        return post

    def react(post, emojis, people_list=None, comment=None):
        pool = [e for e in (people_list or everyone)]
        for emp in rnd.sample(pool, k=min(len(pool), len(emojis))):
            feed.react(db, _actor(emp, cid), post, emojis.pop(0), comment)

    def comment(post, who, text, parent=None):
        c = feed.add_comment(db, A(who) if isinstance(who, str) else who, post, text,
                             parent.uuid if parent is not None else None)
        when = post.created_at + timedelta(minutes=rnd.randint(20, 900))
        c.created_at = min(when, datetime.now(timezone.utc) - timedelta(minutes=rnd.randint(2, 30)))
        db.flush()
        return c

    # ── posts ──
    diwali = feed.create_post(db, A("Priya"), {
        "body": (f"What an evening! 🪔 Thank you all for making our Diwali celebration so special. The rangoli by "
                 f"{_m(p['Neha'])} and {_m(p['Ritu'])} stole the show. More photos in the shared drive. #Diwali #FestiveVibes"),
        "attachments": [_picture(db, cid, "diwali", p["Priya"])]})
    backdate(diwali, 1.2)
    react(diwali, ["❤️", "🎉", "🔥", "😍", "👏", "❤️", "🎉", "🤩", "❤️", "👏", "🙌"])
    c1 = comment(diwali, "Neha", "Thank you! Ritu and I started at 7 in the morning 😅")
    comment(diwali, "Ritu", "And it was worth every minute 🙌", parent=c1)
    comment(diwali, "Arjun", f"Best office Diwali so far. Thank you {_m(p['Priya'])} and team. The sweets from Mysuru were 🔥")
    comment(diwali, owner, "Wonderful work, team. Happy Diwali to you and your families! 🪔")

    deal = feed.create_post(db, A("Rohan"), {
        "body": (f"Signed! 🎉 Our biggest deal this quarter is done, a 3-year rollout across 40 branches. Huge thanks to "
                 f"{_m(p['Lakshmi'])} for six months of patient work and to {_m(p['Rahul'])}'s team for the demo that "
                 f"sealed it. Celebratory lunch on Friday! #WinningTogether #Sales"),
        "attachments": [_picture(db, cid, "team_lunch", p["Rohan"])]})
    backdate(deal, 3.1)
    react(deal, ["🎉", "👏", "🔥", "💯", "🚀", "👏", "🎉", "🙌"])
    c2 = comment(deal, "Lakshmi", "Team effort all the way! Thank you Rohan for backing us at every step 🙏")
    comment(deal, "Imran", "Learnt so much watching this one close.", parent=c2)
    comment(deal, "Meenakshi", "Finance is very happy too 😄")

    poll = feed.create_post(db, A("Rahul"), {
        "body": "Planning the engineering offsite for January. Where should we go? 🏕️ #Offsite",
        "poll": {"options": ["Coorg", "Rishikesh", "Goa", "Munnar"], "multiple": False,
                 "closes_at": datetime.now(timezone.utc) + timedelta(days=4), "show_voters": True}})
    backdate(poll, 2.2)
    opts = {o.label: o.uuid for o in poll.poll_options}
    for name, choice in (("Ananya", "Coorg"), ("Karthik", "Rishikesh"), ("Siddharth", "Goa"), ("Divya", "Coorg"),
                         ("Pooja", "Munnar"), ("Suresh", "Coorg"), ("Aditya", "Goa"), ("Kavitha", "Coorg"), ("Neha", "Rishikesh")):
        if name in p:
            feed.vote(db, A(name), poll, [opts[choice]])
    react(poll, ["👀", "🔥", "😂"])
    comment(poll, "Karthik", "Rishikesh for rafting! 🚣")
    comment(poll, "Ananya", "Coorg in January is perfect weather ☕")

    design = feed.create_post(db, A("Neha"), {
        "body": ("Our new design system is live 🎨 Components, colours and icons in one place, so every screen looks "
                 "and feels the same. Feedback welcome! #Design #ProductLaunch"),
        "attachments": [_picture(db, cid, "hackathon", p["Neha"])]})
    backdate(design, 4.3)
    react(design, ["😍", "👏", "💡", "🚀", "✨", "👏"])
    comment(design, "Ananya", "The new date picker is so much nicer 👌")
    comment(design, "Vikram", f"Love it! {_m(p['Neha'])} can we add a dark theme next?")

    trees = feed.create_post(db, A("Deepak"), {
        "body": ("🌱 32 of us planted 150 saplings at the lake park on Saturday. Thank you to everyone who woke up early "
                 "and got their hands muddy! Next drive in December. #GreenIndia #Volunteering"),
        "attachments": [_picture(db, cid, "volunteering", p["Deepak"])]})
    backdate(trees, 6.1)
    react(trees, ["🙌", "❤️", "🌟", "👏", "💯"])
    comment(trees, "Farah", "Count me in for December!")

    link = feed.create_post(db, A("Shalini"), {
        "body": ("Good read on the new labour codes and what they change for wages, working hours and overtime. Worth 10 "
                 "minutes for anyone who manages shifts or payroll. https://labour.gov.in/labour-codes #Learning"),
        "link_preview": {"url": "https://labour.gov.in/labour-codes", "title": "Labour Codes | Ministry of Labour & Employment",
                         "description": "The four labour codes on wages, industrial relations, social security and "
                                        "occupational safety, health and working conditions.",
                         "site": "labour.gov.in"}})
    backdate(link, 5.0)
    react(link, ["💡", "👍", "🙏"])

    hiring = feed.create_post(db, A("Farah"), {
        "body": ("We're hiring! 🚀 Two Senior Software Engineers and a Product Designer in Bengaluru. Know someone great? "
                 "Refer them and earn a referral bonus of ₹50,000. #Hiring #Referral")})
    backdate(hiring, 7.4)
    react(hiring, ["🚀", "👍", "🔥"])
    comment(hiring, "Ananya", "Sent you two profiles on email 👍")

    trek = feed.create_post(db, A("Ananya"), {
        "body": "Sunrise at Nandi Hills with the weekend trekking group ⛰️ Who's joining next time? #Weekend",
        "attachments": [_picture(db, cid, "offsite", p["Ananya"])]})
    backdate(trek, 8.2)
    react(trek, ["😍", "🔥", "❤️", "🤩"])

    cricket = feed.create_post(db, A("Manish"), {
        "body": ("Sales XI vs Engineering XI this Sunday at 7 am 🏏 Engineering, bring your best excuses. "
                 f"{_m(p['Siddharth'])} you're opening! #Cricket"),
        "attachments": [_picture(db, cid, "cricket", p["Manish"])]})
    backdate(cricket, 0.3)
    react(cricket, ["😂", "🏆", "🔥", "💪", "😂"])
    comment(cricket, "Siddharth", "Challenge accepted 😎")
    comment(cricket, "Rahul", "Engineering has been practising in the parking lot all week.")

    # ── kudos ──
    values = {v["name"]: v["uuid"] for v in kudos.list_values(db, cid)}
    for giver, rec, value, msg, days in (
        ("Rahul", ["Ananya"], "Above and beyond", "Stayed back to fix the payment gateway issue before the bank cut-off. Customers never noticed a thing. Thank you!", 0.5),
        ("Rohan", ["Lakshmi"], "Customer first", "Six months of patient follow-ups and the client trusts us completely. You made this deal happen.", 3.0),
        ("Priya", ["Neha", "Ritu"], "Team player", "The Diwali rangoli and decorations were stunning. Thank you for giving up your morning for it!", 1.0),
        ("Kavitha", ["Karthik"], "Problem solver", "Found the root cause of the slow report in an hour after a week of guessing. Brilliant debugging.", 2.5),
        ("Ananya", ["Aditya"], "Thank you", "For picking up the code reviews while I was on leave. You are going to do great things here!", 1.7),
        ("Priya", ["Deepak"], "Ownership", "Organised the whole tree plantation drive, from permissions to snacks. 🌱", 5.5),
        ("Lakshmi", ["Imran"], "Team player", "Covered all my client calls while I was away. Couldn't ask for a better teammate.", 9.0),
        ("Arjun", ["Priya"], "Inspiring leader", "The new joiner experience you designed this year has changed how people feel in their first week. Thank you.", 12.0),
    ):
        if giver not in p or any(r not in p for r in rec):
            continue
        k = kudos.give(db, A(giver), {"recipients": [str(p[r].uuid) for r in rec], "value": values.get(value), "message": msg})
        k.created_at = _at(days)
        post = db.query(BuzzPost).filter(BuzzPost.kudos_id == k.id).first()
        if post is not None:
            post.created_at = k.created_at
            db.flush()
            react(post, rnd.sample(["👏", "🙌", "❤️", "💯", "🔥", "🎉"], k=rnd.randint(3, 6)))
    db.flush()

    # ── celebrations: today's cards (posted as the morning job would) ──
    celebrations.run_morning(db, company, force=True)
    morning = (datetime.combine(today, datetime.min.time(), tz_for(company)) + timedelta(hours=9)).astimezone(timezone.utc)
    wishers = ["Rahul", "Kavitha", "Neha", "Rohan", "Divya", "Suresh"]
    for post in db.query(BuzzPost).filter(BuzzPost.company_id == cid, BuzzPost.celebration_date == today).all():
        post.created_at = min(morning, datetime.now(timezone.utc) - timedelta(minutes=40))
        first = post.subject.first_name if post.subject else "you"
        wishes = {"birthday": [f"Happy birthday {first}! 🎂 Have a fantastic year!", "Many happy returns of the day! 🎉",
                               "Happy birthday! Cake in the pantry at 4? 🍰"],
                  "work_anniversary": [f"Congratulations {first}! 🎉", "Thank you for everything you do 🙏"],
                  "welcome": [f"Welcome aboard {first}! 👋", "Welcome to the team!"]}.get(post.kind, [])
        for i, msg in enumerate(wishes):
            if wishers[i] in p and p[wishers[i]].id != post.subject_employee_id:
                celebrations.wish(db, A(wishers[i]), post, msg, "🎉" if i % 2 else "❤️")
        react(post, ["🥳", "❤️", "🎉"], [e for e in everyone if e.id != post.subject_employee_id and e.first_name not in wishers])
    sid = p["Siddharth"]
    y_post = BuzzPost(company_id=cid, kind="birthday", is_system=True, subject_employee_id=sid.id,
                      celebration_date=today - timedelta(days=1), theme="cosmic", mentions=[sid.id],
                      body=f"Happy birthday, {sid.first_name}! 🎂 Wishing you a wonderful year ahead.")
    db.add(y_post)
    db.flush()
    y_post.created_at = morning - timedelta(days=1)
    for who, msg in (("Rohan", "Happy birthday Siddharth! 🎉"), ("Ananya", "Have a great one! 🎂"), ("Karthik", "Party tonight? 😄")):
        celebrations.wish(db, A(who), y_post, msg)
    db.flush()

    # ── announcements ──
    leave = announcements.create(db, owner, {
        "kind": "policy", "title": "Leave policy for 2026-27",
        "summary": "Earned leave goes up to 18 days, and you can now carry forward up to 30 days.",
        "body_html": ("<p>Dear team,</p><p>Our leave policy for the financial year 2026-27 is now in effect. The main changes:</p>"
                      "<ul><li><strong>Earned leave</strong> goes up from 15 to <strong>18 days</strong> a year.</li>"
                      "<li>You can carry forward up to <strong>30 days</strong> into the next year.</li>"
                      "<li><strong>Paternity leave</strong> is now 10 working days.</li>"
                      "<li>Sick leave of more than 2 days needs a doctor's note.</li></ul>"
                      "<p>Please read the full policy and acknowledge it by the end of next week.</p><p>Thank you,<br>HR team</p>"),
        "cover": {"preset": "aurora"}, "requires_ack": True, "ack_due_on": today + timedelta(days=7), "pinned": True,
        "audience": {"all": True}, "action": "publish"})
    leave.published_at = leave.publish_at = _at(2.0)
    for name in ("Arjun", "Kavitha", "Rohan", "Meenakshi", "Rahul", "Neha", "Ganesh", "Shalini", "Deepak", "Farah", "Suresh"):
        if name in p:
            announcements.acknowledge(db, A(name), leave)
    for name in ("Karthik", "Divya", "Imran"):
        if name in p:
            announcements.detail(db, A(name), leave, False)
    lp = db.query(BuzzPost).filter(BuzzPost.announcement_id == leave.id).first()
    lp.created_at = leave.published_at
    react(lp, ["👍", "🙏", "🎉", "👍", "❤️"])
    comment(lp, "Ananya", "Thank you for the carry forward change! 🙌")

    news = announcements.create(db, owner, {
        "kind": "news", "title": "We are now 500 customers strong",
        "summary": "Thank you, everyone. Here is what got us here and what comes next.",
        "body_html": ("<p>This week we welcomed our <strong>500th customer</strong>, a hospital group in Pune. "
                      "Every team played a part, from sales and onboarding to support and engineering.</p>"
                      "<h3>What's next</h3><p>Two new offices in Hyderabad and Kochi early next year.</p>"),
        "cover": {"preset": "festive"}, "pinned": False, "audience": {"all": True}, "action": "publish"})
    news.published_at = news.publish_at = _at(4.5)
    np_ = db.query(BuzzPost).filter(BuzzPost.announcement_id == news.id).first()
    np_.created_at = news.published_at
    react(np_, ["🎉", "🚀", "👏", "💯", "🎉", "🔥"])

    # ── eNPS survey, quarterly: last quarter closed, this quarter open with answers ──
    s = surveys.create(db, A("Priya"), {
        "kind": "enps", "title": "Quarterly eNPS", "identity": "anonymous", "recurrence": "quarterly",
        "duration_days": 14, "description": "Two quick questions. Your answers are anonymous.",
        "thank_you": "Thank you! We share the results at the next town hall.", "audience": {"all": True}})
    prev_open = datetime.now(timezone.utc) - timedelta(days=96)
    run1 = SurveyRun(company_id=cid, survey_id=s.id, number=1, opens_at=prev_open, closes_at=prev_open + timedelta(days=14),
                     status="closed", invited_count=22)
    run2 = SurveyRun(company_id=cid, survey_id=s.id, number=2, opens_at=datetime.now(timezone.utc) - timedelta(days=5),
                     closes_at=datetime.now(timezone.utc) + timedelta(days=9), status="scheduled")
    s.runs = [run1, run2]
    db.flush()
    surveys.open_run(db, s, run2)
    enps_q, why_q = s.questions[0], s.questions[1]
    good = ["Great team and a manager who listens.", "I learn something new every week.",
            "Flexible hours make a big difference for my family.", "The work matters and people care."]
    mid = ["Good place, but appraisals feel slow.", "Would like clearer career paths."]
    bad = ["Too many meetings, not enough deep work.", "Salary is behind the market for my role."]

    def answer(run, scores, who):
        for i, sc in enumerate(scores):
            r = SurveyResponse(company_id=cid, run_id=run.id, submitted_on=(run.opens_at + timedelta(days=i % 5)).date(),
                               department_id=who[i % len(who)].department_id)
            why = rnd.choice(good if sc >= 9 else mid if sc >= 7 else bad)
            r.answers = [SurveyAnswer(company_id=cid, question_id=enps_q.id, value_number=sc)] + (
                [SurveyAnswer(company_id=cid, question_id=why_q.id, value_text=why)] if i % 3 != 2 else [])
            db.add(r)

    responders = [e for n, e in p.items() if n not in ("Priya", "Tanvi", "Aditya")][:16]
    answer(run1, [9, 7, 6, 10, 8, 5, 9, 7, 6, 8, 9, 4, 7, 9], responders)
    answer(run2, [10, 9, 8, 9, 7, 10, 6, 9, 8, 10, 9, 7, 9, 10, 8, 6][:len(responders)], responders)
    for e in responders:
        part = db.query(SurveyParticipation).filter(SurveyParticipation.run_id == run2.id,
                                                    SurveyParticipation.employee_id == e.id).first()
        if part:
            part.status, part.submitted_on = "submitted", today - timedelta(days=rnd.randint(0, 4))
    sp = db.query(BuzzPost).filter(BuzzPost.survey_id == s.id).first()
    if sp is not None:
        sp.created_at = run2.opens_at
        react(sp, ["👍", "🙌"])

    # a mandatory pulse check with only a few answers so far (results stay hidden)
    pulse = surveys.create(db, A("Priya"), {
        "kind": "pulse", "title": "Hybrid work check-in", "identity": "optional", "duration_days": 10,
        "description": "Help us shape how we work from office and from home.", "audience": {"all": True},
        "mandatory": True, "reminder_every_days": 2,
        "questions": [
            {"qtype": "scale", "text": "I can do my best work on the days I am in the office.", "settings": {"points": 5}},
            {"qtype": "rating", "text": "How would you rate our office facilities?", "settings": {"max": 5}},
            {"qtype": "multi", "text": "Which days suit you best for the office?",
             "options": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]},
            {"qtype": "single", "text": "How do you usually travel to work?",
             "options": ["Metro", "Bus", "Own vehicle", "Cab", "Walk or cycle"]},
            {"qtype": "text", "text": "One thing that would make office days better?", "required": False},
        ], "action": "publish"})
    for name, ans in (("Ananya", [4, 4, ["Tuesday", "Wednesday"], "Metro", "Quieter focus rooms"]),
                      ("Karthik", [3, 3, ["Wednesday", "Thursday"], "Own vehicle", "Better coffee ☕"]),
                      ("Rahul", [5, 4, ["Tuesday", "Thursday"], "Cab", ""])):
        qs = pulse.questions
        surveys.submit(db, A(name), pulse, {str(qs[i].uuid): v for i, v in enumerate(ans)}, show_name=name == "Rahul")
    pp = db.query(BuzzPost).filter(BuzzPost.survey_id == pulse.id).first()
    if pp is not None:
        pp.created_at = _at(0.8)

    row.config = {**(row.config or {}), "demo": {"seeded": True, "at": datetime.now(timezone.utc).isoformat()}}
    flag_modified(row, "config")
    db.flush()
    print("[seed] Engagement demo content added")
