"""
Einmalige Korrektur: bestehende recurring-Buchungen (Daueraufträge/Abbuchungen)
wurden durch einen Bug immer als + gebucht. Dieses Script korrigiert das Vorzeichen
nachträglich, ohne die bereits gespeicherten Beträge (Höhe) zu verändern.

Nutzung auf dem Server:
    python3 fix_recurring_signs.py [--apply]

Ohne --apply: nur Dry-Run, zeigt was geändert würde.
Mit --apply: schreibt Backup (db.json.<timestamp>.bak) und korrigiert db.json.
"""
import json
import os
import sys
import time

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "db.json"))


def main():
    apply = "--apply" in sys.argv

    with open(DB_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    lines_by_id = {l["id"]: l for l in data.get("lines", [])}
    rules_by_id = {r["id"]: r for r in data.get("recurring_rules", [])}

    changes = []
    for b in data.get("bookings", []):
        if b.get("source") != "recurring" or not b.get("recurring_rule_id"):
            continue
        rule = rules_by_id.get(b["recurring_rule_id"])
        if not rule:
            print(f"WARNUNG: Buchung {b.get('id')} verweist auf gelöschten Dauerauftrag, übersprungen")
            continue

        linked_ids = rule.get("linked_line_ids") or ([rule["linked_line_id"]] if rule.get("linked_line_id") else [])
        linked = [lines_by_id[lid] for lid in linked_ids if lid in lines_by_id]
        is_income = bool(linked) and all(l.get("type") == "income" for l in linked)

        current = float(b.get("amount") or 0)
        correct = abs(current) if is_income else -abs(current)

        if round(current, 2) != round(correct, 2):
            changes.append((b, current, correct))

    if not changes:
        print("Keine falsch gebuchten Daueraufträge/Abbuchungen gefunden.")
        return

    print(f"{len(changes)} Buchung(en) mit falschem Vorzeichen gefunden:")
    for b, current, correct in changes:
        print(f"  {b.get('date')}  {b.get('note','')!r:40}  {current:>10.2f} -> {correct:>10.2f}")

    if not apply:
        print("\nDry-Run — nichts geschrieben. Zum Anwenden: python3 fix_recurring_signs.py --apply")
        return

    backup_path = f"{DB_PATH}.{int(time.time())}.bak"
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Backup geschrieben: {backup_path}")

    for b, current, correct in changes:
        b["amount"] = correct

    tmp = DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, DB_PATH)
    print(f"{len(changes)} Buchung(en) korrigiert und in {DB_PATH} gespeichert.")


if __name__ == "__main__":
    main()
