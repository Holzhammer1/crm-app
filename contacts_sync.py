"""
iPhone-/iCloud-Kontakte -> Firestore Sync fuer das kawea CRM.

Liest die Kontakte (Adressbuch) per CardDAV von einem oder mehreren
iCloud-Konten (dieselben Zugangsdaten wie beim Kalender-Sync) und schreibt
sie in die Firestore-Collection crmPhoneContacts. Diese Kontakte werden
bewusst GETRENNT von crmContacts (den Bexio-Kontakten) gehalten, damit
nichts versehentlich überschrieben wird - im CRM koennen sie dann manuell
mit einem bestehenden Kunden verknuepft werden (siehe Tab "iPhone-Kontakte").

Benoetigte Umgebungsvariablen (als GitHub Secrets gesetzt):
  ICLOUD_ACCOUNTS                Gleiches Secret wie beim Kalender-Sync:
                                  [{"email": "...", "password": "app-spezifisches Passwort"}]
  FIREBASE_SERVICE_ACCOUNT_JSON  Wie bei den anderen Sync-Scripts.

Automatische Verknuepfung: Falls der Name eines iPhone-Kontakts (oder die
Firma im vCard-Feld ORG) in Name/Firma eines bestehenden crmContacts-
Eintrags vorkommt, wird automatisch verknuepft (wie beim Kalender). Alle
anderen bleiben "nicht verknuepft" und koennen im CRM manuell zugeordnet
werden.
"""

import os
import sys
import json

import caldav
import vobject
import firebase_admin
from firebase_admin import credentials, firestore

ICLOUD_URL = "https://contacts.icloud.com/"


def init_firestore():
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if not raw:
        print("FEHLER: FIREBASE_SERVICE_ACCOUNT_JSON ist nicht gesetzt.", file=sys.stderr)
        sys.exit(1)
    cred = credentials.Certificate(json.loads(raw))
    try:
        firebase_admin.initialize_app(cred)
    except ValueError:
        pass
    return firestore.client()


def load_accounts():
    raw = os.environ.get("ICLOUD_ACCOUNTS")
    if not raw:
        print("FEHLER: ICLOUD_ACCOUNTS ist nicht gesetzt.", file=sys.stderr)
        sys.exit(1)
    return json.loads(raw)


def vcard_field(vcard, name, default=""):
    try:
        return str(getattr(vcard, name).value)
    except Exception:
        return default


def fetch_contacts_for_account(email, password):
    print(f"Verbinde mit iCloud-Adressbuch fuer {email} ...", flush=True)
    client = caldav.DAVClient(url=ICLOUD_URL, username=email, password=password)
    principal = client.principal()
    addressbooks = principal.addressbooks()
    print(f"  {len(addressbooks)} Adressbuch/-bücher gefunden.", flush=True)

    contacts = []
    for ab in addressbooks:
        try:
            vcards = ab.get_vcards() if hasattr(ab, "get_vcards") else ab.objects()
        except Exception as e:
            print(f"  WARNUNG: Adressbuch '{ab.name}' konnte nicht gelesen werden: {e}", file=sys.stderr, flush=True)
            continue
        for card in vcards:
            try:
                data = card.data if hasattr(card, "data") else card.vobject_instance.serialize()
                vcard = vobject.readOne(data)
                uid = vcard_field(vcard, "uid") or str(getattr(card, "id", ""))
                name = vcard_field(vcard, "fn")
                org = vcard_field(vcard, "org")
                email_addr = ""
                if hasattr(vcard, "email_list") and vcard.email_list:
                    email_addr = str(vcard.email_list[0].value)
                telefon = ""
                if hasattr(vcard, "tel_list") and vcard.tel_list:
                    telefon = str(vcard.tel_list[0].value)
                if not name:
                    continue
                contacts.append({
                    "sourceId": f"{email}:{uid}",
                    "quelle": email,
                    "name": name,
                    "firma": org,
                    "email": email_addr,
                    "telefon": telefon,
                })
            except Exception as e:
                print(f"  WARNUNG: vCard konnte nicht gelesen werden: {e}", file=sys.stderr, flush=True)
    print(f"  {len(contacts)} Kontakte gefunden.", flush=True)
    return contacts


def load_crm_contacts_for_matching(db):
    matches = []
    for doc in db.collection("crmContacts").stream():
        data = doc.to_dict()
        for label in [data.get("firma"), data.get("name")]:
            if label and len(label.strip()) >= 4:
                matches.append((label.strip().lower(), doc.id))
    matches.sort(key=lambda x: -len(x[0]))
    return matches


def match_contact(name, firma, crm_contacts):
    text = f"{name} {firma}".lower()
    for label, contact_id in crm_contacts:
        if label in text:
            return contact_id
    return None


def sync_phone_contacts(db, all_contacts, crm_contacts):
    coll = db.collection("crmPhoneContacts")
    existing_ids = {doc.id for doc in coll.stream()}
    batch = db.batch()
    batch_count = 0
    matched_count = 0

    def commit_batch():
        nonlocal batch, batch_count
        if batch_count > 0:
            batch.commit()
            print(f"  ... {batch_count} Kontakte geschrieben", flush=True)
        batch = db.batch()
        batch_count = 0

    for c in all_contacts:
        doc_id = "iphone-" + "".join(ch for ch in c["sourceId"] if ch.isalnum())
        ref = coll.document(doc_id)
        if doc_id in existing_ids:
            # Bestehenden Eintrag NICHT contactId-mässig anfassen -> manuelle
            # Zuordnung im CRM bleibt erhalten, auch wenn sich Name/Telefon aendern.
            batch.set(ref, dict(c), merge=True)
        else:
            contact_id = match_contact(c["name"], c["firma"], crm_contacts)
            if contact_id:
                matched_count += 1
            c["contactId"] = contact_id
            batch.set(ref, c)
        batch_count += 1
        if batch_count >= 400:
            commit_batch()
    commit_batch()
    print(f"iPhone-Kontakte synchronisiert: {len(all_contacts)} (davon {matched_count} automatisch zugeordnet)", flush=True)


def main():
    accounts = load_accounts()
    db = init_firestore()
    crm_contacts = load_crm_contacts_for_matching(db)

    all_contacts = []
    for acc in accounts:
        email = acc.get("email")
        password = acc.get("password")
        if not email or not password:
            continue
        try:
            all_contacts.extend(fetch_contacts_for_account(email, password))
        except Exception as e:
            print(f"FEHLER bei Konto {email}: {e}", file=sys.stderr, flush=True)

    sync_phone_contacts(db, all_contacts, crm_contacts)
    print("Kontakte-Sync abgeschlossen.", flush=True)


if __name__ == "__main__":
    main()
