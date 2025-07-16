import sqlite3
from datetime import datetime

DB_NAME = "verrechnung.db"

def init_db():
    with sqlite3.connect(DB_NAME) as db:
        db.execute("DROP TABLE IF EXISTS konten")
        db.execute("DROP TABLE IF EXISTS transaktionen")
        db.execute("DROP TABLE IF EXISTS benutzer")

        # Konten-Tabelle
        db.execute("""
            CREATE TABLE konten (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                typ TEXT,
                punkte REAL,
                letzte_aktivitaet TEXT,
                besitzer TEXT
            )
        """)

        # Benutzer-Tabelle
        db.execute("""
            CREATE TABLE benutzer (
                benutzer TEXT PRIMARY KEY,
                passwort TEXT NOT NULL
            )
        """)

        # Transaktionen-Tabelle
        db.execute("""
    	    CREATE TABLE transaktionen (
      		id INTEGER PRIMARY KEY AUTOINCREMENT,
        	von TEXT,
        	an TEXT,
        	betrag REAL,
        	kulturbeitrag REAL,
        	brutto REAL,
        	netto REAL,
        	beschreibung TEXT,
        	datum TEXT,
        	stand_von_alt REAL,
        	stand_von_neu REAL,
        	stand_an_alt REAL,
        	stand_an_neu REAL
   	   )
	""")

        heute = datetime.today().strftime('%Y-%m-%d')

        # Benutzer
        benutzer = [
            ("anna", "123"),
            ("ben", "123"),
            ("clara", "123"),
            ("david", "123"),
            ("emma", "123"),
            ("admin", "admin123")
        ]
        db.executemany("INSERT INTO benutzer VALUES (?, ?)", benutzer)

        # Konten mit 1000 Punkten (außer Kulturkonto)
        konten = [
            ("Anna (Privat)", "privat", 1000, heute, "anna"),
            ("Anna (Gemüsehof)", "geschäftlich", 1000, heute, "anna"),
            ("Ben (Privat)", "privat", 1000, heute, "ben"),
            ("Ben (Zimmerei)", "geschäftlich", 1000, heute, "ben"),
            ("Clara (Privat)", "privat", 1000, heute, "clara"),
            ("Clara (Dorfladen Küche)", "geschäftlich", 1000, heute, "clara"),
            ("David (Privat)", "privat", 1000, heute, "david"),
            ("David (Büroservice)", "geschäftlich", 1000, heute, "david"),
            ("Emma (Privat)", "privat", 1000, heute, "emma"),
            ("Emma (Werkstatt)", "geschäftlich", 1000, heute, "emma"),
            ("Kulturkonto", "fonds", 0, heute, "system")
        ]
        db.executemany(
            "INSERT INTO konten (name, typ, punkte, letzte_aktivitaet, besitzer) VALUES (?, ?, ?, ?, ?)",
            konten
        )

def main():
    init_db()
    print("✅ Datenbank erfolgreich eingerichtet.")

if __name__ == "__main__":
    main()

