
from flask import Flask, render_template_string, request, redirect, session, Response, url_for
import sqlite3
from datetime import datetime
import csv
import io
import time

app = Flask(__name__)
app.secret_key = 'geheim'
DB_PATH = 'verrechnung.db'

def db_connection():
    return sqlite3.connect(DB_PATH)

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
    conn.commit()
    conn.close()

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
    <title>Login – Hof Sonnried</title>
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
    return redirect(url_for('startseite'))

@app.route('/start', methods=['GET', 'POST'])
def index():
    if 'user' not in session:
        return redirect(url_for('login', an=request.args.get('an')))

    empfaenger_vorauswahl = request.args.get("an")

    conn = db_connection()
    c = conn.cursor()

    if session["user"] == "admin":
        c.execute("SELECT name, typ, punkte FROM konten ORDER BY name ASC")
        konten = c.fetchall()
    else:
        c.execute("SELECT name, typ, punkte, letzte_aktivitaet FROM konten WHERE besitzer = ?", (session["user"],))
        konten = c.fetchall()

    eigene_kontonamen = [k[0] for k in konten]

    c.execute("SELECT name FROM konten WHERE typ = 'fonds'")
    alle_fonds_konten = [row[0] for row in c.fetchall()]
    kulturkonto_name = alle_fonds_konten[0] if alle_fonds_konten else None

    jetzt = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

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
            kulturbeitrag = round(betrag * 0.2, 1)
            netto = round(betrag - kulturbeitrag, 1)
            brutto = betrag

            c.execute("SELECT punkte FROM konten WHERE name = ?", (von,))
            alt_von = c.fetchone()[0]
            c.execute("SELECT punkte FROM konten WHERE name = ?", (an,))
            alt_an = c.fetchone()[0]

            if kulturkonto_name:
                c.execute("SELECT punkte FROM konten WHERE name = ?", (kulturkonto_name,))
                alt_kultur = c.fetchone()[0]
                neu_kultur = alt_kultur + kulturbeitrag
                c.execute("UPDATE konten SET punkte = ?, letzte_aktivitaet = ? WHERE name = ?", (neu_kultur, jetzt, kulturkonto_name))
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

            if kulturkonto_name:
                c.execute("SELECT punkte FROM konten WHERE name = ?", (von,))
                kultur_alt_von = c.fetchone()[0]
                kultur_neu_von = kultur_alt_von
                c.execute('''INSERT INTO transaktionen
                    (von, an, betrag, kulturbeitrag, brutto, netto, beschreibung, datum,
                     stand_von_alt, stand_von_neu, stand_an_alt, stand_an_neu)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (von, kulturkonto_name, kulturbeitrag, 0, kulturbeitrag, kulturbeitrag,
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

    # Aktuelle Kontostände für Admin
    admin_kontostand_uebersicht = []
    if session["user"] == "admin":
        c.execute("SELECT name, punkte FROM konten ORDER BY name ASC")
        admin_kontostand_uebersicht = c.fetchall()

    # Transaktionen je nach Rolle
    if session["user"] == "admin":
        c.execute('''SELECT von, an, brutto, kulturbeitrag, netto, beschreibung, datum,
                     stand_von_alt, stand_von_neu, stand_an_alt, stand_an_neu
                     FROM transaktionen
                     ORDER BY id DESC LIMIT 20''')
    elif any(k[0] in alle_fonds_konten for k in konten):
        placeholder = ",".join("?" for _ in alle_fonds_konten)
        query = f'''SELECT von, an, brutto, kulturbeitrag, netto, beschreibung, datum,
                           stand_von_alt, stand_von_neu, stand_an_alt, stand_an_neu
                    FROM transaktionen
                    WHERE von IN ({placeholder}) OR an IN ({placeholder})
                    ORDER BY id DESC LIMIT 20'''
        c.execute(query, alle_fonds_konten + alle_fonds_konten)
    else:
        c.execute('''SELECT von, an, brutto, kulturbeitrag, netto, beschreibung, datum,
             stand_von_alt, stand_von_neu, stand_an_alt, stand_an_neu
             FROM transaktionen
             WHERE (von IN (SELECT name FROM konten WHERE besitzer = ?)
                OR an IN (SELECT name FROM konten WHERE besitzer = ?))
                AND (? = 'Kulturkonto' OR beschreibung != '[Kulturbeitrag automatisch]')
             ORDER BY id DESC LIMIT 10''', (session["user"], session["user"], session["user"]))

    daten = c.fetchall()

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
            transaktionen.append((von, an, brutto_str, kultur, netto_str, beschreibung, datum))

        elif eigene_kontonamen == ["Kulturkonto"]:
            richtung = "⬇" if von in eigene_kontonamen else "⬆"
            betrag = abs(brutto) if von in eigene_kontonamen else abs(netto)
            vorzeichen = '-' if von in eigene_kontonamen else '+'
            betrag_str = zeichenbetrag(betrag, vorzeichen)
            konto_alt = v_alt if von in eigene_kontonamen else a_alt
            konto_neu = v_neu if von in eigene_kontonamen else a_neu
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

    return render_template_string('''

<!doctype html>
<html lang="de">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Hof Sonnried</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
  </head>
  <body class="container mt-4">

<h2 class="mb-4">Kontenübersicht</h2>

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
    <h3 class="mt-4">Punkte abziehen (Fonds-Verwendung)</h3>
    <form method="post" class="mb-4">
      <input type="hidden" name="ausgabe_von" value="{{konten[0][0]}}">
      <div class="mb-3">
        <label class="form-label">Betrag:</label>
        <input type="number" step="0.01" name="ausgabe_betrag" class="form-control" required>
      </div>
      <div class="mb-3">
        <label class="form-label">Zweck:</label>
        <input type="text" name="ausgabe_beschreibung" class="form-control" required>
      </div>
      <button type="submit" class="btn btn-danger w-100">Abziehen</button>
    </form>
  {% else %}
    <h3 class="mt-4">Punkte übertragen</h3>
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
        <label class="form-label">Betrag:</label>
        <input type="number" step="0.01" name="betrag" class="form-control" required>
      </div>
      <div class="mb-3">
        <label class="form-label">Beschreibung:</label>
        <input type="text" name="beschreibung" class="form-control">
      </div>
      <button type="submit" class="btn btn-primary w-100" onclick="this.disabled=true; this.form.submit();">Senden</button>
    </form>
  {% endif %}
{% endif %}

<h3 class="mt-5">Letzte Transaktionen</h3>
{% if session["user"] == "admin" %}
  <!-- Admin-Transaktionen -->
  <table class="table table-striped table-bordered table-sm">
    <thead>
      <tr><th>Von</th><th>An</th><th>Brutto</th><th>– Kultur</th><th>Netto</th><th>Zweck</th><th>Datum</th></tr>
    </thead>
    <tbody>
      {% for von, an, brutto, kultur, netto, beschreibung, datum in transaktionen %}
      <tr>
        <td>{{von}}</td><td>{{an}}</td><td>{{brutto}}</td><td>{{kultur}}</td><td>{{netto}}</td>
        <td>{{beschreibung}}</td><td>{{datum}}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
{% elif konten[0][1] == 'fonds' %}
  <!-- Kulturkonto-Transaktionen -->
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

</body>
</html>


''', konten=konten, empfaenger_konten=empfaenger_konten,
     empfaenger_vorauswahl=empfaenger_vorauswahl,
     transaktionen=transaktionen, admin_kontostand_uebersicht=admin_kontostand_uebersicht)


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
        c.execute("SELECT name FROM konten WHERE besitzer = ?", (session["user"],))
        eigene_konten = [row[0] for row in c.fetchall()]

        if eigene_konten == ["Kulturkonto"]:
            query = '''
                SELECT von, an, brutto, kulturbeitrag, netto, beschreibung, datum
                FROM transaktionen
                WHERE von = ? OR an = ?
                ORDER BY datum ASC
            '''
            c.execute(query, ("Kulturkonto", "Kulturkonto"))
        else:
            query = '''
            SELECT von, an, brutto, kulturbeitrag, netto, beschreibung, datum
            FROM transaktionen
            WHERE (von IN (SELECT name FROM konten WHERE besitzer = ?)
                OR an IN (SELECT name FROM konten WHERE besitzer = ?))
              AND beschreibung != '[Kulturbeitrag automatisch]'
            ORDER BY datum ASC
            '''
            c.execute(query, (session["user"], session["user"]))

        rows = c.fetchall()

    c.execute("SELECT name, typ FROM konten WHERE besitzer = ?", (session["user"],))
    eigene_konten_info = c.fetchall()
    eigene_konten = [name for name, _ in eigene_konten_info]
    eigene_konto_typen = [typ for _, typ in eigene_konten_info]

    konto_stand = {
        konto: (0.0 if typ == 'fonds' else 1000.0)
        for konto, typ in zip(eigene_konten, eigene_konto_typen)
    }

    output_rows = []

    for row in rows:
        von, an, brutto, kultur, netto, beschreibung, datum = row

        symbol_abgang = "⬇"
        symbol_zugang = "⬆"
        symbol_neutral = "⇄"

        # === Kulturkonto Spezialfall ===
        if eigene_konten == ["Kulturkonto"]:
            betrag = abs(netto)
            richtung = symbol_zugang if an == "Kulturkonto" else symbol_abgang

            konto_alt = format_zahl(konto_stand["Kulturkonto"])
            if an == "Kulturkonto":
                konto_stand["Kulturkonto"] += betrag
            else:
                konto_stand["Kulturkonto"] -= betrag
            konto_neu = format_zahl(konto_stand["Kulturkonto"])

            output_rows.append([
                von,
                an,
                format_zahl(betrag),
                richtung,
                beschreibung,
                datum,
                konto_alt,
                konto_neu
            ])
        else:
            # === Normalfall (Nicht-Kulturkonto oder Admin)
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

    # Tabellenkopf abhängig vom Kulturkonto
    if eigene_konten == ["Kulturkonto"]:
        cw.writerow(["Von", "An", "Betrag", "", "Zweck", "Datum", "Kontostand alt", "Kontostand neu"])
    else:
        cw.writerow(["Von", "An", "Brutto", "", "Netto", "", "Zweck", "Datum", "Kontostand alt", "Kontostand neu"])

    cw.writerows(output_rows)
    conn.close()

    return Response(si.getvalue(), mimetype='text/csv',
                    headers={"Content-Disposition": "attachment; filename=transaktionen.csv"})

@app.route("/")
def startseite():
    return render_template_string('''
    <!doctype html>
    <html lang="de">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Hof Sonnried – Verrechnungssystem</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            h4 {
                margin-top: 40px;
                margin-bottom: 10px;
                font-weight: bold;
            }
            .btn-block {
                min-width: 240px;
                margin: 0.5rem;
                white-space: nowrap;
            }
            .button-row {
                display: flex;
                flex-wrap: nowrap;
                overflow-x: auto;
                padding-bottom: 1rem;
            }
            .btn-rostbraun {
                color: #b35c1e;
                border: 2px solid #b35c1e;
                background-color: transparent;
            }
            .btn-rostbraun:hover {
                background-color: #b35c1e;
                color: white;
            }
        </style>
    </head>
    <body class="container py-5">

    <h2>Willkommen auf Hof Sonnried</h2>

        <!-- Zahlung starten -->
        <div class="mb-5">
            <h4>Zahlung starten</h4>
            <p class="mb-3">Wähle den Betrieb, dem du Punkte überweisen möchtest:</p>
            <div class="button-row">
                <a href="/start?an=Anna%20(Gemüsehof)" class="btn btn-rostbraun btn-lg btn-block">Anna – Gemüsehof</a>
                <a href="/start?an=Ben%20(Zimmerei)" class="btn btn-rostbraun btn-lg btn-block">Ben – Zimmerei</a>
                <a href="/start?an=Clara%20(Dorfladen%20Küche)" class="btn btn-rostbraun btn-lg btn-block">Clara – Dorfladen Küche</a>
                <a href="/start?an=David%20(Büroservice)" class="btn btn-rostbraun btn-lg btn-block">David – Büroservice</a>
                <a href="/start?an=Emma%20(Werkstatt)" class="btn btn-rostbraun btn-lg btn-block">Emma – Werkstatt</a>
            </div>
        </div>

        <hr>

        <!-- Kulturfonds -->
        <div class="mb-5">
            <h4>Unterstütze die Gemeinschaft</h4>
            <p>Hier kannst du freiwillig Punkte an den Kulturfonds überweisen.</p>
            <a href="/start?an=Kulturkonto" class="btn btn-outline-success btn-lg btn-block">Zum Kulturfonds</a>
        </div>

        <hr>

        <!-- Eigene Konten -->
        <div class="mb-5">
            <h4>Meine Konten</h4>
            <p>Hier kannst du Überweisungen tätigen, deinen Kontostand einsehen und deine Transaktionen verwalten.</p>
            <a href="/login" class="btn btn-outline-primary btn-lg btn-block">Zu meinen Konten</a>
        </div>

    </body>
    </html>
    ''')



if __name__ == '__main__':
    erstelle_datenbank()
    app.run(host="0.0.0.0", port=5000, debug=True)
