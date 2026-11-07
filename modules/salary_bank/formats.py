"""Ready-made salary bulk upload layouts for Indian banks.

Layouts are data (see engine.py for the keys). Each one records where it comes from and how sure
we are:
  high    from the bank's own published document or sample file
  medium  from a reputable vendor / ERP implementation or a working open-source generator
  low     the bank does not publish its layout (it is handed out inside the corporate portal or by
          the relationship manager); a common layout for that kind of bank is used instead
Banks issue client-specific variants, so the settings screen says to compare the first file with
the sample in the bank's portal; a custom format (a copy of any layout here) covers differences.

Checked November 2026. Sources are listed per layout in `sources`.

Rules that apply to every bank (RBI):
- IFSC: 11 characters, 4 letters (bank), a zero, then 6 letters or digits (branch).
- RTGS only for ₹2,00,000 and above; NEFT has no minimum or maximum; IMPS up to ₹5,00,000.
- No commas or special characters inside values: text is cleaned to letters, digits, space and
  . , - / ( ) & (and the separator removed where values are not quoted).
"""
from __future__ import annotations


def C(label: str, value: str = "", **kw) -> dict:
    return {"label": label, "value": value, **kw}


# Banks by the first four letters of the IFSC
BANKS = {
    "SBIN": "State Bank of India", "HDFC": "HDFC Bank", "ICIC": "ICICI Bank", "UTIB": "Axis Bank",
    "KKBK": "Kotak Mahindra Bank", "YESB": "Yes Bank", "IDFB": "IDFC FIRST Bank", "INDB": "IndusInd Bank",
    "BARB": "Bank of Baroda", "PUNB": "Punjab National Bank", "CNRB": "Canara Bank", "UBIN": "Union Bank of India",
    "FDRL": "Federal Bank", "SIBL": "South Indian Bank", "IOBA": "Indian Overseas Bank", "BKID": "Bank of India",
    "MAHB": "Bank of Maharashtra", "CBIN": "Central Bank of India", "IDIB": "Indian Bank", "UCBA": "UCO Bank",
    "PSIB": "Punjab & Sind Bank", "KARB": "Karnataka Bank", "KVBL": "Karur Vysya Bank", "TMBL": "Tamilnad Mercantile Bank",
    "CIUB": "City Union Bank", "RATN": "RBL Bank", "DCBL": "DCB Bank", "AUBL": "AU Small Finance Bank",
    "BDBL": "Bandhan Bank", "ESFB": "Equitas Small Finance Bank", "CSBK": "CSB Bank", "DLXB": "Dhanlaxmi Bank",
    "JAKA": "Jammu & Kashmir Bank", "SCBL": "Standard Chartered", "CITI": "Citibank", "HSBC": "HSBC",
    "DBSS": "DBS Bank India", "IBKL": "IDBI Bank",
}

_NARR = 30


GENERIC = {
    "name": "Standard NEFT / RTGS / IMPS (CSV)", "bank": "Any bank", "confidence": "high",
    "notes": "A plain CSV most corporate portals accept through their own file mapping. Debit account and mode on every row.",
    "sources": ["RBI NEFT, RTGS and IMPS rules (rbi.org.in, Payment Systems FAQs)"],
    "spec": {
        "kind": "delimited", "delimiter": ",", "quote": "minimal", "extension": "csv", "line_ending": "crlf", "header_row": True,
        "date_format": "%d/%m/%Y", "amount": {"decimals": 2}, "split": "none", "clean": True,
        "modes": ["internal", "neft", "rtgs", "imps"], "codes": {"internal": "IFT", "neft": "NEFT", "rtgs": "RTGS", "imps": "IMPS"},
        "columns": [C("Payment mode", "{mode_code}"), C("Beneficiary name", "{name}", max=50), C("Account number", "{account}"),
                    C("IFSC", "{ifsc}"), C("Amount", "{amount}"), C("Debit account", "{debit_account}"),
                    C("Value date", "{date}"), C("Narration", "{narration}", max=_NARR), C("Employee ID", "{employee_code}"),
                    C("Email", "{email}"), C("Mobile", "{phone}"), C("Reference", "{payment_ref}")],
        "trailer_lines": [], "header_lines": [],
        "file_name": "salary-{period}-{group}-{batch}",
    },
}

# HDFC Bank ENet bulk upload (medium). Text file, comma separated, no quotes, no header or trailer,
# 28 fields. Type per row: I = HDFC to HDFC, N = NEFT, R = RTGS (D = demand draft, not used).
# Debit account is not in the file (it comes from the ENet login and client code).
# Sources: Odoo India localisation l10n_in_reports/data/bank_template.xml (HDFC template,
# include_header=False, I/N/R/D); field lengths in vendor design notes ("HDFC bulk file", 28
# fields: Beneficiary Code 13, Account 25, Amount 20, Name 40, Address 1-5, Instruction and
# Customer reference 20, Payment details 1-7 30, Cheque no 12, date DD/MM/YYYY, MICR 15, IFSC 11,
# bank name 100, branch 40, email 100).
HDFC = {
    "name": "HDFC Bank ENet bulk upload", "bank": "HDFC Bank", "confidence": "medium",
    "notes": "No header row. Debit account comes from your ENet login. Upload limit 2 MB per file.",
    "sources": ["Odoo India localisation, l10n_in_reports bank_template.xml (HDFC)", "HDFC ENet payment input file field list"],
    "spec": {
        "kind": "delimited", "delimiter": ",", "quote": "none", "extension": "txt", "line_ending": "crlf", "header_row": False,
        "date_format": "%d/%m/%Y", "amount": {"decimals": 2}, "split": "none", "clean": True,
        "ifsc_prefixes": ["HDFC"], "modes": ["internal", "neft", "rtgs"], "codes": {"internal": "I", "neft": "N", "rtgs": "R"},
        "columns": [
            C("Transaction Type", "{mode_code}"), C("Beneficiary Code", "{employee_code}", max=13),
            C("Beneficiary Account Number", "{account}", max=25), C("Instrument Amount", "{amount}"),
            C("Beneficiary Name", "{name}", max=40), C("Drawee Location"), C("DD Print Location"),
            C("Bene Address 1"), C("Bene Address 2"), C("Bene Address 3"), C("Bene Address 4"), C("Bene Address 5"),
            C("Instruction Reference Number", "{payment_ref}", max=20), C("Customer Reference Number", "{batch_ref}", max=20),
            C("Payment Details 1", "{narration}", max=30), C("Payment Details 2", "{employee_code}", max=30),
            C("Payment Details 3"), C("Payment Details 4"), C("Payment Details 5"), C("Payment Details 6"), C("Payment Details 7"),
            C("Cheque Number"), C("Chq / Trn Date", "{date}"), C("MICR Number"), C("IFC Code", "{ifsc}"),
            C("Beneficiary Bank Name", "{bank_name}", max=100), C("Beneficiary Bank Branch Name"),
            C("Beneficiary Email Id", "{email}", max=100),
        ],
        "header_lines": [], "trailer_lines": [],
        "file_name": "HDFC-{client_code}-{date:%d%m%Y}-{batch}",
    },
}

# ICICI Bank CIB bulk upload (medium; field rules high). Widely used CIB Excel bulk template saved
# as CSV with this header row. Pay Mod: I = within ICICI (fund transfer), N = NEFT, R = RTGS.
# Date DD-MMM-YYYY. Debit account on every row; one file may mix modes.
# ICICI's own "File Format for Bulk upload in CIB v3.3" (icici.bank.in, File_Format_Bulk Transfers
# 3.3.pdf) fixes the limits used here: beneficiary name 32 characters without special characters,
# debit account 12 digits, ICICI account 12 digits, other-bank account up to 34, remarks 30.
ICICI = {
    "name": "ICICI Bank CIB bulk upload", "bank": "ICICI Bank", "confidence": "medium",
    "notes": "Debit account on every row. Within-ICICI transfers use mode I; others N (NEFT) or R (RTGS).",
    "sources": ["ICICI Bank, File Format for Bulk upload in CIB v3.3 (icici.bank.in)",
                "CIB bulk upload template header (Pay Mod I/N/R, Date DD-MMM-YYYY) as used by open-source payment file generators"],
    "spec": {
        "kind": "delimited", "delimiter": ",", "quote": "minimal", "extension": "csv", "line_ending": "crlf", "header_row": True,
        "date_format": "%d-%b-%Y", "amount": {"decimals": 2}, "split": "none", "clean": True,
        "ifsc_prefixes": ["ICIC"], "modes": ["internal", "neft", "rtgs"], "codes": {"internal": "I", "neft": "N", "rtgs": "R"},
        "columns": [
            C("Debit Ac No", "{debit_account}"), C("Beneficiary Ac No", "{account}", max=34), C("Beneficiary Name", "{name}", max=32),
            C("Amt", "{amount}"), C("Pay Mod", "{mode_code}"), C("Date", "{date}"), C("IFSC", "{ifsc}"),
            C("Payable Location"), C("Print Location"), C("Bene Mobile No.", "{phone}"), C("Bene email ID", "{email}"),
            C("Bene add1"), C("Bene add2"), C("Bene add3"), C("Bene add4"),
            C("Add Details 1", "{employee_code}"), C("Add Details 2", "{payment_ref}"), C("Add Details 3"), C("Add Details 4"),
            C("Add Details 5"), C("Remarks", "{narration}", max=30),
        ],
        "header_lines": [], "trailer_lines": [],
        "file_name": "ICICI-SAL-{date:%d%m%Y}-{batch}",
    },
}

# State Bank of India, CINB (Vyapaar / Vistaar) bulk upload (low). SBI does not publish the field
# layout: its CINB FAQ says files must follow the "pre-defined file structure" given inside the
# portal (single debit, multiple credits; intra-SBI, NEFT and RTGS). Commonly used SBI upload
# sheets keep intra-bank and inter-bank payments in separate files, so this layout splits them.
# Pipe separated account|IFSC|amount|name|remarks|reference|type, the layout this product used
# for SBI CMP salary uploads before (kept so existing users see the same file).
SBI = {
    "name": "SBI Corporate Internet Banking (CINB / CMP) bulk upload", "bank": "State Bank of India", "confidence": "low",
    "notes": "SBI gives its exact file structure inside CINB. Own-bank (SBI) and other-bank payments go in separate files. "
             "Compare with the sample in your CINB login before the first upload.",
    "sources": ["SBI Corporate Internet Banking FAQs (sbi.bank.in, bulk upload: single debit multiple credit, intra-bank, NEFT, RTGS)",
                "SBI corporate file upload page (corp.sbi.bank.in)"],
    "spec": {
        "kind": "delimited", "delimiter": "|", "quote": "none", "extension": "txt", "line_ending": "crlf", "header_row": False,
        "date_format": "%d/%m/%Y", "amount": {"decimals": 2}, "split": "internal_vs_other", "clean": True,
        "ifsc_prefixes": ["SBIN"], "modes": ["internal", "neft", "rtgs"], "codes": {"internal": "INTRA", "neft": "NEFT", "rtgs": "RTGS"},
        "columns": [C("Beneficiary Account Number", "{account}", max=17), C("IFSC", "{ifsc}"), C("Amount", "{amount}"),
                    C("Beneficiary Name", "{name}", max=35), C("Remarks", "{narration}", max=30),
                    C("Reference", "{payment_ref}", max=16), C("Payment Type", "{mode_code}")],
        "header_lines": [], "trailer_lines": [],
        "file_name": "SBI-SAL-{period}-{group}-{batch}",
    },
}

# Axis Bank Corporate Internet Banking / PayPro bulk payment (low). Axis gives its "Bulk Payment
# Template" inside CIB (15 MB per file); one file may carry NEFT, RTGS, IMPS and internal fund
# transfer (IFT), split by a payment identifier per row. NEFT to a UTIB IFSC is turned into IFT by
# the bank. Corporate code + beneficiary code as in Axis's corporate API document.
AXIS = {
    "name": "Axis Bank Corporate Banking bulk payment", "bank": "Axis Bank", "confidence": "low",
    "notes": "Axis issues the template inside its corporate banking portal; one file may mix fund transfer (I), NEFT (N), "
             "RTGS (R) and IMPS (M). Compare the columns with your portal's template.",
    "sources": ["Axis Bank, Bulk Payment Uploads (axisbank.com, cash management services)",
                "Axis Bank CIB and PayPro FAQs (axisbank.com microsite-faqs_cib-and-paypro.pdf)",
                "Axis Bank corporate integration document (axis.bank.in, sme/integration-document.pdf)"],
    "spec": {
        "kind": "delimited", "delimiter": ",", "quote": "minimal", "extension": "csv", "line_ending": "crlf", "header_row": True,
        "date_format": "%d-%m-%Y", "amount": {"decimals": 2}, "split": "none", "clean": True,
        "ifsc_prefixes": ["UTIB"], "modes": ["internal", "neft", "rtgs", "imps"],
        "codes": {"internal": "I", "neft": "N", "rtgs": "R", "imps": "M"},
        "columns": [C("Payment Method", "{mode_code}"), C("Corporate Code", "{client_code}"), C("Debit Account Number", "{debit_account}"),
                    C("Value Date", "{date}"), C("Customer Reference Number", "{payment_ref}", max=20), C("Amount", "{amount}"),
                    C("Beneficiary Name", "{name}", max=40), C("Beneficiary Code", "{employee_code}", max=20),
                    C("Beneficiary Account Number", "{account}"), C("Beneficiary IFSC", "{ifsc}"), C("Beneficiary Email", "{email}"),
                    C("Beneficiary Mobile", "{phone}"), C("Remarks", "{narration}", max=30)],
        "header_lines": [], "trailer_lines": [],
        "file_name": "AXIS-SAL-{date:%d%m%Y}-{batch}",
    },
}

# Kotak Mahindra Bank CMS bulk upload (medium). Header row with these labels; NEFT, RTGS and IFT
# (internal) are the documented payment types, IMPS is offered on the same journey. Client code
# and product code are given by Kotak. Date dd/mm/yyyy. Debit account on every row; one file can
# carry several payment modes (Kotak bulk upload facility page).
KOTAK = {
    "name": "Kotak Mahindra Bank CMS bulk upload", "bank": "Kotak Mahindra Bank", "confidence": "medium",
    "notes": "Needs the client code Kotak gave you (set it on the debit account). Product code is SALARY; if Kotak gave "
             "you another one, make a custom copy and change it.",
    "sources": ["Kotak bulk upload facility (kotak.bank.in, business solutions)",
                "Kotak Bulk Upload Journey Manual (kapps.kotak.com, Bulk Upload Journey Manual-K1022.pdf)",
                "Open-source payment file generator (formats/kotak_mahindra_bank)"],
    "spec": {
        "kind": "delimited", "delimiter": ",", "quote": "minimal", "extension": "csv", "line_ending": "crlf", "header_row": True,
        "date_format": "%d/%m/%Y", "amount": {"decimals": 2}, "split": "none", "clean": True, "requires": ["client_code"],
        "ifsc_prefixes": ["KKBK"], "modes": ["internal", "neft", "rtgs", "imps"],
        "codes": {"internal": "IFT", "neft": "NEFT", "rtgs": "RTGS", "imps": "IMPS"},
        "columns": [C("Client_Code", "{client_code}"), C("Product_Code", "SALARY"), C("Payment_Type", "{mode_code}"),
                    C("Payment_Ref_No.", "{payment_ref}", max=20), C("Payment_Date", "{date}"), C("Dr_Ac_No", "{debit_account}"),
                    C("Amount", "{amount}"), C("Beneficiary_Code", "{employee_code}", max=15), C("Beneficiary_Name", "{name}", max=40),
                    C("IFSC Code", "{ifsc}"), C("Beneficiary_Acc_No", "{account}"), C("Beneficiary_Email", "{email}"),
                    C("Beneficiary_Mobile", "{phone}"), C("Debit_Narration", "{narration}", max=30),
                    C("Credit_Narration", "{narration}", max=30), C("Enrichment_1", "{employee_code}"), C("Enrichment_2"),
                    C("Enrichment_3"), C("Enrichment_4")],
        "header_lines": [], "trailer_lines": [],
        "file_name": "KOTAK-{client_code}-{date:%d%m%Y}-{batch}",
    },
}

# IDFC FIRST Bank corporate bulk upload (medium). 32 columns with a header row. Transaction Type
# Code BT = within IDFC FIRST (IFSC IDFB), LBT = other bank; Message Type NEFT / RTGS / IMPS;
# Purpose code OTH; currency INR; date DD-MM-YYYY; client code and debit account on every row.
IDFC = {
    "name": "IDFC FIRST Bank corporate bulk upload", "bank": "IDFC FIRST Bank", "confidence": "medium",
    "notes": "Needs your client code. BT = within IDFC FIRST, LBT = other banks (with NEFT, RTGS or IMPS as message type).",
    "sources": ["Open-source payment file generator (formats/idfc_bank, 32-column IDFC FIRST template)"],
    "spec": {
        "kind": "delimited", "delimiter": ",", "quote": "minimal", "extension": "csv", "line_ending": "crlf", "header_row": True,
        "date_format": "%d-%m-%Y", "amount": {"decimals": 2}, "split": "none", "clean": True, "requires": ["client_code"],
        "ifsc_prefixes": ["IDFB"], "modes": ["internal", "neft", "rtgs", "imps"],
        "codes": {"internal": "BT", "neft": "LBT", "rtgs": "LBT", "imps": "LBT"},
        "columns": [C("Client Code", "{client_code}"), C("Debit Account No.", "{debit_account}"),
                    C("Transaction Type Code", "{mode_code}"), C("Value Date", "{date}"), C("Payment Amount", "{amount}"),
                    C("Beneficiary Name", "{name}", max=40), C("Beneficiary Account No.", "{account}"),
                    C("Beneficiary Bank Swift Code / IFSC Code", "{ifsc}"), C("Customer Reference No", "{payment_ref}", max=20),
                    C("Beneficiary E-mail Id", "{email}"), C("Beneficiary Phone No", "{phone}"), C("Remarks", "{narration}", max=30),
                    C("Message Type", "{mode}"), C("Purpose Code", "OTH"), C("Beneficiary Account Type", "10"),
                    C("Pay At Location"), C("Print Branch"), C("Mode of Delivery"), C("Transaction Currency", "INR"),
                    C("Beneficiary Addr. Line 1"), C("Beneficiary Addr. Line 2"), C("Beneficiary Addr. Line 3"),
                    C("Beneficiary Addr. Line 4"), C("Beneficiary ID", "{employee_code}"), C("Remote Printing"),
                    C("Print Branch Location"), C("Nature of Payment", "Salary"), C("PoA Flag"), C("POA Account"),
                    C("Mode of UPI Payment"), C("Virtual Payment Address"), C("Remitter VPA")],
        "header_lines": [], "trailer_lines": [],
        "file_name": "IDFC-{client_code}-{date:%d%m%Y}-{batch}",
    },
}

# Union Bank of India corporate internet banking (high: the bank's own sample files).
# NEFT / RTGS file: TXT, pipe separated, first line FILEHDR|corporate id|file serial|encryption
# Y/N|remarks, then per payment: type|remitter (debit branch) IFSC|debit account|receiver IFSC|
# beneficiary account|INR|amount|remarks (140)|beneficiary name (40)|email (80)|mobile (20).
# Within Union Bank the salary file is separate (CSV): FILEHDR,corporate id,file sequence; one DR
# line for the total from the debit account; then one CR line per employee. Branch Sol ID is the
# branch code at the end of the IFSC.
UNION = {
    "name": "Union Bank of India corporate bulk (NEFT/RTGS + salary)", "bank": "Union Bank of India", "confidence": "high",
    "notes": "Needs your corporate id. Other-bank payments go in a NEFT/RTGS file; Union Bank accounts in a separate "
             "salary file whose debit line equals the total of credits.",
    "sources": ["Union Bank, Sample NEFT RTGS Fund Transfer File (unionbankonline.co.in, BULK_NEFT_RTGS_DEMO)",
                "Union Bank, Sample NEFT Format txt File (unionbankonline.co.in)",
                "Union Bank, Sample Salary Format CSV File (unionbankonline.co.in)"],
    "spec": {
        "kind": "delimited", "delimiter": "|", "quote": "none", "extension": "txt", "line_ending": "crlf", "header_row": False,
        "date_format": "%d/%m/%Y", "amount": {"decimals": 2}, "split": "internal_vs_other", "clean": True,
        "requires": ["corporate_id"], "ifsc_prefixes": ["UBIN"], "modes": ["internal", "neft", "rtgs"],
        "codes": {"internal": "CR", "neft": "NEFT", "rtgs": "RTGS"},
        "header_lines": [[C("Header", "FILEHDR"), C("Corporate ID", "{corporate_id}", max=32), C("File Serial", "{batch:6}"),
                          C("Encryption", "N"), C("Remarks", "{narration}", max=500)]],
        "columns": [C("Payment Type", "{mode_code}"), C("Remitter IFSC", "{debit_ifsc}"), C("Debit Account Number", "{debit_account}", max=24),
                    C("Receiver IFSC", "{ifsc}"), C("Beneficiary Account Number", "{account}", max=24), C("Currency", "INR"),
                    C("Amount", "{amount}"), C("Remarks", "{narration}", max=140), C("Beneficiary Name", "{name}", max=40),
                    C("Email", "{email}", max=80), C("Mobile", "{phone}", max=20)],
        "trailer_lines": [],
        "variants": {"internal": {
            "delimiter": ",", "extension": "csv",
            "header_lines": [[C("Header", "FILEHDR"), C("Corporate ID", "{corporate_id}", max=32), C("File Sequence", "{batch:6}")],
                             [C("Sol ID", "{debit_branch}", max=6), C("Debit Account", "{debit_account}", max=15), C("Currency", "INR"),
                              C("Type", "DR"), C("Amount", "{total}"), C("Remarks", "{narration}", max=13)]],
            "columns": [C("Sol ID", "{branch}", max=6), C("Credit Account", "{account}", max=15), C("Currency", "INR"),
                        C("Type", "CR"), C("Amount", "{amount}"), C("Remarks", "{narration}", max=13)]}},
        "file_name": "UNION-{corporate_id}-{date:%d%m%Y}-{group}-{batch}",
    },
}


def _finacle_psu(bank: str, prefix: str, sources: list[str], notes: str = "") -> dict:
    """Public sector banks on Finacle corporate banking (PNB, Bank of Baroda, Canara): the bank's
    sample files are downloaded inside the portal (PNB FAQ: Bulk file upload > NEFT/RTGS bulk
    upload > Download sample formats; old formats are rejected; salary and NEFT/RTGS are separate
    uploads; the amount typed on screen must equal the file total). Union Bank's published
    Finacle layout is used as the closest public reference (low)."""
    return {
        "name": f"{bank} corporate bulk upload", "bank": bank, "confidence": "low",
        "notes": notes or f"{bank} hands out its sample file inside corporate banking. Own-bank and other-bank payments are in "
                          "separate files. Compare with the sample before the first upload.",
        "sources": sources + ["Union Bank of India sample NEFT/RTGS and salary files (closest published Finacle layout)"],
        "spec": {
            "kind": "delimited", "delimiter": "|", "quote": "none", "extension": "txt", "line_ending": "crlf", "header_row": False,
            "date_format": "%d/%m/%Y", "amount": {"decimals": 2}, "split": "internal_vs_other", "clean": True,
            "ifsc_prefixes": [prefix], "modes": ["internal", "neft", "rtgs"],
            "codes": {"internal": "TRF", "neft": "NEFT", "rtgs": "RTGS"},
            "header_lines": [[C("Header", "FILEHDR"), C("Corporate ID", "{corporate_id}", max=32), C("File Serial", "{batch:6}"),
                              C("Value Date", "{date}"), C("Records", "{count}"), C("Total", "{total}")]],
            "columns": [C("Payment Type", "{mode_code}"), C("Debit Account Number", "{debit_account}"), C("Beneficiary IFSC", "{ifsc}"),
                        C("Beneficiary Account Number", "{account}"), C("Amount", "{amount}"), C("Beneficiary Name", "{name}", max=40),
                        C("Remarks", "{narration}", max=30), C("Reference", "{payment_ref}", max=16), C("Email", "{email}"),
                        C("Mobile", "{phone}")],
            "trailer_lines": [],
            "file_name": f"{prefix}-SAL-{{date:%d%m%Y}}-{{group}}-{{batch}}",
        },
    }


def _private_csv(bank: str, prefix: str, sources: list[str], codes: dict, notes: str = "", requires=None) -> dict:
    """Private banks that hand their template out in the portal (Yes Bank, IndusInd, Federal,
    South Indian Bank): the common private-bank layout (client code, debit account, payment type
    and value date per row, like Kotak and IDFC FIRST) is used (low)."""
    return {
        "name": f"{bank} corporate bulk upload", "bank": bank, "confidence": "low",
        "notes": notes or f"{bank} gives its template inside corporate banking; one file may mix payment types. "
                          "Compare with the sample before the first upload.",
        "sources": sources,
        "spec": {
            "kind": "delimited", "delimiter": ",", "quote": "minimal", "extension": "csv", "line_ending": "crlf", "header_row": True,
            "date_format": "%d/%m/%Y", "amount": {"decimals": 2}, "split": "none", "clean": True, "requires": requires or [],
            "ifsc_prefixes": [prefix], "modes": ["internal", "neft", "rtgs", "imps"], "codes": codes,
            "columns": [C("Client Code", "{client_code}"), C("Debit Account Number", "{debit_account}"),
                        C("Payment Type", "{mode_code}"), C("Value Date", "{date}"), C("Amount", "{amount}"),
                        C("Beneficiary Name", "{name}", max=40), C("Beneficiary Account Number", "{account}"),
                        C("Beneficiary IFSC", "{ifsc}"), C("Customer Reference", "{payment_ref}", max=20),
                        C("Beneficiary Email", "{email}"), C("Beneficiary Mobile", "{phone}"),
                        C("Remarks", "{narration}", max=30)],
            "header_lines": [], "trailer_lines": [],
            "file_name": f"{prefix}-SAL-{{date:%d%m%Y}}-{{batch}}",
        },
    }


_FT = {"internal": "FT", "neft": "NEFT", "rtgs": "RTGS", "imps": "IMPS"}

BUILTIN: dict[str, dict] = {
    "generic_neft": GENERIC,
    "sbi_cinb": SBI,
    "hdfc_enet": HDFC,
    "icici_cib": ICICI,
    "axis_cib": AXIS,
    "kotak_cms": KOTAK,
    "yes_connect": _private_csv("Yes Bank", "YESB", ["YES Bank corporate internet banking (YES CONNECT) bulk payments"], _FT,
                                requires=["client_code"]),
    "idfc_first": IDFC,
    "indusind": _private_csv("IndusInd Bank", "INDB", ["IndusInd Bank corporate internet banking (Indus Direct) bulk upload"], _FT),
    "bob": _finacle_psu("Bank of Baroda", "BARB", ["Bank of Baroda bob World Business / Baroda Connect bulk upload"]),
    "pnb": _finacle_psu("Punjab National Bank", "PUNB",
                        ["PNB corporate internet banking FAQs, Bulk Transfers (ibanking.pnb.bank.in, CorpFaq/BulkTransfers)"]),
    "canara": _finacle_psu("Canara Bank", "CNRB", ["Canara Bank corporate net banking, bulk NEFT and salary upload"]),
    "union": UNION,
    "federal": _private_csv("Federal Bank", "FDRL", ["Federal Bank FedCorp / PayLite (federalbank.co.in/paylite: up to 1000 "
                                                     "entries per file by IMPS, NEFT or RTGS)"], _FT),
    "sib": _private_csv("South Indian Bank", "SIBL", ["South Indian Bank SIBerNet corporate bulk upload"], _FT),
}

# Keys used before these layouts existed (payroll settings may still hold them)
LEGACY_KEYS = {"hdfc": "hdfc_enet", "icici": "icici_cib", "sbi": "sbi_cinb", "axis": "axis_cib", "kotak": "kotak_cms"}

_BY_PREFIX = {p: k for k, f in BUILTIN.items() for p in (f["spec"].get("ifsc_prefixes") or [])}


def bank_label(ifsc: str | None) -> str:
    return BANKS.get((ifsc or "")[:4].upper(), "")


def suggest_for_ifsc(ifsc: str | None) -> str:
    return _BY_PREFIX.get((ifsc or "")[:4].upper(), "generic_neft")
