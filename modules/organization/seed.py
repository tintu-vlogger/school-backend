"""Default organisation data for every company (grades, a skills library, starter tags
and a primary legal entity from the company's own details), and in development a full
demo organisation for the demo company so every screen has realistic Indian data.

`ensure_defaults` runs from company setup (sign-up and every start) and only adds what is
missing. `seed_demo_people` runs at start in development only and is skipped once done."""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from modules.organization.models import (CostCentre, EmployeeSkill, Grade, LegalEntity, Location, Region, Skill, Tag,
                                         TagLink, Team, TeamMember)

DEFAULT_GRADES = [
    ("G1", "Associate", "Band A"), ("G2", "Senior associate", "Band A"), ("G3", "Lead", "Band B"),
    ("G4", "Manager", "Band B"), ("G5", "Senior manager", "Band C"), ("G6", "Director", "Band C"),
    ("G7", "Leadership", "Band D"),
]

# (skill, skill_category code of a sub-value or top value)
DEFAULT_SKILLS = [
    ("Python", "programming"), ("Java", "programming"), ("JavaScript", "programming"), ("React", "programming"),
    ("SQL", "data_and_analytics"), ("Power BI", "data_and_analytics"), ("Excel", "data_and_analytics"),
    ("AWS", "cloud_and_infrastructure"), ("Kubernetes", "cloud_and_infrastructure"), ("Figma", "design"),
    ("Tally", "finance_and_accounts"), ("GST compliance", "finance_and_accounts"), ("Payroll processing", "hr"),
    ("Recruitment", "hr"), ("B2B sales", "sales"), ("Negotiation", "sales"), ("Digital marketing", "marketing"),
    ("Content writing", "marketing"), ("Stakeholder management", "leadership"), ("Public speaking", "communication"),
    ("English", "english"), ("Hindi", "hindi"), ("Kannada", "kannada"), ("Tamil", "tamil"), ("Marathi", "marathi"),
]

DEFAULT_TAGS = [
    ("Key talent", "#4f46e5", ["High potential", "Successor"]),
    ("New joiner", "#059669", []),
    ("Certifications", "#0891b2", ["AWS certified", "PMP", "Chartered accountant"]),
    ("Programmes", "#d97706", ["Graduate trainee", "Women in tech", "Mentor"]),
    ("Retention risk", "#e11d48", []),
]


def _master_ids(db: Session, company_id: int, key: str) -> dict[str, int]:
    from core.masters import MasterType, MasterValue
    t = db.query(MasterType).filter(MasterType.company_id == company_id, MasterType.key == key).first()
    if t is None:
        return {}
    out = {}
    for v in db.query(MasterValue).filter(MasterValue.company_id == company_id, MasterValue.type_id == t.id):
        out[(v.code or "").lower()] = v.id
        out[v.label.lower()] = v.id
    return out


def ensure_defaults(db: Session, company_id: int) -> None:
    if not db.query(Grade.id).filter(Grade.company_id == company_id).first():
        for i, (code, name, band) in enumerate(DEFAULT_GRADES, start=1):
            db.add(Grade(company_id=company_id, code=code, name=name, band=band, rank=i))
    if not db.query(Skill.id).filter(Skill.company_id == company_id).first():
        cats = _master_ids(db, company_id, "skill_category")
        for name, cat in DEFAULT_SKILLS:
            db.add(Skill(company_id=company_id, name=name, category_id=cats.get(cat)))
    if not db.query(Tag.id).filter(Tag.company_id == company_id).first():
        for order, (name, color, children) in enumerate(DEFAULT_TAGS):
            parent = Tag(company_id=company_id, name=name, color=color, sort_order=order, applies_to=[])
            db.add(parent)
            db.flush()
            for j, child in enumerate(children):
                db.add(Tag(company_id=company_id, name=child, color=color, parent_id=parent.id, sort_order=j, applies_to=[]))
    if not db.query(LegalEntity.id).filter(LegalEntity.company_id == company_id).first():
        from modules.company.models import Company
        c = db.query(Company).filter(Company.id == company_id).first()
        if c is not None:
            db.add(LegalEntity(company_id=company_id, name=c.company_name, legal_name=c.company_name,
                               pan=c.pan, tan=c.tan, gstin=c.gstin, cin=c.cin, city=c.city, pincode=c.pincode,
                               address_line1=c.address, email=c.email, phone=c.phone, is_primary=True))
    db.flush()


# ── Demo organisation (development) ──────────────────────────────────

DEMO_SLUG = "campusly-demo"
PASSWORD = "Welcome9#"


def _aadhaar(rng: random.Random) -> str:
    from modules.employees.validators import verhoeff_digit
    base = str(rng.randint(2, 9)) + "".join(str(rng.randint(0, 9)) for _ in range(10))
    return base + verhoeff_digit(base)


def _pan(rng: random.Random, last_name: str) -> str:
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    return ("".join(rng.choice(letters) for _ in range(3)) + "P" + last_name[0].upper()
            + f"{rng.randint(1000, 9999)}" + rng.choice(letters))


def _pdf(title: str) -> bytes:
    """A one-page PDF placeholder for a demo document scan."""
    from io import BytesIO

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica", 18)
    c.drawString(72, 760, title)
    c.setFont("Helvetica", 11)
    c.drawString(72, 735, "Sample document for the demo company.")
    c.rect(72, 520, 450, 190)
    c.showPage()
    c.save()
    return buf.getvalue()


# first, last, gender, dept, designation, location, manager (index or None), grade, joined, type, status, extra
PEOPLE = [
    ("Arjun", "Mehta", "M", "Management", "Principal", "BLR", None, "G7", "2018-04-02", "PERM", "confirmed", {}),
    ("Kavitha", "Raghavan", "F", "Academics", "Vice Principal", "BLR", 0, "G6", "2018-09-10", "PERM", "confirmed", {}),
    ("Rohan", "Kulkarni", "M", "Administration", "Administrative Officer", "PUN", 0, "G6", "2019-01-14", "PERM", "confirmed", {}),
    ("Meenakshi", "Iyer", "F", "Accounts", "Bursar", "CHN", 0, "G5", "2019-06-03", "PERM", "confirmed", {}),
    ("Priya", "Sharma", "F", "Office", "HR & Admin Manager", "BLR", 0, "G4", "2020-02-17", "PERM", "confirmed", {}),
    ("Vikram", "Nair", "M", "Sciences", "Mathematics Teacher", "BLR", 0, "G4", "2020-08-24", "PERM", "confirmed", {}),
    ("Sneha", "Patil", "F", "Languages", "English Teacher", "PUN", 2, "G4", "2021-03-01", "PERM", "confirmed", {}),
    ("Rahul", "Verma", "M", "Sciences", "Head of Science", "BLR", 1, "G4", "2019-11-11", "PERM", "confirmed", {}),
    ("Ananya", "Reddy", "F", "Sciences", "Physics Teacher", "BLR", 7, "G3", "2021-07-05", "PERM", "confirmed", {"tags": ["High potential", "AWS certified"]}),
    ("Karthik", "Subramanian", "M", "Sciences", "Chemistry Teacher", "CHN", 7, "G2", "2022-01-10", "PERM", "confirmed", {}),
    ("Divya", "Menon", "F", "Sciences", "Computer Science Teacher", "BLR", 1, "G2", "2022-06-13", "PERM", "confirmed", {"tags": ["Women in tech"]}),
    ("Siddharth", "Joshi", "M", "Co-curricular", "Physical Education Teacher", "PUN", 1, "G2", "2023-04-03", "PERM", "confirmed", {}),
    ("Pooja", "Deshmukh", "F", "Primary", "Primary Teacher", "PUN", 1, "G2", "2023-09-18", "PERM", "confirmed", {}),
    ("Aditya", "Rao", "M", "Primary", "Trainee Teacher", "BLR", 7, "G1", "2026-07-01", "PROB", "probation", {"tags": ["Graduate trainee", "New joiner"]}),
    ("Neha", "Gupta", "F", "Co-curricular", "Art Teacher", "BLR", 5, "G3", "2021-12-06", "PERM", "confirmed", {}),
    ("Manish", "Agarwal", "M", "Humanities", "Head of Humanities", "PUN", 2, "G4", "2020-10-12", "PERM", "confirmed", {"tags": ["Successor"]}),
    ("Lakshmi", "Narayanan", "F", "Languages", "Hindi Teacher", "CHN", 15, "G2", "2022-11-21", "PERM", "confirmed", {}),
    ("Imran", "Shaikh", "M", "Humanities", "Social Science Teacher", "PUN", 15, "G2", "2023-02-06", "PERM", "notice", {"tags": ["Retention risk"]}),
    ("Ritu", "Singh", "F", "Co-curricular", "Music Teacher", "PUN", 6, "G2", "2022-08-01", "PERM", "confirmed", {}),
    ("Ganesh", "Pillai", "M", "Accounts", "Accountant", "CHN", 3, "G2", "2021-05-17", "PERM", "confirmed", {"tags": ["Chartered accountant"]}),
    ("Shalini", "Krishnan", "F", "Accounts", "Fee & Payroll Officer", "CHN", 3, "G2", "2022-03-14", "PERM", "confirmed", {}),
    ("Deepak", "Chauhan", "M", "Office", "Office Assistant", "BLR", 4, "G1", "2023-06-19", "PERM", "confirmed", {}),
    ("Farah", "Khan", "F", "Office", "Admissions Counsellor", "PUN", 4, "G2", "2024-01-08", "CONT", "confirmed", {}),
    ("Suresh", "Babu", "M", "Administration", "IT & Lab Administrator", "CHN", 7, "G3", "2020-12-01", "PERM", "confirmed", {"tags": ["AWS certified"]}),
    ("Tanvi", "Bhat", "F", "Administration", "Librarian", "BLR", 6, "G1", "2026-12-01", "INTERN", "pre_joining", {"tags": ["New joiner"]}),
]

DEPARTMENTS = [
    ("Management", "MGT", None, "CC-ADM", "BLR"), ("Academics", "ACD", None, "CC-ENG", "BLR"),
    ("Administration", "ADM", None, "CC-ADM", "BLR"), ("Accounts", "ACC", None, "CC-FIN", "CHN"),
    ("Office", "OFF", None, "CC-HR", "BLR"),
    ("Primary", "PRI", "Academics", "CC-ENG", "BLR"), ("Sciences", "SCI", "Academics", "CC-ENG", "BLR"),
    ("Languages", "LAN", "Academics", "CC-ENG", "BLR"), ("Humanities", "HUM", "Academics", "CC-ENG", "BLR"),
    ("Co-curricular", "COC", "Academics", "CC-MKT", "PUN"),
]
DESIGNATION_GRADES = {"Principal": "G7", "Vice Principal": "G6", "Administrative Officer": "G5", "Bursar": "G5", "HR & Admin Manager": "G4", "Mathematics Teacher": "G2", "English Teacher": "G2", "Head of Science": "G4", "Physics Teacher": "G2", "Chemistry Teacher": "G2", "Computer Science Teacher": "G2", "Physical Education Teacher": "G2", "Primary Teacher": "G2", "Trainee Teacher": "G2", "Art Teacher": "G2", "Head of Humanities": "G4", "Hindi Teacher": "G2", "Social Science Teacher": "G2", "Music Teacher": "G2", "Accountant": "G2", "Fee & Payroll Officer": "G2", "Office Assistant": "G2", "Admissions Counsellor": "G2", "IT & Lab Administrator": "G2", "Librarian": "G2"
}
LOCATIONS = {
    "BLR": dict(name="Main Campus, Bengaluru", code="BLR", kind="head_office", state="KA", city="Bengaluru", pincode="560103",
                address_line1="4th Floor, Prestige Tech Park", address_line2="Outer Ring Road, Kadubeesanahalli",
                latitude=Decimal("12.935200"), longitude=Decimal("77.694500"), radius_m=200, region="South",
                entity=0, phone="+91 80 4567 1200"),
    "PUN": dict(name="Pune Campus", code="PUN", kind="branch", state="MH", city="Pune", pincode="411014",
                address_line1="Tower B, EON Free Zone", address_line2="Kharadi",
                latitude=Decimal("18.551800"), longitude=Decimal("73.947500"), radius_m=150, region="West",
                entity=1, phone="+91 20 6789 4500"),
    "CHN": dict(name="Chennai Campus", code="CHN", kind="branch", state="TN", city="Chennai", pincode="600113",
                address_line1="Level 6, Tidel Park", address_line2="Rajiv Gandhi Salai, Taramani",
                latitude=Decimal("12.989900"), longitude=Decimal("80.248600"), radius_m=150, region="South",
                entity=0, phone="+91 44 4012 3300"),
}
CITY_ADDRESSES = {
    "BLR": [("HSR Layout, Sector 2", "560102"), ("Indiranagar 2nd Stage", "560038"), ("Whitefield, ITPL Road", "560066"),
            ("Jayanagar 4th Block", "560011"), ("Koramangala 5th Block", "560095")],
    "PUN": [("Baner Road", "411045"), ("Viman Nagar", "411014"), ("Kothrud", "411038"), ("Hinjewadi Phase 1", "411057")],
    "CHN": [("Adyar", "600020"), ("Velachery Main Road", "600042"), ("Anna Nagar West", "600040"), ("T. Nagar", "600017")],
}
BANKS = [("HDFC Bank", "HDFC"), ("ICICI Bank", "ICIC"), ("State Bank of India", "SBIN"), ("Axis Bank", "UTIB"),
         ("Kotak Mahindra Bank", "KKBK")]
FATHER_NAMES = ["Ramesh", "Suresh", "Mahesh", "Prakash", "Venkatesh", "Rajendra", "Ashok", "Gopal", "Mohan", "Srinivas"]
SPOUSE_NAMES_F = ["Anjali", "Kavya", "Swati", "Rekha", "Deepa", "Nisha"]
SPOUSE_NAMES_M = ["Rajesh", "Nikhil", "Amit", "Sanjay", "Varun", "Kiran"]
COLLEGES = [("B.E.", "Computer Science", "RV College of Engineering", "Visvesvaraya Technological University"),
            ("B.Tech", "Information Technology", "College of Engineering, Pune", "Savitribai Phule Pune University"),
            ("B.Com", "Accounting and Finance", "Loyola College", "University of Madras"),
            ("MBA", "Human Resources", "Symbiosis Institute of Business Management", "Symbiosis International University"),
            ("MBA", "Marketing", "IIM Bangalore", None),
            ("B.Des", "Interaction Design", "National Institute of Design", None),
            ("M.Tech", "Software Systems", "BITS Pilani", "BITS Pilani")]
EMPLOYERS = ["Infosys", "Wipro", "TCS", "Flipkart", "Zoho", "Freshworks", "Swiggy", "Razorpay", "Accenture", "Deloitte"]
EMP_SKILLS = {
    "Sciences": ["Lesson planning", "Lab safety", "Classroom management"], "Primary": ["Phonics", "Storytelling", "Classroom management"],
    "Languages": ["Lesson planning", "Public speaking", "Creative writing"], "Humanities": ["Lesson planning", "Public speaking"],
    "Co-curricular": ["Event management", "Coaching"], "Academics": ["Curriculum design", "Public speaking"],
    "Accounts": ["Tally", "GST compliance", "Excel"], "Office": ["Payroll processing", "Recruitment", "Excel"],
    "Administration": ["Stakeholder management", "Excel"], "Management": ["Stakeholder management", "Public speaking"],
}


def seed_demo_people(db: Session) -> None:
    from modules.company.models import Company
    company = db.query(Company).filter(Company.slug == DEMO_SLUG).first()
    if company is None:
        return
    cid = company.id
    db.info["company_id"] = cid
    if db.query(Location.id).filter(Location.company_id == cid, Location.code == "BLR").first():
        return
    try:
        _seed_demo(db, cid)
        db.commit()
        print("[seed] Demo organisation and 25 employees created")
    except Exception as exc:
        db.rollback()
        print(f"[seed] Demo organisation skipped: {exc}")
        raise


def _seed_demo(db: Session, cid: int) -> None:  # noqa: C901  (one long, linear script)
    from core.security import hash_password
    from modules.departments.models import Department
    from modules.designations.models import Designation
    from modules.employees import sensitive
    from modules.employees.models import (BankAccount, Education, EmergencyContact, Employee, EmployeeDocument,
                                          EmployeeSettings, FamilyMember, JobChange, Nominee, PreviousEmployment,
                                          StoredFile)
    from modules.employees.work_arrangement import DEFAULT_WORK_ARRANGEMENT

    rng = random.Random(42)
    ensure_defaults(db, cid)
    states = _master_ids(db, cid, "state")
    countries = _master_ids(db, cid, "country")
    banks = _master_ids(db, cid, "bank")
    genders = _master_ids(db, cid, "gender")
    marital = _master_ids(db, cid, "marital_status")
    blood = _master_ids(db, cid, "blood_group")
    nationality = _master_ids(db, cid, "nationality")
    relation = _master_ids(db, cid, "relation")
    etypes = _master_ids(db, cid, "employment_type")
    edu_levels = _master_ids(db, cid, "education_level")
    doc_types = _master_ids(db, cid, "document_type")
    now = datetime.now(timezone.utc)

    # Legal entities: rename the default one into the main company, add a second.
    primary = db.query(LegalEntity).filter(LegalEntity.company_id == cid, LegalEntity.is_primary.is_(True)).first()
    if primary is None:
        primary = LegalEntity(company_id=cid, is_primary=True)
        db.add(primary)
    e1 = dict(name="Campusly Education", legal_name="Campusly Education Private Limited", code="NGHI",
              pan="AABCN4321K", tan="BLRN04321E", cin="U72900KA2018PTC112233", gstin="29AABCN4321K1ZQ",
              pf_code="KABNG1234567000", esi_code="53000123450001001", lin="1-2345-6789-1",
              incorporation_date=date(2018, 3, 12), address_line1="4th Floor, Prestige Tech Park",
              address_line2="Outer Ring Road, Kadubeesanahalli", city="Bengaluru", state_id=states.get("ka"),
              pincode="560103", email="compliance@example.com", phone="+91 80 4567 1200",
              registrations=[{"kind": "pt", "state_id": states.get("ka"), "number": "PTKA-29001234567", "valid_from": "2018-04-01", "valid_to": None},
                             {"kind": "pt", "state_id": states.get("tn"), "number": "PTTN-0456/2021", "valid_from": "2021-04-01", "valid_to": None},
                             {"kind": "lwf", "state_id": states.get("ka"), "number": "KLWF-BLR-77812", "valid_from": "2018-04-01", "valid_to": None},
                             {"kind": "lwf", "state_id": states.get("tn"), "number": "TNLWB-CHN-11290", "valid_from": "2021-04-01", "valid_to": None},
                             {"kind": "shops", "state_id": states.get("ka"), "number": "KA/BLR/SE/2018/44121", "valid_from": "2018-04-01", "valid_to": "2028-03-31"}],
              bank_accounts=[{"bank_id": banks.get("hdfc"), "account_name": "Campusly Education Pvt Ltd",
                              "account_number": "50200012345678", "ifsc": "HDFC0000523", "branch": "Koramangala",
                              "purpose": "Salary payouts", "primary": True},
                             {"bank_id": banks.get("icic"), "account_name": "Campusly Education Pvt Ltd",
                              "account_number": "002105012345", "ifsc": "ICIC0000021", "branch": "MG Road",
                              "purpose": "Statutory payments", "primary": False}],
              signatory={"name": "Arjun Mehta", "designation": "Director", "pan": "ABCPM1234D",
                         "email": "arjun.mehta@example.com", "phone": "+91 98450 11223"})
    for k, val in e1.items():
        setattr(primary, k, val)
    second = LegalEntity(
        company_id=cid, name="NextGen Digital Services", legal_name="NextGen Digital Services Private Limited",
        code="NGDS", pan="AAFCN7788L", tan="PNEN07788B", cin="U74999MH2021PTC356789", gstin="27AAFCN7788L1Z2",
        pf_code="MHPUN0098765000", esi_code="33000987650001002", incorporation_date=date(2021, 1, 20),
        address_line1="Tower B, EON Free Zone", address_line2="Kharadi", city="Pune", state_id=states.get("mh"),
        pincode="411014", email="accounts.pune@example.com", phone="+91 20 6789 4500",
        registrations=[{"kind": "pt", "state_id": states.get("mh"), "number": "27561234567P", "valid_from": "2021-02-01", "valid_to": None},
                       {"kind": "lwf", "state_id": states.get("mh"), "number": "MLWB/PUN/2021/5521", "valid_from": "2021-02-01", "valid_to": None},
                       {"kind": "shops", "state_id": states.get("mh"), "number": "PUN/SE/II/2021/00981", "valid_from": "2021-02-01", "valid_to": None}],
        bank_accounts=[{"bank_id": banks.get("utib"), "account_name": "NextGen Digital Services Pvt Ltd",
                        "account_number": "921020045678901", "ifsc": "UTIB0001234", "branch": "Kharadi",
                        "purpose": "Salary payouts", "primary": True}],
        signatory={"name": "Rohan Kulkarni", "designation": "Director", "pan": "BKLPK5521R",
                   "email": "rohan.kulkarni@example.com", "phone": "+91 98220 33445"})
    db.add(second)
    db.flush()
    entities = [primary, second]

    regions = {}
    for name, code, desc in (("South", "S", "Karnataka and Tamil Nadu"), ("West", "W", "Maharashtra and Gujarat"),
                             ("North", "N", "Delhi NCR (planned)")):
        regions[name] = Region(company_id=cid, name=name, code=code, description=desc)
        db.add(regions[name])
    db.flush()
    locations = {}
    for key, d in LOCATIONS.items():
        loc = Location(company_id=cid, name=d["name"], code=d["code"], kind=d["kind"], region_id=regions[d["region"]].id,
                       legal_entity_id=entities[d["entity"]].id, address_line1=d["address_line1"],
                       address_line2=d["address_line2"], city=d["city"], state_id=states.get(d["state"].lower()),
                       pincode=d["pincode"], timezone="Asia/Kolkata", latitude=d["latitude"], longitude=d["longitude"],
                       radius_m=d["radius_m"], phone=d["phone"], email=f"{d['code'].lower()}.office@example.com")
        db.add(loc)
        locations[key] = loc
    db.flush()

    grades = {g.code: g for g in db.query(Grade).filter(Grade.company_id == cid)}
    centres = {}
    for code, name in (("CC-ADM", "Administration"), ("CC-ENG", "Engineering and product"), ("CC-SAL", "Sales"),
                       ("CC-MKT", "Marketing"), ("CC-FIN", "Finance"), ("CC-HR", "People operations")):
        centres[code] = CostCentre(company_id=cid, code=code, name=name, legal_entity_id=primary.id)
        db.add(centres[code])
    db.flush()

    depts = {}
    for name, code, parent, cc, loc in DEPARTMENTS:
        d = db.query(Department).filter(Department.company_id == cid, Department.department_name == name).first()
        if d is None:
            d = Department(company_id=cid, department_name=name, is_global=False)
            db.add(d)
        d.department_code, d.cost_centre_id, d.location_id, d.active = code, centres[cc].id, locations[loc].id, True
        d.parent_id = depts[parent].id if parent else None
        db.flush()
        depts[name] = d
    desigs = {}
    for name, grade in DESIGNATION_GRADES.items():
        d = db.query(Designation).filter(Designation.company_id == cid, Designation.designation_name == name).first()
        if d is None:
            d = Designation(company_id=cid, designation_name=name, is_global=False)
            db.add(d)
        d.grade_id = grades[grade].id
        desigs[name] = d
    db.flush()

    people: list[Employee] = []
    t = date.today()
    for i, (first, last, g, dept, desig, loc, mgr, grade, joined, etype, status, extra) in enumerate(PEOPLE):
        email = f"{first.lower()}.{last.lower()}@example.com"
        emp = db.query(Employee).filter(Employee.company_id == cid, Employee.email == email).first()
        if emp is None:
            emp = Employee(company_id=cid, email=email, password=hash_password(PASSWORD), first_name=first, last_name=last)
            db.add(emp)
        doj = date.fromisoformat(joined)
        age = rng.randint(24, 34) if grade in ("G1", "G2") else rng.randint(30, 48)
        dob = date(t.year - age, rng.randint(1, 12), rng.randint(1, 28))
        married = age > 28 and rng.random() < 0.7
        home = rng.choice(CITY_ADDRESSES[loc])
        state_id = states.get(LOCATIONS[loc]["state"].lower())
        address = {"line1": f"#{rng.randint(10, 400)}, {rng.choice(['Sri Sai Residency', 'Green Glen Layout', 'Lake View Apartments', 'Shanti Nagar', 'Palm Grove'])}",
                   "line2": home[0], "city": LOCATIONS[loc]["city"], "state_id": state_id, "pincode": home[1],
                   "country_id": countries.get("in")}
        native_state = rng.choice(["KL", "UP", "RJ", "AP", "TN", "KA", "MH", "WB"])
        permanent = {"line1": f"H.No {rng.randint(1, 90)}-{rng.randint(1, 30)}", "line2": "Near Bus Stand",
                     "city": {"KL": "Kochi", "UP": "Lucknow", "RJ": "Jaipur", "AP": "Vijayawada", "TN": "Madurai",
                              "KA": "Mysuru", "MH": "Nashik", "WB": "Kolkata"}[native_state],
                     "state_id": states.get(native_state.lower()),
                     "pincode": {"KL": "682016", "UP": "226001", "RJ": "302001", "AP": "520001", "TN": "625001",
                                 "KA": "570001", "MH": "422001", "WB": "700001"}[native_state],
                     "country_id": countries.get("in")}
        number = _aadhaar(rng)
        emp.employee_code = f"ST{1001 + i}"
        emp.greeting = "Mr." if g == "M" else ("Mrs." if married else "Ms.")
        emp.role = ("admin" if i == 0 else "principal" if desig == "Vice Principal" else
                    "hr" if dept == "Office" and grade != "G1" else
                    "accountant" if dept == "Accounts" else "librarian" if desig == "Librarian" else
                    "manager" if desig.startswith("Head of") else
                    "teacher" if "Teacher" in desig else "employee")
        if email == "priya.sharma@example.com":
            emp.role = "employee"  # the demo self-service login stays a plain staff member
        emp.phone = f"+91 9{rng.randint(100000000, 999999999)}"
        emp.alternate_phone = f"+91 8{rng.randint(100000000, 999999999)}" if rng.random() < 0.4 else None
        emp.work_phone = LOCATIONS[loc]["phone"]
        emp.extension = str(200 + i)
        emp.personal_email = f"{first.lower()}{rng.randint(10, 99)}@example.com"
        emp.preferred_name = {"Siddharth": "Sid", "Lakshmi": "Lakshmi N", "Karthik": "Karthi"}.get(first)
        emp.gender_id = genders.get(g.lower())
        emp.date_of_birth = dob
        emp.marital_status_id = marital.get("married" if married else "single")
        emp.marriage_date = date(dob.year + rng.randint(25, 28), rng.randint(1, 12), rng.randint(1, 28)) if married else None
        emp.blood_group_id = blood.get(rng.choice(["a+", "b+", "o+", "ab+", "o-", "b-"]))
        emp.nationality_id = nationality.get("indian")
        emp.father_name = f"{rng.choice(FATHER_NAMES)} {last}"
        emp.spouse_name = (f"{rng.choice(SPOUSE_NAMES_F)} {last}" if g == "M" else f"{rng.choice(SPOUSE_NAMES_M)} {last}") if married else None
        emp.current_address, emp.permanent_address = address, permanent
        emp.pan = _pan(rng, last)
        sensitive.set_aadhaar(emp, number)
        emp.uan = f"10{rng.randint(1000000000, 9999999999)}" if status != "pre_joining" else None
        emp.esi_number = f"{rng.randint(1000000000, 9999999999)}" if grade == "G1" and status != "pre_joining" else None
        if rng.random() < 0.45 or i in (2, 8):
            emp.passport_number = f"{rng.choice('JKLMNPRSTVWZ')}{rng.randint(1000000, 9999999)}"
            emp.passport_expiry = t + timedelta(days=21) if i == 8 else date(t.year + rng.randint(2, 8), rng.randint(1, 12), 15)
        if rng.random() < 0.35:
            emp.driving_licence = f"{LOCATIONS[loc]['state']}{rng.randint(10, 99)}{rng.randint(2005, 2020)}{rng.randint(1000000, 9999999)}"
            emp.driving_licence_expiry = date(t.year + rng.randint(3, 15), rng.randint(1, 12), 1)
        emp.legal_entity_id = entities[LOCATIONS[loc]["entity"]].id
        emp.location_id = locations[loc].id
        emp.department_id = depts[dept].id if dept in depts else depts["Academics"].id
        emp.designation_id = desigs[desig].id
        emp.grade_id = grades[grade].id
        emp.cost_centre_id = depts[dept].cost_centre_id if dept in depts else centres["CC-ENG"].id
        emp.employment_type_id = etypes.get(etype.lower())
        emp.status = status
        emp.active = True
        emp.date_of_joining = doj
        emp.probation_end_date = doj + timedelta(days=180)
        emp.confirmation_date = doj + timedelta(days=180) if status in ("confirmed", "notice") else None
        emp.notice_period_days = 90 if grade in ("G4", "G5", "G6", "G7") else (30 if grade == "G1" else 60)
        emp.work_mode = "hybrid" if dept in ("Administration",) else (
            "field" if desig == "Account Executive" else "office")
        if status == "notice":
            emp.resignation_date = t - timedelta(days=20)
            emp.exit_date = t + timedelta(days=40)
        emp.timezone = None
        emp.custom = emp.custom or {}
        db.flush()
        if emp.settings is None:
            config = dict(DEFAULT_WORK_ARRANGEMENT)
            db.add(EmployeeSettings(company_id=cid, employee_id=emp.id, config=config))
        people.append(emp)

    for i, row in enumerate(PEOPLE):
        if row[6] is not None:
            people[i].reporting_manager_id = people[row[6]].id
    people[8].secondary_manager_id = people[5].id   # Ananya also works with the product manager
    people[14].secondary_manager_id = people[1].id
    for name, head in (("Management", 0), ("Academics", 1), ("Administration", 2), ("Accounts", 3), ("Office", 4),
                       ("Sciences", 7), ("Languages", 6), ("Humanities", 15), ("Primary", 12), ("Co-curricular", 13)):
        depts[name].head_employee_id = people[head].id
    regions["South"].head_employee_id = people[1].id
    regions["West"].head_employee_id = people[2].id
    centres["CC-ENG"].owner_employee_id = people[1].id
    centres["CC-SAL"].owner_employee_id = people[2].id
    centres["CC-FIN"].owner_employee_id = people[3].id
    centres["CC-HR"].owner_employee_id = people[4].id
    db.flush()

    # Teams across departments
    t1 = Team(company_id=cid, name="Payroll Revamp", code="PRV", color="#4f46e5", lead_employee_id=people[7].id,
              description="Cross-functional squad rebuilding payroll processing and statutory reports.")
    t2 = Team(company_id=cid, name="Customer Success Pod", code="CSP", color="#059669", lead_employee_id=people[15].id,
              description="Sales, product and engineering together for the top 20 customers.")
    db.add_all([t1, t2])
    db.flush()
    for team, members in ((t1, [7, 8, 9, 20, 5, 14]), (t2, [15, 16, 17, 5, 11, 18])):
        for m in members:
            db.add(TeamMember(company_id=cid, team_id=team.id, employee_id=people[m].id,
                              role="Lead" if people[m].id == team.lead_employee_id else None))

    tags = {tg.name: tg for tg in db.query(Tag).filter(Tag.company_id == cid)}
    skills = {sk.name: sk for sk in db.query(Skill).filter(Skill.company_id == cid)}
    pdf_cache: dict[str, bytes] = {}

    for i, (first, last, g, dept, desig, loc, mgr, grade, joined, etype, status, extra) in enumerate(PEOPLE):
        emp = people[i]
        eid = emp.id
        married = emp.marital_status_id == marital.get("married")
        for tag_name in extra.get("tags", []):
            if tag_name in tags:
                db.add(TagLink(company_id=cid, tag_id=tags[tag_name].id, entity_type="employees", entity_id=eid))
        # emergency contacts
        father = emp.father_name
        db.add(EmergencyContact(company_id=cid, employee_id=eid, name=emp.spouse_name if married else father,
                                relation_id=relation.get("spouse" if married else "father"),
                                phone=f"+91 9{rng.randint(100000000, 999999999)}", is_primary=True,
                                address=emp.current_address.get("line2") if married else emp.permanent_address.get("city")))
        if rng.random() < 0.6:
            db.add(EmergencyContact(company_id=cid, employee_id=eid, name=f"{rng.choice(['Anil', 'Sunita', 'Kiran', 'Usha'])} {last}",
                                    relation_id=relation.get(rng.choice(["brother", "sister", "mother"])),
                                    phone=f"+91 9{rng.randint(100000000, 999999999)}"))
        # family and nominees
        fam_father = FamilyMember(company_id=cid, employee_id=eid, name=father, relation_id=relation.get("father"),
                                  gender_id=genders.get("m"), date_of_birth=date(emp.date_of_birth.year - 28, 6, 10),
                                  occupation=rng.choice(["Retired", "Business", "Farmer", "Teacher"]))
        fam_mother = FamilyMember(company_id=cid, employee_id=eid, name=f"{rng.choice(['Lalitha', 'Saraswati', 'Kamala', 'Geeta'])} {last}",
                                  relation_id=relation.get("mother"), gender_id=genders.get("f"),
                                  date_of_birth=date(emp.date_of_birth.year - 25, 3, 22), occupation="Homemaker",
                                  is_dependent=True)
        db.add_all([fam_father, fam_mother])
        spouse = None
        child = None
        if married:
            spouse = FamilyMember(company_id=cid, employee_id=eid, name=emp.spouse_name, relation_id=relation.get("spouse"),
                                  gender_id=genders.get("f" if g == "M" else "m"),
                                  date_of_birth=date(emp.date_of_birth.year + rng.randint(-2, 3), 8, 14),
                                  occupation=rng.choice(["Software engineer", "Doctor", "Teacher", "Banker", "Homemaker"]),
                                  phone=f"+91 9{rng.randint(100000000, 999999999)}", is_dependent=rng.random() < 0.3)
            db.add(spouse)
            if rng.random() < 0.6:
                child = FamilyMember(company_id=cid, employee_id=eid, name=f"{rng.choice(['Aarav', 'Diya', 'Ishaan', 'Myra', 'Vihaan'])} {last}",
                                     relation_id=relation.get(rng.choice(["son", "daughter"])),
                                     date_of_birth=date(t.year - rng.randint(2, 10), 2, 2), is_dependent=True)
                db.add(child)
        db.flush()
        if status != "pre_joining":
            main = spouse or fam_mother
            if child is not None:
                db.add(Nominee(company_id=cid, employee_id=eid, kind="pf", family_member_id=main.id, name=main.name,
                               relation_id=main.relation_id, date_of_birth=main.date_of_birth, share_percent=Decimal("70")))
                db.add(Nominee(company_id=cid, employee_id=eid, kind="pf", family_member_id=child.id, name=child.name,
                               relation_id=child.relation_id, date_of_birth=child.date_of_birth, share_percent=Decimal("30"),
                               guardian_name=main.name))
            else:
                db.add(Nominee(company_id=cid, employee_id=eid, kind="pf", family_member_id=main.id, name=main.name,
                               relation_id=main.relation_id, date_of_birth=main.date_of_birth, share_percent=Decimal("100")))
            db.add(Nominee(company_id=cid, employee_id=eid, kind="gratuity", family_member_id=main.id, name=main.name,
                           relation_id=main.relation_id, date_of_birth=main.date_of_birth, share_percent=Decimal("100")))
            db.add(Nominee(company_id=cid, employee_id=eid, kind="insurance", family_member_id=main.id, name=main.name,
                           relation_id=main.relation_id, date_of_birth=main.date_of_birth, share_percent=Decimal("100")))
        # bank
        bank_name, prefix = BANKS[i % len(BANKS)]
        db.add(BankAccount(company_id=cid, employee_id=eid, bank_id=banks.get(prefix.lower()),
                           account_holder=f"{first} {last}", account_number=str(rng.randint(10 ** 11, 10 ** 12 - 1)),
                           ifsc=f"{prefix}0{rng.randint(100000, 999999)}", branch=f"{LOCATIONS[loc]['city']} {home_branch(rng)}",
                           account_type="salary" if i % 3 else "savings", is_primary=True, verified=status != "pre_joining"))
        # education
        school_year = emp.date_of_birth.year + 18
        db.add(Education(company_id=cid, employee_id=eid, level_id=edu_levels.get("12th (hsc)"), degree="Class XII",
                         specialisation=rng.choice(["Science (PCM)", "Commerce", "Science (PCB)"]),
                         institution=rng.choice(["Kendriya Vidyalaya", "Delhi Public School", "St. Joseph's School",
                                                 "DAV Public School"]),
                         university=rng.choice(["CBSE", "State Board", "ICSE"]), end_year=school_year,
                         score=f"{rng.randint(78, 96)}%"))
        degree = COLLEGES[{"Accounts": 2, "Office": 3}.get(dept, rng.choice([0, 1, 6]))]
        db.add(Education(company_id=cid, employee_id=eid,
                         level_id=edu_levels.get("post-graduate" if degree[0] in ("MBA", "M.Tech") else "graduate"),
                         degree=degree[0], specialisation=degree[1], institution=degree[2], university=degree[3],
                         start_year=school_year + (4 if degree[0] in ("MBA", "M.Tech") else 0),
                         end_year=school_year + (6 if degree[0] in ("MBA", "M.Tech") else 4),
                         score=f"{rng.uniform(6.8, 9.4):.1f} CGPA"))
        # previous employment
        doj = emp.date_of_joining
        career_start = date(school_year + 4, 7, 1)
        if (doj - career_start).days > 500 and grade != "G1":
            first_end = min(doj - timedelta(days=15), career_start + timedelta(days=rng.randint(700, 1500)))
            prev = PreviousEmployment(company_id=cid, employee_id=eid, company_name=rng.choice(EMPLOYERS),
                                      designation=rng.choice(["Associate", "Analyst", "Software Engineer", "Executive",
                                                              "Consultant"]),
                                      from_date=career_start, to_date=first_end,
                                      last_ctc=Decimal(rng.randint(6, 30) * 100000),
                                      reason_for_leaving=rng.choice(["Better opportunity", "Relocation", "Career growth"]),
                                      is_relevant=True, previous_pf_member_id=f"KN/BNG/{rng.randint(10000, 99999)}/{rng.randint(100, 999)}")
            # joined during this financial year: income and TDS from the earlier employer this year
            fy_start = date(t.year if t.month >= 4 else t.year - 1, 4, 1)
            if doj >= fy_start:
                prev.to_date = doj - timedelta(days=10)
                prev.financial_year = f"{fy_start.year}-{str(fy_start.year + 1)[-2:]}"
                prev.fy_taxable_income = Decimal(rng.randint(3, 9) * 100000)
                prev.fy_tds = (prev.fy_taxable_income * Decimal("0.08")).quantize(Decimal("1"))
                prev.fy_pf = (prev.fy_taxable_income * Decimal("0.05")).quantize(Decimal("1"))
                prev.fy_pt = Decimal("1200")
            db.add(prev)
        if i == 13:  # the probationer joined this year from another job
            fy_start = date(t.year if t.month >= 4 else t.year - 1, 4, 1)
            db.add(PreviousEmployment(company_id=cid, employee_id=eid, company_name="Mindtree", designation="Intern",
                                      from_date=fy_start, to_date=doj - timedelta(days=5), last_ctc=Decimal("360000"),
                                      is_relevant=True, financial_year=f"{fy_start.year}-{str(fy_start.year + 1)[-2:]}",
                                      fy_taxable_income=Decimal("72000"), fy_tds=Decimal("0"), fy_pf=Decimal("3600"),
                                      fy_pt=Decimal("600"), reason_for_leaving="Full-time offer"))
        # job history
        first_desig = desigs.get({"Senior Software Engineer": "Software Engineer", "Engineering Manager": "Senior Software Engineer",
                                  "Sales Manager": "Account Executive", "HR Manager": "HR Executive"}.get(desig, desig))
        promoted = first_desig is not None and first_desig.id != emp.designation_id
        db.add(JobChange(company_id=cid, employee_id=eid, change_type="joining", effective_date=doj, status="applied",
                         applied_at=now, created_by_name="Data import",
                         changes={"designation_id": {"old": None, "new": first_desig.id if promoted else emp.designation_id},
                                  "department_id": {"old": None, "new": emp.department_id},
                                  "location_id": {"old": None, "new": emp.location_id},
                                  "grade_id": {"old": None, "new": (grades[_prev_grade(grade)].id if promoted else emp.grade_id)}}))
        if emp.confirmation_date and emp.confirmation_date <= t:
            db.add(JobChange(company_id=cid, employee_id=eid, change_type="confirmation", effective_date=emp.confirmation_date,
                             status="applied", applied_at=now, created_by_name="Data import",
                             changes={"status": {"old": "probation", "new": "confirmed"}},
                             reason="Completed probation"))
        if promoted:
            when = min(t - timedelta(days=120), doj + timedelta(days=rng.randint(500, 900)))
            db.add(JobChange(company_id=cid, employee_id=eid, change_type="promotion", effective_date=when, status="applied",
                             applied_at=now, created_by_name="Priya Sharma", reason="Annual appraisal",
                             changes={"designation_id": {"old": first_desig.id, "new": emp.designation_id},
                                      "grade_id": {"old": grades[_prev_grade(grade)].id, "new": emp.grade_id}}))
        if i in (9, 16):  # transferred to Chennai from Bengaluru
            db.add(JobChange(company_id=cid, employee_id=eid, change_type="transfer",
                             effective_date=doj + timedelta(days=300), status="applied", applied_at=now,
                             created_by_name="Priya Sharma", reason="Opened the Chennai office",
                             changes={"location_id": {"old": locations["BLR"].id, "new": locations["CHN"].id}}))
        if i == 17:
            db.add(JobChange(company_id=cid, employee_id=eid, change_type="status", effective_date=emp.resignation_date,
                             status="applied", applied_at=now, created_by_name="Priya Sharma",
                             reason="Resigned: relocating to Hyderabad",
                             changes={"status": {"old": "confirmed", "new": "notice"},
                                      "exit_date": {"old": None, "new": emp.exit_date.isoformat()}}))
        if i == 11:  # a scheduled future change shows on the timeline
            db.add(JobChange(company_id=cid, employee_id=eid, change_type="transfer", effective_date=t + timedelta(days=24),
                             status="scheduled", created_by_name="Priya Sharma", reason="Moving to the Bengaluru mobile team",
                             changes={"location_id": {"old": locations["PUN"].id, "new": locations["BLR"].id}}))
        # documents
        def add_doc(type_code, number=None, issue=None, expiry=None, status_="verified", file_title=None, remarks=None):
            file_id = None
            if file_title:
                data = pdf_cache.setdefault(file_title, _pdf(file_title))
                f = StoredFile(company_id=cid, employee_id=eid, filename=f"{file_title.lower().replace(' ', '-')}.pdf",
                               content_type="application/pdf", size=len(data), data=data)
                db.add(f)
                db.flush()
                file_id = f.id
            db.add(EmployeeDocument(company_id=cid, employee_id=eid, document_type_id=doc_types.get(type_code),
                                    document_number=number, issue_date=issue, expiry_date=expiry, file_id=file_id,
                                    verification_status=status_,
                                    verified_by_name="Priya Sharma" if status_ != "pending" else None,
                                    verified_at=now - timedelta(days=rng.randint(5, 400)) if status_ != "pending" else None,
                                    remarks=remarks))
        pending = status == "pre_joining" or i in (13, 22)
        add_doc("pan", emp.pan, None, None, "pending" if pending else "verified", "PAN card")
        add_doc("aadhaar", f"XXXX XXXX {emp.aadhaar_last4}", None, None, "pending" if pending else "verified", "Aadhaar card")
        add_doc("degree_certificate", None, date(school_year + 4, 6, 30), None,
                "rejected" if i == 22 else ("pending" if pending else "verified"), "Degree certificate",
                remarks="The scan is blurred. Upload a clearer copy." if i == 22 else None)
        if emp.passport_number:
            add_doc("passport", emp.passport_number, emp.passport_expiry - timedelta(days=3650), emp.passport_expiry,
                    "verified", "Passport")
        if status != "pre_joining" and i % 4 == 0:
            add_doc("relieving_letter", None, doj - timedelta(days=20), None, "verified", "Relieving letter")
        # skills
        pool = EMP_SKILLS.get(dept, ["Excel"]) + ["English", rng.choice(["Hindi", "Kannada", "Tamil", "Marathi"])]
        for sk_name in rng.sample(pool, k=min(len(pool), rng.randint(3, 5))):
            if sk_name in skills:
                verified = rng.random() < 0.6
                db.add(EmployeeSkill(company_id=cid, employee_id=eid, skill_id=skills[sk_name].id,
                                     level=rng.randint(2, 5), years=Decimal(str(rng.randint(1, 10))), verified=verified,
                                     verified_by_name=(people[mgr].first_name + " " + people[mgr].last_name)
                                     if verified and mgr is not None else ("Priya Sharma" if verified else None),
                                     verified_at=now if verified else None))
    db.flush()


def home_branch(rng: random.Random) -> str:
    return rng.choice(["Main", "Koramangala", "Andheri", "Anna Nagar", "Baner", "Indiranagar", "Adyar"])


def _prev_grade(code: str) -> str:
    n = int(code[1:])
    return f"G{max(1, n - 1)}"
