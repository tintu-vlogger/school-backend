"""Statutory joining forms, filled from the employee record and nominees:

    pf_form2         EPF Form 2 (revised): nomination and declaration for PF and EPS
    pf_form11        EPF Form 11: declaration by a new employee (previous membership, UAN)
    esi_declaration  ESI Form 1: declaration form with family details
    gratuity_form_f  Gratuity Form F: nomination under the Payment of Gratuity rules

The forms follow the official layouts closely enough to be printed, signed and filed;
the employee signs them (by hand or electronically through Letters)."""
from __future__ import annotations

import html
from datetime import date

from sqlalchemy.orm import Session

FORMS = {
    "pf_form2": "EPF Form 2: nomination and declaration",
    "pf_form11": "EPF Form 11: new employee declaration",
    "esi_declaration": "ESI declaration (Form 1)",
    "gratuity_form_f": "Gratuity nomination (Form F)",
}


def _e(x) -> str:
    if x is None or x == "":
        return "&nbsp;"
    if isinstance(x, date):
        return x.strftime("%d/%m/%Y")
    return html.escape(str(x))


def _data(db: Session, company_id: int, emp) -> dict:
    from core.masters import labels_for
    from modules.employees.models import FamilyMember, Nominee, PreviousEmployment
    from modules.recruitment import common as c
    fam = db.query(FamilyMember).filter(FamilyMember.company_id == company_id, FamilyMember.employee_id == emp.id).all()
    noms = db.query(Nominee).filter(Nominee.company_id == company_id, Nominee.employee_id == emp.id).all()
    prev = (db.query(PreviousEmployment).filter(PreviousEmployment.company_id == company_id,
                                                PreviousEmployment.employee_id == emp.id)
            .order_by(PreviousEmployment.to_date.desc().nullslast()).all())
    ids = [emp.gender_id, emp.marital_status_id, *[f.relation_id for f in fam], *[n.relation_id for n in noms]]
    labels = labels_for(db, company_id, [i for i in ids if i])
    comp = c.company(db, company_id)
    addr = emp.permanent_address or emp.current_address or {}
    address = ", ".join(str(addr.get(k)) for k in ("line1", "line2", "city", "pincode") if addr.get(k))
    return {"emp": emp, "fam": fam, "noms": noms, "prev": prev, "labels": labels, "company": comp,
            "address": address, "name": f"{emp.first_name} {emp.middle_name + ' ' if emp.middle_name else ''}{emp.last_name}".strip()}


def _head(title: str, sub: str, d: dict) -> str:
    comp = d["company"]
    return (f'<div class="center"><h2 style="margin-bottom:2pt">{html.escape(title)}</h2>'
            f'<p class="muted" style="margin-bottom:8pt">{html.escape(sub)}</p></div>'
            f'<table class="plain"><tr><td><strong>Establishment:</strong> {_e(comp.company_name if comp else "")}</td>'
            f'<td class="right"><strong>Employee ID:</strong> {_e(d["emp"].employee_code)}</td></tr></table>')


def _person(d: dict) -> str:
    emp, lab = d["emp"], d["labels"]
    return ("<table>"
            f"<tr><th style='width:38%'>Name</th><td>{_e(d['name'])}</td></tr>"
            f"<tr><th>Father's / husband's name</th><td>{_e(emp.father_name or emp.spouse_name)}</td></tr>"
            f"<tr><th>Date of birth</th><td>{_e(emp.date_of_birth)}</td></tr>"
            f"<tr><th>Gender</th><td>{_e(lab.get(emp.gender_id))}</td></tr>"
            f"<tr><th>Marital status</th><td>{_e(lab.get(emp.marital_status_id))}</td></tr>"
            f"<tr><th>Date of joining</th><td>{_e(emp.date_of_joining)}</td></tr>"
            f"<tr><th>UAN</th><td>{_e(emp.uan)}</td></tr>"
            f"<tr><th>Aadhaar (last 4)</th><td>{_e(('XXXX XXXX ' + emp.aadhaar_last4) if emp.aadhaar_last4 else '')}</td></tr>"
            f"<tr><th>PAN</th><td>{_e(emp.pan)}</td></tr>"
            f"<tr><th>Permanent address</th><td>{_e(d['address'])}</td></tr>"
            "</table>")


def _nominee_table(d: dict, kinds: tuple[str, ...]) -> str:
    rows = [n for n in d["noms"] if n.kind in kinds]
    lab = d["labels"]
    body = "".join(f"<tr><td>{_e(n.name)}</td><td>{_e(n.address)}</td><td>{_e(lab.get(n.relation_id))}</td>"
                   f"<td>{_e(n.date_of_birth)}</td><td class='right'>{_e(n.share_percent)}%</td>"
                   f"<td>{_e(n.guardian_name)}</td></tr>" for n in rows)
    if not body:
        body = "<tr><td colspan='6' class='muted'>No nominee recorded yet</td></tr>"
    return ("<table><tr><th>Name of nominee</th><th>Address</th><th>Relationship</th><th>Date of birth</th>"
            "<th class='right'>Share</th><th>Guardian (if minor)</th></tr>" + body + "</table>")


def _family_table(d: dict) -> str:
    lab = d["labels"]
    body = "".join(f"<tr><td>{_e(f.name)}</td><td>{_e(lab.get(f.relation_id))}</td><td>{_e(f.date_of_birth)}</td>"
                   f"<td>{'Yes' if f.lives_with_employee else 'No'}</td></tr>" for f in d["fam"])
    if not body:
        body = "<tr><td colspan='4' class='muted'>No family members recorded yet</td></tr>"
    return "<table><tr><th>Name</th><th>Relationship</th><th>Date of birth</th><th>Living with employee</th></tr>" + body + "</table>"


def _sign(d: dict) -> str:
    return ("<table class='plain' style='margin-top:18mm'><tr>"
            "<td>Place: ______________<br>Date: ______________</td>"
            f"<td class='right'>______________________<br>Signature of {html.escape(d['name'])}</td></tr></table>"
            "<p class='muted' style='margin-top:8mm;font-size:8.5pt'>Certificate by employer: certified that the above "
            "declaration and nomination has been signed by the employee before me after the particulars were read "
            "over to them.</p><table class='plain'><tr><td></td><td class='right'>______________________<br>"
            "Signature of the employer or authorised officer</td></tr></table>")


def pf_form2(d: dict) -> str:
    return (_head("Form 2 (Revised)", "Nomination and declaration form for unexempted / exempted establishments "
                  "(Employees' Provident Funds and Employees' Pension Scheme)", d)
            + "<h3>Part A: details of the member</h3>" + _person(d)
            + "<h3>Part A (EPF): nomination for provident fund</h3>"
            "<p>I hereby nominate the person(s) below to receive the amount standing to my credit in the Employees' "
            "Provident Fund in the event of my death.</p>" + _nominee_table(d, ("pf",))
            + "<h3>Part B (EPS): family members eligible for widow/children pension</h3>" + _family_table(d)
            + "<p>Nominee for monthly widow pension where the member has no family:</p>" + _nominee_table(d, ("eps",))
            + _sign(d))


def pf_form11(d: dict) -> str:
    emp = d["emp"]
    prev = d["prev"][0] if d["prev"] else None
    was_member = bool(emp.uan or (prev and prev.previous_pf_member_id))
    return (_head("Form 11 (Declaration form)", "To be retained by the employer for future reference "
                  "(Employees' Provident Fund Organisation)", d)
            + _person(d)
            + "<h3>Previous employment</h3><table>"
            f"<tr><th style='width:58%'>Earlier a member of the Employees' Provident Fund Scheme?</th><td>{'Yes' if was_member else 'No'}</td></tr>"
            f"<tr><th>Earlier a member of the Employees' Pension Scheme?</th><td>{'Yes' if was_member else 'No'}</td></tr>"
            f"<tr><th>Universal Account Number (UAN)</th><td>{_e(emp.uan)}</td></tr>"
            f"<tr><th>Previous PF account number</th><td>{_e(prev.previous_pf_member_id if prev else '')}</td></tr>"
            f"<tr><th>Previous employer</th><td>{_e(prev.company_name if prev else '')}</td></tr>"
            f"<tr><th>Date of exit from previous employment</th><td>{_e(prev.to_date if prev else '')}</td></tr>"
            f"<tr><th>International worker?</th><td>No</td></tr>"
            "</table><h3>Undertaking</h3><p>I certify that all the information given above is true to the best of my "
            "knowledge and belief. In case of a change in any of the above, I will inform the employer at once. I "
            "authorise the employer to use my Aadhaar for verification with the UIDAI and the EPFO for PF purposes.</p>"
            + _sign(d))


def esi_declaration(d: dict) -> str:
    emp = d["emp"]
    return (_head("Declaration form (Form 1)", "Employees' State Insurance Corporation, under the Code on Social "
                  "Security, 2020", d)
            + _person(d)
            + "<table>"
            f"<tr><th style='width:38%'>Insurance number (if already insured)</th><td>{_e(emp.esi_number)}</td></tr>"
            f"<tr><th>Present address</th><td>{_e(', '.join(str((emp.current_address or {}).get(k)) for k in ('line1', 'city', 'pincode') if (emp.current_address or {}).get(k)))}</td></tr>"
            f"<tr><th>Phone</th><td>{_e(emp.phone)}</td></tr></table>"
            "<h3>Particulars of family members</h3>" + _family_table(d)
            + "<h3>Nominee for payment of dependants' benefit and funeral expenses</h3>"
            + _nominee_table(d, ("insurance", "other"))
            + "<p class='muted'>ESI applies when monthly wages are at or below the wage ceiling (₹21,000, ₹25,000 for "
              "persons with disability). The employer registers the employee on the ESIC portal within 10 days of joining.</p>"
            + _sign(d))


def gratuity_form_f(d: dict) -> str:
    return (_head("Form F", "Nomination under the Payment of Gratuity rules (Code on Social Security, 2020)", d)
            + "<p>To the employer: I, the undersigned, nominate the person(s) below to receive the gratuity payable "
              "after my death, and the gratuity standing to my credit if it has not been paid before my death, in "
              "the shares shown.</p>"
            + _nominee_table(d, ("gratuity",))
            + "<p>I certify that the person(s) named are members of my family within the meaning of the law. I "
              "declare that I have no family / the nomination covers my family as required.</p>"
            + "<h3>Statement</h3>" + _person(d) + _sign(d))


BUILDERS = {"pf_form2": pf_form2, "pf_form11": pf_form11, "esi_declaration": esi_declaration,
            "gratuity_form_f": gratuity_form_f}


def render(db: Session, company_id: int, emp, kind: str) -> tuple[str, bytes]:
    from modules.documents import pdf
    if kind not in BUILDERS:
        raise ValueError(kind)
    d = _data(db, company_id, emp)
    body = BUILDERS[kind](d)
    doc = pdf.pdf_html(body, header=None, footer=None, header_mm=0, footer_mm=0, title=FORMS[kind], font_size=10,
                       margins=(14, 16, 14, 16))
    return FORMS[kind], pdf.render_pdf(doc)
