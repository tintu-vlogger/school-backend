# Campusly progress

Campusly runs any educational institution (school, preschool, college, university, coaching or
training centre) as a multi-tenant SaaS. The staff, HR, payroll, approvals, notifications and
platform features come from the base platform (history in `PLATFORM-HISTORY.md`).

## Foundation (academics, students, portal sign-in)
- Institution profile in `companies.config["institution"]`: type, board, the words used on
  screen (class/programme/course, section/batch, teacher/faculty …), modules switched on,
  working days, attendance mode (daily / period / session) and default grading.
- Academic structure (`modules/academics`): sessions, terms, programs, sections, subjects,
  curriculum, teacher allocation, rooms, houses; quick set-up endpoint.
- Students (`modules/students`): students, guardians, enrolment per session, promotion,
  documents, bulk import, portal accounts for parents and students.
- Parent / student sign-in through `portal_accounts`; portal tokens may call only API prefixes
  registered with `register_portal_prefix`.
- Roles: admin, principal, hr, manager, teacher, accountant, librarian, employee, parent, student.

## Academics and Students front end
Screens for the foundation modules, plus a few small endpoints the screens needed (inside
`modules/academics` and `modules/students` only; existing behaviour kept). No new tables, so no
Alembic revision.

**Pages (web)**
- `/dashboard/academics`: classes/programmes/courses with their sections per session (session
  switcher), strength vs capacity, class teacher, room, medium, shift, next class for promotion;
  add/edit program and section modals; "Quick set-up" modal (`POST /academics/quick-setup`) with
  lists prefilled per institution type, shown as the empty state of a new institution.
- `/dashboard/academics/subjects` (kind, code, credits, colour, department),
  `/curriculum` (per program, grouped by term number for term-based programmes, electives with
  groups, credit and period totals), `/allocation` (per-section grid: teacher, assistants,
  weekly periods, "Apply curriculum"; Workload tab), `/sessions` (sessions, make current,
  terms), `/rooms` (Rooms / Houses tabs).
- `/dashboard/students`: directory with stats strip, filters (class, section, status, house,
  gender), search, server paging; Add student (Stepper: personal, enrolment, guardians with
  sibling link to an existing guardian, medical + custom fields, create portal sign-ins; new
  logins shown once with copy buttons and CSV); detail modal with tabs (profile, enrolment
  history + move section, guardians add/remove, documents upload via `/documents/uploads`,
  portal sign-ins with create/reset/switch off, status change); bulk import (CSV or XLSX read
  in the browser, template download, dry run preview, import); CSV export.
- `/dashboard/students/guardians` (children, sign-in status, edit contact, create/reset sign-in),
  `/promotion` (source session/section, target defaults to the program's next class with the
  same section name, tick learners, promoted/detained/completed, confirm, result),
  `/id-cards` (printable A4 sheet of CR80 cards: photo, name, class, admission no, blood group,
  guardian phone, QR of admission no, logo and name), `/accounts` (portal sign-ins: filters,
  switch on/off incl. bulk, reset password, create for a whole section with CSV download).
- `/dashboard/settings/institution`: type, board, affiliation, head, year, motto, words used on
  screen (only overrides of the type's defaults are saved), modules with descriptions, working
  days, attendance mode, default grading; saves with `PUT /academics/profile` then
  `useInstitution().reload()`.
- Portal `/dashboard/my/profile`: child switcher (`/students/mine`), profile, guardians,
  documents (read-only), change own password (`POST /auth/change-password`).
- Home cards: `frontend/src/components/students/home-cards.tsx` exports `StaffHomeCard`
  (active, new this month, by-class mini chart, gender split; `/students/stats`) and
  `PortalHomeCard({ studentId })` (photo, class, roll, class teacher;
  `/students/portal/summary`).
- Shared front-end helpers: `components/academics/shared.ts`, `components/academics/ui.tsx`,
  `components/students/{shared,student-form,student-detail,import-modal,sheet,home-cards}.tsx`.

**Endpoints added**
- `GET /students` now also takes `house_id`, `gender` and returns `guardian_name`,
  `guardian_phone`, `blood_group` per row.
- `GET /students/export` (students.view): filtered list as CSV, audited `export.downloaded`.
- `GET /students/portal/summary?student_id=` (parent/student for own children, staff with
  students.view): home card data.
- `GET /students/section/{id}/id-cards` (students.view).
- `POST /students/section/{id}/portal-accounts` (students.portal.manage): sign-ins for every
  learner and guardian of a section, new passwords returned once; audited.
- `GET /students/{uuid}` adds `class_teacher_name`, `house_color`, `session_name`, and for staff
  each guardian's `portal_account`.
- Student documents accept `upload_uuid` (a file from `/documents/uploads`); stored as
  `upload:<uuid>` and given fresh signed links when listed (signed links expire, so the
  short-lived URL is never stored).
- `GET /portal-accounts` takes `active`, searches names and admission numbers, returns the
  children of each parent.
- `GET /academics/workload?session_id=`: weekly periods per teacher (assistants separate),
  class-teacher roles and line items, plus the count of subjects without a teacher.

**Tests**: `tests/test_students_ui_api.py` (bulk sign-ins created once, parent cannot open
another family's child via detail, summary or documents, portal blocked from staff lists,
directory filters, export, ID cards, uploaded documents, workload, other institution cannot use
a section).

**Decisions**
- Student photos are resized in the browser to a 320px square JPEG data URL and saved in
  `photo_url` (same storage approach as staff photos), no extra endpoint.
- Gender, blood group and relation use fixed lists because the student columns store text, not
  master ids.
- XLSX is read with a small built-in reader (browser `DecompressionStream`), no new package;
  the template is CSV. Dates accept YYYY-MM-DD, DD/MM/YYYY or Excel serial numbers.
- Custom fields on students use the `student` entity (cast in the UI; the shared
  `CustomFieldEntity` type does not list it yet).

**Not done / follow-ups**
- Curriculum rows are ordered by a number, not drag and drop.
- ID cards are a print-friendly page (no PDF).
- While this was built, the app could not configure its models because two class names were
  each defined in two modules: `Question` (lms and learning) and `Application` (admissions and
  recruitment). The tests above and `tests/test_tenant_isolation.py` pass when run with a local
  helper that points those string relationships at the class in their own module; the owners
  of those modules need to write the module path in full (e.g.
  `relationship("modules.lms.models.Question")`). The pages were checked with the type checker
  only, not in a browser, because the shared API could not sign anyone in while that conflict
  stood.

## Communication (comms)

Module `backend/modules/comms/` (prefix `/comms`, portal `/comms/portal`), nav module key `comms`.
Alembic revision: **`cm1a2b3c4d5e`** (down_revision `sc1b2c3d4e5f`). Home cards:
**`StaffHomeCard`** and **`PortalHomeCard({ studentId })`** in `frontend/src/components/comms/home-cards.tsx`.

**What is built**
- Notices & circulars: title, markdown body, attachments (up to 10), category (general, academic,
  event, holiday, fee, exam, emergency), audience builder (everyone; all staff; staff by
  department / role / person; parents and/or students: all, by program, by section, or chosen
  learners), draft, send now or schedule (background job publishes), expiry, pin, require
  acknowledgement, duplicate, archive. Delivery: in-app (`notify` / `notify_portal`) always; email
  through the platform delivery log (`notification_deliveries`, retried, logged in development);
  SMS / WhatsApp through the Settings > Integrations connections (skipped with a note when not
  set up; Meta Cloud API WhatsApp and MSG91 SMS are sent in production, other gateways are logged
  as failed "not available yet"). Emergency notices also use the institution's emergency channels.
  Recipients are materialised per person (`e:` staff, `g:` guardian, `s:` learner) so read
  receipts and acknowledgements are tracked per recipient, including guardians without a sign-in
  (reachable by email/SMS). "Send again to unread" (or unacknowledged) re-delivers.
  Teachers (`comms.notices.section`) can send only to families of their own sections.
- Messages: threads always have two sides, one staff member (or the shared office inbox) and one
  parent or learner; no learner-to-learner or parent-to-parent path exists. Parents can write to
  their child's class teacher and subject teachers (teacher allocation; subject teachers can be
  switched off) and the office; learners likewise when the setting allows; teachers write to
  families of learners in their sections; `comms.messages.all` writes to anyone. Attachments,
  unread counts, mark read, close / reopen (staff reply reopens; families start a new one), class
  message (broadcast to all parents of a section, one private thread per family), office hours
  warning, safeguarding view (`comms.messages.audit`, read-only), every message kept.
- Events calendar: events, holidays, exams, PTMs, sports, trips, meetings, cultural; all-day or
  timed (stored UTC, calendar dates in the institution timezone), location, audience like notices,
  RSVP with guests and deadline, cancel / restore, optional in-app announcement, private iCal feed
  per person (signed URL, no sign-in) for staff and families. Month / week / agenda views (phones
  get the agenda).
- Parent-teacher meetings: meeting day for chosen sections, times, slot length, gap, breaks,
  in person / online / parent chooses, location and link; teachers default to the class and
  subject teachers of the sections; slots generated (booked slots kept when times change); open
  bookings (adds a calendar event and notifies parents and teachers); parents book one slot per
  teacher per child from the portal (only teachers of their child's section; no two meetings at
  the same time; partial unique indexes stop double booking at database level); cancel; teacher
  blocks slots, records met / no-show, notes, action points and whether parents see them;
  reminders 24 hours before (background job).
- Daily class diary: one note per section per day by its teachers, with attachments; families
  see it in the portal and get an in-app note for today's entry.
- Settings (`comms.settings.manage`): office hours, parents / students may message, subject
  teachers messageable, office inbox, default and emergency channels, diary on/off.

**Pages**: staff `/dashboard/comms/notices` (For me / Sent or All, receipts, settings modal),
`/dashboard/comms/messages` (inbox: thread list + conversation), `/dashboard/comms/events`,
`/dashboard/comms/ptm`, `/dashboard/comms/diary` (new nav entry). Portal `/dashboard/my/notices`
(acknowledge), `/dashboard/my/messages` (now parents and students), `/dashboard/my/events`
(RSVP, subscribe), `/dashboard/my/meetings` (book), `/dashboard/my/diary` (new nav entry).

**Endpoints** (all under `/api/v1/comms`)
- `GET/PUT /settings`, `GET /meta`, `POST /uploads`, `GET /unread`, `GET /summary`
- Notices: `POST /audience/preview`, `GET /notices`, `GET /notices/inbox`, `POST /notices`,
  `GET/PUT /notices/{uuid}`, `POST /notices/{uuid}/publish|unschedule|archive|pin|duplicate|resend|read|acknowledge`,
  `GET /notices/{uuid}/receipts`
- Messages: `GET /messages/boxes`, `GET /messages/contacts`, `GET/POST /messages/threads`,
  `GET /messages/threads/{uuid}`, `POST /messages/threads/{uuid}/messages|read|status`,
  `GET/POST /messages/broadcasts`
- Events: `GET/POST /events`, `GET /events/feed-link`, `GET/PUT/DELETE /events/{uuid}`,
  `POST /events/{uuid}/cancel|rsvp`, `GET /events/{uuid}/rsvps`, `GET /ical/{company}/{key}/{sig}.ics`
- PTM: `GET/POST /ptm/days`, `GET/PUT /ptm/days/{uuid}`, `POST /ptm/days/{uuid}/teachers|status`,
  `GET /ptm/suggest-teachers`, `PUT /ptm/teachers/{uuid}`, `GET /ptm/teachers/{uuid}/schedule`,
  `POST /ptm/slots/{uuid}/block`, `PUT /ptm/bookings/{uuid}`, `POST /ptm/bookings/{uuid}/cancel`, `GET /ptm/mine`
- Diary: `GET /diary/sections`, `GET /diary`, `PUT /diary`
- Portal: `POST /portal/uploads`, `GET /portal/notices`, `GET /portal/notices/{uuid}` (marks read),
  `POST /portal/notices/{uuid}/acknowledge`, `GET /portal/messages/contacts`,
  `GET/POST /portal/messages/threads`, `GET /portal/messages/threads/{uuid}`,
  `POST /portal/messages/threads/{uuid}/messages|read`, `GET /portal/events`,
  `GET /portal/events/feed-link`, `POST /portal/events/{uuid}/rsvp`, `GET /portal/ptm`,
  `POST /portal/ptm/book`, `POST /portal/ptm/bookings/{uuid}/cancel`, `GET /portal/diary`,
  `GET /portal/unread`, `GET /portal/summary?student_id=`

**Tables** (all with `company_id`, company-first index, forced RLS): `comms_settings`,
`comms_notices`, `comms_notice_recipients`, `comms_broadcasts`, `comms_threads`, `comms_messages`,
`comms_events`, `comms_event_rsvps`, `comms_ptm_days`, `comms_ptm_teachers`, `comms_ptm_slots`,
`comms_ptm_bookings`, `comms_diary_entries`.

**Permissions**: `comms.view`, `comms.notices.manage`, `comms.notices.section`,
`comms.messages.broadcast`, `comms.messages.all`, `comms.messages.office`, `comms.messages.audit`,
`comms.events.manage`, `comms.ptm.manage`, `comms.diary.post`, `comms.settings.manage`,
`comms.portal` (parent, student). Principal gets all; hr: view, notices, office inbox, events;
teacher / manager: view, section notices, broadcast, diary; other staff: view.

**Background jobs** (`core.periodic`): `comms.scheduled_notices`, `comms.ptm_reminders`.

**Demo data**: 8 notices (3 to Grade 8 - A parents by Divya Menon, holiday, fee, exam, staff
meeting, an expired emergency notice; some read/acknowledged by parent@test.com), 3 threads
between parent@test.com and Divya Menon, 10 calendar events this month and next (plus the PTM
event), an open Grade 8 PTM day with slots for all class and subject teachers and one booking by
parent@test.com with Divya, 4 diary notes for Grade 8 - A.

**Tests**: `tests/test_comms.py` (8): section-parent audience (other section, students and other
parents don't get it), read receipts / acknowledgement / resend, teacher notice scope, parent may
message only staff linked to their children (and the office), audit view read-only, teacher can't
message other sections' families, students can't message students and the students-messaging
switch, PTM slot generation with a break, double booking refused (API and database), event
audience + RSVP + iCal feed signature, tenant isolation. `tests/test_tenant_isolation.py` passes.

**Decisions**
- Audiences are resolved once at publish time into recipient rows (people who join a section
  later don't get old notices); events are filtered per viewer at read time instead.
- Staff see the whole institution calendar; families see events whose audience includes them.
- Staff-to-staff messaging is left to the existing Buzz / announcements; threads are staff-family only.
- The office inbox is shared by everyone with `comms.messages.office` (and admins); office
  staff get an in-app notification for new messages.
- Attachments reuse the documents file store; portal users upload through `/comms/portal/uploads`
  because `/documents` is closed to portal tokens.
- Read receipts come from opening a notice (portal detail endpoint, staff `read`), not from
  email opens.

**Not done / follow-ups**
- SMS/WhatsApp sending supports only Meta Cloud API and MSG91 (DLT templates are not managed);
  other gateways need adapters in `modules/comms/channels.py`.
- No real-time push for messages (the inbox refreshes every 30 seconds).
- Pages were checked with the type checker and lint; the shared API on :8200 did not have this
  module loaded yet, so no browser run. The API was tried on its own copy (port 8207) with the
  demo logins.

## Admissions and certificates (module `admissions`)

**Built**: enquiry pipeline (leads), admission applications (office and online), entrance test and
interview scheduling with scores, merit list with seats per program, offers through the workflow
engine, offer letters (PDF), conversion to a student, a public online application form with a
status check on the institution's own address, admission settings, and certificates (templates
with merge fields, numbered issue register, cancel with reason, PDF).

**Pages**
- `/dashboard/admissions`: dashboard (enquiries by stage and source, applications by status, seats
  filled per program, enquiry and application conversion, follow-ups due, tests and interviews).
- `/dashboard/admissions/enquiries`: kanban board (drag a card to move it; Lost asks for a reason,
  Visit scheduled for a date) and list view (bulk assign counsellor, archived filter). Detail modal
  with activity log (call / email / meeting / note + next follow-up), counsellor, stage change,
  "Start application". Deep links `?view=<uuid>`, `?new=1`.
- `/dashboard/admissions/applications`: list (status, program, via, fee filters) and Merit list tab
  (seats taken/open, Rank by score, Offer seats from the top N with optional waitlist). Detail modal
  with tabs Overview / Documents (upload, verify) / Test and interview / Activity and actions:
  change status, schedule, scores, fee, Make offer, Mark accepted, Enrol (section, roll, admission
  no., create sign-ins; shows the new passwords once), Offer letter PDF.
- `/dashboard/admissions/settings`: programs open per session (seats, fee, fee-before-offer, online
  form dates, test/interview flags, age limits), online form text and privacy notice, required
  documents, acknowledgement and offer emails, offer letter HTML, offer validity, merit weights.
- `/dashboard/certificates`: issue register (filters, view, PDF, cancel with reason) and templates
  (HTML editor with merge-field palette, fields asked at issue, paper/font/letterhead, "mark the
  student transferred/left", live preview). Issue modal with live preview per student.
- Public `/apply` (tenant sub-domain, no sign-in): pick a program, child and parent details,
  extra fields (custom fields entity `admission`), document uploads, consent, honeypot; shows the
  application number. "Check status" tab (application number + phone or email). When nothing is
  open it shows a short enquiry form.
- Nav (Students section): Admissions (exactOnly), Enquiries, Applications, Admission settings,
  Certificates (`admissions.certificates.view` / `.manage`), all `module: "admissions"`.
- Home card: `frontend/src/components/admissions/home-cards.tsx` exports `StaffHomeCard` (open
  enquiries + follow-ups due, applications this month, conversion) and `PortalHomeCard` (returns null).

**Endpoints** (all under `/api/v1`)
- `/admissions/settings` GET/PUT; `/admissions/offerings` GET/POST, `/{uuid}` PUT/DELETE (close);
  `/admissions/meta`, `/admissions/dashboard?session_id=`, `/admissions/summary`.
- `/admissions/enquiries` GET (q, stage, source_id, program_id, session_id, counsellor_id,
  follow_up=due|today|overdue|none, mine, archived; paginated) / POST; `/board`; `/bulk-assign`;
  `/{uuid}` GET/PUT/DELETE (archive); `/{uuid}/restore`, `/stage`, `/assign`, `/activities`,
  `/application` (start an application from the enquiry).
- `/admissions/applications` GET (q, status, program_id, session_id, channel, fee_paid, sort=merit)
  / POST; `/{uuid}` GET/PUT/DELETE (archive); `/{uuid}/status`, `/notes`, `/schedule`, `/scores`,
  `/fee`, `/documents/{key}` (attach an upload), `/documents/{key}/verify`, `/offer`, `/accept`,
  `/enrol`, `/offer-letter.pdf`.
- `/admissions/merit?session_id&program_id`, `/admissions/merit/rank`, `/admissions/merit/offer`.
- `/certificates/fields`, `/certificates/templates` GET/POST, `/{uuid}` GET/PUT/DELETE (switch off),
  `/{uuid}/preview`, `/certificates/preview` (unsaved draft), `/certificates/issued` GET/POST,
  `/issued/summary`, `/issued/{uuid}` GET, `/issued/{uuid}/pdf`, `/issued/{uuid}/cancel`.
- Public (no auth, company from the slug in the path, rate-limited in Redis by IP and phone):
  `GET /public/admissions/{slug}/form`, `POST /public/admissions/{slug}/apply` (multipart: `data`
  JSON + `doc_<key>` files, PDF/images up to 5 MB), `POST /public/admissions/{slug}/enquiry`,
  `POST /public/admissions/{slug}/status` (number + matching phone/email; a mismatch returns the
  same 404 as an unknown number; the answer carries no contact details or guardians).

**Tables** (Alembic revision `ad1a2b3c4d5e`, down_revision `sc1b2c3d4e5f`; all with company_id,
company-first indexes and RLS): `admission_settings`, `admission_offerings`, `admission_enquiries`,
`admission_applications`, `admission_activities` (append-only log), `certificate_templates`,
`certificates`.

**Permissions**: `admissions.view`, `admissions.manage`, `admissions.enrol`,
`admissions.settings.manage`, `admissions.certificates.view`, `admissions.certificates.manage`.
Defaults: principal and hr all; accountant view; manager view + certificates view.
**Number series**: `enquiry` (ENQ{YY}{SEQ:4}, yearly), `admission_application`
(APP{YYYY}{SEQ:4}, yearly); certificates use the existing `certificate` series.
**Masters** appended to `core/master_seed.py`: `enquiry_source` (walk-in, website, phone, campaign,
referral, social media, other) and `enquiry_lost_reason`.
**Workflow**: request type `admissions.offer` with no default steps (approved at once when no
flow is set up); a pending offer holds a seat; approval re-checks seats.

**Rules**
- Enquiry stages: `converted` only through enrolment (final); `applied` only when an application
  exists (set automatically); `lost` needs a reason and reopens only to new/contacted;
  `visit_scheduled` needs a visit time; the first call/meeting moves a new enquiry to contacted.
- Application statuses move along an allowed map (see `service.ALLOWED`); `offered` only via Make
  offer, `enrolled` only via Enrol; reject/withdraw need a reason (and cancel a pending offer request).
- Seats taken = offered + accepted + enrolled + offers awaiting approval; an offer beyond the seats
  is refused. Optional "fee must be paid before an offer" per program. Age limits checked on the
  "age as on" date (default session start).
- Merit score = test % and interview % combined by the weights in settings (one alone counts
  alone); rank orders by merit score, test %, then submission time.
- Enrol calls `modules.students.router.create_student(db, company_id, StudentCreate(...))` with the
  guardians, enrols in a section of the same program and session, optionally creates portal
  sign-ins, copies uploaded documents to the student's documents (links signed for five years),
  marks the enquiry converted, audits `admissions.enrolled`.
- Certificates: never deleted; cancel keeps the row with reason/by/time and the PDF is stamped
  CANCELLED. Templates with `marks_student` offer to mark the student transferred/left; this makes
  the same changes as the students module's status endpoint (student status, left_on, reason,
  current enrolment transferred/dropped, portal sign-ins off) and audits `students.status_changed`
  with `via: certificate`. Cancelling does not undo the status change (said on screen).

**Seeds**: `setup_company` adds four certificate templates (transfer, bonafide, character, study).
Demo (`seed_demo`): seven programs open for 2026-27 with seats/fees/age limits, 30 enquiries across
all stages with counsellors and call logs, 20 applications across statuses (tests, interviews,
scores, offers, 1 accepted, 2 enrolled as students), merit ranks, two certificates issued
(bonafide and character). It calls the students demo first (idempotent) because `admissions`
sorts before `students`.

**Tests**: `tests/test_admissions.py` (12): enquiry pipeline rules, first call moves to contacted +
board, application status rules, merit ranking and seat limit (offer top N, waitlist, seat frees
after withdrawal), fee before offer, offer approval flow holding a seat then approving, conversion
creates student + guardians + enrolment + portal sign-ins and converts the enquiry, public
application without sign-in (documents, duplicate, other institution refused, status check needs
the right contact and institution), public enquiry, certificates (TC marks transferred, register,
PDF, no delete, cancel), permissions, tenant isolation. `tests/test_tenant_isolation.py` passes.

**Decisions**
- Admission settings live in their own table (one row) instead of `companies.config`.
- Offer letters use the module's own HTML template (settings) rendered with the documents merge
  and PDF helpers on the letterhead, not the HR letter templates (those are employee-centred).
- The status check is a POST (number + contact) so nothing sits in URLs or logs.
- Money in API responses is a string with two decimals.
- Rate limits: apply 10/hour per IP and 5/day per phone, enquiry 10/hour per IP, status 20 per
  15 minutes per IP (Redis; steps aside if Redis is down).
- The application custom fields entity `admission` is passed to the shared `CustomFields`
  component with a cast; `custom-fields.tsx`'s `CustomFieldEntity` type could add "admission".

**Not done / follow-ups**
- Online payment of the application fee (fees module) — the fee is recorded by the office.
- No parent portal view of applications (families use the public status check).
- Emails go through `core.email.send_email` (prints when SMTP is not configured); no SMS.
- Pages were checked with tsc/eslint and in a headless browser against my own API copy (port 8202).

## Teaching & learning (LMS) and online classes (lms)

**Built**: homework and assignments (multi-section, online / on-paper hand-in, late rules,
grading with feedback, return, "send again"), study materials (file, link, YouTube video, note;
by program, subject and syllabus topic; per-section visibility; view counts and viewer list),
live online classes (one-off or weekly series; built-in Jitsi Meet room via the iframe API on
meet.jit.si or the institution's own Jitsi server, or Zoom / Google Meet / Teams / other links;
join log as attendance; recording link; "starting soon" reminders; auto-close of stale live
classes), quizzes and tests (question bank with single / multiple choice, true/false, short answer
with accepted answers, numeric with tolerance, long answer; difficulty, topic, tags; fixed or
random-from-bank quizzes; time limit, attempts, shuffle, open/close window, answers shown after
submit / after close / never; learner attempt with timer and autosave, auto-submit when time runs
out, auto-marking with negative marks, manual marking of long answers, results and item analysis),
syllabus (units and topics per program + subject, term-wise), topic coverage per section,
weekly lesson plans (objectives, activities, resources, assessment, homework link, reflection,
"mark topics covered") and coverage % per section + subject.

**Pages** (staff): `/dashboard/lms/homework` (`?view=<uuid>`), `/dashboard/lms/materials`,
`/dashboard/lms/online-classes` (`?view=<uuid>`) and `/dashboard/lms/online-classes/<uuid>/join`,
`/dashboard/lms/quizzes` (Quizzes + Question bank tabs), `/dashboard/lms/lesson-plans`
(Lesson plans + Syllabus + Coverage tabs), `/dashboard/lms/settings` (new nav entry under
Settings, `lms.settings.manage`, module `lms`). Portal: `/dashboard/my/homework`,
`/dashboard/my/classes` and `/dashboard/my/classes/<uuid>/join`, `/dashboard/my/materials`,
`/dashboard/my/quizzes`. `intro`/`features` removed from the LMS nav entries.
Components in `frontend/src/components/lms/` (`lib.ts`, `shared.tsx`, `material-preview.tsx`,
`video-room.tsx`, `home-cards.tsx`).

**Home cards**: `StaffHomeCard` and `PortalHomeCard({ studentId })` from
`frontend/src/components/lms/home-cards.tsx`, backed by `GET /lms/summary` and
`GET /lms/portal/summary?student_id=`.

**Endpoints** (`/api/v1/lms`): `options`, `settings` (GET/PUT), `uploads`, `summary`;
`homework` (list/create/get/put/delete), `homework/{uuid}/submissions`,
`homework/{uuid}/submissions/{student_uuid}/grade|resubmit`, `homework/{uuid}/return`;
`materials` (CRUD), `materials/{uuid}/views`; `online-classes` (`when=today|upcoming|past|all`,
CRUD), `online-classes/{uuid}/cancel|end|join|attendance`; `questions` (CRUD),
`questions/facets`; `quizzes` (CRUD), `quizzes/{uuid}/results`, `quiz-attempts/{uuid}` and
`/mark`; `syllabus` (GET), `syllabus/units` (POST/PUT/DELETE), `syllabus/units/{uuid}/topics`,
`syllabus/topics/{uuid}` (PUT/DELETE), `syllabus/topics/{uuid}/coverage`, `coverage`;
`lesson-plans` (CRUD). Portal (`/lms/portal`, registered with `register_portal_prefix`):
`summary`, `subjects`, `uploads` (students), `homework`, `homework/{uuid}`,
`homework/{uuid}/submit` (students only; the learner comes from the token), `materials`,
`materials/{uuid}` (records a view), `online-classes`, `online-classes/{uuid}`,
`online-classes/{uuid}/join`, `quizzes`, `quizzes/{uuid}/start`, `attempts/{uuid}` (GET),
`attempts/{uuid}/answers` (PUT autosave), `attempts/{uuid}/submit`.

**Tables** (all with `company_id`, company-first index and forced RLS): `lms_settings`,
`lms_assignments`, `lms_assignment_sections`, `lms_submissions`, `lms_materials`,
`lms_material_sections`, `lms_material_views`, `lms_live_classes`, `lms_live_class_joins`,
`lms_questions`, `lms_quizzes`, `lms_quiz_sections`, `lms_quiz_questions`, `lms_quiz_attempts`,
`lms_syllabus_units`, `lms_syllabus_topics`, `lms_topic_coverage`, `lms_lesson_plans`.
Alembic revision **`lm1a2b3c4d5e`** (down_revision `sc1b2c3d4e5f`).

**Permissions**: `lms.view`, `lms.teach`, `lms.manage`, `lms.settings.manage`. Defaults:
principal all four; teacher and manager `lms.view` + `lms.teach`; hr `lms.view`. Portal access
is by `require_portal` plus `visible_student_ids` checks (no portal permission keys).

**Background job**: `lms.reminders` (every 15 minutes): "starting soon" notifications to learners,
parents and the teacher (lead time from settings, at least 15 minutes because of the job
interval), closes live classes an hour past their end, submits quiz attempts whose time ran out.

**Notifications**: new homework (learners + parents, setting), work returned / send again,
new live class / cancelled / recording ready, starting soon, quiz published, quiz marked; the
teacher gets a grouped "N submissions to check" notification.

**Demo data**: Grade 8 - A Computer Science homework by Divya Menon (flowchart: Aarav submitted,
others submitted/returned; binary worksheet: pending for Aarav; an on-paper project already
marked and returned), a Mathematics homework for Grade 8 - A, Divya's homework for Grade 7 - A and
Grade 9 - A, 5 materials (file, YouTube video, note, links), two built-in video classes for
Grade 8 - A (today about two hours after seeding, tomorrow 10:00) plus a past class with
attendance and a recording, a 10-question "Computer basics quiz" (all six question kinds,
three classmates' attempts, one hand-marked), and the Grade 8 Computer Science syllabus
(5 units, 16 topics, 7 covered in 8-A and 4 in 8-B) with three weekly lesson plans.
`seed_demo` first runs the students demo seed (modules seed in name order and `lms` comes
before `students`).

**Tests**: `tests/test_lms.py` (11): marking of single / true-false / multiple choice (exact
set) / numeric tolerance / short answer / long answer, question validation, teacher limited to
own allocation, submission rules (empty, re-send before marking, max marks, no re-send after
return, send-again), late work only when allowed and no online hand-in for on-paper work,
another section's learner and parent cannot see or submit, parents cannot submit, students blocked
from staff endpoints, quiz attempts (other section refused, resume, attempt limit, auto-marking,
manual marking of a long answer, results + item analysis), live class join log and weekly
series, tenant isolation. `tests/test_tenant_isolation.py` passes.

**Decisions**
- Each live class occurrence is its own row; a weekly schedule creates up to 60 rows sharing
  `series_uuid` (cancel one or the rest of the series).
- Built-in rooms use a 96-bit random room name; the teacher's join marks the class live.
  Learners can join from `join_early_minutes` before the start until 30 minutes after the end;
  parents join only when `parents_can_join` is on. No Jitsi JWT/moderator auth (meet.jit.si does
  not support it); institutions wanting locked rooms should run their own Jitsi server.
- Learners can't open `/dashboard/lms/...` (portal route guard), so the learner join page is
  `/dashboard/my/classes/<uuid>/join`; staff use `/dashboard/lms/online-classes/<uuid>/join`.
- Teachers set work only for section + subject pairs in their teacher allocation; `lms.manage`
  can set work anywhere. Teachers see items for the sections they teach or lead (plus their own).
- Marks and feedback are visible to families only after the work is returned ("Return now" is
  on by default when grading).
- Multiple choice is all-or-nothing; negative marks apply to wrong, non-blank answers; an attempt
  never scores below zero. Random quizzes draw per attempt and freeze the paper at start.
- Files use the documents file store (`stored_files`, signed links); portal uploads go through
  `/lms/portal/uploads` because `/documents` is closed to portal tokens.
- The bank class is `BankQuestion` (not `Question`) because the HR learning module already maps a
  `Question` class; LMS relationships use module-qualified names.

**Not done / follow-ups**
- No plagiarism check, rubric marking or peer review; no bulk download of submissions.
- Recordings are links only (no recording storage or Jitsi recording integration).
- No Zoom/Meet/Teams API integration (links are pasted); attendance for external providers is
  "opened the link from Campusly".
- Pages were checked with the type checker and lint; the shared API on :8200 did not have this
  module loaded, so no browser run. The API was tried on its own copy (port 8205) with the demo
  logins (Divya, Kavitha, student@test.com, parent@test.com).

## Student attendance (module `student_attendance`) and Timetable (module `timetable`)

### Student attendance
API prefix `/api/v1/student-attendance` (`/attendance` is staff attendance). Permission prefix
`attendance.`, nav module key `attendance`. Alembic revision **`sa1a2b3c4d5e`** (down `sc1b2c3d4e5f`).

**Tables** (all with `company_id`, company-first index, forced RLS): `student_attendance_statuses`,
`student_attendance_registers`, `student_attendance_marks`, `student_leave_requests`,
`student_calendar_days`, `student_attendance_corrections`, `student_attendance_alerts` (send log,
not audited), `student_attendance_settings`.

**Rules**
- Mode from the institution profile `attendance_mode`: `daily` (one register a day, or morning and
  afternoon when the setting `daily_sessions` = 2; profile value `session` also means two) or
  `period` (one register per timetable period, from `timetable.service.section_day`, including
  substitutions and skipping cancelled periods; subject teacher, substitute or allocation teacher
  marks; electives only list learners who chose the subject).
- Statuses seeded per institution: P present 1, A absent 0 (alert), L late 1 (minutes), HD half day
  0.5, EX leave 0, ML medical leave 0, OD on duty 1. Each has `counts_as`, `in_total`, `is_absent`,
  `is_late`, `is_leave`; editable, own statuses can be added.
- Percentage = sum(counts_as) / count(marks that count in total) x 100; two sessions or periods are
  units, so one formula fits every mode (`service.compute_percentage`).
- Working days: profile `week_days`, minus non-optional holidays of the default staff holiday
  calendar (setting `use_staff_holidays`), with the institution's own calendar days on top
  (holiday / extra working day, for everyone or some programs). Marking a non-working or future day
  is refused.
- Locking: teacher edits until `lock_after_days` (default 3) after the date; later changes need
  `attendance.correct` and a reason, are written to `student_attendance_corrections` and
  `audit.event("student_attendance.corrected")`. Every mark change is also row-audited.
- Daily mode: by default only the class teacher (and `attendance.mark.any`) marks (setting
  `class_teacher_only`).
- Leave: portal request (dates, kind = a leave status, reason, attachments via
  `POST /student-attendance/portal/uploads`); limits from settings (max days, how far back, note
  needed after N days). Request type **`attendance.student_leave`** registered with the workflow
  engine; default flow: `field` approver `request.class_teacher_id` (put on the payload). Approval
  (inbox or the leave page) turns absent marks in the range into the leave status and pre-fills
  later registers; families are notified of the outcome. Office can record leave (approved at once)
  and cancel approved leave (leave marks go back to absent).
- Alerts: absence (and optionally late) alerts to guardians and the learner via `notify_portal`,
  once per learner per day, either on save (today only) or at a set local time (periodic job
  `student_attendance.alerts`); weekly low-attendance alert below the threshold (default 75 %, after
  N marked units) to families plus a summary to class teachers; manual "alert guardians" from the
  defaulters report.

**Endpoints**: `GET/PUT /settings`; CRUD `/statuses`, `/calendar`; `GET /calendar-days`;
`GET /overview?on=`; `GET /my-registers?on=`; `GET /sections`; `GET /register` + `POST /register`;
`POST /register/all-present`; `GET /corrections`; reports `GET /reports/monthly` (`format=csv`),
`/reports/percentages`, `/reports/subjects`, `/reports/defaulters`, `POST /reports/defaulters/notify`;
`GET /students/{uuid}` (a learner's attendance for staff); `GET /summary` (home card); leave
`GET /leave`, `GET /leave/{uuid}`, `POST /leave` (office), `POST /leave/{uuid}/decide`,
`POST /leave/{uuid}/cancel`; portal (opened with `register_portal_prefix`) `GET /portal/summary`,
`GET /portal/calendar`, `GET/POST /portal/leave`, `POST /portal/leave/{uuid}/withdraw`,
`POST /portal/uploads`.

**Permissions**: `attendance.view`, `attendance.mark`, `attendance.mark.any`, `attendance.correct`,
`attendance.leave.manage`, `attendance.settings`. Defaults: principal all; hr view, mark, mark.any,
leave.manage; manager view, mark; teacher mark. Teachers without `attendance.view` only see the
sections they teach.

**Pages**: `/dashboard/student-attendance` (today's overview, your registers, every section),
`/register` (phone-friendly grid: All present, tap exceptions, late minutes, remarks, correction
reason for locked registers), `/leave` (waiting for me / all, approve/reject, record, cancel),
`/reports` (daily summary, monthly register with print + CSV, percentages per term/session,
subject-wise in period mode, below threshold with alerts, corrections), `/settings` (rules and
alerts, statuses, calendar). Portal `/dashboard/my/attendance` (heat-map calendar, month/session %,
absences) and `/dashboard/my/leave`. Nav entries added in the Teaching section (Mark register,
Attendance reports, Attendance settings); intro/features removed.

**Home cards** (`frontend/src/components/student_attendance/home-cards.tsx`): `StaffHomeCard`,
`PortalHomeCard({ studentId })`.

### Timetable
API prefix `/api/v1/timetable`, permissions `timetable.view`, `timetable.manage`, nav key
`timetable`. Alembic revision **`tt1a2b3c4d5e`** (down `sc1b2c3d4e5f`).

**Tables**: `timetable_bell_schedules`, `timetable_bell_periods`, `timetable_entries`,
`timetable_publications`, `timetable_substitutions`, `timetable_settings` (company-first indexes, RLS).

**Rules**
- Bell schedules: periods (numbered teaching periods) and breaks per set of weekdays, optionally only
  for some programs (a program-specific schedule wins over the default). Every institution gets a
  "Regular day" (Mon-Fri, 8 periods, assembly, short break, lunch) and "Saturday" (4 periods) once.
- Entries: section x weekday x period -> subject, teacher (defaults from allocation), room (defaults
  to the section's room). Several entries in one cell with an `elective_group` = electives/split
  groups; one entry across several sections sharing `group_key` = combined class.
- Clash checks: teacher or room double-booked (refused with a plain message, `force` saves anyway
  and it stays listed), two subjects in one section period without elective group, teacher weekly /
  daily load and back-to-back limits (settings). Rooms of kind `online` and `sports` can be shared.
- Generator: greedy from allocation `weekly_periods`, busiest teachers first, round-robin over
  subjects, spreads a subject over the week, respects existing entries, teacher clashes and the
  daily limit; reports what it could not place.
- Copy a section's timetable to others (teachers from each target's allocation, same, or none).
- Publish per section (version, who/when; edits afterwards flag "changed"); teachers, parents and
  learners only see published timetables; publishing notifies teachers and families (setting).
- Substitutions: per date; absent teachers come from approved staff leave; for each period free
  teachers are suggested (teaches this section, then this subject, then lightest day; excludes busy,
  on leave and already substituting); assign or cancel the class; substitute notified; register with
  fair-share counts. `teacher_day()` swaps substituted periods in and out (used by attendance).

**Endpoints**: `GET/PUT /settings`; `GET/POST /bell-schedules`, `PUT/DELETE /bell-schedules/{uuid}`;
`GET /sections`; `GET /grid?section_id=`; `POST /entries`, `PUT/DELETE /entries/{uuid}`;
`GET /clashes`; `GET /teacher?employee_id=`; `GET /room?room_id=`; `GET /teachers`; `POST /copy`;
`POST /publish`; `POST /generate`; `GET /mine`; `GET /mine/day?on=`; `GET /summary`;
substitutions `GET /substitutions/absent`, `/substitutions/periods`, `/substitutions/free`,
`POST /substitutions`, `GET /substitutions`, `GET /substitutions/counts`,
`DELETE /substitutions/{uuid}`; portal `GET /portal/week?student_id=`, `GET /portal/summary?student_id=`.

**Defaults**: principal view + manage; hr, manager, teacher view.

**Pages**: `/dashboard/timetable` (builder with Sections / Teachers / Rooms / Clashes & load / Bell
schedules tabs; click-to-assign modal with "save anyway" on clashes, allocation panel with
placed/weekly, Generate, Copy, Publish, Print), `/dashboard/timetable/substitutions`,
`/dashboard/timetable/mine` (new nav entry "My timetable" in Academics), portal
`/dashboard/my/timetable` (grid on desktop, day list on phones, today highlighted).

**Home cards** (`frontend/src/components/timetable/home-cards.tsx`): `StaffHomeCard`,
`PortalHomeCard({ studentId })`.

### Demo data
Rooms (one per Grade 6-10 section, Computer Lab, Science Lab, Music Room, Art Room, Playground);
realistic weekly periods on the Grade 6-10 allocations (Maths 7, Science 7, English 6, SST 6, Hindi 5,
CS 3, PE 3, Art 2, Music 2); generated and published timetables for all ten Grade 6-10 sections
(every section Divya teaches, including Grade 8 - A); two substitutions today. Attendance registers
for the last 30 days for all 30 sections (about 5 % absent, late and half days, a few chronically
absent learners, six approved leaves shown as EX/ML), part of today's registers left unmarked, two
pending parent leave requests (Aarav Kumar's waits for Divya Menon). Student attendance's demo seed
first calls the students demo seed, because `student_attendance` sorts before `students`.

### Tests
`tests/test_student_attendance.py` (8) and `tests/test_timetable.py` (8), with shared set-up in
`tests/school_helpers.py`: percentage formula and report numbers, teacher scope and locking with
logged corrections, holidays, leave approved by the class teacher through the workflow pre-filling
registers (and turning an earlier absence into leave), absence alert once per day, clash detection,
combined classes and electives, generator/copy/publish, substitutions, portal isolation (other
families' children and staff endpoints) and tenant isolation. All pass with
`tests/test_tenant_isolation.py`.

### Decisions and notes
- Settings live in one JSONB row per institution per module (`*_settings`) with code defaults.
- Section ids are passed as integer query parameters (as the foundation's section endpoints do);
  my own records are addressed by uuid.
- Portal withdrawal of a pending leave calls `core.workflow.service._finish(..., "cancelled")`
  because `workflow.cancel()` only allows employee requesters. Suggested engine change: support
  portal requesters (requester_portal_id) so the engine does not notify the company owner about
  decisions on parent requests (today `_notify` sends "…was approved" to the owner when the
  requester has no employee record).
- In the demo, staff load warnings are expected: the foundation allocation gives a few teachers
  (Divya included) more periods than the weekly limit of 32.
- Not built: drag-and-drop in the builder (click-to-assign instead), PDF export of timetables
  (print-friendly pages instead), SMS/WhatsApp for absence alerts (in-app/portal only).

## Campus services: library, transport, hostel, wellbeing
Four modules: `modules/library`, `modules/transport`, `modules/hostel`, `modules/wellbeing`, each
with models, service, router, hooks and seed. Tests: `tests/test_campus_services.py` (14 tests).

**Alembic revisions** (each `down_revision = "sc1b2c3d4e5f"`, RLS forced on every table):
`lb1a2b3c4d5e` (library), `tr1a2b3c4d5e` (transport), `hs1a2b3c4d5e` (hostel), `wb1a2b3c4d5e` (wellbeing).

**Tables**
- Library: `library_policies, library_categories, library_books, library_copies, library_members,
  library_loans, library_fines, library_reservations, library_eresources, library_stock_checks,
  library_stock_check_items`.
- Transport: `transport_vehicles, transport_crew, transport_zones, transport_routes, transport_stops,
  transport_riders, transport_trip_marks`.
- Hostel: `hostel_blocks, hostel_rooms, hostel_beds, hostel_mess_plans, hostel_allocations,
  hostel_roll_calls, hostel_leaves, hostel_visitors`.
- Wellbeing: `wb_health_records, wb_checkup_camps, wb_checkups, wb_infirmary_visits, wb_incident_types,
  wb_incidents, wb_points, wb_counselling_notes, wb_visitors, wb_gate_passes`.

**Permissions** (defaults in brackets)
- `library.view` (principal, teacher, hr, accountant, librarian), `library.circulate` and `library.manage` (librarian).
- `transport.view` (principal, teacher, accountant, hr), `transport.manage` (hr).
- `hostel.view` (principal, hr, accountant), `hostel.manage` (admins; give it to wardens' roles).
- `wellbeing.view` and `wellbeing.record` (principal, teacher, hr, manager), `wellbeing.manage` (principal),
  `wellbeing.counselling` (nobody by default except admins; restricted on purpose).
- Number series: `library_accession` (ACC000001), `gate_pass` (GP2611-0001), `visitor_pass` (VP2611-0001).
- Portal prefixes: `/library/portal`, `/transport/portal`, `/hostel/portal`, `/wellbeing/portal`.
- Daily jobs (`core.periodic`): `library.daily` (overdue reminders to parents and students, expired
  holds released), `transport.daily` (finished allocations closed, expiry reminders to transport managers).

**Endpoints (main ones)**
- Library `/library`: `summary`, `policies` (GET/PUT), `categories`, `eresources` (crud), `books`
  (list/create/get/put/archive), `isbn/{isbn}` (Open Library lookup, 503/404 when offline or not found),
  `books/{uuid}/copies`, `copies`, `copies/lookup?code=`, `copies/{uuid}`, `labels.pdf` (barcode or QR,
  A4 3x8), `members`, `members/sync`, `members/lookup?code=`, `members/for-student/{id}`,
  `members/for-employee/{id}`, `members/{uuid}`, `issue`, `return`, `loans`, `loans/{uuid}/renew`,
  `loans/{uuid}/lost`, `fines`, `fines/{uuid}/settle` (pay | waive with reason | bill via fees),
  `reservations` (+ `/cancel`), `stock-checks` (+ `/scan`, `/close`), `reports/most-borrowed`.
  Portal: `portal/summary`, `portal/loans`, `portal/catalogue`, `portal/categories`, `portal/eresources`,
  `portal/reservations` (+ `/cancel`).
- Transport `/transport`: `summary`, `zones` (crud), `vehicles`, `crew`, `routes` (+ `/stops`, `/reorder`,
  `/trip` GET/POST, `/sheet.pdf`), `stops/{uuid}`, `riders` (+ `/end`, `for-student/{id}`), `expiring`,
  `reports/stops`. Portal: `portal/summary`.
- Hostel `/hostel`: `summary`, `mess-plans` (crud), `blocks`, `rooms`, `beds/free`, `allocations`
  (+ `/check-out`), `blocks/{uuid}/roll-call` GET/POST, `leaves` (+ `/decide`: approve, reject, out,
  back, cancel), `visitors` (+ `/out`). Portal: `portal/summary`, `portal/leaves` (+ `/cancel`), parents only.
- Wellbeing `/wellbeing`: `summary`, `incident-types`, `camps` (crud), `health`, `health/{student_id}`
  GET/PUT, `checkups`, `infirmary`, `incidents`, `points` (+ `/void`, `/leaders`), `houses`,
  `counselling` (needs `wellbeing.counselling`; reads are audited), `visitors` (+ `/out`),
  `pickups/{student_id}`, `gate-passes` (+ `/returned`). Portal: `portal/summary`.

**Pages (web)**
- `/dashboard/library` with tabs: Catalogue, Circulation desk (scan input for cards and accession
  numbers), Members, Overdue & fines, Holds, Stock check, Reports, Settings.
- `/dashboard/transport` with tabs: Routes, Vehicles, Drivers & attendants, Riders, Reports.
- `/dashboard/hostel` with tabs: Rooms, Boarders, Roll-call, Leave & out-passes, Visitors, Settings.
- `/dashboard/wellbeing/health` (records, infirmary, check-up camps), `/dashboard/wellbeing/discipline`
  (incidents, merit points, houses, counselling, settings), `/dashboard/wellbeing/visitors`
  (visitor log, gate passes).
- Portal: `/dashboard/my/library`, `/dashboard/my/transport`, `/dashboard/my/hostel` (new nav entry),
  `/dashboard/my/wellbeing` (new nav entry "Health & merits").
- Home cards: `components/{library,transport,hostel,wellbeing}/home-cards.tsx`, each exporting
  `StaffHomeCard` and `PortalHomeCard({ studentId })`.

**Rules**
- Library: limits per member type (max books, loan days, fine per day, grace days, max fine, renewals,
  renew when overdue, hold days, max reservations, block above an unpaid amount), all editable, with a
  per-member book limit override. Every active student and staff member is a member (auto-created on
  scan or sync; leavers switched off). Reference-only titles can't be issued, and a member can't hold
  two copies of the same title. A renewal is refused when the book is overdue (policy), when no renewals
  are left, or when someone else waits for the title and no other copy is free. A return turns the days
  late into a pending fine, and the copy goes to the first waiting hold, whose member is notified.
  Lost = replacement price + overdue. Damaged returns take a damage fine.
- Transport: seats = route capacity or vehicle capacity, checked across the whole date range. One live
  allocation per learner; a new one ends the old one the day before and tells the parents (route
  change). The fee is the stop's fee, else the zone's. Parents are told when vehicle, crew or stop
  times change, if the toggle is on. `Student.uses_transport` follows live allocations.
- Hostel: one active boarder per bed and one bed per boarder. The block's gender must suit the learner,
  and rooms under maintenance take nobody. Moves check out the old bed. `Student.is_boarder` follows
  allocations. Roll-call presets approved leave. Absences notify the wardens; leave decisions notify
  the parents.
- Wellbeing: BMI from height and weight. Infirmary visits marked sent home or parent informed notify
  the parents. Confidential incidents are visible only with `wellbeing.manage` (or to their reporter).
  House totals sum points by the learner's current house. A gate pass is issued only to a guardian
  linked with `can_pickup`; the parents are notified and the pass is audited.

**Fees**
- Library fines ("Bill via fees"), transport allocations and hostel allocations (optional "bill fee")
  call `modules.fees.service.add_charge(db, company_id, student_id, head_code, amount, description,
  due_date)`. The call is only made when that function exists at runtime, inside a savepoint.
- Head codes used: `LIBRARY_FINE`, `TRANSPORT`, `HOSTEL`.
- Without the fees module, transport and hostel only record the amount on the allocation. Library
  billing returns a clear 400 and the UI hides the button (`fees_billing` flag).

**Demo data**
- Library: 150 titles, 369 copies, about 220 past loans and 42 open loans (about 10 overdue), a few
  fines and holds, 5 e-resources. Aarav Kumar has "Wings of Fire" on loan.
- Transport: 4 routes with stops, vehicles (some documents expiring), drivers and attendants, 3 zones,
  and 40 riders. Aarav rides Route 1.
- Hostel: one boys' block (Tagore House) with 12 rooms, 20 boarders, last night's roll-call, leave
  requests and visitors.
- Wellbeing: health records and an annual camp for every learner, 8 infirmary visits (one for Aarav),
  incidents, merit points across houses, today's visitor log and a gate pass.
- The seeds call the academics and students demo seeds first, because module seeds run in
  alphabetical order.

**Decisions**
- Hostel leave uses a simple warden approve/reject on the request, not the workflow engine (as asked).
- Counselling has its own permission with no default holders.
- The ISBN lookup goes through the server, so the browser never calls outside services.
- Labels and route sheets are server PDFs (WeasyPrint, with Code 128 from ReportLab and QR from `qrcode`).

**Not done / later**
- No GPS live tracking (only a device id field) and no trip attendance on the portal.
- Library fine payments are recorded at the desk without a printed receipt.
- Mess plan billing happens only together with the hostel allocation charge.

## Fees and finance (module `fees`)

**Built**
- Fee heads (recurring / one-time / optional / refundable deposit, optional GST rate and HSN/SAC, ledger
  account; 17 defaults per institution incl. system heads LATE, BOUNCE, MISC), fee categories (General, New
  admission, RTE, Staff ward, NRI), concession types, cash book categories, an (off) late fee rule — all from
  `seed.setup_company`.
- Fee structures per session + program (or all programs) + optional fee category; items billed every
  instalment or once in a chosen instalment; optional heads billed only to learners who opt in
  (`fee_opt_ins`); instalment plans monthly / quarterly / term-wise (from academic terms) / half-yearly /
  yearly / custom with suggested due dates; copy a structure to other classes or the next session.
- Concessions: percentage of each line or a fixed amount per invoice, on one head or all heads, validity
  dates, reasons; approval through the workflow request type `fees.concession` (setting; default on);
  automatic sibling discount (setting: % and heads; the eldest active sibling sharing a guardian pays in full,
  younger ones get an auto, approved concession); approving / cancelling reprices open invoices (never below
  what is already paid).
- Invoices: generated per learner from the matching structure + concessions + tax, in bulk for a session,
  program or section and chosen instalments (idempotent; partial unique index on live
  (student, instalment)); one-off invoices; credit notes (series `fee_credit_note`) and debit notes
  (`fee_debit_note`), audited; cancel with reason (only when nothing is paid); number series `fee_invoice`;
  invoice PDF.
- Late fee rules: fixed or per day after grace days with a cap per invoice; applied as a line when money is
  collected (people with `fees.manage` may waive).
- Collection: search by name / admission no. / learner or parent phone / parent name; dues by instalment;
  full or part payment, oldest due first (line by line); split across modes (cash, cheque/DD with number,
  bank and date, UPI, card, bank transfer, online); advance (wallet) for extra money and using it later;
  receipt number series `fee_receipt`, PDF (WeasyPrint via `documents.pdf.render_pdf`, amount in words with
  lakh/crore for INR) and print; cancel receipt with reason; cheque bounce reopens dues and bills a charge.
- Online payments: gateway settings (test, Razorpay, Stripe, PayU, Cashfree) with secrets Fernet-encrypted
  per provider and only "…last4" hints returned (same pattern as `assistant/integrations.py`); checkout
  orders (Razorpay Orders API, Stripe Checkout Session, Cashfree PG orders via httpx; PayU hosted form with
  SHA-512 hash), browser verification (Razorpay HMAC signature; Stripe / Cashfree read back), webhooks at
  `/api/v1/public/fees/webhook/<provider>/<slug>` (signature checked, idempotent), PayU return at
  `/api/v1/public/fees/return/payu/<slug>`. **Test mode** (default, also used when keys are missing)
  simulates payment so the full flow works without keys.
- Refunds: advance, refundable deposit (incl. "refund deposit on leaving") or a receipt; approval via
  `fees.refund`; pay-out records mode/reference and reduces the wallet / marks deposit lines refunded.
- Dues: by learner with ageing buckets (1-30, 31-60, 61-90, 90+), defaulters, instalment-wise dues, CSV;
  reminders (portal notification via `notify_portal` + email to guardians) one by one, selected or all
  overdue; automatic reminders N days before / after due dates via `core.periodic` job `fees.reminders`
  (once a day per institution after 08:00 local, logged in `fee_reminder_logs` so each goes once).
- Reports (all with CSV): daily collection (by mode, by collector, by day), head-wise collection,
  outstanding by section or class with collection %, concessions, fee register, instalment dues.
- Cash book: other income and expenses with categories, payee/payer, mode, reference, attachments (via
  `/documents/uploads`), voucher series `fee_voucher`, cancel with reason; totals beside fee collection and
  refunds of the period.
- `modules.fees.service.add_charge(db, company_id, student_id, head_code, amount, description, due_date=None)`
  for other modules (creates the head when unknown; flushes, caller commits). Also
  `service.student_dues(db, company_id, student_id)`.

**Pages** `/dashboard/fees` (overview: today / month / session, outstanding, 30-day trend and billed vs
collected charts with recharts, dues by class, modes, recent receipts), `/dashboard/fees/collect`,
`/invoices`, `/payments` (receipts + refunds tabs), `/structures` (structures, heads, categories tabs),
`/concessions` (given + types), `/dues`, `/ledger`, `/reports`, `/settings` (collection rules, sibling
discount, reminders, late fee rules, online payment provider + webhook address). Portal
`/dashboard/my/fees` (child picker, dues by instalment, pay selected instalments oldest-first, test
checkout, history, receipt download; phone friendly). Nav: overview entry is `exactOnly`; added
"Fee reports" and "Fee settings"; ledger now needs `fees.view` (writes need `fees.manage`).

**Home cards** `frontend/src/components/fees/home-cards.tsx`: `StaffHomeCard` (GET `/fees/summary`) and
`PortalHomeCard({ studentId })` (GET `/fees/portal/summary?student_id=`).

**Endpoints** (`/api/v1`) `fees/meta, summary, overview, settings (GET/PUT), gateway (GET/PUT)`;
crud `fees/heads, categories, concession-types, late-rules, ledger-categories`;
`fees/structures` (list/get/create/update/switch off, `/plan`, `/{uuid}/copy`);
`fees/students/search`, `fees/students/{uuid}/account`, `/opt-ins` (PUT), `/deposit-refund`;
`fees/invoices` (list, get, POST one-off, `/generate`, `/{uuid}/cancel`, `/adjust`, `/pdf`);
`fees/collect`; `fees/payments` (list with sum, get, `/pdf`, `/print`, `/cancel`, `/bounce`);
`fees/concessions` (list, create, `/{uuid}/cancel`, `/apply-sibling-rule`); `fees/refunds` (list,
create, `/{uuid}/pay`); `fees/dues` (+ `format=csv`), `fees/dues/remind`;
`fees/reports/{daily|heads|outstanding|concessions|register|instalments}` (+ `format=csv`);
`fees/ledger` (list with totals, create, update, `/{uuid}/cancel`).
Portal (registered prefix `/fees/portal`): `summary, account, receipts/{uuid}/pdf, invoices/{uuid}/pdf,
orders (POST), orders/{uuid} (GET), orders/{uuid}/verify, orders/{uuid}/test-pay` — each checks
`require_portal` + the learner belongs to the signed-in parent/student.

**Tables** (revision `fe1a2b3c4d5e`, down_revision `sc1b2c3d4e5f`, RLS on all): fee_heads, fee_categories,
fee_structures, fee_structure_items, fee_instalments, fee_opt_ins, fee_concession_types, fee_concessions,
fee_late_rules, fee_invoices, fee_invoice_lines, fee_adjustments, fee_payments, fee_payment_modes,
fee_allocations, fee_wallet_entries, fee_refunds, fee_gateway_settings, fee_online_orders,
fee_reminder_logs, fee_ledger_categories, fee_ledger_entries.

**Permissions** fees.view, fees.collect, fees.manage, fees.refund (refunds, cancel receipt, bounce),
fees.settings. Defaults: accountant all five; principal and hr fees.view. Request types `fees.concession`,
`fees.refund` (default step: role principal; owner when none). Number series fee_invoice, fee_receipt,
fee_credit_note, fee_debit_note, fee_refund, fee_voucher (financial-year reset).

**Demo data** quarterly structures for every class (tuition, exam, computer, activity, lab for senior
classes, annual charges and caution deposit in Q1, optional transport for riders), invoices for the
instalments due so far (Q1–Q3), ~70% fully paid, ~15% part paid, ~15% overdue, a few payments today,
late fee rule on, sibling discount 10% on tuition (Anika Kumar gets it automatically; the Kumar children
have Q3 overdue), six cash book entries.

**Tests** `tests/test_fees.py` (16): late fee maths, line pricing (percent + fixed spread + GST),
invoice amounts with concession + sibling rule + tax and repricing on cancel, concession approval through
the workflow, partial payment oldest first + receipt numbering + receipt cancel, late fee on collection,
cheque bounce with charge, add_charge, refund flow with approval, deposit refund, test-mode online payment
by a parent (incl. oldest-first rule and idempotency), parents only see/pay their own children, teacher
blocked, tenant isolation (API + RLS), reports/dues/reminders, gateway secrets masked + signed Razorpay
webhook. `tests/test_tenant_isolation.py` passes.

**Decisions**
- Fixed concessions are per invoice (per instalment), not per session. Sibling discount is an automatic
  concession record (visible, auditable) rather than a hidden calculation.
- Late fee counts days after the grace period; added only when collecting (and shown as a preview on
  the counter and in the portal); only on structure invoices.
- Online orders pay whole instalments, oldest first; any instalment paid meanwhile turns into advance.
- Requests raised by the company owner are approved at once by the workflow engine (platform rule), so
  approvals show when staff (e.g. the accountant) raise them.
- Settings live in `companies.config["fees"]`; currency from `companies.currency`.

**Not done / notes**
- Live calls to Razorpay / Stripe / Cashfree / PayU follow their public APIs but were not run against real
  accounts (no keys); test mode is fully exercised.
- No GST invoice format beyond the tax column; no accounting export (Tally) yet.
- Fee category is matched by the learner's free-text `fee_category` (name or code of a fee category).
