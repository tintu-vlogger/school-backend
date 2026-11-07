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
