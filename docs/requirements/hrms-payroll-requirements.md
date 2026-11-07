# Campusly: What the Product Must Do

**An HR and payroll system for Indian companies, fully compliant with Indian law**

| | |
|---|---|
| Version | 0.2 (draft for review) |
| Date | 6 November 2026 |
| Who this is for | Founders, HR people, payroll people, accountants, investors, designers and developers. No technical knowledge needed. |
| Country | India only |

---

## 1. The product in one page

Campusly is an online system that an Indian company uses to manage its people and pay them correctly, from the day someone applies for a job to the day they leave and get their final dues.

It replaces spreadsheets, paper registers, separate attendance machines, separate payroll software and the stack of government forms that HR and accounts teams handle every month.

**Our promise to customers:** if you run your HR and payroll in Campusly, you follow Indian labour and tax law by default. The system knows the rules, applies them automatically, warns you before you break one, keeps every register the law asks for, and reminds you before every government deadline.

What a company can do in Campusly:

- Set up the company, its branches, departments and job titles
- Keep a complete record of every employee
- Hire people: post jobs, track candidates, send offer letters
- Welcome new joiners and collect their documents
- Track attendance and working hours, including shifts and overtime
- Manage leave: balances, requests and approvals
- Run monthly payroll and give payslips
- Deduct and pay PF, ESI, Professional Tax, Labour Welfare Fund and income tax correctly
- Handle expense claims, loans and salary advances
- Set goals and run performance reviews
- Manage company assets like laptops and phones
- Handle resignations, exits and final settlements
- Let employees and managers do most things themselves on web, mobile and WhatsApp
- Produce every report, register and government file the law requires

An intelligent assistant will be added on top of all this later. It will be planned in a separate document once this one is agreed.

---

## 2. Our compliance promise

### 2.1 What "100% compliant" means for us

1. **Every rule is built in.** Every legal rule that affects HR or payroll in India is in the system: national laws, state laws, and the rates and limits that come with them.
2. **Rules are applied automatically.** Salaries, deductions, leave, overtime, gratuity, bonus and tax are calculated by the rules. Nobody has to remember them.
3. **Mistakes are stopped before they happen.** If someone tries something that would break the law, the system blocks it or shows a clear warning that has to be acknowledged. Examples: paying less than minimum wage, deducting more than the law allows, settling dues late.
4. **Every register and return is ready.** The registers, forms, returns and payment files the law asks for are produced in the official format, ready to file or upload.
5. **No missed deadlines.** A compliance calendar shows what is due, for which company and state, and sends reminders before the due date.
6. **Law changes are handled by us.** When the government changes a rate, slab, limit or form (in the Budget, a state notification or new Labour Code rules), we update the system centrally. The new rule applies from the right date, and older months stay calculated under the old rule.
7. **Everything is on record.** Every change is recorded with who made it and when, so the company can show an inspector or auditor exactly what happened.

### 2.2 What stays the company's responsibility

The software prepares everything, but some steps are done by the company itself: depositing money with the government, filing on government portals (unless we offer a filing service later), and decisions that need human judgement, such as disciplinary action. The system tells the company exactly what to do and when, and records that it was done.

### 2.3 How we keep it correct

- A qualified payroll and labour-law consultant checks every rule, rate and calculation before launch, and again whenever the law changes.
- Every calculation is tested against worked examples prepared by that expert. The tests run every time we change the software.
- A "Law updates" page inside the product tells customers what changed, from which date, and how it affects them.

### 2.4 Everything is a setting, not code

Laws change often: the Budget changes tax slabs, states change Professional Tax and minimum wages, and the government changes PF and ESI limits and forms. **When a rule changes, it is updated in the system's settings. Our developers do not need to change the software.**

**What can be changed through settings**

| Kind of rule | Example | How it is changed |
|---|---|---|
| Rates, limits and slabs | PF 12%, PF wage limit ₹15,000, ESI limit ₹21,000, PT slabs per state, tax slabs, gratuity tax-free limit | Enter the new value with the date it starts |
| Minimum wages | New minimum wage for a state, area or skill level | Update the state's minimum wage table with the start date |
| Due dates | PF due by the 15th, TDS by the 7th | Edit the compliance calendar |
| How something is calculated | "Overtime = 2 × hourly wage × hours", "Bonus = 8.33% of wages, capped" | Edit the formula in the formula builder, test it, then switch it on |
| Checks and limits | "Block payroll if pay is below minimum wage", "Warn when deductions are over 50% of wages" | Add or edit a rule: the condition, the message, and whether to block, warn or ask for approval |
| Leave, attendance, overtime, expense and loan policies | New leave type, new accrual rule, new late-coming rule | Build or edit the policy on screen |
| Approval steps | Add finance approval for loans over ₹50,000 | Edit the approval steps |
| Government files, registers and letters | New PF return layout, new register format, new letter | Edit the file or letter template |
| Holidays | New state holiday | Add it to the state's holiday calendar |
| Notifications | New reminder before a deadline | Add a message template and when to send it |

**Rules for changing rules**

- **Every change has a start date.** Old months are always recalculated with the rules that applied then, so past payrolls never change by accident.
- **Two levels.** Our team keeps the national and state law library up to date for every customer. Each company sets its own policies on top. A company can be more generous than the law, but never less, and the system blocks any setting that goes below the legal minimum.
- **Try before applying.** Before a change goes live, a dry run shows its effect, for example "this is how last month's payroll would change for each employee".
- **Checked and approved.** Changes to the national and state law library are reviewed by a second person (and by our legal expert) before going live.
- **Versions and undo.** Every rule has a version history. A change can be rolled back, and every change records who made it, when, and why.
- **Customers are told.** When we update a law rule, customers see it on the "Law updates" page and on their compliance dashboard, with what it means for them.

**The limit of settings**

Almost every law change (new rates, limits, slabs, dates, formulas, form layouts and state rules) is handled through settings alone. Software changes are only needed when something completely new appears that the system has no building block for, such as a brand-new government scheme with its own online connection. In that case the new building block is added once, and from then on it can be changed through settings too.

---

## 3. The laws we follow

These are the laws that apply to HR and payroll in India, and what the system does for each one. The four Labour Codes came into force on 21 November 2025 and replaced 29 older labour laws. The new Income-tax Act, 2025 came into force on 1 April 2026.

### 3.1 Code on Wages, 2019

Replaces the old Minimum Wages, Payment of Wages, Payment of Bonus and Equal Remuneration Acts.

| The law says | What the system does |
|---|---|
| Nobody can be paid below the minimum wage for their state, area and skill level. A national floor wage also applies. | Keeps the minimum wage table for every state. Checks every employee's pay every month and blocks payroll if anyone is below the minimum. |
| "Wages" has a fixed meaning. Basic pay, dearness allowance and retaining allowance must be at least 50% of total pay. If allowances go above 50%, the extra counts as wages. | Works out "wages" correctly for every employee. Warns when a salary structure breaks the 50% rule, and uses the corrected figure for PF, gratuity, bonus and leave pay. |
| Wages must be paid on time. Monthly wages are due by the 7th of the next month. | Tracks the pay date for every pay group and warns before the deadline. Late payments show on the compliance dashboard. |
| Men, women and transgender persons doing the same or similar work must get equal pay. Hiring must not discriminate on gender. | Pay-equity report comparing pay by gender for the same job title and grade, with flagged gaps. |
| Only allowed deductions can be made, and total deductions cannot exceed 50% of wages in a month. Fines have their own limits. | Every deduction type is marked as allowed or not. Payroll warns when deductions would cross 50%. A register of fines and deductions is kept. |
| Overtime is paid at twice the normal rate. | Overtime is paid at twice the normal rate by default. A state that sets a different rule can be configured. |
| Annual bonus of 8.33% to 20% of wages for eligible employees (those earning up to ₹21,000 a month), worked out on the set calculation ceiling. | Works out who is eligible and the bonus amount, keeps the bonus register, and pays it through payroll. |
| When someone leaves, is dismissed or retrenched, or the business closes, all dues must be paid within 2 working days. | Exit settlement has a countdown to the 2-day deadline, with alerts to HR and finance. |
| Registers and wage slips must be kept and given. | Payslips go to every employee every month. Wage registers are kept electronically in the required format. |

### 3.2 Code on Social Security, 2020

Covers Provident Fund, ESI, gratuity, maternity benefit, employee compensation and other social security schemes.

| The law says | What the system does |
|---|---|
| **Provident Fund (EPF):** the employee pays 12% of PF wages and the employer pays 12%. The employer's part is split between the pension scheme (8.33%, on wages up to ₹15,000) and the PF account. Insurance (EDLI) and admin charges are added. | Works out the PF of every covered employee correctly. Handles voluntary extra PF, employees over 58 (no pension part), people who were never members and earn above the limit, and the employer's choice to pay on full salary or on the ₹15,000 limit. |
| The monthly PF return (ECR) is uploaded to the EPFO portal and money is paid by the 15th. | Produces the PF return file in the EPFO format every month, plus files for arrears. Records the payment reference once paid. |
| **ESI:** employees earning up to ₹21,000 a month (₹25,000 for persons with disability) are covered. The employee pays 0.75% and the employer 3.25%. | Decides who is covered, follows the six-monthly coverage rule, works out contributions and produces the monthly ESI file. |
| **Gratuity:** paid after 5 years of service (1 year for fixed-term employees). The amount is 15 days' wages for every completed year, with more than 6 months counting as a full year. There is a tax-free limit. | Works out gratuity on exit, sets aside the cost every month, records nominees, and applies the tax-free limit. |
| **Maternity benefit:** 26 weeks of paid leave for the first two children, 12 weeks after that, plus rules for adoption and surrogacy. Creche facility applies at 50+ employees. | Maternity leave is built in with the right number of weeks. It keeps the maternity benefit register and reminds about the creche rule. |
| **Employee compensation** for workplace injury or death. | Records workplace accidents, links them to the employee and shows what compensation is due. |
| Registration of employees and of the establishment on the government portal. | Stores all registration numbers per company and branch (PF, ESI, others). Collects UAN and ESI numbers for each employee at joining. |

### 3.3 Industrial Relations Code, 2020

| The law says | What the system does |
|---|---|
| Companies with 300+ workers must have standing orders (service rules). | Stores the company's service rules and makes every employee acknowledge them. |
| Companies with 20+ workers must have a grievance redressal committee. | Grievance channel for employees with tracking, deadlines and committee access. |
| Fixed-term employees get the same pay and benefits as permanent ones doing the same work. | Fixed-term is an employment type. The system checks they are on the same pay and benefits as permanent staff in the same role. |
| Rules for notice, lay-off, retrenchment (with compensation of 15 days' average pay per year of service) and closure. Larger companies need government permission. | Separate exit processes for resignation, termination, retrenchment, lay-off and closure, each with the right notice, compensation and letters. Warns when government permission is needed. |
| Retrenched workers get 15 days' wages towards a re-skilling fund. | Adds this amount automatically to retrenchment settlements. |

### 3.4 Occupational Safety, Health and Working Conditions Code, 2020

| The law says | What the system does |
|---|---|
| Every employee must get an appointment letter. | An appointment letter is created for every new joiner from a template. The system will not mark onboarding complete without one. |
| Normal working hours are up to 8 hours a day and 48 a week. Overtime needs the worker's consent, and there are quarterly overtime limits. | Shift rules follow these limits. The system warns when someone is close to the daily, weekly or quarterly limit and records overtime consent. |
| Paid annual leave is 1 day for every 20 days worked, after 180 days of work in a year, with carry-forward and encashment rules. | The annual leave policy follows these rules by default. Leave is earned automatically from attendance. |
| Free annual health check-up for employees above a set age, in covered establishments. | Reminds HR of employees due for their health check-up and records that it was done. |
| Women may work night shifts only with their consent and with safety arrangements. | Records consent before a woman is rostered on a night shift, and blocks the roster without it. |
| Contract workers: registration and licensing above the thresholds, and the main employer is responsible if the contractor does not pay. | Records contractors, their licences and contract workers. Tracks proof that contractor wages and PF/ESI were paid. |
| Registers and returns are kept electronically. | All attendance, leave, overtime and wage registers are kept electronically in the required format. |

### 3.5 State Shops and Establishments Acts

| The law says | What the system does |
|---|---|
| Each state sets its own rules for registration, working hours, weekly offs, holidays, leave and opening hours. | Every branch is linked to its state, and that state's rules apply to the people working there. |
| National holidays (Republic Day, Independence Day, Gandhi Jayanti) plus state festival holidays are compulsory. | Holiday calendars by state with the compulsory holidays already filled in. Working on a holiday earns extra pay or a day off as the state requires. |

### 3.6 Professional Tax (state law)

| The law says | What the system does |
|---|---|
| Many states charge Professional Tax on salaries, with their own slabs and due dates. The maximum is ₹2,500 a year. | Has the PT slabs for every state that charges it, including special rules such as Maharashtra's February amount and slabs that differ for women. Deducts it from salary and produces the state returns. |

### 3.7 Labour Welfare Fund (state law)

| The law says | What the system does |
|---|---|
| Some states collect a small Labour Welfare Fund contribution from employee and employer, monthly, half-yearly or yearly. | Has the LWF rules for every state that charges it. Deducts the right amount at the right time and produces the returns. |

### 3.8 Income-tax Act, 2025 (tax deducted from salary)

| The law says | What the system does |
|---|---|
| Employers must deduct income tax (TDS) from salary every month, based on the employee's expected yearly income. | Works out monthly TDS for each employee and adjusts it as salary, bonus and declarations change. |
| The new tax regime is the default; employees can choose the old regime. | Each employee picks a regime and sees a side-by-side comparison of how much tax they would pay in each. |
| Employees can claim exemptions and deductions (rent allowance, home loan interest, insurance, investments and others) by declaring them and later showing proof. | Employees declare investments at the start of the year and upload proof later. HR checks and approves them, and tax is recalculated. |
| Tax must be deposited by the 7th of the next month. A quarterly return is filed, and every employee gets an annual tax certificate. | Produces the quarterly TDS return file and the annual tax certificate for every employee, with bulk digital signing. Form names follow the new Act. |
| Higher tax must be deducted if an employee has no valid PAN. | Applies the higher rate automatically and warns HR. |
| Employer-provided benefits such as a car, house or cheap loan are taxable "perquisites". | Works out the taxable value of these benefits and includes it in tax. |

### 3.9 Digital Personal Data Protection Act, 2023 and its Rules (2025)

| The law says | What the system does |
|---|---|
| People must be told what personal data is collected and why. Consent is needed where the law requires it. Employment-related use is allowed for proper purposes. | Shows a clear privacy notice to employees and candidates and records their consent where needed. |
| Data is used only for the stated purpose and kept only as long as needed. | Sets how long each kind of data is kept. Old candidate data is deleted automatically. Records the law requires us to keep (wages, PF, tax) are kept for the required period. |
| Data must be kept secure, and any data breach must be reported to the Data Protection Board and to the affected people. | Strong security (see section 21), plus a breach-response process with templates for the notices. |
| People can ask to see, correct or delete their data, and must have someone to complain to. | Employees can view and correct their own data. Requests are tracked. The company's grievance contact is shown in the app. |
| Children's data needs a parent's consent. | Flags anyone under 18 (for example, apprentices) and asks for a parent's consent. |

### 3.10 Other laws that apply

| Law | What the system does |
|---|---|
| **POSH Act, 2013** (sexual harassment at the workplace) | A confidential complaint channel that only the Internal Committee can see (an Internal Committee is required at 10+ employees). Tracks deadlines for inquiry and report, produces the annual report, and records each employee's acknowledgement of the POSH policy. |
| **Rights of Persons with Disabilities Act, 2016** | Records disability details privately, applies the higher ESI limit, supports an equal-opportunity policy, and makes the product usable for people with disabilities. |
| **Apprentices Act, 1961** | Apprentice is an employment type, with stipend rules and records for the apprenticeship portal. |
| **Child and Adolescent Labour Act** | Checks date of birth at hiring and blocks hiring of anyone under the legal age. Warns about restrictions for 14–18-year-olds. |
| **Aadhaar Act** | Aadhaar numbers are always hidden except the last 4 digits and used only where allowed. |
| **Information Technology Act, 2000** | Electronic records and electronic signatures used for letters and documents are legally valid. |
| **National Pension System** (optional for employers) | Supports the employer's NPS contribution and its tax treatment. |

### 3.11 The compliance dashboard

One screen for the owner and HR that shows, for each company and state:

- What is due this month (PF, ESI, PT, LWF, TDS, returns, registers, renewals), and whether it is done, pending or late
- Anyone paid below minimum wage, anyone with missing PAN, UAN or bank details
- Final settlements close to the 2-day deadline
- Overtime close to the legal limit
- Registrations and licences about to expire
- Law changes that affect the company

---

## 4. Who uses the system

| Person | What they do in the system |
|---|---|
| Company owner | Sees everything, approves payroll, owns compliance |
| HR team | Manages employees, policies, hiring, onboarding, exits and letters |
| Payroll team | Runs monthly payroll, statutory payments and returns, and tax |
| Finance team | Approves payroll and payments, reimbursements and loans |
| Managers | Approve their team's leave, attendance corrections and expenses; review performance |
| Employees | See payslips, apply for leave, mark attendance, claim expenses, submit tax proofs, update their details |
| Hiring team | Create job openings, review candidates, interview, make offers |
| Auditor | Read-only access to records and reports for a period |
| Candidates | Apply for jobs, accept offers, fill joining details |
| Our own team | Runs the platform, supports customers, keeps legal rules up to date |

**Access control.** Each person sees only what they need. A manager sees their own team. HR sees their branch or the whole company. Salary, bank details, PAN, Aadhaar and health information are visible only to people who are specifically allowed to see them. Companies can create their own roles. One person can be blocked from both preparing and approving the same payroll.

---

## 5. Getting started and company setup

**Signing up**
- A company signs up on the website, gets its own web address (for example `yourcompany.campusly.app`) and starts a free trial.
- A step-by-step setup guide walks through company details, branches, departments, policies, importing employees, salary structures and the first payroll. Progress is saved.
- Plans are priced per employee per month. Billing includes GST invoices.

**Company details**
- Company name, logo, address, PAN, TAN, GSTIN, CIN, and the financial year (April to March).
- **More than one legal company under one account:** each has its own PAN, TAN, PF and ESI codes, PT and LWF registrations, bank accounts and authorised signatory. Payroll and government filings are done separately for each.
- **Branches** with address and state. The state decides which PT, LWF, holiday and shop-and-establishment rules apply.
- **Departments, job titles, grades, cost centres and projects**, all editable.
- **Types of employment:** permanent, probation, fixed-term, contract (through a contractor), intern, trainee, apprentice, consultant and part-time.
- **Reporting lines:** each person's manager, plus an optional second manager and HR contact.
- **Organisation chart** that can be searched and downloaded.
- **Holiday calendars** by state, with compulsory and optional holidays.
- **Work weeks:** 5-day, 6-day, alternate Saturdays, or custom.
- **Policy library:** company policies with versions, which employees must read and accept.
- **Registrations and licences** with their expiry dates and renewal reminders.

---

## 6. Employee records

**What is stored for each employee**
- **Personal:** name, photo, date of birth, gender, marital status, father's or spouse's name (needed for PF and ESI), blood group, emergency contacts, current and permanent address.
- **Government IDs:** PAN (checked for format and validity), Aadhaar (hidden except last 4 digits), UAN, ESI number, passport.
- **Job:** company, branch, department, job title, grade, type of employment, joining date, probation and confirmation dates, notice period, shift, manager.
- **Pay:** salary structure, CTC, bank account (IFSC checked), whether PF, ESI, PT and LWF apply, chosen tax regime.
- **Background:** education, previous jobs (with income and tax already deducted this year, needed for correct TDS), skills.
- **Family and nominees** for PF, gratuity and insurance.
- **Documents:** offer and appointment letters, ID proofs, certificates, signed policies.
- **Extra fields** that the company can add as it likes.

**What HR can do**
- Add employees one by one, or upload hundreds at once from Excel. The system checks every row and shows mistakes before saving.
- See the full history of every change, such as transfers, promotions, new manager or salary revisions, with the date it took effect and the reason. Changes can be scheduled for a future date.
- Follow each employee's stage: joining, probation, confirmed, on notice, left, suspended or long leave.
- Get reminders before probation ends. The manager recommends confirming, extending or ending the employment, and the letter is created automatically.
- Search and filter the employee list and download it. Downloads still hide what the person isn't allowed to see.
- Get reminders before documents expire, such as passports and licences.
- Catch duplicates: the system warns if a PAN, Aadhaar, UAN, phone or email already belongs to someone else.
- Rehire former employees with their old history and UAN kept, and move people between the group's companies without breaking their service period.

**What employees can do**
- Update their own contact and family details.
- Request changes to sensitive details (bank, name, date of birth, PAN) with proof. HR approves these.

---

## 7. Hiring

- **Hiring request:** a manager asks for a new or replacement position with a salary range. It goes for approval.
- **Job posts** on the company's own careers page, and shared to job sites such as Naukri, LinkedIn and Indeed.
- **Candidates** apply online. Their CV is read and their details are filled in automatically. Each candidate's source is recorded (careers page, referral, agency, job site or walk-in).
- **Hiring stages** shown as a board: applied, screening, interviews, offer, hired or rejected.
- **Interviews** scheduled with calendar invites and reminders by email or WhatsApp. Interviewers fill in a scorecard.
- **Offers** are built from the salary structure. The letter is created from a template, gets approval if pay is outside the range, is signed electronically, and is accepted online.
- **Employee referrals** with referral bonuses paid through payroll.
- **Background checks** through verification partners.
- **Privacy:** candidates are told how their data is used, and rejected candidates' data is deleted after a set time.
- **Reports:** time to hire, best sources, offer acceptance rate, and how long positions stay open.

---

## 8. Joining (onboarding)

- An accepted offer turns into a new-joiner record. Nothing needs to be typed again.
- Before day one, the new joiner fills in personal details, bank, PAN, UAN, nominees, previous employer's income and tax, and tax regime choice, and uploads documents online.
- **Joining checklist** of tasks for HR, IT, admin, the manager and the joiner, with due dates. The checklist can differ by department or branch.
- The **appointment letter** (required by law) and other joining documents are created and signed electronically.
- **Government joining forms** are created where they apply: PF nomination and declaration, ESI declaration and gratuity nomination.
- Laptop, ID card and system access are requested as checklist tasks.
- Welcome message and a buddy for the first weeks.
- Check-ins at 30, 60 and 90 days during probation.

---

## 9. Attendance and working hours

**Marking attendance**
- On the web, optionally only from the office network.
- On the mobile app, only within a set distance of the office, with an optional selfie. It works without internet and syncs later.
- From biometric, card or face-recognition machines already in the office.
- Field staff can check in at customer sites (with their consent).
- Work-from-home and on-duty (client visit, training) requests, with approval.

**Shifts and rules**
- Shifts with start and end times, breaks, grace time, night shifts and flexible hours.
- Shift rosters, rotating shifts and shift swaps with approval.
- Weekly offs, fixed or rotating.
- Late-coming and early-leaving rules, for example 3 late arrivals = half day. Each company sets its own, within the law.
- Overtime: who is eligible, how it is calculated (twice the normal rate), approval and worker consent. It goes straight to payroll.
- Comp-off (a day off earned for working on a holiday or weekly off), with an expiry date.
- Warnings when legal limits on daily, weekly or quarterly hours are close.

**Every day and every month**
- Each person's status every day: present, absent, half day, on leave, holiday, weekly off, work from home, on duty or unpaid.
- Employees can fix a missed punch with a reason, and their manager approves it.
- Attendance is locked for the month before payroll. Later corrections are adjusted in the next payroll.
- Daily view: who is in, absent, late or on leave, by department and branch.
- Timesheets against projects, for companies that bill clients by the hour.
- Legal attendance and overtime registers in the state format.

---

## 10. Leave

- **Types of leave:** earned or annual, casual, sick, maternity, paternity, adoption, bereavement, marriage, comp-off, optional holiday, unpaid, and any custom type.
- **Rules for each type:**
  - who gets it (by branch, grade, type of employment or length of service)
  - how much, and how it builds up (yearly, monthly, or from days worked as the law requires)
  - carry-forward limits, encashment and lapse
- **Request rules:**
  - minimum and maximum days, half days, hourly leave, and advance notice
  - whether weekends between leave days count
  - a medical certificate after a set number of sick days
- **Legal minimums cannot be reduced:** the system won't let a policy give less than the law (for example, maternity leave or annual leave).
- Employees apply, change or cancel leave on web, mobile or WhatsApp, and see their balance and who else in the team is away.
- **Approval** by the manager, optionally HR, with reminders and escalation if no one acts.
- **Leave balance history:** opening, earned, used, encashed, lapsed and manual changes (with reason).
- **Year-end:** carry forward, lapse, encashment through payroll and new-year balances, all automatic.
- **Leave encashment** at year-end and on exit, taxed correctly.
- Team and company leave calendars.
- Leave register in the legal format.

---

## 11. Payroll

### 11.1 Setting up salaries

- **Pay groups:** for example, monthly staff, workers and interns, each with its own pay period, attendance cut-off and pay date.
- **Salary parts**:
  - **Earnings:** Basic, DA, HRA, allowances, overtime, bonus, commission, incentives, arrears and more
  - **Deductions:** PF, ESI, PT, LWF, TDS, loan instalments, advances, recoveries
  - **Employer's costs:** PF, ESI, gratuity, NPS
  - **Reimbursements** and **one-time payments**
- **For each salary part**, the system knows:
  - how it is calculated
  - whether it counts as "wages" under the Code on Wages
  - whether PF and ESI apply to it
  - how it is taxed
  - whether it is cut for unpaid days
- **Salary structures by grade**, which split CTC into its parts automatically, including different HRA for metro and non-metro cities.
- **50% wage rule check** on every structure (see 3.1).
- **Minimum wage check** for every employee (see 3.1).
- Salary revisions with a start date. Revisions back-dated to an earlier month create arrears automatically.

### 11.2 Running payroll every month

Payroll follows clear steps, and the system shows who did each step and when:

1. **Collect inputs.** Attendance and unpaid days, overtime, new joiners (paid for the days they worked), people leaving, salary revisions and arrears, bonuses and incentives, loan instalments, approved expense claims, and salary holds. Bulk changes can be uploaded from Excel.
2. **Check for problems.** The system lists anyone with:
   - missing bank details or PAN
   - pay below minimum wage
   - negative take-home pay
   - deductions above 50% of wages
   - a big change from last month
   - attendance not yet locked
3. **Calculate.** Gross pay, unpaid-day cuts, PF, ESI, PT, LWF, TDS, take-home pay and total employer cost.
4. **Review.** A full payroll sheet, plus a comparison with last month by person and by salary part.
5. **Approve.** By payroll, then finance, then the owner. The company decides the order.
6. **Lock.** The month can't be changed after this. Reopening needs special permission and a recorded reason.
7. **Pay.** Bank transfer files in each bank's format (HDFC, ICICI, SBI, Axis, Kotak and standard NEFT/RTGS), and later direct payment through payout partners. Paid and failed payments are tracked.
8. **Accounting.** Accounting entries are ready to import into Tally, Zoho Books or QuickBooks, split by company, cost centre and department.

**Other payroll needs**
- **Payslips** with the company's branding, showing each salary part, totals for the year so far, leave balance and tax summary. They are password-protected, emailed and available in the app, in English, Hindi or regional languages.
- **Extra pay runs** outside the monthly cycle: bonus, arrears, final settlement and corrections.
- Holding or releasing a person's salary, and paying part of it.
- Employees who have stopped coming to work (absconding): salary held and recovery handled.
- Cost reports by department, branch, cost centre and company, and actual cost against budget.
- When moving from another system, a side-by-side comparison of our results with the old system's for the first months.

### 11.3 Government deductions and payments

These are covered in detail in section 3. In short, the system:

- calculates PF, ESI, PT, LWF, TDS, gratuity and bonus by the current rules
- prepares every monthly, quarterly and yearly file in the official format
- records the payment reference (challan) once paid
- shows everything on the compliance calendar with reminders

| What | Usually due |
|---|---|
| TDS deposit | 7th of the next month |
| PF payment and return | 15th of the next month |
| ESI payment | 15th of the next month |
| Professional Tax | As per the state |
| Labour Welfare Fund | As per the state (monthly, half-yearly or yearly) |
| Quarterly TDS return | End of the month after each quarter (May 31 for the last quarter) |
| Annual tax certificate to employees | By 15 June |
| Salary payment | By the 7th of the next month |
| Full and final settlement | Within 2 working days of exit |

### 11.4 Income tax for employees

- Employees choose their tax regime and see a comparison of both.
- **Investment declaration at the start of the year:** rent paid (with the landlord's PAN above the limit), home loan, insurance, investments, medical insurance, education loan, donations, travel allowance, previous employer's income and other income.
- **Proof submission window**, usually January to February, with HR verification (accept, reject or partly accept, with comments).
- **Monthly tax calculation** that spreads the year's tax over the remaining months. One-time payments are taxed in the month they are paid. Employees can ask for extra tax to be deducted.
- **Flexible benefits:** employees choose how to split part of their salary among tax-saving allowances within limits, and claim them with bills.
- **Tax worksheet** in the employee's app, showing how their tax is worked out month by month.
- **Quarterly TDS return file**, and the **annual tax certificate** for every employee, digitally signed and sent out.

---

## 12. Expenses and reimbursements

- Expense types (travel, food, fuel, phone, internet, client meetings) with limits by grade and city.
- Claims submitted on mobile with a photo of the bill, including GST details.
- Approval by the manager and then finance, with warnings when a claim breaks policy.
- Payment with salary (taxed or tax-free as the rules say) or as a separate bank transfer.
- Travel advances and their settlement.
- Fuel and mileage claims by distance.

---

## 13. Loans and salary advances

- Loan types (personal, vehicle, emergency, festival advance) with eligibility, maximum amount, interest (or interest-free) and maximum number of instalments.
- Employee request, approval, payment, then an instalment plan.
- Instalments deducted from salary. Instalments can be skipped or paused with approval, and loans can be closed early.
- Cheap or interest-free loans are treated as a taxable benefit where the law says so.
- Any balance still owed is recovered in the final settlement.

---

## 14. Performance and growth

- Goals for the company, departments and individuals, with weights, targets and deadlines, linked to each other.
- Regular check-ins and progress updates.
- **Review cycles** (yearly, half-yearly, quarterly or probation): self review, manager review, and optional peer and skip-level review, with rating scales and skills.
- Reminders and escalation so reviews finish on time.
- **Calibration:** see the rating spread across the company and adjust it fairly, with every change recorded.
- One-to-one meetings with shared notes.
- Ongoing feedback and appreciation.
- Performance improvement plans with goals and outcomes.
- **Pay reviews:** budget by department, a guide for increments based on rating, manager proposals, approvals and promotions. Revision letters are released on a set date and the new pay goes into payroll automatically.

---

## 15. Company assets

- List of all assets (laptops, phones, SIM cards, ID cards, vehicles) with serial number, value and condition.
- Give assets to employees, who confirm they received them. Assets are returned on transfer or exit, and recovery for loss or damage goes into the final settlement.
- Employees can request assets.

---

## 16. Help desk, letters and announcements

- **Employee requests to HR, payroll, IT or admin**, with categories, target response times, status and a satisfaction rating.
- **Policy FAQs** that employees can search.
- **Letter templates** filled in automatically:
  - offer, appointment, confirmation, increment, promotion, transfer
  - experience and relieving letters
  - salary certificate and address proof
  - warning, show-cause and termination letters
- **Self-service letters:** employees request a salary certificate or address proof, and it is created after approval.
- **Electronic signature** using Aadhaar eSign or a digital signature.
- **Announcements**, birthdays and work anniversaries, polls and surveys.
- **Grievance and POSH complaints**, kept confidential, with deadlines tracked (see section 3).

---

## 17. Leaving the company

- **Resignation:** the employee resigns online with a reason and proposed last day. The manager and HR accept it. It can be withdrawn before acceptance.
- **Other ways of leaving:** termination, retrenchment, end of contract, retirement (started automatically by age), death and absconding. Each has its own legal steps and letters.
- **Notice period:** worked out automatically. It can be bought out, waived or recovered.
- **Exit checklist:** clearance from IT, admin, finance, the manager and assets.
- **Exit interview** with reports on why people leave.
- **Full and final settlement** in one statement, approved and **paid within 2 working days** as the law requires. It includes:
  - salary till the last day
  - leave encashment, gratuity and bonus
  - notice pay or recovery
  - loan, advance and asset recovery
  - pending reimbursements
  - retrenchment compensation and re-skilling fund amount where applicable
  - tax on all of it
- **After exit:** relieving and experience letters, exit date updated for PF, and a tax certificate for the part of the year worked.
- Former employees can still log in for a while to download payslips, tax certificates and letters.

---

## 18. Employee and manager self-service

**Employees, on web, mobile app and WhatsApp, can:**
- mark attendance, and see their attendance and leave balance
- apply for leave, work from home, on duty or attendance corrections
- download payslips and tax certificates, and see their tax worksheet
- submit tax declarations and proofs
- claim expenses and ask for loans
- request letters and raise help-desk requests
- update their own details
- read and accept company policies

**Managers can:**
- see their team at a glance (who is in, away or late)
- approve everything from one list, many at a time
- see their team's attendance, leave and performance

**Other things**
- Log in with a mobile OTP (for staff without a company email) or with Google or Microsoft accounts.
- Use the app in English, Hindi and regional languages (Tamil, Malayalam, Kannada, Telugu, Marathi, Bengali, Gujarati), including payslips.
- **WhatsApp:** get payslips, check leave balance, apply for leave and see the holiday list.

---

## 19. Approvals and notifications

**Approvals**
- Each type of request has its own approval steps: manager, manager's manager, HR, department head, finance or a named person. Steps can change by amount, number of days, grade or branch.
- Steps can run one after another or at the same time.
- Reminders, automatic escalation, and optional automatic approval after a time limit.
- Approve or reject straight from email, mobile notification or WhatsApp.
- Comments and attachments on every request, with full history.
- Simple automation: "when this happens, do that". For example, when someone is confirmed, create the letter and inform them.

**Notifications**
- Sent in the app, by email, mobile notification, WhatsApp and SMS.
- Message templates the company can edit, in different languages.
- Daily summaries for managers (pending approvals) and HR.
- A record of every message sent, with retries if one fails.

---

## 20. Reports

- **Ready-made reports**, all filterable and downloadable in Excel or PDF, and can be emailed on a schedule:
  - **People:** headcount, joiners, leavers, attrition, demographics, diversity, birthdays, probation due, documents expiring
  - **Time:** attendance summary, late-coming, overtime, leave balances and leave taken
  - **Pay:** payroll register, salary summary, bank statement, CTC, cost by cost centre
  - **Statutory:** PF, ESI, PT, LWF and TDS
  - **Other:** final settlements, loans outstanding, reimbursements, hiring and performance
  - **Compliance:** every register and return the law requires
- **Build your own report:** pick fields, filters and totals, then save and share.
- **Dashboards** for HR, payroll, attendance and leave.
- Reports always hide what the viewer isn't allowed to see. Every download is recorded.

---

## 21. Safety and privacy of data

- All data is kept in India.
- Strong passwords, mobile OTP, Google and Microsoft login, and single sign-on for larger companies. A second login step is required for owners, HR and payroll users. Inactive sessions log out automatically.
- All data is encrypted when stored and when sent. Extra protection applies to Aadhaar, PAN, bank details and salary.
- Aadhaar is never shown in full, never written into logs and never included in downloads by default.
- Every change is recorded (who, what, when, old and new value) and can't be edited.
- Protection against repeated wrong logins, and alerts for suspicious logins.
- Backups are taken daily, and data can be restored to any point in the last days. Restores are tested regularly. At most 1 hour of data can be lost, and service is back within 4 hours in a disaster.
- Regular security testing by outside experts.
- We plan to work towards international security certifications (ISO 27001 and SOC 2).
- Personal data protection as described in section 3.9.

---

## 22. Connections with other tools

- **Accounting:** Tally, Zoho Books, QuickBooks
- **Attendance machines:** eSSL, ZKTeco, Matrix and similar
- **Banks and payment partners:** salary transfers
- **Google Workspace and Microsoft 365:** login, calendar invites, and creating or closing accounts when people join or leave
- **Slack and Microsoft Teams:** notifications and approvals
- **Verification partners:** PAN, bank account, Aadhaar via DigiLocker, UAN and background checks
- **Job sites:** Naukri, LinkedIn, Indeed
- **Electronic signature partners**
- **Insurance partners** for group health insurance enrolment
- An open connection point so other software the customer uses can exchange data with us

---

## 23. Moving from another system

- Ready-made Excel templates for: company setup, employees, salary structures, opening leave balances, this year's payroll so far (needed for correct tax and PF if the company joins mid-year), loans outstanding, tax declarations and assets.
- Every upload is checked, with a clear list of errors and totals to tick against the old system.
- Joining mid-year is fully supported, with tax and statutory totals carried over.
- Run both systems side by side for the first months and compare results line by line.

---

## 24. Speed and reliability

- Pages open in under 2 seconds.
- Payroll for 1,000 employees is calculated in under a minute, and for 10,000 in under 10 minutes, with a progress bar.
- The system is available 99.9% of the time. No planned maintenance in the last 5 and first 7 days of a month, when payroll is busiest.
- Money is always calculated exactly to the paisa, with clear rounding rules.
- Past months are always recalculated using the rules that applied in that month.
- Works on computers, tablets and phones, in all common browsers.
- Indian formats everywhere: ₹, lakhs and crores, DD-MM-YYYY dates, Indian time.
- Usable by people with disabilities (follows international accessibility standards).

---

## 25. Release plan

| Stage | What is included | Result |
|---|---|---|
| **1. Core HR** | Company setup, employee records, roles and access, documents, basic letters, employee profile, bulk upload, change history | A company has all its people and structure in the system |
| **2. Time** | Attendance (web, mobile, machines), shifts and rosters, leave, holidays, attendance corrections, approvals | Daily operations run in the system |
| **3. Payroll and compliance** | Salary setup, monthly payroll, payslips, PF, ESI, PT, LWF, TDS and declarations, bank files, accounting entries, compliance calendar and dashboard, final settlement, loans, reimbursements, legal registers | A first company runs real payroll, first alongside its old system, then fully |
| **4. Talent** | Hiring, onboarding, performance, exit processes, help desk, POSH and grievances | The full employee journey is covered |
| **5. Growth** | Pay reviews, custom reports, more connections, regional languages, WhatsApp self-service | Ready for larger companies |

The compliance work in section 3 is part of each stage, not left to the end. Nothing goes live until the labour-law and payroll expert has signed it off.

---

## 26. What we can reuse from the current base

The system starts from an existing online product that already has the basic building blocks:

| Already there | Will be used for |
|---|---|
| Company sign-up, own web address per company, setup guide | Company accounts and setup |
| Logins (password, Google, mobile QR), roles and permissions, protection against wrong logins | Access control and security |
| Staff, departments, job titles | Employee records and organisation setup (to be expanded a lot) |
| Staff rosters | Shifts and rosters |
| Change history, notifications, WhatsApp, email, mobile notifications | Records for audit, alerts, WhatsApp self-service |
| Documents and templates | Employee documents and letters |
| Equipment list | Company assets |
| Subscriptions, payments, platform admin, partner programme | Our own billing of customers |
| Mobile app that works offline | Employee mobile app |
| English, Hindi and Tamil | Multi-language support |
| Approval and automation basics | Approvals |

The parts made for clinics (appointments, patients, prescriptions, pharmacy, lab, wards, booking websites) will be removed step by step.

---

## 27. Questions for the founder

1. **First customers:** which company sizes and industries do we target first? Factories and shift workers need weekly wages and contract-labour features earlier.
2. **Government filings:** do customers file returns themselves using our files, or do we also offer a service where we file for them?
3. **Salary payments:** do we start with bank files only, or pay salaries directly through a payment partner from day one?
4. **Pricing:** per employee per month, packages by module, or something else?
5. **Pilot company:** is there a first company that can run payroll with us alongside their current system?
6. **Contract workers:** do we need full contractor management (licences, contractor wage checks) in the first version?
7. **Languages:** which regional languages are needed at launch?
8. **Mobile app:** only for employees at first, or also full features for managers and HR?
9. **Legal expert:** do we already have a labour-law and payroll consultant to check the rules, or should we find one?

---

## Words used in this document

| Word | Meaning |
|---|---|
| CTC | Cost to company: total yearly cost of an employee, including the employer's contributions |
| Gross pay | Total earnings in a month before deductions |
| Take-home (net) pay | What the employee actually receives after all deductions |
| PF / EPF | Employees' Provident Fund: retirement savings managed by EPFO |
| UAN | Universal Account Number: an employee's lifelong PF number |
| ECR | Electronic Challan cum Return: the monthly PF file uploaded to EPFO |
| ESI | Employees' State Insurance: medical and cash benefits for lower-paid employees |
| PT | Professional Tax: a state tax on salaries |
| LWF | Labour Welfare Fund: a small state contribution for worker welfare |
| TDS | Tax Deducted at Source: income tax the employer deducts from salary |
| Gratuity | A lump sum paid on leaving after the required years of service |
| F&F | Full and final settlement: all dues paid when someone leaves |
| LOP | Loss of pay: unpaid days |
| Arrears | Pay owed for an earlier month, for example after a back-dated raise |
| Challan | Proof of payment to the government |
| Comp-off | A day off given for working on a holiday or weekly off |
| Labour Codes | The four new national labour laws in force from 21 November 2025 |

---

## Appendix A: Complete feature checklist

One line per feature. Items marked <span class="new">new</span> were not on the founder's original list and have been added to cover the full HR and payroll cycle and Indian law.

### A1. Organisation and setup
- Organisation profile: name, logo, address, PAN, TAN, GSTIN, CIN, financial year
- Sub-companies / legal entities, each with its own PAN, TAN, PF, ESI, PT and LWF registrations and bank accounts
- Regions to group locations
- Locations / branches with address, state and attendance boundary on the map
- Departments, with sub-departments and department heads
- Teams, across departments if needed
- Designations (job titles)
- Grades / bands for pay and policy rules <span class="new">new</span>
- Cost centres and projects for costing <span class="new">new</span>
- Employment types: permanent, probation, fixed-term, contract, intern, trainee, apprentice, consultant, part-time <span class="new">new</span>
- Roles and permissions, including custom roles
- Privacy controls so only allowed people see salary, bank, PAN, Aadhaar and health details <span class="new">new</span>
- Tags with optional sub-tags that can be put on anything, especially people
- Skills master list and each employee's skills with level
- Numbering formats for every series (employee ID, letters, assets, tickets, requests) with their own counters
- Custom fields that can be added to employee records and other records
- Holiday calendars by state and location, with compulsory and optional holidays <span class="new">new</span>
- Work-week patterns: 5-day, 6-day, alternate Saturdays, custom <span class="new">new</span>
- Company policies with versions and employee acknowledgement
- PDF templates with custom headers and footers, several per document type, picking the right one for the situation
- Organisation chart, searchable and downloadable
- Company registrations and licences with expiry reminders <span class="new">new</span>
- Multiple languages: English, Hindi and regional languages <span class="new">new</span>
- Company sign-up, own web address, free trial and subscription plans <span class="new">new</span>
- Step-by-step setup guide for new companies <span class="new">new</span>

### A2. Core building blocks (used across the whole product)
- One common rule engine used everywhere: leave, attendance, payroll, approvals and alerts
- Formula builder for salary parts, overtime, bonus and other calculations <span class="new">new</span>
- Everything versioned with a "valid from" date
- Dynamic approval process: multi-level, conditional, parallel, with reminders, escalation and delegation
- Every database change audited (who, what, when, old and new value)
- No delete: records are only edited or archived, with a full change log on employee fields
- Dry run to see the effect of a rule change before it goes live <span class="new">new</span>
- Central law library (PF, ESI, PT, LWF, tax, minimum wages) kept up to date by our team for all customers <span class="new">new</span>
- Notification centre: in-app, email, mobile, WhatsApp and SMS, with editable templates <span class="new">new</span>
- Compliance calendar and compliance dashboard <span class="new">new</span>

### A3. Employee records
- Employee master record
- Employee contact details
- Next of kin and emergency contacts
- Family members and nominees for PF, gratuity and insurance <span class="new">new</span>
- Government IDs: PAN, Aadhaar (masked), UAN, ESI number, passport <span class="new">new</span>
- Bank details with account verification <span class="new">new</span>
- Education and certifications <span class="new">new</span>
- Employment history before joining, including this year's income and tax for TDS
- Employee history within the company: transfers, promotions, salary changes, manager changes
- Employee documents with verification
- Document expiry reminders (passport, licences, visas) <span class="new">new</span>
- Employee stages: pre-joining, probation, confirmed, on notice, exited, archived <span class="new">new</span>
- Probation tracking and confirmation process with letter <span class="new">new</span>
- Transfers between branches and companies in the group <span class="new">new</span>
- Employees update their own details, with approval for sensitive changes <span class="new">new</span>
- Duplicate check on PAN, Aadhaar, UAN, phone and email <span class="new">new</span>
- Rehiring former employees with their history kept <span class="new">new</span>
- Employee tasks: assignment and tracking

### A4. Hiring
- Hiring request and approval
- Hiring workflow with stages that can be set per job
- Job posting to Naukri, Indeed, LinkedIn and other job sites
- Custom careers page that can also be embedded in the company website
- Resume follow-up through a candidate pipeline board
- AI resume analysis and screening
- Interview scheduling with calendar invites and reminders
- Interview process with AI evaluation plus manual evaluation
- Interviewer evaluation scorecards
- Candidate reference verification
- Background verification through verification partners <span class="new">new</span>
- Offer letter creation, approval, e-signature and online acceptance <span class="new">new</span>
- Employee referrals with referral bonus <span class="new">new</span>
- Recruitment agency submissions <span class="new">new</span>
- Message templates for candidates by email and WhatsApp <span class="new">new</span>
- Talent pool of good candidates for future roles <span class="new">new</span>
- Candidate privacy consent and automatic deletion of old data <span class="new">new</span>
- Hiring reports: time to hire, sources, offer acceptance <span class="new">new</span>

### A5. Joining and learning
- Onboarding workflows
- Pre-joining portal for new joiners to fill details and upload documents <span class="new">new</span>
- Appointment letter for every joiner (required by law) <span class="new">new</span>
- Government joining forms: PF nomination, ESI declaration, gratuity nomination <span class="new">new</span>
- Laptop, ID card and system access as joining tasks <span class="new">new</span>
- Buddy and 30/60/90-day check-ins <span class="new">new</span>
- Induction programme
- Course management for courses and induction
- Course assignment, completion tracking, quizzes and certificates <span class="new">new</span>
- Mandatory training (POSH, safety, policies) with completion tracking <span class="new">new</span>

### A6. Attendance and time
- Biometric device setup for any device (connection, FTP file, CSV and others)
- Biometric enrolment of employees
- Syncing time and attendance from devices
- Web and mobile clock-in with location boundary and selfie <span class="new">new</span>
- Kiosk attendance on a shared tablet <span class="new">new</span>
- Employee work arrangement: working days and default shift timings
- Shift master with breaks, grace time and night shifts <span class="new">new</span>
- Employee roster
- Attendance rules: late arrival, early leaving, half day, absent <span class="new">new</span>
- Attendance correction requests
- Permission hours (short time off)
- Work from home with categories that can be set up
- On-duty and field work requests <span class="new">new</span>
- Overtime with worker consent and legal hour limits <span class="new">new</span>
- Comp-off for working on holidays and weekly offs <span class="new">new</span>
- Women's night-shift consent before rostering <span class="new">new</span>
- Automatic timesheet for people who don't need to punch
- Project timesheets for client billing <span class="new">new</span>
- Timesheet and attendance locking before payroll
- Legal attendance and overtime registers <span class="new">new</span>

### A7. Leave
- Leave types that can be set up, with configurable rules
- Every leave rule comes from settings, not code
- Leave rule versions with start dates
- Leave approval
- Leave balances with earning, carry-forward, encashment and lapse <span class="new">new</span>
- Year-end leave processing <span class="new">new</span>
- Legal minimum leave enforced (annual leave, maternity) <span class="new">new</span>
- Team and company leave calendar <span class="new">new</span>

### A8. Payroll
- Salary components
- Salary structures by grade that split CTC automatically <span class="new">new</span>
- Pay period opening and payroll run
- Warnings, notifications and emails before payroll about pending leave, attendance and approvals
- Payroll AI verification
- Unpaid days, arrears, new joiner and leaver part-month pay <span class="new">new</span>
- Bonus, incentives and other one-time payments <span class="new">new</span>
- Salary hold and release <span class="new">new</span>
- Minimum wage check and 50% wage rule check <span class="new">new</span>
- Extra payroll runs outside the monthly cycle (bonus, arrears, corrections) <span class="new">new</span>
- Payslips
- Payroll export in all required formats
- Bank transfer text file creation
- Accounting entries for Tally, Zoho Books and others <span class="new">new</span>
- Final settlement for every exit scenario
- Expense claims with approval, included in timesheet and payroll
- Flexible benefits and reimbursements <span class="new">new</span>
- Loans and salary advances with instalment deduction <span class="new">new</span>
- Gratuity and statutory bonus <span class="new">new</span>
- Side-by-side check with the old payroll system in the first months <span class="new">new</span>

### A9. Tax and statutory compliance
- Tax rule configuration
- Automatic TDS calculation
- Investment declaration and proof submission <span class="new">new</span>
- Old vs new tax regime comparison for employees <span class="new">new</span>
- Form 16 (annual tax certificate) generation
- Quarterly TDS return file <span class="new">new</span>
- PF calculation and monthly PF return file (ECR) <span class="new">new</span>
- ESI calculation and monthly file <span class="new">new</span>
- Professional Tax by state <span class="new">new</span>
- Labour Welfare Fund by state <span class="new">new</span>
- Registers and returns required by the Labour Codes <span class="new">new</span>
- Pay-equity report by gender <span class="new">new</span>

### A10. Performance and growth
- Employee performance reviews
- Automatic performance review by AI with manual verification
- Goals and OKRs linked from company to individual <span class="new">new</span>
- 360-degree feedback <span class="new">new</span>
- Rating calibration across teams <span class="new">new</span>
- One-to-one meetings with shared notes <span class="new">new</span>
- Performance improvement plans <span class="new">new</span>
- Promotion and salary increment
- Kudos to an employee

### A11. Engagement and communication
- Buzz feed: birthdays, work anniversaries and custom posts with reactions and comments
- Automatic emails on birthdays and work anniversaries
- Kiosk screen for birthdays, anniversaries, news and announcements
- Announcements and news sent to chosen groups
- Surveys
- Polls and quick pulse checks <span class="new">new</span>

### A12. Employee relations and help desk
- Grievance forms, anonymous or named, with tracking
- POSH complaints handled only by the Internal Committee, with legal deadlines
- Disciplinary actions: warnings and show-cause notices <span class="new">new</span>
- Help-desk requests to HR, payroll, IT and admin <span class="new">new</span>
- Self-service letters: salary certificate, address proof, experience letter <span class="new">new</span>

### A13. Assets
- Asset management and tracking
- Assigning assets to employees, with acknowledgement and return
- Asset requests from employees <span class="new">new</span>
- Recovery for lost or damaged assets in the final settlement <span class="new">new</span>

### A14. Exit
- Exit workflow
- Resignation request
- Resignation acceptance workflow
- Notice period calculation, buyout and waiver <span class="new">new</span>
- Handover of work
- Clearance from departments as set in the workflow
- Exit interview <span class="new">new</span>
- Full and final settlement within 2 working days, as the law requires <span class="new">new</span>
- Relieving and experience letters <span class="new">new</span>
- Closing email and system accounts when someone leaves <span class="new">new</span>
- Properly disabled in the system and archived
- Former employees can still download payslips and tax certificates <span class="new">new</span>

### A15. Reports, analytics and AI
- Reports
- Analytics
- Custom report dashboards
- Data visualisation in many chart types
- AI analytics
- Data assistant (AI) to ask questions about HR data
- Option to connect any AI model through MCP
- Scheduled reports by email <span class="new">new</span>
- Record of who downloaded which report <span class="new">new</span>

### A16. Self-service and mobile
- Employee self-service on web and mobile app <span class="new">new</span>
- Manager approvals inbox, with bulk approve <span class="new">new</span>
- WhatsApp self-service: payslip, leave balance, apply leave <span class="new">new</span>
- Login with mobile OTP, Google or Microsoft <span class="new">new</span>

### A17. Data, connections and security
- Advanced data import and export wizard
- Connections: accounting, Google and Microsoft, Slack and Teams, attendance machines, banks, verification partners <span class="new">new</span>
- Open connection for other software (API and event alerts) <span class="new">new</span>
- Moving data from an old system, including mid-year payroll totals <span class="new">new</span>
- Personal data protection (DPDP): notices, consent, data requests, breach handling <span class="new">new</span>
- Aadhaar masking and encryption of sensitive data <span class="new">new</span>
- Two-step login for owners, HR and payroll users <span class="new">new</span>
- Data stored in India, daily backups and disaster recovery <span class="new">new</span>

---

## Appendix B: Extra features found in competitor research

These items were found by studying a leading HR product module by module (its pages, settings, forms and every on-screen text). Each one is missing from Appendix A and worth adding. The full research for each module is in `docs/research/zoho-people/features/`.

### B.1 Employee information (26)

- **Employees without login (employee profiles)** with a separate, cheaper licence. Very useful for factory, retail and site workers who never log in but must be on payroll and registers.
- **Custom employee statuses** with an Active/Inactive type that drives login and licence use.
- **Separate login-disable date** from the exit date, with clear rules for past and future dates.
- **HR process builder:** admins define their own change processes (transfer, promotion, location change), choose the key and linked fields, who can raise them, with bulk import, duplicate blocking and a one-click revoke that restores old values.
- **Rule-based employee groups** (applicability groups) whose members update automatically, reused for policies, visibility and approvals. Our rule engine (A2) should expose this as a named, reusable group.
- **Identity document set-up:** which documents apply to whom, with exceptions, mandatory flags, automatic reading of uploaded ID cards to fill fields, a separate "identity document admin" permission, and upload reminders after joining.
- **Employee privacy choices:** let employees hide their birthday, wedding anniversary, work anniversary and mobile number from colleagues. This supports DPDP Act purpose limits.
- **Profile photo change approval** and control over who can change photos.
- **Employee stakeholders:** named relationships (HR partner, mentor, buddy, payroll contact) per employee, with history, usable as approvers.
- **Business unit and division levels** in the organisation structure, with renamable level names.
- **Streams / job families** under job titles, with stream heads per department.
- **Private HR notes on an employee.**
- **Preferred name**, "about me" and "ask me about" fields, and seating location, work phone and extension.
- **Experience worked out automatically** (current experience from joining date, total experience), with a "relevant" flag on past jobs.
- **Display preferences:** name format, 12/24-hour clock, time zone per location.
- **Mobile app lock and screenshot blocking** for sensitive data.
- **Custom email sender address and domain checks** so system emails come from the company's own address and do not go to spam.
- **Company portal on the customer's own domain.**
- **Home page widgets** with an admin option to make them the same for everyone.
- **Org chart default view** (whole company or own reporting tree) and option to show years of service.
- **Recycle bin** for deleted setup records, and a planned **retention and deletion schedule** for ex-employee data (see law note below).
- **Webhooks and custom scripts** in automation, plus run logs.
- **Blocked deletion** of a person or setup record while it is used in a workflow, approval or org structure.
- **Reassign pending approvals** before making someone inactive.
- **Test (sandbox) copy** of the company account to try settings safely.
- **Rename on-screen terms** to match the company's own words.

### B.2 Onboarding (20)

- **Several onboarding flows with audience rules,** each with its own forms, documents, courses and checklists, and a preview.
- **Two stages (pre-joining and from joining day)** that can be switched on separately, with automatic start on the joining date.
- **Admin review of candidate submissions field by field,** with remarks, reopen for corrections and change history.
- **Require admin closure** before onboarding counts as complete.
- **Portal link expiry** and per-person portal access switch.
- **Reminder schedule** (first after N days, then every N days until done) and a completion deadline time.
- **Minimum gap rule between finishing pre-joining and joining date.**
- **Restrict access to other features until onboarding is done.**
- **Offer and appointment letter life cycle:** statuses (pending, sent, accepted, declined, expired, withdrawn), offer expiry date, withdraw and regenerate, decline reason, status lists, bulk import.
- **Salary packages** that are locked once used and can be cloned.
- **Offer administrator** permission separate from general HR.
- **Identity document collection rules for candidates** with mandatory flags and automatic reading of uploaded cards.
- **Physical signature option:** download, sign by hand, upload (for staff who cannot e-sign).
- **Onboarding timeline** with time per stage and the longest stage.
- **Field mapping screen** from candidate to employee fields.
- **Bulk invite by email list** and bulk trigger/complete.
- **Retrigger with a new flow** when a person's details change.
- **Reassign HR partner duties** when the HR partner leaves.
- **Background check types** (employment, education, credit, address by digital, physical or postal check) with consent text and a consolidated report.
- **Reports:** candidate and employee onboarding reports, new joiner report.

### B.3 Probation and confirmation (15)

- **Several probation processes** with their own audience (for example by grade, employment type or location).
- **Duration in days or months** counted from joining date or a chosen start date.
- **Extension rules:** allowed or not, length of each extension, maximum count, and a lock on changing the length while extensions are running.
- **Multi-level reviewers** for the decision, not just the manager.
- **On hold status with a reason** for doubtful cases.
- **Feedback from other people** (peers, project leads) before the decision.
- **Early review trigger** a set number of days before the end.
- **Batch reminder email** to each reviewer listing all their due cases.
- **Automated actions at each stage** (start, each extension, confirmation, on hold, exit).
- **Automatic confirmation date** written back to the employee record, with a clear rule when HR overrides it.
- **Mark as exited** to link probation with the exit process.
- **Bulk actions:** start, trigger review, request feedback, create letters, complete, cancel.
- **Import of open probation records** when a company moves from another system.
- **Probation summary dashboard** and per-reviewer pending lists.
- **Cancel probation** for someone added by mistake.

### B.4 Exit and alumni (14)

- **Separate flows per exit type with audience rules** (department, location, company), each with its own clearances, interview form, documents and emails.
- **Permission matrix for who can raise each exit type** (employee, manager, HR partner, admin).
- **Choice of when the exit date is written** (on submission, after approval, on last working day).
- **Cancellation and revocation of an exit, each with its own approval.**
- **Recalculation of clearance and interview dates when the last working day changes.**
- **Clearance owner, due dates and delayed-clearance list,** reusable clearance forms shared across flows.
- **Reassign the leaver's pending approvals and HR partner duties** before they go.
- **Import of past exits** with automatic completion for past dates (for migration).
- **Restore a deleted exit record** and a full timeline of every step.
- **Alumni portal** with career timeline, chosen document categories, alumni announcements, HR contact and an expiry date (goes beyond our "log in for a while").
- **Alumni survey** sent some days after exit, with expiry.
- **Exit reports:** experience-wise exits, exit reasons with counts, clearance completion and delay analysis, demographic report.
- **"Replacement needed" field** on resignations to link to hiring requests.
- **Exit interview checklist** items (vehicle, books, equipment, security, notice followed).

### B.5 Leave (24)

- **Grant-based leave with its own request flow** (maternity, adoption, bereavement, marriage credited on approval), with min/max units, max requests per period, minimum gap and document rules.
- **Leave extension** with its own permissions, reason and approval, plus full extension history.
- **Partial cancellation and cancellation windows** (current pay period, last N pay periods, current year), set separately for employee, manager and approver.
- **Loss-of-pay control**: maximum LOP days per pay period, excess carried to next period, LOP adjustment report before payroll, and lock after processing.
- **Absence-to-leave auto conversion**: absent days are automatically deducted from chosen leave types (in a set order) or marked LOP, with notices to employee and manager.
- **Late-coming deductions from leave** (grace period policy deducting leave in a chosen order, with automatic reversal when attendance is corrected). We list "3 late = half day" but not deduction from leave balances and reversal.
- **Proration rules for joiners and leavers**: joining-date cut-off rule, first/last month rules, rounding (to half/quarter day), probation-period credit.
- **Overdraw options**: allow negative balance up to a limit or up to year-end accrual, or mark excess as LOP, with clear messages at apply time.
- **Leave expiry reminders** to employees and managers before year-end so balances are used (also helps manage encashment cost).
- **Restricted (optional) holidays** chosen by employees from a list, with a cap per year. (Our leave types mention "optional holiday", but not the employee picking flow or limits.)
- **Holiday applicability by location and shift**, with re-processing of already-applied leave when a holiday is added or changed.
- **Substitute day off** (pick a specific day off instead of a comp-off balance), alongside comp off.
- **Comp off from overtime via scheduler**, with hours-to-days conversion, partial-day units and "take first, work later" option.
- **Leave pay rules** (pay percentage by length of leave, for example half-pay sick leave after N days). Useful for company sick-leave schemes and for ESI-linked sickness where the employer tops up.
- **Clubbing rules** (which leave types cannot be combined, for example casual leave with earned leave).
- **Maximum consecutive days, minimum gap between requests, maximum requests per month or year.**
- **Balance display choice** (as on leave start date, today, or year-end estimate) and "balance after this request" at apply time.
- **Per-employee policy override** with reset-to-default, plus "refresh/rerun policy" tools for admins.
- **Calendar sync** of approved leave and holidays to Google and Microsoft 365 calendars with busy/out-of-office status.
- **Leave policy template gallery** with ready Indian templates (for example state-wise Shops & Establishments leave, factory leave, standard IT company policy).
- **Absence pattern score** (Bradford-type) to flag frequent short absences, with levels and alerts. Optional, for larger employers.
- **Statutory vs company leave tag** on each leave type, so reports and audits show which leave comes from law and which from policy.
- **Leave administrators** (named non-HR people who can manage leave for a group) and visibility settings for the leave calendar (all, department only, department head only).
- **Previous years' leave report access** limits for employees.

### B.6 Attendance (21)

- **Office-in vs remote-in tagging** with clear rules for mixed days, and a remote work policy with day limits, weekday blocks, approval only above the limit, and ad-hoc grants.
- **Break management**: paid/unpaid breaks per shift, automatic breaks, break credit rising with hours worked, missed-break alerts and exceptions. Important for the OSH Code rest-interval rule (see law notes).
- **Geo-fenced location tracking for field staff** at set intervals, with automatic check-out on leaving the area or repeated GPS failures (only with consent, under DPDP).
- **Face verification on punches** (not just a selfie) with photo retention rules (delete after a set period).
- **Restrict check-out to the same source as check-in** and block punches on approved leave days.
- **Check-in/check-out reminders** before and after shift, and **deviation alerts** to employee and manager (late, early, missed check-in, short hours, short core hours).
- **Absent scheduler**: automatically mark absent (or notify) when there is no punch by a set time.
- **Round-off rules** for punches (to shift start/end, or by N minutes) and **shift margins** for payable hours.
- **Choice of hours calculation**: first-in/last-out or sum of every in/out pair.
- **Specific policies with a priority order** (person, employment type, department, designation, shift, general).
- **Recalculate past attendance when a rule changes** (with a date range), with audit trail.
- **Overtime submission choices**: pre-approval before working (helps the consent rule) and post-submission; overtime scheduler that sends monthly overtime for approval before payroll.
- **Overtime limit alerts** by month, quarter, year and rolling averages, to employee and manager.
- **Regularization controls**: time window (N days), count limit per month, reason list, document required, keep original vs replace, block when on duty exists.
- **Hourly permission policy** rules (eligibility, allowed days, within shift time only).
- **Attendance administrators** (named non-HR staff for a group, such as a site supervisor).
- **Present-by-default groups** (senior staff, field staff without devices) as a clear setting.
- **Expected vs worked hours report** with carry-forward of balance hours.
- **Password-protected exports** of attendance data.
- **Attendance API** for other systems to post punches (part of our open API, but name attendance explicitly).
- **Night-work hours window** to drive night-shift allowance and women's night-shift checks.

### B.7 Shifts and rosters (15)

- **Shift patterns** (weekly, week-of-month, date-of-month, custom N-day cycle) mapped to groups, with conflict checks and a template gallery.
- **Auto shift assignment from check-in time** (closest, recently started, about to start) for flexible workforces such as retail, hospitals and BPOs.
- **Shift change requests** separate from swaps, with reason and approval.
- **Rich shift swap rules**: request-based vs direct swap, same-department only, request and acceptance cut-off hours, monthly limits, expiry, admin revert.
- **Fatigue and rest rules** that block rosters breaking minimum rest between shifts, maximum consecutive nights, or weekly hour limits. This is the natural place to enforce 8h/48h, weekly rest day and women's night-shift consent.
- **Shift allowance eligibility** (minimum hours, full or prorated) feeding payroll, including night-shift allowance.
- **Core working hours** within a shift, with alerts and break blocking.
- **Non-working hours alerts** when someone punches outside allowed hours.
- **Auto check-out by shift rule** (at shift end, fixed time, or N hours after check-in).
- **Shift margins** for payable hours.
- **Shift-based weekends and holidays** overriding location defaults.
- **Recalculate attendance when a past shift changes**, with a lock for processed pay periods.
- **Shift administrators** for site supervisors who are not HR.
- **Shift mapping import** (employee-wise and shift-wise).
- **Employee view of colleagues' schedules** (department), helpful for swaps.

### B.8 Timesheets (15)

- **Clients, projects and jobs (tasks) master** with owners, managers, budgets, estimated hours and billable flag.
- **Three ways to log time** (total hours, start-end, timer) with company choice per policy.
- **Logging rules**: no logging on leave/holidays, daily/weekly caps, within estimated hours, within attendance hours, no zero-hour logs, minimum rest gap.
- **Timesheet submission rules**: period (day/week/month), submission window dates, minimum and maximum hours, no overlapping timesheets.
- **Timesheet scheduler** that auto-creates and submits timesheets, plus reminders for missing or short timesheets.
- **Line-by-line approval** with partial approval, approver edits, predefined comments, and resubmission of rejected lines.
- **Job schedule / resource planning** (planned vs actual hours, publish and clone schedules, budget warnings).
- **Hourly wage rates per employee** for hourly and contract staff, with **minimum wage check** against the state table we already keep.
- **Billing from time logs**: bills per client with GST, auto bill numbers, and push to Tally, Zoho Books or QuickBooks.
- **Payroll preferences**: include only approved hours, include paid leave, include non-billable hours.
- **Time logs to attendance** (for people who only use timesheets) and **attendance limits on time logs** (cannot log more than worked).
- **Specific timesheet policies** by department, location or person.
- **IP and geo restrictions on timers**.
- **Full audit trail** of time log and timesheet events.
- **Project tool sync** (Jira, Asana and similar) for companies that already log work elsewhere.

### B.9 Compensation and payroll (19)

- **Salary range (minimum and maximum) on each salary structure**, with a warning when an offered or revised salary falls outside it.
- **Compa ratio** report: each person's pay against the middle of the range for their grade, shown to HR only.
- **Fixed allowance as an automatic balancing figure** (CTC minus all other parts), with a block if it turns negative.
- **"Hand entry" components** whose value is typed in each month (incentives, variable bonus) rather than calculated.
- **Revision reasons** (raise, correction, first salary, promotion) that can be renamed, and a reason on every revision.
- **Bulk revision letter run**: generate for a date range and group, check, then publish; retry failed ones; employee display name.
- **Choice of letter delivery**: password-protected PDF by email with password sent separately, or secure in-app link.
- **Re-authentication timer** for salary pages (ask again after 5 to 60 minutes), separate from login.
- **Separate compensation administrators** with location limits, and "Data admin" versus "Settings admin" split.
- **Role permissions split into "view total", "view breakup", "view benefits", "view revisions", "add revisions"**, each with all-staff or reportees-only scope.
- **Import of past salary history** (not only current salary) when moving from another system, plus single-component import such as a one-off bonus.
- **Sanction step for raises coming from performance reviews**, so payroll checks them before they become visible.
- **Salary-change lock while an appraisal or raise cycle is open** for that employee.
- **Rounding choice** (nearest, up, down, or two decimals) as a company setting.
- **Hourly, daily and weekly pay schedules** for wage workers and part-time staff (useful for factories and retail; pairs with our pay groups).
- **Sample data loader** for trial companies.
- **Check against the offer when converting a candidate to an employee**, so the salary structure in payroll matches the accepted offer.
- **Salary distribution and compensation growth reports** with optional breakup columns.
- **Indian amount-in-words** (lakh, crore) on letters and payslips.

### B.10 Benefits and group insurance (18)

- **Benefit plan catalogue**: plans for group health (GMC), group personal accident (GPA), group term life (GTL), top-up and parental cover, with provider, policy number, description, documents and website.
- **Insurer and broker (provider) records** with contacts and policy numbers.
- **Plan years** (usually the policy year, which may differ from the financial year), with clone for renewal.
- **Enrollment windows** at joining and at policy renewal, with a deadline countdown, progress tracking and "complete enrollment" to lock the list.
- **Coverage tiers** in Indian terms: employee only, employee + spouse, employee + spouse + up to N children, plus optional parents / parents-in-law, each with its own premium.
- **Sum insured options**: fixed amount or a multiple of annual salary (common for life cover), with minimum and maximum.
- **Premium split between employer and employee**, as percentage or flat amount, with the employee share deducted from salary in monthly instalments, pro-rata for mid-year joiners.
- **Age-band premiums** (especially for parental cover), with checks that bands do not overlap or leave gaps.
- **Eligibility rules**: by grade, location, employment type, age and salary; start date options (on joining, first of joining month, first of next month, after a waiting period); end date options on exit.
- **Waiver and mandatory flags**: base cover is mandatory; top-ups and parental cover can be waived.
- **Automatic enrollment of new joiners** in mandatory plans.
- **Automatic recalculation** of premium and deductions when salary or age band changes, and automatic removal when someone stops being eligible.
- **Dependent records** with import and export, reused for insurance, PF and gratuity nominees (one family list, many uses).
- **Enrollment statuses and history** (draft, awaiting approval, enrolled, waived, withdrawn, terminated) for audits and insurer reconciliation.
- **Insurer upload file**: monthly additions and deletions list (endorsements) for the insurer or broker.
- **Employee view of their cover**: e-card, sum insured, covered family members, policy documents.
- **Section 80D tax link**: the employee's share of health premium for parents feeds into the tax declaration where the regime allows it.
- **Decimal precision setting** for premiums and rates.

### B.11 Performance (23)

- Choice of evaluation method: KRAs, goals, both, or goals rolled up under KRAs, with weight rules (must be 100% or must not exceed 100%, min/max weight, multiples).
- KRA library tagged by department, designation and location so KRAs pre-fill for new joiners and role changes.
- Competency library with weightage, separate from skills.
- Skill levels with colours and descriptions, skill upgrade requests with reason and certificate, skill approval, skill matrix view.
- Goal-setting window with deadlines, extension requests and goal sheet approval (min/max goals per person).
- Appraisal cycle templates (ready-made and saved custom templates).
- Multi-rater rules: rater selection by employee or manager, rater types, max raters, hide rater status, block reviewers being raters.
- Lock and unlock of self and manager reviews; review extension requests; option to hide self review from manager until publish.
- Normalisation workflow details: ideal distribution, requests to managers, acknowledgement with decline, mandatory reason, publish only after normalisation, ideal vs actual report.
- Publish window, preview, unpublish, and employee's formal response to their review (agree/disagree with rules).
- Exclude employees on notice or on probation from cycles automatically.
- Backup/restore of cycle data, restart a completed cycle, delete requests approved by chosen admins.
- Compare a cycle with past cycles.
- Hike appraiser role separate from reviewer; hike as % or amount; push to payroll with sanction step and scheduled date.
- Appraisal letters sent automatically on publish with e-signature (up to 3 signers) and recall; signed copy stored in employee documents.
- Attendance and leave summary on the review page (attendance %, late check-ins, overtime, leave taken by type).
- Courses taken in the period shown in the review; reviewer can suggest courses.
- Performance vs potential nine-box grid with renamable boxes.
- Bulk import of ratings for ongoing cycles; import of past completed cycles (helps customers moving from another system).
- Rename terms (let each company use its own words like "KPI" instead of "KRA").
- Export controls: admins-only export and choosing employee fields in exports.
- Home page cards with deadlines for self review, manager review, rater feedback and pending goals.
- Reminder rate limits (prevent spamming employees).

### B.12 Goals and OKRs (10)

- Location (branch) level OKRs in addition to company, department and individual — useful for multi-branch Indian companies.
- Milestone-based key results (up to 10 milestones with dates and progress values) besides number-based key results.
- Sub-key results under a key result.
- Planned check-in dates with missed and delayed check-in flags, and a deviation % (planned vs actual).
- Alignment rules: a lower objective counts as a key result of the higher one; unalign with recalculation.
- Custom key result status list (3 to 10 statuses) with automatic status update from progress.
- Separate OKR administrators (not every admin).
- Optional approvals for adding objectives, adding key results and aligning objectives, with re-raise after rejection.
- OKR cycle with a start month (so companies can run April-to-March cycles matching the Indian financial year).
- OKR results shown on the performance review page.

### B.13 Learning (19)

- Learning plans (curricula) with levels, mandatory courses, course locks and completion outcomes.
- Classroom and online trainer-led courses with batches, capacity, waitlist, auto-cancel if too few, recurring batches.
- Session attendance marking, trainer and training room management with free/busy search.
- Item lock rules and completion rules (fully viewed vs marked done; no skipping in videos).
- SCORM package support (lets companies reuse bought e-learning content).
- Full assessment engine: question bank, random questions, negative marking, timers, attempts, result release rules, test security (IP range, tab switching, no copy/print).
- Grade book with grade categories, weightage and pass percentage.
- Certificate rules (on completion, pass, score or grade) with templates.
- Pre-tests and post-tests to measure learning gain.
- Manager evaluation after training with rubrics (measures on-the-job impact).
- Course feedback templates with NPS and trainer ratings.
- Employee course requests that admins convert into courses.
- Enrolment approval (org-wide or per course) and manager-led enrolment of reportees.
- Training cost tracking per course (useful for training budgets and cost per employee trained).
- Training calendar sync with Google and Microsoft calendars.
- Online manuals (e-material) for policies and SOPs.
- Learning reports: progress, enrolment, ratings, grades, by department, designation and location.
- Course completion upgrading skill levels and feeding performance reviews and succession.
- Mandatory-training compliance report for POSH awareness, fire safety and similar statutory trainings, with expiry and re-certification.

### B.14 Positions, succession and internal moves (11)

- Position (seat) management: approved positions with codes, seats, salary band, skills and a parent position, forming a position tree.
- Headcount plan and headcount change requests with approval; block joining or hiring when no seat is free.
- Filled / vacant / critical position views and a vacancy report by department.
- Department head permissions to view their positions and raise position or headcount requests.
- Resource request insights (vacancy, open requests, fulfilment rate, overdue requests) when a manager raises a hiring request.
- Replacement requests linked to an exit record.
- Auto-close and reopen of hiring requests based on vacancy.
- Internal job marketplace: employees list themselves (or are listed by managers) as available with skills and availability; fit score matching; hire as an internal transfer with an effective date.
- Succession planning for critical roles: plan manager, potential successors, nomination by peers or seniors, evaluation, development plan (goals, KRAs, competencies, courses).
- Internal talent pools (high potentials) with criteria-based search on performance and learning data.
- Matrix (dotted-line) reporting between positions.

### B.15 Files, letters and e-signature (20)

- **Mandatory acknowledgement or consent with an access lock**: after the deadline the employee cannot use the app until they acknowledge, with exempt roles. Very useful for POSH, standing orders and DPDP notices.
- **Consent with opt-in / opt-out records and dates**, exportable as proof for DPDP audits.
- **Separate file areas**: company files, employee files (HR-only), personal private uploads, department files shared by the department head, and manager-visible reportee files, each with on/off settings.
- **Personal private locker** that even admins cannot open, with a per-employee file limit.
- **Bulk upload of employee files from a zip** using a file-name rule (e.g. "EmployeeCode-DocumentName"), with or without folders. Very useful for migrating old records and Form 16 PDFs.
- **Automatic archiving** of old files on a schedule, plus a retention period setting (fits DPDP "keep only as long as needed").
- **Expiry reminder modes**: one email per file or a single weekly digest to HR.
- **IP and location restrictions for opening sensitive files**.
- **Restricting Files admin access to the owner only**, with separate identity-document administrators.
- **File version restore** and full activity log on every file action.
- **Bonafide letter request** (common in India for bank loans, visas and children's school admission), alongside address proof and experience letter.
- **Reason picklist on letter requests** (bank, visa, passport, rental, other) for reporting.
- **E-sign flow details**: signing order, sign or view-only recipients, email OTP check, signing deadline, expiry, automatic reminders, recall, cancel, in-person signing, per-recipient status and signature report.
- **Document prefill** from employee data into the signing document.
- **Signed documents automatically filed** into the employee's folder.
- **Regenerate exit letters** when the last working day changes, with a log of generate / regenerate / send.
- **Usage alerts for metered features** (e-sign and WhatsApp credits): warning at a percentage, failure notice when exhausted, auto-recharge option.
- **Virus scan, executable block and password-protected file check** on every upload.
- **"Mark as policy"** so policy files also appear in a separate Policies page.
- **Document download button on records** (generate and download a filled letter in one click).

### B.16 HR help desk (17)

- **Subcategories** with their own agents, and an option to make choosing a subcategory mandatory.
- **Location-based agents** in a category (each branch's HR handles its own people), with reassignment of open cases when agents change.
- **Two assignment models**: agents pick up from a shared queue, or a category owner assigns every new case.
- **Unpicked, unmanaged, escalated and following queues** as separate lists.
- **SLA escalation that also raises priority**, plus an SLA report with breach times.
- **Automatic reopen on a poor rating**, and mandatory feedback for chosen categories.
- **3-point or 5-point rating scale** with editable labels and thank-you message.
- **Restrictions**: stop requesters reopening closed cases, block comments on closed cases, hide priority.
- **Case types** (e.g. question, request, complaint), limited to a small set, alongside categories.
- **Editable status names and order**, and editable case sources (web, mobile, WhatsApp, email).
- **Raise a case for an employee** (for staff without logins, e.g. shop-floor workers who call HR).
- **Follow a case** and send reminders.
- **Bulk pick up, close, status change and delete** with sensible limits.
- **Link any request form to the help desk** so, for example, a letter request or a payroll query lands as a case with SLA tracking.
- **Category FAQs and reference documents** shown before a case is raised, to reduce repeat questions.
- **Require a reply before closing** a case.
- **Workflow and custom-button logs** for troubleshooting automations.

### B.17 Engagement and surveys (22)

- eNPS as a named survey type with Promoter / Passive / Detractor scoring and trend.
- Separate survey types with clear limits: eNPS (1 question), pulse (up to ~10 questions), annual engagement (up to ~200).
- Metrics library (engagement drivers such as work-life balance, recognition, growth) and metric-based scoring.
- Grouped-question and question-level metric structures.
- Question pool shared across surveys, with lock on questions already in use.
- Ready-made survey templates and a library of proven eNPS/engagement questions (localised for India).
- Anonymity rules: minimum response threshold before reports appear, identity choice (anonymous / optional / mandatory).
- Mandatory surveys with pop-up, final-days enforcement window and opt-out with reason (logged).
- Recurring surveys (daily weekdays, weekly, monthly) ending on a date or after N instances, with recurrence comparison report.
- Manual start/stop/reopen surveys.
- Survey report insights: heatmap by department/location/gender/tenure, favourable vs unfavourable, metric contribution to overall score, response rate by department.
- Comment keyword analysis (top positive and negative keywords).
- Non-respondent list and one-click reminder to non-respondents.
- Report sharing controls (owner-only by default; share with HR admins, department heads for their department, managers for their reportees).
- Survey administrators with location scope and separate settings/data rights; department heads and managers can run surveys for their own people only.
- Password-protected PDF exports of survey reports.
- Exit interview templates with trigger timing (on approval, X days/weeks before last day, on last day).
- Help-desk satisfaction: 3- or 5-point scale, mandatory for chosen categories, auto-reopen on poor rating.
- Feedback requests ("request feedback from a colleague") with decline option.
- Employee privacy choice to hide birthday, wedding anniversary and work anniversary from the feed (fits DPDP consent thinking).
- Wedding anniversary celebrations (common in Indian workplaces).
- Survey themes and cover images, and survey link sharing for frontline staff via WhatsApp.

### B.18 Tasks, approvals and automation (26)

- Approval for cancellation and revocation of already-approved requests (e.g. cancel approved leave).
- Requester follow-up / nudge button and approver reminders "every N days until approved".
- No-approver handling rules (fallback to admin, hold until approver exists).
- Skip duplicate approval levels when the same person appears twice.
- Employee chooses approver (with "not yourself" rule) for some request types.
- Forward / reassign a request to another approver; bulk reassign when someone leaves (up to a set number at once).
- Auto-reassign pending approvals and update approval configs when an approver exits.
- Approval timeline with time taken at each level, and report of pending duration (TAT reporting).
- Safe switch-off of an approval process (only new requests affected) and warnings when an edit would re-trigger approval.
- Workflow trigger list: on add, edit, field change (any/all fields), delete, approve, reject, date-based (monthly/yearly) with time zone.
- Time-based actions (e.g. 7 days before probation end, send reminder).
- Webhooks with success/failure logs and retry.
- Field update actions and custom buttons on records with criteria-based branches.
- Guided process flows (blueprints) with states, transition owners and transition checks.
- Custom validation rules on forms (block save if condition fails).
- Checklists with nested tasks, claimable tasks, custom task statuses, and "on checklist completion, do X".
- Schedulers: email scheduler, absent-marking scheduler, overtime auto-submission scheduler by pay period, timesheet auto-submission scheduler.
- Weekly rest compliance alert (no weekly off taken) — directly useful for Indian weekly-holiday rules.
- Dynamic applicability groups reused by all modules (attributes update membership automatically).
- Execution logs for every workflow, scheduler and function run.
- Sandbox / test copy of the account with deploy-to-live and change tracking (valuable for large customers changing payroll rules).
- Account verification before outbound emails (anti-spam / anti-abuse).
- Mail-merge / document generation usage limits per plan, with warnings.
- Profile photo change approval.
- Visitor entry approval (office visitor management) — optional add-on.
- Chat assistant guard-rails for later: role-based access, sensitive data excluded, usage meter, custom tone instructions.

### B.19 Data, reports and analytics (22)

- Employee profiles without login vs users with login (for factory/field staff who never log in), with convert in either direction and bulk login enable/disable.
- User invitation by SMS when no email; import users with mobile only.
- Import users from Google Workspace / Microsoft 365.
- Import options: add vs update by unique field, duplicate rules, background processing with emailed summary, error file link with expiry, row limits stated clearly.
- Import history and export history pages, plus "export in import format" for easy round-trip edits.
- Password-protected exports with the password sent separately (important for salary and PAN data).
- Limits on bulk exports (date ranges) and background export with emailed link.
- Specialised imports: punches, shifts, holidays from calendar files, appraisal ratings, resignations.
- Report-level data scope choices (my data, direct reportees, all reportees) and manager access toggles per report.
- Report sharing rules spelled out: what permissions apply, record caps for non-admins.
- Report pivot view, view underlying data, regenerate, clone.
- Home dashboard widgets that employees can customise, with admin-mandated company widgets.
- Dependency protection: block deleting fields/forms used in reports or schedulers.
- API usage meter (credits used/unused, daily trend) and token-based API security with retirement of old methods.
- Web tabs and custom widgets so customers can embed their own tools.
- Payroll data push controls: LOP report push, timesheet push for a date range, org-structure based sync — useful if a customer keeps an outside payroll during migration.
- Travel request (place, dates, purpose, billable to client, client name) linked to travel expense lines (ticket, lodging, boarding, phone, local conveyance, incidentals).
- Expected vs worked hours and scheduled vs worked hours reports; early/late bird report; missed break report.
- Manager-facing "reportee tree" and team availability reports.
- Sending-domain setup (from addresses and email authentication) so emails do not land in spam.
- Integration report showing successful, skipped and failed sync records.
- Report a bug / suggest a feature from inside the app.

### B.20 Platform, security and administration (35)

- **Company-run data backup and download**: let the owner take an on-demand or scheduled full backup (weekly, fortnightly, monthly) delivered by email link or to their own cloud folder, with a backup log. Our §21 covers our own backups, not a backup the customer can hold.
- **Recycle bin with restore**: deleted records go to a bin for a chosen number of months and can be restored by admins, with an email when done. This fits alongside our "archive, don't delete" rule for records that are allowed to be removed.
- **Close account flow**: guided, irreversible account closure with a backup step, tick-box consents, immediate login shutdown, and an option to drop to a free or read-only plan instead. Needed for DPDP erasure and for clean offboarding of customers.
- **Field, record and action permission rules with clear limits**: record-level view, add, edit and delete plus field-level view and edit per role, with the built-in rule that edit can never exceed view. Our §4 describes the goal but not this level of detail.
- **Data-masking access log**: log every time someone reveals a masked value (Aadhaar, bank, salary) and allow export; API returns masked values unless explicitly asked. Strong DPDP evidence.
- **Reason-for-change on sensitive fields**: a setting per form that forces a reason when an existing value is edited, stored in the audit trail.
- **Sign-in activity screen**: login history per user for admins, linked to suspicious-login alerts.
- **IP and map-area restrictions**: allow access to the whole product, or only to attendance marking, from set office IP ranges or map areas; ranges must not overlap.
- **Re-authentication before sensitive actions**: ask for the password or OTP again before changing roles, admin rights, bank details or payroll approval.
- **Mobile app protection**: app lock (PIN or fingerprint) and screenshot blocking on payslip and salary screens.
- **Domain verification, custom login address and branded login page**: verify a company domain by DNS record; offer `hr.company.com`, own logo, background and favicon on the login page.
- **Email sending set-up**: company "from" addresses, DKIM and DMARC checks, and an option to send through the company's own mail server. Improves delivery of payslip and OTP emails.
- **Super administrator and module administrators**: one undeletable account owner who receives billing mail, plus "module admins" who can manage only, for example, attendance for one branch.
- **Non-user employee records**: keep records for staff who never log in (for example factory workers on contract) without spending a paid login, and convert them to users later. Important for Indian blue-collar workforces.
- **Bulk login enable or disable** with automatic login shutdown on the exit date and an audit entry.
- **Invite vs direct add of users**, bulk invites and invitation tracking, with a customisable invite email.
- **Custom forms and custom modules builder**: let companies add their own forms (for example vehicle details, canteen registration) with sections, repeating tables, unique field combinations, drafts, and per-role permissions. Our A1 only mentions custom fields.
- **Reusable company variables** (for example authorised signatory name, company seal text) used in letters and emails, with read-only and hidden options.
- **OCR auto-fill from uploaded documents**: read PAN card, Aadhaar, bank cheque or certificates and fill the form; formats PDF and JPG.
- **Copy settings to a new account**: useful for group companies and for our implementation partners setting up similar customers.
- **Sample data and implementation guide**: optional demo data that can later be cleared module by module.
- **Rename terminology and org-structure labels**: let a company call "Department" "Function", or "Business unit" "Vertical".
- **Personal privacy choices**: let employees hide birthday, anniversary or mobile number from colleagues on the home page.
- **Full accessibility settings panel**: contrast, text size and spacing, focus outline, reading mask, link underline, switch labels, toast timing, dark mode, keyboard shortcuts and page navigator. Goes beyond "follows standards" in §24.
- **Home page widgets and spaces**: personal, team and company spaces, with admin-mandated widgets and a choice of standard widgets.
- **Global search** across people, settings, forms and help, plus saved and shared list views.
- **Plan limits and licence screen**: show licences used and left, storage used, add-ons, and next billing date; view-only mode when a subscription lapses rather than locking people out of payslips.
- **Developer extensions**: custom widgets, embedded web tabs and custom buttons for partners and large customers.
- **Indian payroll-partner sync** is not needed (we have our own payroll), but a **"push loss-of-pay and attendance to an external payroll"** export is worth having for customers who keep their old payroll during side-by-side runs (§23).
- **Rate limiting with clear messages** on APIs and heavy actions.
- **In-app support with screen recording and redaction**, plus bug/idea feedback, to cut support load.
- **Health insurance partner and rewards partner** connections listed as optional marketplace items.
- **Multi-organisation switcher** for consultants and payroll bureaus who manage several client companies with one login.
- **Notification master switch and per-user notification choices** (mentions, department posts, feed alerts).
- **Delegation permissions** (who may set up delegations for others) and visible delegation lists.
