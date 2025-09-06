
from flask import Flask, render_template_string, request, redirect, session, Response, url_for
import sqlite3
from datetime import datetime
from datetime import timedelta
import csv
import io
import time
import re
from datetime import datetime
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)
app.secret_key = 'geheim'
import os

# Basis-Ordner = der Ordner, in dem dieses Skript liegt
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# DB-Datei liegt im selben Ordner wie das Skript
DB_PATH = os.path.join(BASE_DIR, 'verrechnung.db')

def db_connection():
    return sqlite3.connect(DB_PATH)

def ensure_settings_table(conn):
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()

def get_brotpreis_eur_pro_punkt(default_value=8.38):
    conn = db_connection()
    ensure_settings_table(conn)
    c = conn.cursor()
    c.execute("SELECT value, updated_at FROM settings WHERE key = 'brotpreis_eur_pro_punkt'")
    row = c.fetchone()
    conn.close()
    if row:
        return float(row[0]), row[1]
    else:
        # default initial eintragen
        set_brotpreis_eur_pro_punkt(default_value, source_label="Initial (manuell)")
        return default_value, datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def set_brotpreis_eur_pro_punkt(value, source_label="Bäckerei Schüren"):
    conn = db_connection()
    ensure_settings_table(conn)
    c = conn.cursor()
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute("""
        INSERT INTO settings (key, value, updated_at)
        VALUES ('brotpreis_eur_pro_punkt', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
    """, (str(float(value)), ts))
    conn.commit()
    conn.close()
    return ts

def erstelle_datenbank():
    conn = db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS konten (
        id INTEGER PRIMARY KEY,
        name TEXT,
        typ TEXT,
        punkte INTEGER,
        letzte_aktivitaet TEXT,
        besitzer TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS transaktionen (
        id INTEGER PRIMARY KEY,
        von TEXT,
        an TEXT,
        betrag INTEGER,
        kulturbeitrag INTEGER,
        brutto INTEGER,
        netto INTEGER,
        beschreibung TEXT,
        datum TEXT,
        stand_von_alt INTEGER,
        stand_von_neu INTEGER,
        stand_an_alt INTEGER,
        stand_an_neu INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS benutzer (
        benutzer TEXT PRIMARY KEY,
        passwort TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS systemstatus (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS konten_benutzer (
        id INTEGER PRIMARY KEY,
        konto_id INTEGER NOT NULL,
        benutzer_login TEXT NOT NULL,
        UNIQUE(konto_id, benutzer_login)
    )''')

    conn.commit()
    conn.close()

def month_key(dt):
    """Liefert den Monat als 'YYYY-MM'."""
    return dt.strftime('%Y-%m')

def add_month(key_yyyy_mm):
    """Erhöht einen 'YYYY-MM'-Key um 1 Monat."""
    y, m = map(int, key_yyyy_mm.split('-'))
    m += 1
    if m == 13:
        y += 1
        m = 1
    return f"{y}-{m:02d}"

def previous_month_key(dt):
    """Gibt den Vormonat als 'YYYY-MM' zurück."""
    y, m = dt.year, dt.month
    if m == 1:
        return f"{y-1}-12"
    return f"{y}-{m-1:02d}"

def eom_cutoff(month_key: str) -> str:
    """
    Gibt 'YYYY-MM-DD HH:MM:SS' für den letzten Moment des Monats 'YYYY-MM'
    (23:59:59 am letzten Tag) zurück.
    """
    y, m = map(int, month_key.split('-'))
    if m == 12:
        y2, m2 = y + 1, 1
    else:
        y2, m2 = y, m + 1
    first_of_next = datetime(y2, m2, 1, 0, 0, 0)
    cutoff = first_of_next - timedelta(seconds=1)
    return cutoff.strftime('%Y-%m-%d %H:%M:%S')


def balance_as_of(conn, konto_name: str, cutoff_str: str) -> float:
    """
    Kontostand des Kontos zum Zeitpunkt 'cutoff_str' (<= cutoff), basierend
    auf Transaktionshistorie. Wenn bis dahin keine Transaktion: 0.0.
    """
    c = conn.cursor()
    c.execute("""
        WITH tx AS (
          SELECT id, datum, von AS konto, stand_von_neu AS stand_neu
          FROM transaktionen
          WHERE von = ? AND datum <= ?
          UNION ALL
          SELECT id, datum, an AS konto, stand_an_neu AS stand_neu
          FROM transaktionen
          WHERE an = ? AND datum <= ?
        )
        SELECT stand_neu
        FROM tx
        ORDER BY datum DESC, id DESC
        LIMIT 1
    """, (konto_name, cutoff_str, konto_name, cutoff_str))
    row = c.fetchone()
    if row and row[0] is not None:
        try:
            return float(row[0])
        except Exception:
            return 0.0
    return 0.0



def apply_parkgebuehr_catchup(now=None):
    if now is None:
        now = datetime.now()
    """
    Holt verpasste Monate nach und bucht für jeden ausstehenden Monat 1% auf Basis des
    EoM-Kontostands (Stand am Monatsende 23:59:59) von allen positiven Guthaben
    (Kulturfonds ausgenommen) ab und schreibt sie dem Kulturfonds gut.
    Idempotent pro Monat (merkt 'last_fee_month' in systemstatus).
    """
    conn = db_connection()
    c = conn.cursor()

    now = datetime.now()
    target_month = previous_month_key(now)  # bis inkl. Vormonat aufholen

    # letzten erledigten Monat lesen (falls keiner: so initialisieren,
    # dass NICHT rückwirkend gebucht wird)
    c.execute("SELECT value FROM systemstatus WHERE key = 'last_fee_month'")
    row = c.fetchone()
    if row:
        last_done = row[0]
    else:
        # Erster Lauf: so initialisieren, dass der Vormonat JETZT gebucht wird.
        # Die Schleife unten startet mit: current = add_month(last_done).
        # Deshalb setzen wir last_done auf "zwei Monate zurück", damit current = Vormonat.
        last_done = previous_month_key(previous_month_key(now))
        c.execute("INSERT OR REPLACE INTO systemstatus (key, value) VALUES ('last_fee_month', ?)", (last_done,))
        conn.commit()
        # WICHTIG: KEIN return – die Schleife unten bucht jetzt den Vormonat.


    # Kulturfonds ermitteln
    c.execute("SELECT name FROM konten WHERE typ = 'fonds' ORDER BY id LIMIT 1")
    kk_row = c.fetchone()
    kulturfonds = kk_row[0] if kk_row else None

    if not kulturfonds:
        # Kein Kulturfonds-Konto gefunden -> NICHT vorspulen, sonst gehen Monate verloren.
        print(f"[fees] Kein Kulturfonds-Konto – breche ab. last_fee_month bleibt bei {last_done}, target={target_month}")
        conn.close()
        return

    # Nichts zu tun?
    if last_done >= target_month:
        print(f"[fees] Start: now={now:%Y-%m-%d %H:%M:%S}, last_done={last_done}, target={target_month}")
        conn.close()
        return

    # Namen der Kandidaten einmal holen (alle Konten außer Fonds)
    c.execute("""
        SELECT name FROM konten
        WHERE (typ IS NULL OR typ != 'fonds')
    """)
    konto_namen = [r[0] for r in c.fetchall()]

    # Monat für Monat aufholen
    current = add_month(last_done)
    jetzt_str = now.strftime('%Y-%m-%d %H:%M:%S')

    while current <= target_month:
        cutoff = eom_cutoff(current)

        # Kulturfonds-Kontostand alt (für Logging)
        c.execute("SELECT punkte FROM konten WHERE name = ?", (kulturfonds,))
        kk_alt = c.fetchone()[0]

        for name in konto_namen:
            # EoM-Stand für diesen Monat bestimmen
            eom_stand = balance_as_of(conn, name, cutoff)
            if eom_stand <= 0:
                continue

            gebuehr = round(eom_stand * 0.01, 1)  # 1 Nachkommastelle wie bei dir üblich
            if gebuehr <= 0:
                continue

            # Altstand des Kontos (aktueller IST-Stand, da wir jetzt buchen)
            c.execute("SELECT punkte FROM konten WHERE name = ?", (name,))
            alt_von = c.fetchone()[0]

            # Abbuchen / Gutschreiben
            c.execute("UPDATE konten SET punkte = punkte - ?, letzte_aktivitaet = ? WHERE name = ?", (gebuehr, jetzt_str, name))
            c.execute("UPDATE konten SET punkte = punkte + ?, letzte_aktivitaet = ? WHERE name = ?", (gebuehr, jetzt_str, kulturfonds))

            # Neue Stände nach Buchung
            c.execute("SELECT punkte FROM konten WHERE name = ?", (name,))
            neu_von = c.fetchone()[0]
            c.execute("SELECT punkte FROM konten WHERE name = ?", (kulturfonds,))
            neu_an = c.fetchone()[0]

            # Transaktion erfassen (kein zusätzlicher 5%-Kulturbeitrag)
            c.execute('''INSERT INTO transaktionen 
                (von, an, betrag, kulturbeitrag, brutto, netto, beschreibung, datum,
                 stand_von_alt, stand_von_neu, stand_an_alt, stand_an_neu)
                VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (name, kulturfonds, gebuehr, gebuehr, gebuehr,
                 f"[Parkgebühr 1% für {current} (EoM-Basis)]", jetzt_str,
                 alt_von, neu_von, kk_alt, neu_an))

            kk_alt = neu_an  # fürs nächste Konto fortschreiben

        # Monat als erledigt markieren, weiter
        c.execute("INSERT OR REPLACE INTO systemstatus (key, value) VALUES ('last_fee_month', ?)", (current,))
        print(f"[fees] Gebucht: month={current}, new_last_done={current}")
        current = add_month(current)

    conn.commit()
    conn.close()

def _pad_to_11(rows):
    """Sorgt dafür, dass jede Zeile 11 Felder hat (füllt ggf. mit leeren Strings auf)."""
    padded = []
    for r in rows:
        if len(r) >= 11:
            padded.append(tuple(r[:11]))
        else:
            padded.append(tuple(r) + tuple("" for _ in range(11 - len(r))))
    return padded

def _select_transaktionen(c, where_sql="", params=(), limit=20):
    """
    Versucht zuerst die 11-Spalten-Variante.
    Fällt bei älterem Schema (ohne stand_* Spalten) automatisch auf 7 Spalten zurück
    und füllt die fehlenden 4 Felder auf.
    """
    base11 = """SELECT von, an, brutto, kulturbeitrag, netto, beschreibung, datum,
                       stand_von_alt, stand_von_neu, stand_an_alt, stand_an_neu
                FROM transaktionen"""
    base7  = """SELECT von, an, brutto, kulturbeitrag, netto, beschreibung, datum
                FROM transaktionen"""

    order_limit = f" ORDER BY id DESC LIMIT {int(limit)}"
    where_clause = f" WHERE {where_sql}" if where_sql.strip() else ""

    # 11-Spalten versuchen
    try:
        sql = base11 + where_clause + order_limit
        return c.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        # 7-Spalten Fallback + auf 11 auffüllen
        sql = base7 + where_clause + order_limit
        rows = c.execute(sql, params).fetchall()
        return _pad_to_11(rows)

@app.before_request
def check_inactivity_and_single_user():
    # Wenn niemand eingeloggt ist, gib die Variable frei
    if 'user' not in session:
        app.config['ACTIVE_USER'] = None
        return

    # Wenn jemand eingeloggt ist:
    jetzt = time.time()
    letzte_aktivitaet = session.get('last_active', jetzt)

    # Automatischer Logout nach 2 Minuten Inaktivität
    if jetzt - letzte_aktivitaet > 120:
        session.clear()
        app.config['ACTIVE_USER'] = None
        return redirect(url_for('login'))

    # Aktualisiere Aktivitätszeit
    session['last_active'] = jetzt

    # Wenn bereits jemand anders eingeloggt ist, blockieren
    active_user = app.config.get('ACTIVE_USER')
    if active_user is not None and session['user'] != active_user:
        session.clear()
        return redirect(url_for('login'))



@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        benutzer = request.form['benutzer']
        passwort = request.form['passwort']
        conn = db_connection()
        c = conn.cursor()
        c.execute("SELECT * FROM benutzer WHERE benutzer = ? AND passwort = ?", (benutzer, passwort))
        if c.fetchone():
            session['user'] = benutzer
            session['last_active'] = time.time()
            app.config['ACTIVE_USER'] = benutzer
            ziel = request.args.get('an')
            if ziel:
              return redirect(url_for('index', an=ziel))
            return redirect('/start')
        else:
            return "Login fehlgeschlagen"
    return '''
<!doctype html>
<html lang="de">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Login – Wieshof</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
  </head>
  <body class="container mt-5">

    <div class="card shadow p-4 mx-auto" style="max-width: 400px;">
      <h2 class="mb-4 text-center">Login</h2>
      <form method="post">
        <div class="mb-3">
          <label for="benutzer" class="form-label">Benutzername:</label>
          <input type="text" class="form-control" id="benutzer" name="benutzer" required>
        </div>
        <div class="mb-3">
          <label for="passwort" class="form-label">Passwort:</label>
          <input type="password" class="form-control" id="passwort" name="passwort" required>
        </div>
        <button type="submit" class="btn btn-primary w-100">Login</button>
      </form>
    </div>

  </body>
</html>
'''

@app.route('/logout')
def logout():
    session.clear()
    app.config['ACTIVE_USER'] = None
    return redirect(url_for('system'))

@app.route('/start', methods=['GET', 'POST'])
def index():
    if 'user' not in session:
        return redirect(url_for('login', an=request.args.get('an')))

    empfaenger_vorauswahl = request.args.get("an")

    conn = db_connection()
    c = conn.cursor()

    # Konten laden (Admin = alle, sonst über Mapping-Tabelle)
    if session["user"] == "admin":
        c.execute("SELECT name, typ, punkte FROM konten ORDER BY name ASC")
        rows = c.fetchall()
        # Admin: 3 Spalten -> hier runden
        konten = [(name, typ, round(punkte, 1)) for name, typ, punkte in rows]
    else:
        c.execute("""
            SELECT k.name, k.typ, k.punkte, k.letzte_aktivitaet
            FROM konten k
            JOIN konten_benutzer kb ON kb.konto_id = k.id
            WHERE kb.benutzer_login = ?
        """, (session["user"],))
        rows = c.fetchall()
        # Nutzer/Fonds: 4 Spalten -> hier runden
        konten = [(name, typ, round(punkte, 1), letzte) for name, typ, punkte, letzte in rows]

    eigene_kontonamen = [k[0] for k in konten]

    # Kulturfonds-Konto bestimmen
    c.execute("SELECT name FROM konten WHERE typ = 'fonds'")
    alle_fonds_konten = [row[0] for row in c.fetchall()]
    kulturfonds_name = alle_fonds_konten[0] if alle_fonds_konten else None

    jetzt = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # POST: Überweisung oder Fonds-Ausgabe
    if request.method == 'POST' and session['user'] != 'admin':
        if 'ausgabe_von' in request.form:
            # Abzug aus Fonds
            von = request.form['ausgabe_von'].strip()
            betrag = float(request.form['ausgabe_betrag'])
            beschreibung = request.form['ausgabe_beschreibung'].strip()
            c.execute("SELECT punkte FROM konten WHERE name = ?", (von,))
            alt_von = c.fetchone()[0]
            neu_von = alt_von - betrag
            c.execute("UPDATE konten SET punkte = ?, letzte_aktivitaet = ? WHERE name = ?", (neu_von, jetzt, von))
            c.execute('''INSERT INTO transaktionen 
                (von, an, betrag, kulturbeitrag, brutto, netto, beschreibung, datum,
                 stand_von_alt, stand_von_neu, stand_an_alt, stand_an_neu)
                VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, 0, 0)''',
                (von, "[Ausgabe]", betrag, betrag, betrag, beschreibung, jetzt, alt_von, neu_von))
            conn.commit()
            conn.close()
            return redirect('/start')
        else:
            # Normale Transaktion
            von = request.form['von'].strip()
            an = request.form['an'].strip()
            betrag = float(request.form['betrag'])
            beschreibung = request.form['beschreibung'].strip()
            kulturbeitrag = round(betrag * 0.05, 1)
            netto = round(betrag - kulturbeitrag, 1)
            brutto = betrag

             # aktuellen Stand des Senders laden (brauchen wir für die Limitprüfung)
            c.execute("SELECT punkte FROM konten WHERE name = ?", (von,))
            alt_von = c.fetchone()[0]

            # Überziehungsgrenze prüfen – vom Sender geht nur BRUTTO ab
            limit = overdraft_limit(von)
            projected = round(alt_von - brutto, 1)  # NICHT minus kulturbeitrag
            if projected < -limit:
                conn.close()
                return render_template_string(
                "<h2>Fehler: Überziehungsgrenze überschritten</h2>"
                f"<p>Minusrahmen erreicht: erlaubt –{limit:.1f} Punkte.</p>"
                f"<p>Geplanter neuer Stand: {projected:.1f} Punkte.</p>"
                "<p>Buchung nicht möglich.</p>"
                "<p><a href='/start'>Zurück</a></p>"
            )

            c.execute("SELECT punkte FROM konten WHERE name = ?", (an,))
            alt_an = c.fetchone()[0]

            if kulturfonds_name:
                c.execute("SELECT punkte FROM konten WHERE name = ?", (kulturfonds_name,))
                alt_kultur = c.fetchone()[0]
                neu_kultur = alt_kultur + kulturbeitrag
                c.execute("UPDATE konten SET punkte = ?, letzte_aktivitaet = ? WHERE name = ?", (neu_kultur, jetzt, kulturfonds_name))
            else:
                alt_kultur = neu_kultur = 0

            c.execute("UPDATE konten SET punkte = punkte - ?, letzte_aktivitaet = ? WHERE name = ?", (brutto, jetzt, von))
            c.execute("UPDATE konten SET punkte = punkte + ?, letzte_aktivitaet = ? WHERE name = ?", (netto, jetzt, an))
            neu_von = alt_von - brutto
            neu_an = alt_an + netto

            c.execute('''INSERT INTO transaktionen 
                (von, an, betrag, kulturbeitrag, brutto, netto, beschreibung, datum,
                 stand_von_alt, stand_von_neu, stand_an_alt, stand_an_neu)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (von, an, betrag, kulturbeitrag, brutto, netto, beschreibung, jetzt,
                 alt_von, neu_von, alt_an, neu_an))

            if kulturfonds_name:
                c.execute("SELECT punkte FROM konten WHERE name = ?", (von,))
                kultur_alt_von = c.fetchone()[0]
                kultur_neu_von = kultur_alt_von
                c.execute('''INSERT INTO transaktionen
                    (von, an, betrag, kulturbeitrag, brutto, netto, beschreibung, datum,
                     stand_von_alt, stand_von_neu, stand_an_alt, stand_an_neu)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (von, kulturfonds_name, kulturbeitrag, 0, kulturbeitrag, kulturbeitrag,
                     "[Kulturbeitrag automatisch]", jetzt,
                     kultur_alt_von, kultur_neu_von, alt_kultur, neu_kultur))
            conn.commit()
            conn.close()
            return redirect('/start')

    # Anzeige (GET)
    conn = db_connection()
    c = conn.cursor()
    c.execute("SELECT name, besitzer FROM konten")
    alle_konten = c.fetchall()
    aktives_von = eigene_kontonamen[0] if eigene_kontonamen else None
    empfaenger_konten = [name for name, _ in alle_konten if name != aktives_von]

    # Besitzername für die Überschrift ermitteln (nur für Nicht-Admin)
    besitzername = None
    if session["user"] != "admin":
        # Map "Kontoname -> Besitzer"
        name2besitzer = {n: b for (n, b) in alle_konten}
        # Nimm den ersten nicht-leeren Besitzer aus den eigenen Konten
        for kname in eigene_kontonamen:
            b = name2besitzer.get(kname)
            if b and b.strip():
                besitzername = b.strip()
                besitzername = besitzername.title()
                break
            
    # Überschrift abhängig von der Rolle/Kontoart
    if session["user"] == "admin":
        ueberschrift = "Alle Konten"
    elif len(konten) == 1 and konten[0][1] == 'fonds':
        # Kulturfonds-Ansicht (ein einziges Konto vom Typ 'fonds')
        ueberschrift = "Kulturfonds"
    else:
        # Normaler Nutzer – Fallback auf Login, falls besitzername leer ist
        anzeigename = (besitzername.strip() if besitzername else session["user"]).title()
        ueberschrift = f"Konten von {anzeigename}"

    # (Für den Kulturfonds ist 'besitzer' evtl. leer/NULL -> bleibt None)

    # Aktuelle Kontostände für Admin
    admin_kontostand_uebersicht = []
    if session["user"] == "admin":
        c.execute("SELECT name, punkte FROM konten ORDER BY name ASC")
        admin_kontostand_uebersicht = c.fetchall()

    # Transaktionen je nach Rolle (immer 11 Spalten!)
    if session["user"] == "admin":
        daten = _select_transaktionen(c, limit=20)

    elif any(k[0] in alle_fonds_konten for k in konten):
        placeholder = ",".join("?" for _ in alle_fonds_konten)
        where_sql = f"(von IN ({placeholder}) OR an IN ({placeholder}))"
        params = tuple(alle_fonds_konten + alle_fonds_konten)
        daten = _select_transaktionen(c, where_sql, params, limit=20)

    else:
        where_sql = """
            (
                von IN (
                    SELECT k.name
                    FROM konten k
                    JOIN konten_benutzer kb ON kb.konto_id = k.id
                    WHERE kb.benutzer_login = ?
                )
                OR
                an IN (
                    SELECT k.name
                    FROM konten k
                    JOIN konten_benutzer kb ON kb.konto_id = k.id
                    WHERE kb.benutzer_login = ?
                )
            )
            AND beschreibung != '[Kulturbeitrag automatisch]'
        """
        params = (session["user"], session["user"])
        daten = _select_transaktionen(c, where_sql, params, limit=10)

    def zeichenbetrag(betrag, vorzeichen):
        betrag = round(betrag, 1)
        if vorzeichen == '+':
            return f'+{betrag}'
        elif vorzeichen == '-':
            return f'-{betrag}'
        else:
            return str(betrag)

    transaktionen = []
    for row in daten:
        von, an, brutto, kultur, netto, beschreibung, datum, v_alt, v_neu, a_alt, a_neu = row

        if session["user"] == "admin":
            brutto_str = zeichenbetrag(brutto, '-')
            netto_str = zeichenbetrag(netto, '+')
            transaktionen.append((von, an, brutto_str, kultur, netto_str, beschreibung, datum, v_alt, v_neu, a_alt, a_neu))

        elif len(konten) == 1 and konten[0][1] == 'fonds':
            richtung = "⬇" if von in eigene_kontonamen else "⬆"
            betrag = abs(brutto) if von in eigene_kontonamen else abs(netto)
            vorzeichen = '-' if von in eigene_kontonamen else '+'
            betrag_str = zeichenbetrag(betrag, vorzeichen)
            konto_alt = round(v_alt, 1) if von in eigene_kontonamen else round(a_alt, 1)
            konto_neu = round(v_neu, 1) if von in eigene_kontonamen else round(a_neu, 1)
            transaktionen.append((von, an, betrag_str, beschreibung, datum, konto_alt, konto_neu))

        else:
            eigener_alt = eigener_neu = None
            vorzeichen = None

            if von in eigene_kontonamen:
                eigener_alt = v_alt
                eigener_neu = v_neu
                brutto_str = zeichenbetrag(brutto, '-')
                netto_str = zeichenbetrag(netto, '')
            elif an in eigene_kontonamen:
                eigener_alt = a_alt
                eigener_neu = a_neu
                brutto_str = zeichenbetrag(brutto, '')
                netto_str = zeichenbetrag(netto, '+')
            else:
                brutto_str = str(brutto)
                netto_str = str(netto)

            transaktionen.append((von, an, brutto_str, kultur, netto_str, beschreibung, datum, eigener_alt, eigener_neu))

    conn.close()

    # Brotpreis laden (für Euro-Hilfe im Formular)
    brotpreis, _brotpreis_ts = get_brotpreis_eur_pro_punkt()

    return render_template_string('''

<!doctype html>
<html lang="de">
    <style>
    /* Hauptüberschrift ganz oben */
    .page-title {
        font-size: 1.9rem !important;   /* größte */
        font-weight: 700;
        margin-top: 1.25rem;
        margin-bottom: 1rem;
    }

    /* Abschnitte darunter (Punkte übertragen, Transaktionen) */
    .section-title,
    .subsection-title {
        font-size: 1.6rem !important;  /* zwei bis drei Stufen größer */
        font-weight: 600;
        margin-top: 3.3rem;             /* mehr Luft drüber */
        margin-bottom: 0.8rem;
    }
    </style>
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Wieshof</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
  </head>
  <body class="container mt-4">

<h2 class="page-title">{{ ueberschrift }}</h2>

{% if session["user"] == "admin" %}
    <!-- Admin-Kontenübersicht -->
    <table class="table table-striped table-bordered table-sm">
        <thead>
            <tr><th>Name</th><th>Typ</th><th>Punkte</th></tr>
        </thead>
        <tbody>
            {% for name, typ, punkte in konten %}
            <tr><td>{{name}}</td><td>{{typ}}</td><td>{{punkte}}</td></tr>
            {% endfor %}
        </tbody>
    </table>
{% else %}
    <!-- Eigene Kontenübersicht -->
    <table class="table table-striped table-bordered table-sm">
        <thead>
            <tr><th>Name</th><th>Typ</th><th>Punkte</th><th>Letzte Aktivität</th></tr>
        </thead>
        <tbody>
            {% for name, typ, punkte, letzte in konten %}
            <tr><td>{{name}}</td><td>{{typ}}</td><td>{{punkte}}</td><td>{{letzte}}</td></tr>
            {% endfor %}
        </tbody>
    </table>
{% endif %}
{% if konten|length > 0 and session["user"] != "admin" %}
  {% if konten[0][1] == 'fonds' %}
    <h3 class="section-title">Punkte an den Kulturverein übertragen</h3>
    <form method="post" class="mb-4">
      <!-- Von: Kulturfonds -->
      <input type="hidden" name="von" value="{{ konten[0][0] }}">

      <!-- An: fest "Kulturverein Wieshof" -->
      <div class="mb-3">
        <label class="form-label">An:</label>
        <input type="hidden" name="an" value="Kulturverein Wieshof">
        <p><strong>Empfänger:</strong> Kulturverein Wieshof</p>
      </div>

      <div class="mb-3">
        <label class="form-label">Betrag (in Punkten):</label>
        <input type="number" step="0.01" name="betrag" id="fondsBetragInput" class="form-control" required>
        <div id="fondsEuroHinweis" class="form-text text-muted"></div>
      </div>

      <div class="mb-3">
        <label class="form-label">Beschreibung:</label>
        <input type="text" name="beschreibung" class="form-control">
      </div>

      <button type="submit" class="btn btn-primary w-100"
              onclick="this.disabled=true; this.form.submit();">
        Senden
      </button>
    </form>
  {% else %}
    <h3 class="section-title">Punkte übertragen</h3>
    <form method="post" oninput="updateEmpfaenger()" class="mb-4">
      <div class="mb-3">
        <label class="form-label">Von:</label>
        <select name="von" id="vonKonto" class="form-select" onchange="updateEmpfaenger()">
          {% for name, _, _, _ in konten %}
          <option value="{{name}}">{{name}}</option>
          {% endfor %}
        </select>
      </div>
      <div class="mb-3">
        <label class="form-label">An:</label>
        {% if empfaenger_vorauswahl %}
            <input type="hidden" name="an" value="{{ empfaenger_vorauswahl }}">
            <p><strong>Empfänger:</strong> {{ empfaenger_vorauswahl }}</p>
        {% else %}
            <select name="an" id="anKonto" class="form-select">
                {% for name in empfaenger_konten %}
                <option value="{{ name }}">{{ name }}</option>
                {% endfor %}
            </select>
        {% endif %}


      </div>
      <div class="mb-3">
        <div class="mb-3">
            <label class="form-label">Betrag (in Punkten):</label>
            <input type="number" step="0.01" name="betrag" id="betragInput" class="form-control" required>
            <div id="euroHinweis" class="form-text text-muted"></div>
        </div>

      <div class="mb-3">
        <label class="form-label">Beschreibung:</label>
        <input type="text" name="beschreibung" class="form-control">
      </div>
      <button type="submit" class="btn btn-primary w-100" onclick="this.disabled=true; this.form.submit();">Senden</button>
    </form>
  {% endif %}
{% endif %}

<h3 class="subsection-title">Letzte Transaktionen</h3>
{% if session["user"] == "admin" %}
  <!-- Admin-Transaktionen -->
  <table class="table table-striped table-bordered table-sm">
    <thead>
      <tr><th>Von</th><th>An</th><th>Brutto</th><th>– Kultur</th><th>Netto</th><th>Zweck</th><th>Datum</th></tr>
    </thead>
    <tbody>
      {% for von, an, brutto, kultur, netto, beschreibung, datum, v_alt, v_neu, a_alt, a_neu in transaktionen %}
      <tr>
        <td>{{von}}</td><td>{{an}}</td><td>{{brutto}}</td><td>{{kultur}}</td><td>{{netto}}</td>
        <td>{{beschreibung}}</td><td>{{datum}}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
{% elif konten[0][1] == 'fonds' %}
  <!-- Kulturfonds-Transaktionen -->
  <table class="table table-striped table-bordered table-sm">
    <thead>
      <tr><th>Von</th><th>An</th><th>Betrag</th><th>Zweck</th><th>Datum</th><th>Kontostand vorher</th><th>Kontostand nachher</th></tr>
    </thead>
    <tbody>
      {% for von, an, betrag, beschreibung, datum, alt, neu in transaktionen %}
      <tr>
        <td>{{von}}</td><td>{{an}}</td><td>{{betrag}}</td><td>{{beschreibung}}</td><td>{{datum}}</td><td>{{alt}}</td><td>{{neu}}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
{% else %}
  <!-- Normale Nutzer-Transaktionen -->
  <table class="table table-striped table-bordered table-sm">
    <thead>
      <tr><th>Von</th><th>An</th><th>Brutto</th><th>– Kultur</th><th>Netto</th><th>Zweck</th><th>Datum</th><th>Kontostand vorher</th><th>Kontostand nachher</th></tr>
    </thead>
    <tbody>
      {% for von, an, brutto, kultur, netto, beschreibung, datum, alt, neu in transaktionen %}
      <tr>
        <td>{{von}}</td><td>{{an}}</td><td>{{brutto}}</td><td>{{kultur}}</td><td>{{netto}}</td>
        <td>{{beschreibung}}</td><td>{{datum}}</td><td>{{alt}}</td><td>{{neu}}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
{% endif %}
<p class="mt-4">
  <a href="/export" class="btn btn-outline-secondary w-100">📥 Alle Transaktionen als CSV herunterladen</a>
</p>

<script>
  function updateEmpfaenger() {
    const von = document.getElementById(\"vonKonto\").value;
    const anSelect = document.getElementById(\"anKonto\");
    for (const option of anSelect.options) {
      option.disabled = (option.value === von);
    }
  }
  window.onload = updateEmpfaenger;
</script>

<p class="mt-4 text-center">
  <a href="/logout" class="btn btn-link">Logout</a>
</p>
                                  
<script>
  // Brotpreis robust parsen (Fallback 8.38)
  const BP = (function() {
    try { return parseFloat({{ brotpreis|tojson }}); } catch(e) { return 8.38; }
  })();

  // kleine Helfer
  const $ = (id) => document.getElementById(id);
  const toNum = (val) => parseFloat(String(val || "").replace(",", ".").trim());

  function liveCalc(inputId, outputId, toText) {
    const inp = $(inputId), out = $(outputId);
    if (!inp || !out) return; // Feld existiert auf dieser Seite nicht -> ok
    const render = () => {
      const n = toNum(inp.value);
      if (!isNaN(n) && n > 0) { out.textContent = toText(n); }
      else { out.textContent = ""; }
    };
    inp.addEventListener("input", render);
    // sofort initial rechnen, falls schon ein Wert drin steht
    render();
  }

  // Startseite (Überweisung)
  liveCalc("betragInput",       "euroHinweis",       (p) => `≈ ${(p*BP).toFixed(2)} €`);
  // Startseite (Fonds)
  liveCalc("fondsBetragInput",  "fondsEuroHinweis",  (p) => `≈ ${(p*BP).toFixed(2)} €`);
  // Infoseite (Punkte-Rechner rechts)
  liveCalc("punkteInput",       "punkteZuEuro",      (p) => `≈ ${(p*BP).toFixed(2)} €`);
  liveCalc("euroInput",         "euroZuPunkte",      (e) => `≈ ${(e/BP).toFixed(2)} Punkte`);
</script>

</body>
</html>


''', konten=konten,
    empfaenger_konten=empfaenger_konten,
    empfaenger_vorauswahl=empfaenger_vorauswahl,
    transaktionen=transaktionen,
    admin_kontostand_uebersicht=admin_kontostand_uebersicht,
    besitzername=besitzername,
    ueberschrift=ueberschrift,
    brotpreis=brotpreis
)


@app.route('/export')
def export_csv():
    if 'user' not in session:
        return redirect('/')

    def format_zahl(x):
        return str(int(x)) if x == int(x) else str(round(x, 1)).replace('.', ',')

    conn = db_connection()
    c = conn.cursor()

    if session["user"] == "admin":
        c.execute("SELECT DISTINCT name FROM konten")
        eigene_konten = [row[0] for row in c.fetchall()]

        c.execute('''
            SELECT von, an, brutto, kulturbeitrag, netto, beschreibung, datum
            FROM transaktionen
            ORDER BY datum ASC
        ''')
        rows = c.fetchall()

        konten_ueberschrift = "Alle Konten"

    else:
        c.execute("""
            SELECT k.name
            FROM konten k
            JOIN konten_benutzer kb ON kb.konto_id = k.id
            WHERE kb.benutzer_login = ?
        """, (session["user"],))
        eigene_konten = [row[0] for row in c.fetchall()]

        if eigene_konten == ["Kulturfonds"]:
            query = '''
                SELECT von, an, brutto, kulturbeitrag, netto, beschreibung, datum
                FROM transaktionen
                WHERE von = ? OR an = ?
                ORDER BY datum ASC
            '''
            c.execute(query, (eigene_konten[0], eigene_konten[0]))
        else:
            # ... (dein bisheriger Normalfall bleibt)

            query = '''
            SELECT von, an, brutto, kulturbeitrag, netto, beschreibung, datum
            FROM transaktionen
            WHERE (
                von IN (
                    SELECT k.name
                    FROM konten k
                    JOIN konten_benutzer kb ON kb.konto_id = k.id
                    WHERE kb.benutzer_login = ?
                )
                OR
                an IN (
                    SELECT k.name
                    FROM konten k
                    JOIN konten_benutzer kb ON kb.konto_id = k.id
                    WHERE kb.benutzer_login = ?
                )
            )
            AND beschreibung != '[Kulturbeitrag automatisch]'
            ORDER BY datum ASC
            '''
            c.execute(query, (session["user"], session["user"]))


        rows = c.fetchall()

    c.execute("""
        SELECT k.name, k.typ
        FROM konten k
        JOIN konten_benutzer kb ON kb.konto_id = k.id
        WHERE kb.benutzer_login = ?
    """, (session["user"],))
    eigene_konten_info = c.fetchall()
    eigene_konten = [name for name, _ in eigene_konten_info]
    eigene_konto_typen = [typ for _, typ in eigene_konten_info]

    # Neu:
    is_fonds_only = (len(eigene_konten) == 1 and eigene_konto_typen[0] == 'fonds')
    fonds_name = eigene_konten[0] if is_fonds_only else None

    konto_stand = { konto: 0.0 for konto in eigene_konten }

    output_rows = []

    for row in rows:
        von, an, brutto, kultur, netto, beschreibung, datum = row

        symbol_abgang = "⬇"
        symbol_zugang = "⬆"
        symbol_neutral = "⇄"

    for row in rows:
        von, an, brutto, kultur, netto, beschreibung, datum = row

        symbol_abgang = "⬇"
        symbol_zugang = "⬆"
        symbol_neutral = "⇄"

        # === Kulturfonds Spezialfall ===
        if is_fonds_only:
            betrag = abs(netto)
            richtung = symbol_zugang if an == fonds_name else symbol_abgang

            konto_alt = format_zahl(konto_stand[fonds_name])
            if an == fonds_name:
                konto_stand[fonds_name] += betrag
            else:
                konto_stand[fonds_name] -= betrag
            konto_neu = format_zahl(konto_stand[fonds_name])

            output_rows.append([
                von, an, format_zahl(betrag), richtung, beschreibung, datum, konto_alt, konto_neu
            ])
        else:
            # === Normalfall (Nicht-Kulturfonds oder Admin)
            richtung_brutto = ""
            richtung_netto = ""
            interne_umbuchung = von in eigene_konten and an in eigene_konten
            if interne_umbuchung:
                richtung_brutto = symbol_neutral
                richtung_netto = symbol_neutral
                if session["user"] != "admin":
                    beschreibung = "Interne Überweisung"
            else:
                if von in eigene_konten:
                    richtung_brutto = symbol_abgang
                if an in eigene_konten:
                    richtung_netto = symbol_zugang

            if session["user"] == "admin":
                konto_alt = ""
                konto_neu = ""
            elif von in eigene_konten:
                konto_alt = format_zahl(konto_stand[von])
                konto_stand[von] -= abs(brutto)
                konto_neu = format_zahl(konto_stand[von])
            elif an in eigene_konten:
                konto_alt = format_zahl(konto_stand[an])
                konto_stand[an] += abs(netto)
                konto_neu = format_zahl(konto_stand[an])
            else:
                konto_alt = ""
                konto_neu = ""

            if session["user"] == "admin":
                # Admin: ohne Kontostand-Spalten
                output_rows.append([
                    von,
                    an,
                    format_zahl(abs(brutto)),
                    richtung_brutto,
                    format_zahl(abs(netto)),
                    richtung_netto,
                    beschreibung,
                    datum
                ])
            else:
                # Nutzer/Fonds: mit Kontostand alt/neu
                output_rows.append([
                    von,
                    an,
                    format_zahl(abs(brutto)),
                    richtung_brutto,
                    format_zahl(abs(netto)),
                    richtung_netto,
                    beschreibung,
                    datum,
                    konto_alt,
                    konto_neu
                ])

    # === CSV-Export erstellen ===
    si = io.StringIO()
    cw = csv.writer(si, quoting=csv.QUOTE_ALL)

    if session["user"] != "admin":
        for konto in eigene_konten:
            cw.writerow(["Kontostand", f"{konto}:", format_zahl(konto_stand[konto])])
        cw.writerow([])

    # Tabellenkopf
    if session["user"] == "admin":
        # Admin: ohne Kontostand-Spalten
        cw.writerow(["Von", "An", "Brutto", "", "Netto", "", "Zweck", "Datum"])
    else:
        # Nutzer/Kulturfonds wie bisher
        if is_fonds_only:
            cw.writerow(["Von", "An", "Betrag", "", "Zweck", "Datum", "Kontostand alt", "Kontostand neu"])
        else:
            cw.writerow(["Von", "An", "Brutto", "", "Netto", "", "Zweck", "Datum", "Kontostand alt", "Kontostand neu"])

    cw.writerows(output_rows)
    conn.close()

    return Response(si.getvalue(), mimetype='text/csv',
                    headers={"Content-Disposition": "attachment; filename=transaktionen.csv"})

@app.route('/export_monatsbericht')
def export_monatsbericht():
    if 'user' not in session:
        return redirect('/')

    conn = db_connection()
    c = conn.cursor()

    # Zeitraum: laufendes Jahr
    now = datetime.now()
    year_start = f"{now.year}-01-01 00:00:00"
    year_end   = f"{now.year}-12-31 23:59:59"

    # Welche Konten gehören dem Nutzer?
    if session["user"] == "admin":
        c.execute("SELECT name FROM konten")
        eigene_konten = [r[0] for r in c.fetchall()]
        scope_sql = "datum BETWEEN ? AND ?"
        scope_params = (year_start, year_end)
    else:
        c.execute("""
            SELECT k.name
            FROM konten k
            JOIN konten_benutzer kb ON kb.konto_id = k.id
            WHERE kb.benutzer_login = ?
        """, (session["user"],))
        eigene_konten = [r[0] for r in c.fetchall()]

        placeholders = ",".join("?" for _ in eigene_konten)
        # Kultur-Autobuchungen für Nicht-Admin ausklammern wie in /start
        scope_sql = f"""
            datum BETWEEN ? AND ? AND beschreibung != '[Kulturbeitrag automatisch]'
            AND (von IN ({placeholders}) OR an IN ({placeholders}))
        """
        scope_params = (year_start, year_end, *eigene_konten, *eigene_konten)

    # Monats-Aggregation initialisieren
    from collections import defaultdict
    agg = defaultdict(lambda: {"einnahmen": 0.0, "ausgaben": 0.0, "kultur": 0.0})

    # Relevante Transaktionen holen (immer die 11-Spalten-Variante, Fallback via Helper)
    rows = _select_transaktionen(c, scope_sql, scope_params, limit=10_000)

    # Helper: Monats-Key
    def month_key_from_ts(ts):
        try:
            return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m")
        except:
            # Falls Format mal anders ist
            return ts[:7]

    # Durchgehen und klassifizieren
    for (von, an, brutto, kulturbeitrag, netto, beschreibung, datum,
         v_alt, v_neu, a_alt, a_neu) in rows:

        mk = month_key_from_ts(datum)

        # Kulturbeiträge zählen wir separat (die "5 %")
        if kulturbeitrag and kulturbeitrag != 0:
            agg[mk]["kultur"] += float(kulturbeitrag)

        # Aus Sicht: Admin = neutral beide Seiten summieren
        # Nutzer = nur eigene Perspektive
        if session["user"] == "admin":
            # Brutto ist "Abgang" beim Sender, Netto ist "Zugang" beim Empfänger
            if brutto:
                agg[mk]["ausgaben"] += abs(float(brutto))
            if netto:
                agg[mk]["einnahmen"] += abs(float(netto))
        else:
            # Nur Bewegungen, die eigene Konten betreffen
            if von in eigene_konten and brutto:
                agg[mk]["ausgaben"] += abs(float(brutto))
            if an in eigene_konten and netto:
                agg[mk]["einnahmen"] += abs(float(netto))

    # CSV bauen
    import csv, io
    si = io.StringIO()
    cw = csv.writer(si, quoting=csv.QUOTE_ALL)
    header_title = "Alle Konten" if session["user"] == "admin" else ", ".join(eigene_konten)
    cw.writerow(["Konten", header_title])
    cw.writerow([])
    cw.writerow(["Monat", "Einnahmen (Netto)", "Ausgaben (Brutto)", "Kulturbeiträge", "Saldo (Einnahmen - Ausgaben)"])

    for mk in sorted(agg.keys()):
        e = round(agg[mk]["einnahmen"], 1)
        a = round(agg[mk]["ausgaben"], 1)
        k = round(agg[mk]["kultur"], 1)
        s = round(e - a, 1)
        cw.writerow([mk, e, a, k, s])

    conn.close()
    return Response(si.getvalue(), mimetype='text/csv',
                    headers={"Content-Disposition": "attachment; filename=monatsbericht.csv"})

@app.route("/system")
@app.route("/system")
def system():
    conn = db_connection()
    c = conn.cursor()
    c.execute("SELECT name, typ FROM konten WHERE typ IN ('betrieb', 'verein') ORDER BY name ASC")
    empfaenger = c.fetchall()
    conn.close()

    # Brotpreis für den Punkte-Rechner laden
    brotpreis, _ts = get_brotpreis_eur_pro_punkt()
    
    return render_template_string('''
    <!doctype html>
    <html lang="de">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Wieshof – Verrechnungssystem</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body { color:#333; }

            /* Obere Empfänger-Buttons */
            .btn-rostbraun {
                background-color: #f9ece4;
                color: #333;
                border: 1px solid #e7d5c9;
                width: 100%;
            }
            .btn-col { margin-bottom: 1rem; }

            /* Untere Info-Boxen */
            .cta-card{
              border: none;
              background: #f9f9f9;
              border-radius: 12px;
              box-shadow: 0 1px 2px rgba(0,0,0,.05);
              padding: 16px;
              min-height: 220px;          /* gleiche Höhe */
              display: flex;
              flex-direction: column;
            }
            .cta-card h4{ margin-bottom: .5rem; }
            .cta-card p{  margin-bottom: .75rem; }

            /* Buttons in den Boxen */
            .btn-cta{
              width: 100%;
              border-radius: 12px;
              padding: .75rem 1rem;
              font-weight: 600;
            }

            .btn-link-clean {
                background: none;
                border: none;
                color: #333;
                text-decoration: none;
                font-size: 1.05rem;
                padding: 0;
            }
            .btn-link-clean:hover {
                text-decoration: underline;
                color: #000;
            }
        </style>
    </head>
    <body class="container py-5">

    <h2 class="mb-4">Hier kannst du loslegen</h2>
    <hr class="mb-5">
    
    <!-- Zahlung starten -->
    <div class="mt-2 mb-5">
        <h4>Zahlung starten</h4>
        <p class="mb-3">Wähle einen Empfänger (Betrieb oder Verein):</p>
        <div class="row">
            {% for name, typ in empfaenger %}
              <div class="col-12 col-sm-6 col-md-4 btn-col">
                <a href="/start?an={{ name | urlencode }}" class="btn btn-rostbraun btn-lg">
                  {{ name }}
                </a>
              </div>
            {% endfor %}
        </div>
    </div>

    <hr class="mb-5">

    <!-- Drei Boxen unten -->
    <div class="row mt-5">
      <div class="col-md-4 mb-4">
        <div class="cta-card">
          <h4>Unterstütze die Gemeinschaft</h4>
          <p>Hier kannst du freiwillig Punkte an den Kulturfonds überweisen.</p>
          <div class="mt-auto">
            <a href="/fonds" class="btn btn-success btn-cta">Kulturfonds</a>
          </div>
        </div>
      </div>

      <div class="col-md-4 mb-4">
        <div class="cta-card">
          <h4>Meine Konten</h4>
          <p>Hier kannst du Überweisungen tätigen, deinen Kontostand einsehen und deine Transaktionen verwalten.</p>
          <div class="mt-auto">
            <a href="/start" class="btn btn-primary btn-cta">Zu meinen Konten</a>
          </div>
        </div>
      </div>

      <div class="col-md-4 mb-4">
        <div class="cta-card">
          <h4>Punkte-Rechner</h4>
          <p>Rechne Punkte in Euro – oder Euro in Punkte.</p>

          <div class="mb-2">
            <label class="form-label">Punkte → Euro</label>
            <input type="number" step="0.01" id="punkteInput" class="form-control">
            <div id="punkteZuEuro" class="form-text text-muted"></div>
          </div>

          <div class="mb-2">
            <label class="form-label">Euro → Punkte</label>
            <input type="number" step="0.01" id="euroInput" class="form-control">
            <div id="euroZuPunkte" class="form-text text-muted"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- Zurück-Button -->
    <hr class="mb-4">
    <div class="text-center">
      <a href="/" class="btn-link-clean">← Zurück zur Infoseite</a>
    </div>
    
    <script>
        const BP = (function(){
            try { return parseFloat({{ brotpreis|tojson }}); } catch(e) { return 8.38; }
        })();

        const $ = (id) => document.getElementById(id);
        const toNum = (v) => parseFloat(String(v || "").replace(",", ".").trim());

        function liveCalc(inputId, outputId, calc) {
            const inp = $(inputId), out = $(outputId);
            if (!inp || !out) return;
            const render = () => {
              const n = toNum(inp.value);
              out.textContent = (!isNaN(n) && n > 0) ? calc(n) : "";
            };
            inp.addEventListener("input", render);
            render();
        }

        liveCalc("punkteInput", "punkteZuEuro", (p) => `≈ ${(p * BP).toFixed(2)} €`);
        liveCalc("euroInput",   "euroZuPunkte", (e) => `≈ ${(e / BP).toFixed(2)} Punkte`);
    </script>

    </body>
    </html>
    ''', empfaenger=empfaenger, brotpreis=brotpreis)

@app.route("/brotpreis/refresh")
def brotpreis_refresh():
    """
    Holt manuell den €/kg-Preis. Ergebnis:
    - Wenn ein Preis gefunden wird -> Wert + Zeitstempel aktualisieren.
    - Wenn KEIN Preis gefunden wird -> nur den Zeitstempel aktualisieren (Wert bleibt).
    - Wenn es einen Fehler gibt (kein Internet etc.) -> ebenfalls nur Zeitstempel aktualisieren.
    """
    url = "https://xn--bcker-schren-shop-qqb87b.de/produkt/bio-vollwertbrot/bio-vollwert-dinkelbrot/"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        text = soup.get_text(" ", strip=True)

        # Robusteres Muster: erlaubt "/kg", "pro kg", "je kg", optional "1 kg"
        m = re.search(r"(\d+[.,]\d+)\s*€\s*(?:/|pro|je)\s*(?:1\s*)?kg", text, re.IGNORECASE)

        if m:
            raw = m.group(1).replace(".", "").replace(",", ".")  # "8,38" -> "8.38"
            eur_pro_kg = float(raw)
            set_brotpreis_eur_pro_punkt(eur_pro_kg, source_label="Bäckerei Schüren (Web)")
        else:
            # Kein Treffer -> Zeitstempel trotzdem erneuern (Wert unverändert)
            current, _ = get_brotpreis_eur_pro_punkt()
            set_brotpreis_eur_pro_punkt(current, source_label="Refresh (unverändert)")

    except Exception:
        # Fehler (z. B. kein Internet) -> Zeitstempel trotzdem erneuern
        current, _ = get_brotpreis_eur_pro_punkt()
        set_brotpreis_eur_pro_punkt(current, source_label="Refresh (Fehler, Wert unverändert)")

    # Zurück zur Systemseite
    return redirect(request.args.get("back") or request.referrer or url_for("system"))
    
@app.route("/brotpreis/admin", methods=["GET", "POST"])
def brotpreis_admin():
    # nur Admin
    if 'user' not in session or session['user'] != 'admin':
        return redirect(url_for('login'))

    # aktuellen Wert laden
    aktueller_preis, ts = get_brotpreis_eur_pro_punkt()

    # POST: manuell speichern
    if request.method == "POST":
        eingabe = request.form.get("eur_pro_kg", "").strip().replace(",", ".")
        try:
            wert = float(eingabe)
            set_brotpreis_eur_pro_punkt(wert, source_label="Manuell (Admin)")
            return redirect(url_for("system"))
        except ValueError:
            # ungültige Eingabe -> einfach Seite erneut rendern (optional: Hinweistext)
            pass

    # Formular anzeigen
    return render_template_string("""
    <!doctype html>
    <html lang="de">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Brotpreis (Admin)</title>
      <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body class="container py-4" style="max-width:720px;">
      <h2 class="mb-3">Brotpreis manuell setzen</h2>
      <p class="text-muted">
        Aktuell: <strong>{{ '%.2f' % aktueller_preis }} €</strong> pro kg (Stand: {{ ts }})
      </p>

      <form method="post" class="mt-3">
        <div class="mb-3">
          <label class="form-label">Neuer Preis in € pro kg</label>
          <input type="text" name="eur_pro_kg" class="form-control" placeholder="z. B. 8,40" required>
          <div class="form-text">Komma oder Punkt sind ok.</div>
        </div>
        <button class="btn btn-primary w-100" type="submit">Speichern</button>
      </form>

      <p class="mt-4 text-center">
        <a class="btn btn-link" href="{{ url_for('system') }}">← Zur Systemseite</a>
      </p>
    </body>
    </html>
    """, aktueller_preis=aktueller_preis, ts=ts)

@app.route("/")
def startseite():
    # Brotpreis + Zeitstempel für das Template bereitstellen
    brotpreis, brotpreis_ts = get_brotpreis_eur_pro_punkt()

    return render_template_string("""
    <!doctype html>
    <html lang="de">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Wieshof – Verrechnungssystem</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
        /* Grundfarbe etwas weicher */
        body { background:#fff; color:#3f3f3f; }

        .content{
            max-width:880px;
            margin:40px auto 52px auto;
            padding:0 24px;
        }

        /* Globale Überschriften (Abschnittstitel) */
        h1, h2 { font-weight:600; }
        h1{
            font-size:1.9rem;
            margin-top:3rem;
            margin-bottom:.6rem;
            color:#2f2f2f; /* kräftig, aber nicht tiefschwarz */
        }
        h2{
            font-size:1.28rem;
            margin:2rem 0 1rem 0;
            color:#2f2f2f; /* wie Haupttitel */
        }

        /* Fließtext */
        p{
            font-size:1.12rem;
            line-height:1.75;
            margin-bottom:.9rem;
            text-align:justify;
        }

        /* Brotpreis-Hinweis */
        .hint{
            background:#f8f9fa;
            border-left:4px solid #e2e6ea;
            padding:.55rem .8rem;
            color:#6c757d;
            font-size:.96rem;
            font-style:italic;
            border-radius:.25rem;
            margin:.35rem 0 1rem 0;
        }

        /* Links im Fließtext */
        .link-plain{
            background:none; border:none; padding:0;
            color:#0d47a1; text-decoration:none; font:inherit;
        }
        .link-plain:hover{ text-decoration:underline; color:#08306b; }

        /* Begrüßung (kleiner, dezenter) */
        .page-title{
            font-size:1.6rem;
            font-weight:600;
            margin-top:3rem;
            margin-bottom:.9rem;
            text-align:left;
            color:#2f2f2f; /* bewusst heller als Text/H2 */
        }

        /* Hauptfokus: "Unser Verrechnungssystem" */
        .page-subtitle{
            font-size:1.9rem;
            font-weight:700;
            text-align:left;          /* linksbündig */
            margin:.4rem 0 1.6rem 0;
            color:#0d47a1 !important;    /* gleiche Farbe wie Fließtext – wirkt harmonisch */
        }
        </style>
    </head>
    <body>
        <main class="content">

            <h1 class="page-title">Willkommen am Wieshof</h1>
            <div class="page-subtitle">Unser Verrechnungssystem</div>
            <p>
                Der Wieshof ist mehr als nur ein landwirtschaftlicher Betrieb – 
                er ist ein Ort, an dem persönliche Freiheit, gemeinsames Gestalten und gegenseitige Unterstützung zusammenfinden.
            </p>

            <p>
                Damit unsere Gemeinschaft lebendig bleibt, haben wir ein eigenes Verrechnungssystem geschaffen. 
                Es ermöglicht uns, unsere Zusammenarbeit eigenständig zu organisieren und Produkte, Dienstleistungen, Schenkungen und Leihgaben fair und transparent miteinander auszutauschen – unabhängig vom Euro.
            </p>
            </p>

            <h2>So funktioniert es</h2>
            <p>Die Verrechnungseinheit ist einfach: 1 Punkt entspricht dem Wert von 1&nbsp;kg Bio-Dinkelbrot.</p>

            <!-- Brotpreis-Hinweis als dezente Box -->
            <div class="hint">
              1&nbsp;Punkt ≈ {{ '%.2f' % brotpreis }}&nbsp;€
              <span class="text-muted"> (Stand: {{ brotpreis_ts[:16] }}, Quelle: Bäcker Schüren)</span>
              &nbsp;·&nbsp;
              <a href="/brotpreis/refresh?back=/" class="link-plain">Preis aktualisieren</a>
            </div>

            <p>
                Bei jeder Transaktion – ob Kauf, Verleih oder Schenkung – fließen 5&nbsp;% in unseren Kulturfonds.
                Diese werden dem Empfänger abgezogen und unterstützen so direkt Bildung, Kultur und gemeinschaftliche Projekte am Hof.
            </p>
            <p>
                Zum Beispiel bezahlt derjenige, der eine Dienstleistung in Anspruch nimmt, 100 Punkte.
                Derjenige, der die Dienstleistung erbringt, erhält 95 Punkte, und 5 Punkte gehen automatisch an den Kulturfonds.
            </p>
            <p>
                Alle Konten zusammengerechnet ergeben immer 0 Punkte
                <em>(anhand unseres Beispiels würde das so aussehen: –100, +95 und +5 = 0)</em>.
            </p>

            <h2>Warum das System lebendig bleibt</h2>
            <p>
                Für alle Konten, die im Plus stehen, wird am Monatsende 1&nbsp;% Parkgebühr abgezogen und dem Kulturfonds gutgeschrieben.
                So entsteht ein Anreiz, die Punkte im Umlauf zu halten – entweder durch Ausgeben, Verleihen oder Verschenken.
            </p>

            <h2>Punkte verleihen</h2>
            <p>
                Mitglieder mit vielen Punkten können diese an andere verleihen, die sie für Projekte oder Investitionen benötigen.
                Auch hier werden 5&nbsp;% an den Kulturfonds abgeführt, Zinsen fallen dabei nicht an.
            </p>

            <h2>Unsere Vision</h2>
            <p>
                Dieses System kann wachsen: Mehrere Höfe oder Regionen können sich zusammenschließen und gemeinsam eine
                nachhaltige Wirtschaftsregion aufbauen – am besten in Form einer Genossenschaft.
            </p>

            <hr class="my-4">
            <div class="text-center">
                <a href="/system" class="link-plain" style="font-size:1.25rem;">→ Zum Verrechnungssystem</a>
            </div>

        </main>
    </body>
    </html>
    """, brotpreis=brotpreis, brotpreis_ts=brotpreis_ts)

def get_config_value(key, default=None):
    conn = sqlite3.connect("verrechnung.db")
    c = conn.cursor()
    c.execute("SELECT value FROM systemstatus WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return float(row[0]) if row else default

def has_positive_balance(name):
    """Check if account is currently >= 0"""
    conn = sqlite3.connect("verrechnung.db")
    c = conn.cursor()
    c.execute("""
        SELECT ROUND(
            SUM(
                CASE
                    WHEN t.an = ? THEN t.betrag
                    WHEN t.von = ? THEN -t.betrag
                    ELSE 0
                END
            ), 1
        )
        FROM transaktionen t
    """, (name, name))
    saldo = c.fetchone()[0] or 0
    conn.close()
    return saldo >= 0

def ytd_income(name, year=None):
    """Sum of incoming transactions this year (excl. Parkgebühr/Kulturfonds)"""
    if year is None:
        from datetime import date
        year = date.today().year
    conn = sqlite3.connect("verrechnung.db")
    c = conn.cursor()
    c.execute("""
        SELECT ROUND(COALESCE(SUM(betrag),0),1)
        FROM transaktionen
        WHERE an = ?
          AND strftime('%Y', datum) = ?
          AND beschreibung NOT LIKE '[Parkgebühr%'
          AND beschreibung NOT LIKE '[Kulturfonds%'
    """, (name, str(year)))
    total = c.fetchone()[0] or 0
    conn.close()
    return total

def overdraft_limit(name):
    """Calculate current overdraft allowance for account"""
    start_allowance = get_config_value("overdraft_start_allowance", 20)
    percent = get_config_value("overdraft_income_percent", 10)

    # Einkommen im laufenden Jahr
    income = ytd_income(name)

    # Start-Regel: solange Einkommen = 0 (noch keine Einnahme)
    if income <= 0:
        return start_allowance

    # Ab hier gilt die 10%-Regel, sobald es mal Einnahmen gab
    return round(income * (percent/100.0), 1)


if __name__ == '__main__':
    erstelle_datenbank()
    app.run(host="0.0.0.0", port=5000, debug=True)
