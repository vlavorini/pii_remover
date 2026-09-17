#!/usr/bin/env python3
"""Generate realistic sample documents (with synthetic PII) for local testing.

Every value here is fabricated. The shapes are correct (valid IBAN checksums,
Luhn-valid card numbers) so the validator layer reports them as confirmed.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SAMPLES = ROOT / "samples"

CV_TEXT = """Curriculum Vitae

PERSONAL DETAILS
Name: Mario Rossi
Date of birth: 14/03/1985
Place of birth: Milano (MI)
Nationality: Italian
Address: Via Roma 42, 20100 Milano, Italy
Email: mario.rossi@example.com
Mobile: +39 333 1234567
Codice Fiscale: RSSMRA85C14F205Z
IBAN: IT60X0542811101000000123456

PROFESSIONAL SUMMARY
Senior data engineer with 9 years of experience building batch and streaming
pipelines for financial services and retail clients.

WORK EXPERIENCE

2019 - present | Acme Analytics S.r.l., Milano
Senior Data Engineer
- Led the migration of 40+ ETL jobs to Spark, cutting nightly runtime by 62%.
- Designed the streaming ingestion layer for 1.2 billion events per month.

2015 - 2019 | Beta Consulting S.p.A., Torino
Data Engineer
- Built the warehouse for a 3 TB retail reporting platform.

EDUCATION
2012 - 2015 | Politecnico di Milano
MSc in Computer Engineering, 110/110 cum laude

SKILLS
Python, Spark, Kafka, SQL, Airflow, AWS, Terraform

LANGUAGES
Italian (native), English (C1), German (B1)

REFERENCES
Prof. Laura Conti - laura.conti@unimi.example.it - +39 02 5551234
"""

BANK_TEXT = """STATEMENT OF ACCOUNT

Account holder: Giulia Bianchi
Account number: 000012345678
IBAN: DE89370400440532013000
BIC: COBADEFFXXX
Statement period: 01/01/2024 - 31/01/2024
Tax ID: BNCGLI90A41F205X

SUMMARY
Opening balance: 4,120.55 EUR
Closing balance: 3,890.10 EUR
Total credits: 2,450.00 EUR
Total debits: 2,680.45 EUR

MOVEMENTS

| Date | Value date | Description | Counterparty | Amount |
| --- | --- | --- | --- | --- |
| 03/01/2024 | 03/01/2024 | Salary January | Acme Analytics S.r.l. | 2,450.00 |
| 07/01/2024 | 07/01/2024 | Rent January | Immobiliare Sole | -1,200.00 |
| 12/01/2024 | 12/01/2024 | Card payment | Supermarket Milano | -184.20 |
| 15/01/2024 | 15/01/2024 | Transfer out | IT60X0542811101000000123456 | -600.00 |
| 21/01/2024 | 21/01/2024 | Utility bill | Enel Energia | -96.25 |
| 28/01/2024 | 28/01/2024 | Card payment | Trattoria da Gino | -600.00 |

CONTACTS
Your personal advisor: Dott. Paolo Ferrari
Phone: +39 02 87654321
Email: paolo.ferrari@bank.example

Unless you notify us of any error within 60 days, this statement is considered
approved. For assistance call 800 123 456 or write to assistenza@bank.example.
"""

LEDGER_ROWS = [
    ("2024-01-05", "INV-2024-001", "Acme Analytics S.r.l.", "Consulting", "12,500.00", "EUR", "paid"),
    ("2024-01-11", "INV-2024-002", "Beta Consulting S.p.A.", "Licence fee", "3,200.00", "EUR", "paid"),
    ("2024-01-18", "INV-2024-003", "Gamma Retail Ltd", "Implementation", "48,000.00", "GBP", "open"),
    ("2024-01-24", "INV-2024-004", "Delta Finance AG", "Support", "9,750.00", "CHF", "paid"),
    ("2024-02-02", "INV-2024-005", "Epsilon Media S.r.l.", "Training", "5,400.00", "EUR", "overdue"),
    ("2024-02-09", "INV-2024-006", "Zeta Logistics GmbH", "Integration", "22,300.00", "EUR", "open"),
    ("2024-02-15", "INV-2024-007", "Eta Retail Ltd", "Audit", "15,000.00", "GBP", "paid"),
    ("2024-02-21", "INV-2024-008", "Theta Health S.r.l.", "Consulting", "7,850.00", "EUR", "open"),
    ("2024-03-01", "INV-2024-009", "Iota Bank SA", "Implementation", "61,000.00", "EUR", "paid"),
    ("2024-03-08", "INV-2024-010", "Kappa Manufacturing", "Support", "4,120.00", "EUR", "overdue"),
]


def write_text_samples() -> None:
    (SAMPLES / "cv_mario_rossi.txt").write_text(CV_TEXT, encoding="utf-8")
    (SAMPLES / "bank_statement_giulia_bianchi.txt").write_text(BANK_TEXT, encoding="utf-8")


def write_csv_sample() -> None:
    lines = ["date,invoice,customer,service,amount,currency,status",
             "note: contact billing at billing@example.com or +39 02 1234567"]
    for row in LEDGER_ROWS:
        lines.append(",".join(row))
    (SAMPLES / "invoices_ledger.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_pdf_sample() -> bool:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    except ImportError:
        print("    reportlab not installed - skipping the PDF sample", file=sys.stderr)
        return False

    styles = getSampleStyleSheet()
    path = SAMPLES / "cv_mario_rossi.pdf"
    doc = SimpleDocTemplate(str(path), pagesize=A4, title="Curriculum Vitae")
    story = []
    for raw_block in CV_TEXT.split("\n\n"):
        block = raw_block.strip()
        if not block:
            continue
        style = styles["Heading2"] if block.isupper() else styles["BodyText"]
        for line in block.splitlines():
            story.append(Paragraph(line.replace("&", "&amp;"), style))
        story.append(Spacer(1, 10))
    doc.build(story)
    return True


def write_spreadsheet_sample() -> bool:
    try:
        import pandas as pd
    except ImportError:
        print("    pandas not installed - skipping the XLSX sample", file=sys.stderr)
        return False

    frame = pd.DataFrame(
        LEDGER_ROWS,
        columns=["date", "invoice", "customer", "service", "amount", "currency", "status"],
    )
    frame["account_manager_email"] = [
        "sara.ricci@example.com", "sara.ricci@example.com", "luca.marino@example.com",
        "luca.marino@example.com", "sara.ricci@example.com", "luca.marino@example.com",
        "sara.ricci@example.com", "luca.marino@example.com", "sara.ricci@example.com",
        "luca.marino@example.com",
    ]
    frame["manager_phone"] = "+39 02 1234567"
    frame.to_excel(SAMPLES / "invoices_ledger.xlsx", index=False)
    return True


def write_image_sample() -> bool:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False

    image = Image.new("RGB", (1100, 620), "white")
    draw = ImageDraw.Draw(image)
    lines = [
        "PERSONAL DETAILS",
        "Name: Mario Rossi",
        "Email: mario.rossi@example.com",
        "Mobile: +39 333 1234567",
        "IBAN: IT60X0542811101000000123456",
        "Address: Via Roma 42, 20100 Milano",
    ]
    y = 40
    for index, line in enumerate(lines):
        draw.text((50, y), line, fill="black", font_size=26 if index else 32)
        y += 60 if index else 80
    draw.rectangle([50, y + 10, 1050, y + 130], outline="#888", width=2)
    draw.text((70, y + 40), "[photo: portrait of a person]", fill="#666", font_size=22)
    image.save(SAMPLES / "form_scanned.png")
    return True


def main() -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    write_text_samples()
    write_csv_sample()
    made = ["cv_mario_rossi.txt", "bank_statement_giulia_bianchi.txt", "invoices_ledger.csv"]
    if write_pdf_sample():
        made.append("cv_mario_rossi.pdf")
    if write_spreadsheet_sample():
        made.append("invoices_ledger.xlsx")
    if write_image_sample():
        made.append("form_scanned.png")
    print("samples written to", SAMPLES.relative_to(ROOT))
    for name in made:
        print("  -", name)


if __name__ == "__main__":
    main()
