"""Plain-language guidance for each compliance item, the annual filings we track, and the
law-change notices shown on the dashboard. Notices are informational: the values that
payroll uses live in the payroll statutory library (effective-dated), and notices that
come from that library are added automatically."""
from __future__ import annotations

GUIDANCE = {
    "pf": "Pay PF (employee and employer shares, EPS, EDLI and admin charges) and upload the ECR on the EPFO "
          "portal by the 15th of the next month. Late payment attracts interest (12% a year) and damages.",
    "esi": "Pay ESI contributions for covered employees (gross up to ₹21,000; ₹25,000 for disabled employees) by "
           "the 15th of the next month on the ESIC portal.",
    "tds": "Deposit the tax deducted from salaries by the 7th of the next month (30 April for March) with "
           "challan ITNS 281.",
    "pt": "Pay Professional Tax to the state by its due date. Rates and due dates differ by state.",
    "lwf": "Pay the Labour Welfare Fund contribution to the state board for the period. Most states collect it "
           "half-yearly or yearly.",
    "salary": "Under the Code on Wages, monthly wages must be paid by the 7th of the next month.",
    "tds_return": "File the quarterly TDS return (Form 24Q) for salaries. A late return costs ₹200 a day.",
    "certificate": "Give every employee their Form 16 (salary TDS certificate) by 15 June.",
    "registers": "Keep the wage register, muster roll, overtime register and deductions register for the month "
                 "(Code on Wages, OSH Code). They can be kept electronically; the payroll and attendance reports "
                 "produce them.",
    "licence": "Renew the registration before it expires. An expired Shops and Establishments, Professional "
               "Tax or Labour Welfare registration can lead to penalties during an inspection.",
    "posh_report": "The Internal Committee must give an annual report (number of complaints, how they were "
                   "handled, workshops held) to the employer, who forwards it to the District Officer. Most "
                   "states expect it by 31 January for the previous calendar year.",
    "bonus_return": "File the annual bonus return (Form D under the Payment of Bonus Rules) within 30 days of "
                    "paying the bonus, and by 1 February at the latest.",
    "min_wage": "Pay at least the minimum wage notified for the state, zone and skill. Paying less is an "
                "offence; correct the salary and pay the difference.",
    "missing_ids": "PAN is needed for TDS (without it tax is deducted at 20%), UAN for PF and a bank account for "
                   "salary transfers. Ask these people to add them, or add them from their profile.",
    "settlement": "Under the Code on Wages, the full and final settlement must be paid within 2 working days of "
                  "the last working day.",
    "overtime": "The OSH Code limits overtime to 125 hours in a quarter (states may set their own limit), and "
                "overtime must be paid at twice the ordinary rate.",
    "policy_ack": "These people have not accepted a policy that needs their acknowledgement (for example the "
                  "POSH policy). Send a reminder from Documents → Policies.",
}

# Annual filings tracked here (month, day of the due date; the period is the year before).
ANNUAL = [
    {"kind": "posh_report", "label": "POSH annual report", "month": 1, "day": 31},
    {"kind": "bonus_return", "label": "Bonus annual return (Form D)", "month": 2, "day": 1},
]

# Law-change notices. Each has an id, the date it takes effect and plain text.
NOTICES = [
    {"id": "labour-codes-2025", "date": "2025-11-21", "title": "The four labour codes are in force",
     "body": "The Code on Wages, the Industrial Relations Code, the Social Security Code and the OSH Code replace "
             "29 older labour laws. Wages must be at least 50% of pay for PF, gratuity and bonus, wages are due "
             "by the 7th, and final settlements within 2 working days.",
     "tags": ["wages", "pf", "gratuity"]},
    {"id": "new-regime-default", "date": "2025-04-01", "title": "New tax regime is the default",
     "body": "Employees are taxed under the new regime unless they choose the old one. The rebate makes income "
             "up to ₹12 lakh tax-free (₹12.75 lakh for salaried people with the standard deduction).",
     "tags": ["tds"]},
    {"id": "gratuity-fixed-term", "date": "2025-11-21", "title": "Gratuity for fixed-term employees after 1 year",
     "body": "Under the Social Security Code, fixed-term employees are entitled to gratuity after one year of "
             "continuous service instead of five.",
     "tags": ["gratuity"]},
    {"id": "esi-wage-ceiling", "date": "2017-01-01", "title": "ESI covers gross wages up to ₹21,000",
     "body": "Employees earning up to ₹21,000 a month (₹25,000 for people with disabilities) are covered by ESI. "
             "Contributions are 0.75% by the employee and 3.25% by the employer.",
     "tags": ["esi"]},
]
