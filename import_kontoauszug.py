"""
Importiert einen Trade-Republic-Kontoauszug (PDF) und bucht automatisch:
  - Handel (Kauf/Verkauf, NICHT "Savings plan execution") -> Rentensparen Sammelkonto
  - Alles andere (Kartentransaktionen, Bonus, Zinsen, SEPA-Lastschrift, sonstige
    Überweisungen) -> Lebensmittel (= Lebenskonto)
  - Übersprungen: "Savings plan execution"-Zeilen (schon per Dauerauftrag
    getrackt) und Überweisungen mit "Thomas Kurfiss" im Text (schon als
    Consorsbank<->Trade-Transfer modelliert)

Dedup: jede Zeile bekommt einen Fingerprint (Datum+Typ+Beschreibung+Betrag);
bereits importierte Fingerprints werden in db.json unter "imported_statement_txns"
gespeichert, ein erneuter Import desselben Auszugs bucht nicht doppelt.

Nutzung:
    python3 import_kontoauszug.py <pfad-zum-pdf> [--apply]

Ohne --apply: nur Dry-Run, zeigt was gebucht würde.
Mit --apply: schreibt Backup (db.json.<timestamp>.bak) und bucht.
"""
import hashlib
import json
import os
import re
import sys
import time

import fitz  # PyMuPDF

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "db.json"))

LEBENSMITTEL_ACC = "4f41ae14-3a8f-4a31-8f76-9db052a0f910"   # Lebensmittel Trade Republic (= Lebenskonto)
RENTENSPAREN_ACC = "5e30d1a6-50f8-41bb-a362-c16308e04a4d"   # Traderepublic Rentensparen Sammelkonto

MONTHS = {
    "jan": 1, "feb": 2, "mär": 3, "mrz": 3, "apr": 4, "mai": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "okt": 10, "nov": 11, "dez": 12,
}

MONEY_RE = re.compile(r"^([\d.]+),(\d{2})\s*.$")
DAY_RE = re.compile(r"^(\d{1,2})$")
MONTH_RE = re.compile(r"^([A-Za-zÄÖÜäöü]+)\.$")
YEAR_RE = re.compile(r"^(\d{4})$")


def _parse_money(s):
    m = MONEY_RE.match(s)
    if not m:
        return None
    return float(m.group(1).replace(".", "") + "." + m.group(2))


def parse_statement(pdf_path):
    return _parse_doc(fitz.open(pdf_path))


def parse_statement_bytes(data):
    return _parse_doc(fitz.open(stream=data, filetype="pdf"))


def _parse_doc(doc):
    lines = []
    for pg in doc:
        pglines = pg.get_text().split("\n")
        try:
            start = next(i for i, l in enumerate(pglines) if l.strip() == "SALDO")
        except StopIteration:
            continue
        try:
            end = next(i for i, l in enumerate(pglines) if i > start and "TRADE REPUBLIC BANK" in l.upper())
        except StopIteration:
            end = len(pglines)
        lines.extend(pglines[start + 1:end])
    lines = [l.strip() for l in lines if l.strip() != ""]

    txns = []
    i, n = 0, len(lines)
    while i < n:
        dm = DAY_RE.match(lines[i])
        if not dm:
            i += 1
            continue
        mm = MONTH_RE.match(lines[i + 1]) if i + 1 < n else None
        ym = YEAR_RE.match(lines[i + 2]) if i + 2 < n else None
        if not (mm and ym):
            i += 1
            continue
        month = MONTHS.get(mm.group(1).lower())
        if not month:
            i += 1
            continue
        day, year = int(dm.group(1)), int(ym.group(1))
        j = i + 3
        typ = lines[j]
        j += 1
        desc_parts = []
        m2 = re.match(r"^(Kartentransaktion)\s+(.*)$", typ)
        if m2:
            typ, first = m2.group(1), m2.group(2)
            desc_parts.append(first)
        while j < n and _parse_money(lines[j]) is None:
            desc_parts.append(lines[j])
            j += 1
        amount = _parse_money(lines[j]) if j < n else None
        saldo = _parse_money(lines[j + 1]) if j + 1 < n else None
        if amount is None or saldo is None:
            i += 1
            continue
        txns.append({
            "date": f"{year:04d}-{month:02d}-{day:02d}",
            "typ": typ,
            "desc": " ".join(desc_parts),
            "amount": amount,
            "saldo": saldo,
        })
        i = j + 2

    # Vorzeichen aus Saldo-Verlauf ableiten (robuster als Spaltenzuordnung im Text)
    prev = None
    for t in txns:
        if prev is not None:
            t["signed_amount"] = round(t["saldo"] - prev, 2)
        else:
            t["signed_amount"] = None  # erste Zeile: kein Vorsaldo bekannt, unten korrigiert
        prev = t["saldo"]
    return txns


def fingerprint(t):
    raw = f"{t['date']}|{t['typ']}|{t['desc']}|{t['amount']:.2f}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def classify(t):
    typ = t["typ"]
    desc = t["desc"]
    if "Handel" in typ:
        if desc.startswith("Savings plan execution"):
            return "skip", "Sparplan (bereits als Dauerauftrag getrackt)"
        return "book", RENTENSPAREN_ACC
    if "berweisung" in typ or "Überweisung" in typ:  # Ü teils als Ersatzzeichen extrahiert
        if "Thomas Kurfiss" in desc:
            return "skip", "Interner Consorsbank<->Trade-Transfer (bereits modelliert)"
        return "book", LEBENSMITTEL_ACC
    return "book", LEBENSMITTEL_ACC


def build_plan(txns, imported_fingerprints):
    """Klassifiziert geparste Transaktionen gegen bereits importierte Fingerprints.
    Gibt (to_book, skipped, duplicates) zurück."""
    if txns and txns[0]["signed_amount"] is None:
        # allererste Zeile: kein Vorgänger-Saldo bekannt -> Vorzeichen über Betrag annehmen
        txns[0]["signed_amount"] = txns[0]["amount"] if txns[0]["saldo"] > txns[0]["saldo"] - txns[0]["amount"] else -txns[0]["amount"]

    to_book, skipped, duplicates = [], [], []
    for t in txns:
        fp = fingerprint(t)
        if fp in imported_fingerprints:
            duplicates.append(t)
            continue
        action, target = classify(t)
        if action == "skip":
            skipped.append((t, target))
        else:
            to_book.append((t, target, fp))
    return to_book, skipped, duplicates


def main():
    if len(sys.argv) < 2:
        print("Nutzung: python3 import_kontoauszug.py <pfad-zum-pdf> [--apply]")
        sys.exit(1)
    pdf_path = sys.argv[1]
    apply = "--apply" in sys.argv

    data = json.load(open(DB_PATH, encoding="utf-8"))
    imported = set(data.get("imported_statement_txns") or [])

    txns = parse_statement(pdf_path)
    to_book, skipped, duplicates = build_plan(txns, imported)

    print(f"{len(txns)} Transaktionen im Auszug, {len(duplicates)} bereits importiert (übersprungen)")
    print(f"\n{len(skipped)} bewusst übersprungen:")
    for t, reason in skipped:
        print(f"  {t['date']}  {t['signed_amount']:>10.2f}  {t['typ']:16} {t['desc'][:55]:55} | {reason}")

    print(f"\n{len(to_book)} werden gebucht:")
    acc_names = {RENTENSPAREN_ACC: "Rentensparen Sammelkonto", LEBENSMITTEL_ACC: "Lebensmittel"}
    for t, target, _ in to_book:
        print(f"  {t['date']}  {t['signed_amount']:>10.2f}  -> {acc_names[target]:26} | {t['typ']:16} {t['desc'][:45]}")

    total = sum(t["signed_amount"] for t, _, _ in to_book)
    print(f"\nSumme neu zu buchender Betrag: {total:.2f} €")

    if not apply:
        print("\nDry-Run — nichts geschrieben. Zum Anwenden: --apply")
        return

    backup_path = f"{DB_PATH}.{int(time.time())}.import.bak"
    json.dump(data, open(backup_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\nBackup geschrieben: {backup_path}")

    commit_plan(data, to_book, skipped, imported)
    json.dump(data, open(DB_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"{len(to_book)} Buchung(en) angelegt, DB gespeichert.")


def commit_plan(data, to_book, skipped, imported_fingerprints):
    """Legt Buchungen aus to_book an und merkt alle (gebuchten + übersprungenen)
    Fingerprints als importiert vor, damit ein erneuter Import nichts doppelt bucht.
    Mutiert `data` in place."""
    import uuid
    for t, target, fp in to_book:
        data["bookings"].append({
            "id": str(uuid.uuid4()),
            "account_id": target,
            "date": t["date"],
            "amount": t["signed_amount"],
            "note": f"{t['typ']}: {t['desc']}"[:200],
            "source": "manual",
            "recurring_rule_id": None,
        })
        imported_fingerprints.add(fp)
    for t, _ in skipped:
        imported_fingerprints.add(fingerprint(t))
    data["imported_statement_txns"] = sorted(imported_fingerprints)


if __name__ == "__main__":
    main()
