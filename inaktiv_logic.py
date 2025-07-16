from datetime import datetime, timedelta
from webapp import db_connection

def pruefe_inaktive_konten():
    conn = db_connection()
    c = conn.cursor()

    grenze = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')

    c.execute('''
        SELECT name, punkte
        FROM konten
        WHERE letzte_aktivitaet < ?
        AND typ != 'fonds'
    ''', (grenze,))
    inaktive = c.fetchall()

    jetzt = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    for name, punkte in inaktive:
        neuer_stand = punkte - 10
        c.execute('''
            UPDATE konten
            SET punkte = ?, letzte_aktivitaet = ?
            WHERE name = ?
        ''', (neuer_stand, jetzt, name))

        c.execute('''
            INSERT INTO transaktionen
            (von, an, betrag, kulturbeitrag, brutto, netto, beschreibung, datum,
             stand_von_alt, stand_von_neu, stand_an_alt, stand_an_neu)
            VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, 0, 0)
        ''', (
            name, "[Inaktivität]", 10, 10, 10,
            "[Automatischer Abzug wegen Inaktivität]",
            jetzt, punkte, neuer_stand
        ))

    conn.commit()
    conn.close()
