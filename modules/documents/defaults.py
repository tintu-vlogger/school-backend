"""Default header and footer designs and a ready template for every kind of letter.
Companies get these once and edit them freely."""

SIGN_OFF = """<p style="margin-top:10mm">For <strong>{{legal_entity.name}}</strong></p>
<p style="margin-top:14mm">{{letter.signatory_name}}<br><span class="muted">{{letter.signatory_designation}}</span></p>"""

REF_LINE = """<table class="plain"><tr><td>Ref: {{letter.reference_no}}</td><td class="right">Date: {{letter.date|date}}</td></tr></table>"""

ADDRESSEE = """<p>{{employee.title}} {{employee.full_name}}<br>Employee ID: {{employee.employee_code}}<br>{{employee.address}}</p>"""

HEADERS = [
    ("Classic with logo", 28, True, """<table class="plain" style="width:100%"><tr>
<td style="width:55%;vertical-align:middle">{{company.logo}}<div style="font-size:15pt;letter-spacing:0.02em">{{company.name}}</div></td>
<td class="right muted" style="font-size:8.5pt;vertical-align:middle">{{company.address}}<br>{{company.phone}} · {{company.email}}</td>
</tr></table><hr style="margin:3mm 0 0;border-top:1.2pt solid #4f46e5">"""),
    ("Centred letterhead", 30, False, """<div class="center">{{company.logo}}
<div style="font-size:16pt;letter-spacing:0.08em;text-transform:uppercase">{{company.name}}</div>
<div class="muted" style="font-size:8.5pt">{{company.address}}</div></div><hr style="margin:3mm 0 0">"""),
]

FOOTERS = [
    ("Registered office", 16, True, """<hr style="margin:0 0 2mm"><table class="plain" style="font-size:7.5pt;color:#6b7080"><tr>
<td>{{legal_entity.name}} · CIN {{legal_entity.cin}}<br>Registered office: {{legal_entity.address}}</td>
<td class="right" style="vertical-align:bottom">Page {{page.number}} of {{page.total}}</td></tr></table>"""),
    ("Page number only", 8, False, """<div class="center muted" style="font-size:8pt">{{company.name}} · Page {{page.number}} of {{page.total}}</div>"""),
]

MONEY = "money"

# letter_type: (name, subject, body, extra_fields, self_service, esign_required)
TEMPLATES = {
    "offer": ("Offer letter", "Offer of employment", f"""{REF_LINE}{ADDRESSEE}
<p><strong>Subject: Offer of employment</strong></p>
<p>Dear {{{{employee.first_name}}}},</p>
<p>We are pleased to offer you the position of <strong>{{{{job.designation}}}}</strong> in the {{{{job.department}}}} team at
{{{{legal_entity.name}}}}, based at {{{{job.location}}}}. We expect you to join on or before <strong>{{{{job.joining_date|date}}}}</strong>.</p>
<p>Your annual cost to company will be <strong>{{{{salary.ctc|money}}}}</strong> ({{{{salary.ctc|words}}}}). The break-up is given below.</p>
<table><tr><th>Component</th><th class="right">Monthly</th></tr>
<tr><td>Basic</td><td class="right">{{{{salary.basic|money}}}}</td></tr>
<tr><td>House rent allowance</td><td class="right">{{{{salary.hra|money}}}}</td></tr>
<tr><td>Special allowance</td><td class="right">{{{{salary.special_allowance|money}}}}</td></tr>
<tr><th>Gross</th><th class="right">{{{{salary.gross_monthly|money}}}}</th></tr></table>
<p>You will be on probation for {{{{job.probation_months}}}} months. This offer depends on satisfactory reference and background
checks and on the documents you submit at joining. A detailed appointment letter will be issued when you join.</p>
<p>Please sign this letter to accept the offer. We look forward to working with you.</p>{SIGN_OFF}""",
              [{"key": "salary.ctc", "label": "Annual CTC", "type": MONEY, "required": True},
               {"key": "salary.basic", "label": "Basic (monthly)", "type": MONEY},
               {"key": "salary.hra", "label": "HRA (monthly)", "type": MONEY},
               {"key": "salary.special_allowance", "label": "Special allowance (monthly)", "type": MONEY},
               {"key": "salary.gross_monthly", "label": "Gross (monthly)", "type": MONEY},
               {"key": "job.probation_months", "label": "Probation (months)", "type": "number"}], False, True),
    "appointment": ("Appointment letter", "Letter of appointment", f"""{REF_LINE}{ADDRESSEE}
<p><strong>Subject: Letter of appointment</strong></p>
<p>Dear {{{{employee.first_name}}}},</p>
<p>With reference to your application and the discussions you had with us, we are pleased to appoint you as
<strong>{{{{job.designation}}}}</strong> with effect from {{{{job.joining_date|date}}}} on the following terms.</p>
<ol>
<li><strong>Place of work.</strong> You will be based at {{{{job.location}}}}. You may be transferred to any office of the company.</li>
<li><strong>Salary.</strong> Your annual cost to company is {{{{salary.ctc|money}}}}, paid monthly, subject to deductions under law
(provident fund, employees' state insurance, professional tax and income tax as applicable).</li>
<li><strong>Probation.</strong> You will be on probation for {{{{job.probation_months}}}} months, which may be extended. On successful
completion you will be confirmed in writing.</li>
<li><strong>Working hours and leave.</strong> As per the company's standing orders and policies, within the limits of the applicable
labour codes and Shops and Establishments Act.</li>
<li><strong>Notice period.</strong> After confirmation, either side may end this appointment with {{{{job.notice_period_days}}}} days'
notice or salary in lieu of notice.</li>
<li><strong>Confidentiality.</strong> You will keep the company's information confidential during and after your employment.</li>
<li><strong>Policies.</strong> You will follow the company's policies, including the code of conduct and the Prevention of Sexual
Harassment (POSH) policy, as amended from time to time.</li>
</ol>
<p>Please sign this letter to confirm that you accept these terms.</p>{SIGN_OFF}""",
                    [{"key": "salary.ctc", "label": "Annual CTC", "type": MONEY, "required": True},
                     {"key": "job.probation_months", "label": "Probation (months)", "type": "number"}], False, True),
    "confirmation": ("Confirmation letter", "Confirmation of employment", f"""{REF_LINE}{ADDRESSEE}
<p><strong>Subject: Confirmation of employment</strong></p>
<p>Dear {{{{employee.first_name}}}},</p>
<p>We are happy to inform you that, on successful completion of your probation, your employment as
<strong>{{{{job.designation}}}}</strong> is confirmed with effect from <strong>{{{{job.confirmation_date|date}}}}</strong>.</p>
<p>All other terms of your appointment remain the same. Your notice period from now on is {{{{job.notice_period_days}}}} days.</p>
<p>We thank you for your contribution and wish you continued success.</p>{SIGN_OFF}""", [], False, False),
    "increment": ("Increment letter", "Revision of salary", f"""{REF_LINE}{ADDRESSEE}
<p><strong>Subject: Revision of salary</strong></p>
<p>Dear {{{{employee.first_name}}}},</p>
<p>In recognition of your contribution, we are pleased to revise your annual cost to company from
{{{{salary.previous_ctc|money}}}} to <strong>{{{{salary.ctc|money}}}}</strong> ({{{{salary.ctc|words}}}}), an increase of
{{{{salary.increment_percent}}}}%, with effect from <strong>{{{{salary.effective_from|date}}}}</strong>.</p>
<p>All other terms of your employment remain unchanged. This letter is confidential.</p>{SIGN_OFF}""",
                  [{"key": "salary.previous_ctc", "label": "Previous annual CTC", "type": MONEY},
                   {"key": "salary.ctc", "label": "Revised annual CTC", "type": MONEY, "required": True},
                   {"key": "salary.increment_percent", "label": "Increment %", "type": "number"},
                   {"key": "salary.effective_from", "label": "Effective from", "type": "date", "required": True}], False, False),
    "promotion": ("Promotion letter", "Promotion", f"""{REF_LINE}{ADDRESSEE}
<p><strong>Subject: Promotion</strong></p>
<p>Dear {{{{employee.first_name}}}},</p>
<p>We are delighted to promote you from {{{{job.designation}}}} to <strong>{{{{job.new_designation}}}}</strong> with effect from
<strong>{{{{job.effective_date|date}}}}</strong>. Your revised annual cost to company will be {{{{salary.ctc|money}}}}.</p>
<p>This promotion reflects your performance and the trust we place in you. All other terms of your employment remain unchanged.</p>
{SIGN_OFF}""",
                  [{"key": "job.new_designation", "label": "New designation", "type": "text", "required": True},
                   {"key": "job.effective_date", "label": "Effective date", "type": "date", "required": True},
                   {"key": "salary.ctc", "label": "Revised annual CTC", "type": MONEY}], False, False),
    "transfer": ("Transfer letter", "Transfer", f"""{REF_LINE}{ADDRESSEE}
<p><strong>Subject: Transfer</strong></p>
<p>Dear {{{{employee.first_name}}}},</p>
<p>You are transferred from {{{{job.location}}}} ({{{{job.department}}}}) to <strong>{{{{job.new_location}}}}</strong>
({{{{job.new_department}}}}) with effect from <strong>{{{{job.effective_date|date}}}}</strong>.</p>
<p>Please hand over your current responsibilities before you move and report to the new location on the effective date. Relocation
support, if any, will be as per the company's transfer policy. All other terms of your employment remain unchanged.</p>{SIGN_OFF}""",
                 [{"key": "job.new_location", "label": "New location", "type": "text", "required": True},
                  {"key": "job.new_department", "label": "New department", "type": "text"},
                  {"key": "job.effective_date", "label": "Effective date", "type": "date", "required": True}], False, False),
    "experience": ("Experience letter", "Experience certificate", f"""{REF_LINE}
<p class="center" style="margin:6mm 0"><strong>TO WHOM IT MAY CONCERN</strong></p>
<p>This is to certify that <strong>{{{{employee.title}}}} {{{{employee.full_name}}}}</strong> (Employee ID {{{{employee.employee_code}}}})
worked with {{{{legal_entity.name}}}} from <strong>{{{{job.joining_date|date}}}}</strong> to <strong>{{{{job.last_working_day|date}}}}</strong>.
At the time of leaving, the designation was <strong>{{{{job.designation}}}}</strong> in the {{{{job.department}}}} department.</p>
<p>During this period of {{{{job.tenure}}}}, we found the work and conduct satisfactory. We wish {{{{employee.first_name}}}} success in the future.</p>
{SIGN_OFF}""", [], True, False),
    "relieving": ("Relieving letter", "Relieving letter", f"""{REF_LINE}{ADDRESSEE}
<p><strong>Subject: Relieving letter</strong></p>
<p>Dear {{{{employee.first_name}}}},</p>
<p>This refers to your resignation. You are relieved from the services of {{{{legal_entity.name}}}} at the close of business on
<strong>{{{{job.last_working_day|date}}}}</strong>. You worked with us as {{{{job.designation}}}} from {{{{job.joining_date|date}}}}.</p>
<p>Your full and final settlement will be paid as per law. We remind you of your continuing obligation to keep the company's
information confidential. We thank you for your contribution and wish you the very best.</p>{SIGN_OFF}""", [], False, False),
    "address_proof": ("Address proof letter", "Address proof", f"""{REF_LINE}
<p class="center" style="margin:6mm 0"><strong>TO WHOM IT MAY CONCERN</strong></p>
<p>This is to certify that <strong>{{{{employee.title}}}} {{{{employee.full_name}}}}</strong> (Employee ID {{{{employee.employee_code}}}})
is working with {{{{legal_entity.name}}}} as {{{{job.designation}}}} since {{{{job.joining_date|date}}}}.</p>
<p>As per our records, the residential address is:<br><strong>{{{{employee.address}}}}</strong></p>
<p>This letter is issued on request for the purpose of {{{{letter.reason}}}}.</p>{SIGN_OFF}""", [], True, False),
    "salary_certificate": ("Salary certificate", "Salary certificate", f"""{REF_LINE}
<p class="center" style="margin:6mm 0"><strong>SALARY CERTIFICATE</strong></p>
<p>This is to certify that <strong>{{{{employee.title}}}} {{{{employee.full_name}}}}</strong> (Employee ID {{{{employee.employee_code}}}})
is employed with {{{{legal_entity.name}}}} as {{{{job.designation}}}} since {{{{job.joining_date|date}}}}.</p>
<p>The current annual cost to company is <strong>{{{{salary.ctc|money}}}}</strong> and the monthly gross salary is
{{{{salary.gross_monthly|money}}}}.</p>
<p>This certificate is issued on request for the purpose of {{{{letter.reason}}}} and does not create any liability for the company.</p>
{SIGN_OFF}""", [{"key": "salary.ctc", "label": "Annual CTC", "type": MONEY},
                {"key": "salary.gross_monthly", "label": "Gross (monthly)", "type": MONEY}], True, False),
    "bonafide": ("Bonafide letter", "Bonafide certificate", f"""{REF_LINE}
<p class="center" style="margin:6mm 0"><strong>TO WHOM IT MAY CONCERN</strong></p>
<p>This is to certify that <strong>{{{{employee.title}}}} {{{{employee.full_name}}}}</strong> is a bonafide employee of
{{{{legal_entity.name}}}}, working as {{{{job.designation}}}} since {{{{job.joining_date|date}}}}.</p>
<p>This letter is issued on request for the purpose of {{{{letter.reason}}}}.</p>{SIGN_OFF}""", [], True, False),
    "warning": ("Warning letter", "Warning", f"""{REF_LINE}{ADDRESSEE}
<p><strong>Subject: Warning</strong></p>
<p>Dear {{{{employee.first_name}}}},</p>
<p>It has been brought to our notice that {{{{letter.details}}}}.</p>
<p>This conduct is not acceptable and goes against the company's code of conduct. You are hereby warned to ensure that this is
not repeated. Any repetition will lead to disciplinary action as per the company's standing orders.</p>
<p>Please sign a copy of this letter as acknowledgement of receipt.</p>{SIGN_OFF}""",
                [{"key": "letter.details", "label": "What happened", "type": "textarea", "required": True}], False, True),
    "show_cause": ("Show-cause notice", "Show-cause notice", f"""{REF_LINE}{ADDRESSEE}
<p><strong>Subject: Show-cause notice</strong></p>
<p>Dear {{{{employee.first_name}}}},</p>
<p>It is alleged that {{{{letter.details}}}}.</p>
<p>If proved, this would amount to misconduct under the company's standing orders. You are hereby asked to explain in writing,
within <strong>{{{{letter.reply_days}}}} days</strong> of receiving this notice, why disciplinary action should not be taken against you.
If no reply is received within this time, it will be assumed that you have nothing to say, and the company will proceed as it
thinks fit.</p>{SIGN_OFF}""",
                   [{"key": "letter.details", "label": "Allegation", "type": "textarea", "required": True},
                    {"key": "letter.reply_days", "label": "Days to reply", "type": "number", "required": True}], False, True),
    "termination": ("Termination letter", "Termination of employment", f"""{REF_LINE}{ADDRESSEE}
<p><strong>Subject: Termination of employment</strong></p>
<p>Dear {{{{employee.first_name}}}},</p>
<p>{{{{letter.details}}}}</p>
<p>Accordingly, your employment with {{{{legal_entity.name}}}} stands terminated with effect from
<strong>{{{{job.last_working_day|date}}}}</strong>. Your full and final settlement, including wages in lieu of notice where due, will be
paid as per law. Please return all company property and complete the exit formalities.</p>{SIGN_OFF}""",
                    [{"key": "letter.details", "label": "Reason and background", "type": "textarea", "required": True}],
                    False, False),
}
