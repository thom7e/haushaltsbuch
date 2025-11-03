# Haushaltsbuch

## Überblick

**Haushaltsbuch** ist eine selbst gehostete Applikation, um private Einnahmen und Ausgaben zu verwalten.  
Die Anwendung besteht aus einem Python-Backend (**FastAPI**) und einem modernen Frontend mit **Alpine.js** und **Chart.js**.  
Alle Daten werden lokal in einer JSON-Datei gespeichert – keine externe Datenbank erforderlich.

---

## 🧾 Funktionen

- **Benutzerverwaltung:** Registrierung & Login über JWT-Token, Unterstützung mehrerer Nutzer  
- **Ein- und Ausgabenverwaltung:**  
  - Posten als *Einnahme* oder *Ausgabe*  
  - Unterposten (Subitems) und fixe/variable Kennzeichnung  
  - Kategorien und Labels  
- **Kategorienverwaltung:** Kategorien anlegen, umbenennen oder löschen (mit automatischer Umsortierung)  
- **Responsive Oberfläche:** Anpassung an mobile & Desktop-Ansicht  
- **Statistiken & Diagramme:**  
  - Donut-Diagramm nach Kategorien  
  - Top-10-Liste der größten Ausgaben  
  - Vergleich Fix vs. Variabel  
- **Theme-Support:** Hell/Dunkel-Modus mit Pastellfarben  
- **Lokal & portabel:** Speicherung aller Daten in `db.json`

---

## ⚙️ Installation

### Voraussetzungen

- Python ≥ 3.10  
- Uvicorn (wird durch `requirements.txt` installiert)

### Schritte

```bash
git clone https://github.com/thom7e/haushaltsbuch.git
cd haushaltsbuch
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Optionale Umgebungsvariablen:

| Variable | Beschreibung | Standard |
|-----------|---------------|-----------|
| `AUTH_SECRET` | Geheimschlüssel für JWT-Signierung | `dev-secret-change-me` |
| `DB_PATH` | Pfad zur JSON-Datei | `db.json` |

### Server starten

```bash
uvicorn main:app --reload
```

Dann im Browser öffnen:

```
http://127.0.0.1:8000/static/index.html
```

Nach dem Login können neue Benutzer angelegt und der Standard-User entfernt werden.

---

## 🧠 API-Referenz

Alle API-Routen (außer Login/Registrierung) erfordern ein gültiges Bearer-Token.

| Methode | Pfad | Beschreibung |
|----------|------|--------------|
| **POST** | `/auth/register` | Benutzer registrieren (`username`, `password`) |
| **POST** | `/auth/login` | Login → JWT-Token |
| **GET** | `/me` | Aktuellen Benutzer abrufen |
| **GET** | `/api/lines` | Alle Posten (Einnahmen/Ausgaben) abrufen |
| **POST** | `/api/lines` | Neuen Posten anlegen |
| **PUT** | `/api/lines/{id}` | Bestehenden Posten aktualisieren |
| **DELETE** | `/api/lines/{id}` | Posten löschen |
| **POST** | `/api/lines/{id}/subitems` | Unterposten hinzufügen |
| **DELETE** | `/api/lines/{id}/subitems/{sub_id}` | Unterposten löschen |
| **GET** | `/api/categories` | Alle Kategorien abrufen |
| **DELETE** | `/api/categories/{name}` | Kategorie löschen (optional mit `target`) |
| **POST** | `/api/categories/rename` | Kategorie umbenennen |
| **GET** | `/api/summary` | Einnahmen, Ausgaben, Netto & Kategorien-Summen |
| **GET** | `/api/groups` | Gruppierung nach Typ und Kategorie |

---

## 💾 Datenmodell

Daten werden in einer JSON-Datei (`db.json`) gespeichert:

```json
{
  "users": [
    {
      "id": "uuid",
      "username": "string",
      "password_hash": "bcrypt",
      "created_at": 1730000000
    }
  ],
  "lines": [
    {
      "id": "uuid",
      "user_id": "uuid",
      "label": "string",
      "type": "income|expense",
      "category": "string",
      "base_amount": 100.0,
      "subitems": [
        {"id": "uuid", "label": "Miete", "amount": 500.0}
      ],
      "is_variable": false
    }
  ]
}
```

---

## 🔒 Sicherheit

- Setze `AUTH_SECRET` auf einen **starken, geheimen Wert** in der Produktion  
- Entferne oder ändere den Standardbenutzer  
- Verwende HTTPS für den produktiven Betrieb

---

## 🧩 Frontend

Das Frontend (in `static/`) nutzt:
- **Alpine.js** – Reaktive UI ohne Framework-Overhead  
- **Chart.js** – Diagramme (Donut, Balken)  
- **Pastellfarben-Theming** (hell/dunkel)  
- **Responsive Layouts** für Mobilgeräte  

---

## 💡 Beitrag leisten

Pull-Requests, Bug-Reports und Feature-Vorschläge sind willkommen.  
Erstelle bitte einen Fork, arbeite in einem Feature-Branch und öffne danach einen Pull-Request.

---

## 📜 Lizenz

Im Repository ist aktuell keine Lizenzdatei vorhanden.  
Empfohlen wird eine Open-Source-Lizenz wie **MIT** oder **GPL-3.0**, falls du den Code veröffentlichen möchtest.
