# fees_job.py — führt die monatliche Parkgebühr-Buchung aus
# Robust: versucht mehrere mögliche Modulnamen deiner App zu importieren.

import importlib
import sys
import sqlite3
from datetime import date

CANDIDATES = ["webapp"]

appmod = None
for name in CANDIDATES:
    try:
        appmod = importlib.import_module(name)
        break
    except ModuleNotFoundError:
        continue

if appmod is None:
    print("Kein passendes App-Modul gefunden. Erwarte eine Datei wie webapp.py im selben Ordner.")
    sys.exit(1)

try:
    erstelle_datenbank = getattr(appmod, "erstelle_datenbank")
    apply_parkgebuehr_catchup = getattr(appmod, "apply_parkgebuehr_catchup")
except AttributeError as e:
    print("Die benötigten Funktionen wurden im App-Modul nicht gefunden:", e)
    print("Stelle sicher, dass in deiner App-Datei folgende Funktionen definiert sind:")
    print(" - erstelle_datenbank()")
    print(" - apply_parkgebuehr_catchup()")
    sys.exit(1)

def get_last_fee_month():
    conn = sqlite3.connect("verrechnung.db")
    c = conn.cursor()
    c.execute("SELECT value FROM systemstatus WHERE key = 'last_fee_month'")
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def get_target_month():
    today = date.today()
    year = today.year
    month = today.month - 1
    if month == 0:
        month = 12
        year -= 1
    return f"{year:04d}-{month:02d}"

if __name__ == "__main__":
    erstelle_datenbank()

    before = get_last_fee_month()
    target = get_target_month()

    apply_parkgebuehr_catchup()

    after = get_last_fee_month()

    if before == after:
        print(f"Parkgebühr: keine Buchung notwendig (last_done={after}, target={target})")
    else:
        print(f"Parkgebühr: gebucht für {target} (neuer last_done={after})")
