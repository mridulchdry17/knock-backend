"""One-shot fix: split 61 blob-name contacts + delete 3 non-contacts."""
from __future__ import annotations

from sqlalchemy import text

from app.db.session import SessionLocal

TITLE_PREFIXES = {"dr", "mr", "ms", "mrs", "prof", "late", "shri"}


def first_name(full_name: str) -> str:
    for part in full_name.strip().split():
        c = part.lower().rstrip(".")
        if c not in TITLE_PREFIXES and len(c) > 1:
            return c
    return full_name.strip().split()[0].lower().rstrip(".")


DELETES = [
    "Late Shri TV Sundram Iyengar",
    "Late. Shri Om Prakash Gupta",
    "Collaboration between Tata Sons and Volkart Brothers",
]

# (exact_blob_name, [person1, person2, ...])
# Use trailing % for blobs with invisible chars
SPLITS: list[tuple[str, list[str]]] = [
    # ── 5-word ──
    ("Abhishek Singh Chetan Kumar Garg",                ["Abhishek Singh",        "Chetan Kumar Garg"]),
    ("Adnan Asar Dr. Ahmed Zaafran",                    ["Adnan Asar",            "Dr. Ahmed Zaafran"]),
    ("Afsal Salu Fayaz Bin Abdu",                       ["Afsal Salu",            "Fayaz Bin Abdu"]),
    ("Balaji Ganesan Don Bosco Durai",                  ["Balaji Ganesan",        "Don Bosco Durai"]),
    ("Ekin Dogus Cubuk Liam Fedus",                     ["Ekin Dogus Cubuk",      "Liam Fedus"]),
    ("Gabriel Le Roux Paul Anthony",                    ["Gabriel Le Roux",       "Paul Anthony"]),
    ("Humberto Ayres Pereira Torben Schulz",            ["Humberto Ayres Pereira","Torben Schulz"]),
    ("Karl Bach Archy de Berker",                       ["Karl Bach",             "Archy de Berker"]),
    ("Koen Bok Jorn Van Dijk",                          ["Koen Bok",              "Jorn Van Dijk"]),
    ("Kumar Sudarsan Pratap T P",                       ["Kumar Sudarsan",        "Pratap T P"]),
    ("Maahin Puri Nitesh Kumar Niranjan",               ["Maahin Puri",           "Nitesh Kumar Niranjan"]),
    ("Nish Chasmawala Amit S Sharma",                   ["Nish Chasmawala",       "Amit S Sharma"]),
    ("Ramadhan Satrio Nugroho Nyoman Anjani",           ["Ramadhan Satrio Nugroho","Nyoman Anjani"]),
    ("Rory San Miguel Francis Vierboom",                ["Rory San Miguel",       "Francis Vierboom"]),
    ("Sandesh Mysore Anand Rakshitha Rao",              ["Sandesh Mysore Anand",  "Rakshitha Rao"]),
    # ── 7-word ──
    ("Alex Svanevik Lars Bakke Krogvig Evgeny Medvedev",["Alex Svanevik",         "Lars Bakke Krogvig",    "Evgeny Medvedev"]),
    ("Aniket Sunil Shah Saran Sureshkumar Ujjwal Sukheja",["Aniket Sunil Shah",  "Saran Sureshkumar",     "Ujjwal Sukheja"]),
    ("Anusha Ramakrishnan Anisha D Aibara Aditya Mehta",["Anusha Ramakrishnan",  "Anisha D Aibara",       "Aditya Mehta"]),
    ("Avijit Biswas Partha Pratim Ghosh Girish Koppar", ["Avijit Biswas",         "Partha Pratim Ghosh",   "Girish Koppar"]),
    ("Deobrat Singh Kevin William David Nachiketas Ramanujam",["Deobrat Singh",   "Kevin William David",   "Nachiketas Ramanujam"]),
    ("Dushyant Mishra Jot Sarup Singh Abhinay Vyas",    ["Dushyant Mishra",       "Jot Sarup Singh",       "Abhinay Vyas"]),
    ("Firmin Zocchetto Ghislain De Fontenay Florian Fournier",["Firmin Zocchetto","Ghislain De Fontenay",  "Florian Fournier"]),
    ("Jason Du Preez Gerard Buggy John Taysom",         ["Jason Du Preez",        "Gerard Buggy",          "John Taysom"]),
    ("Oskar Hjertonsson Daniel Undurraga Juan Pablo Cuevas",["Oskar Hjertonsson", "Daniel Undurraga",      "Juan Pablo Cuevas"]),
    ("Pavel Avgustinov Oege De Moor Julian Tibble",     ["Pavel Avgustinov",      "Oege De Moor",          "Julian Tibble"]),
    ("Ritesh Singh Chandel Sonu Kumar Rushabh Kothari", ["Ritesh Singh Chandel",  "Sonu Kumar",            "Rushabh Kothari"]),
    ("Shashank P S Carlos Escutia Madhusudan Kagwad",   ["Shashank P S",          "Carlos Escutia",        "Madhusudan Kagwad"]),
    ("Andy Pavlo Dana Van Aken Bohan Zhang",            ["Andy Pavlo",            "Dana Van Aken",         "Bohan Zhang"]),
    # ── 9-word ──
    ("Arpit Jain Joy Deep Nath Mayank Jain Umang Jain", ["Arpit Jain",            "Joy Deep Nath",         "Mayank Jain",          "Umang Jain"]),
    ("Jean-Baptiste Aviat Pierre Betouin Arnaud Breton Vladimir de Turckheim",
                                                        ["Jean-Baptiste Aviat",   "Pierre Betouin",        "Arnaud Breton",        "Vladimir de Turckheim"]),
    ("Johannes Schildt Josefin Landgård Fredrik Jung Abbou Joachim Hedenius",
                                                        ["Johannes Schildt",      "Josefin Landgård",      "Fredrik Jung Abbou",   "Joachim Hedenius"]),
    ("Peter Reinhardt Ilya Volodarsky Calvin French-Owen Ian Storm Taylor",
                                                        ["Peter Reinhardt",       "Ilya Volodarsky",       "Calvin French-Owen",   "Ian Storm Taylor"]),
    ("Rodolfo Corcuera Meier Juan Jose Fernandez Gallardo Daniel Tamayo",
                                                        ["Rodolfo Corcuera Meier","Juan Jose Fernandez Gallardo","Daniel Tamayo"]),
    ("Luis Sanz (CEO) Javier de la Torre Sergio Álvarez Leiva",
                                                        ["Luis Sanz",             "Javier de la Torre",    "Sergio Álvarez Leiva"]),
    # ── 10-word ──
    ("Arvind M Avinash B R Gururaj S Rao Santhosh Narasipura",
                                                        ["Arvind M",              "Avinash B R",           "Gururaj S Rao",        "Santhosh Narasipura"]),
    ("Benjamin Blackmore Marcel Birkner Michele Mancioppi Miel Donkers Mirko Novakovic",
                                                        ["Benjamin Blackmore",    "Marcel Birkner",        "Michele Mancioppi",    "Miel Donkers",         "Mirko Novakovic"]),
    ("Charles Kantor Karl Tuyls Laurent Sifre Daan Wierstra Julien Perolat",
                                                        ["Charles Kantor",        "Karl Tuyls",            "Laurent Sifre",        "Daan Wierstra",        "Julien Perolat"]),
    ("Godard Abel Mark Myers Matt Gorniak Mike Wheeler Tim Handorf",
                                                        ["Godard Abel",           "Mark Myers",            "Matt Gorniak",         "Mike Wheeler",         "Tim Handorf"]),
    ("Marcella Moniaga Sherlyn G Vincent Tjendra Wandi Budianto Jessica Jap",
                                                        ["Marcella Moniaga",      "Sherlyn G",             "Vincent Tjendra",      "Wandi Budianto",       "Jessica Jap"]),
    ("Matthew Darrow Dominique Darrow John Bruce Claire Bruce Joseph Miller",
                                                        ["Matthew Darrow",        "Dominique Darrow",      "John Bruce",           "Claire Bruce",         "Joseph Miller"]),
    ("Rick Gibbs Mark Bonfigli Mike Lane Ryan Dunn James LaScolea",
                                                        ["Rick Gibbs",            "Mark Bonfigli",         "Mike Lane",            "Ryan Dunn",            "James LaScolea"]),
    ("Sudip Ghose Uday Sodhi Arnob Mondal Dheeraj Goyal Nidhi Rajora",
                                                        ["Sudip Ghose",           "Uday Sodhi",            "Arnob Mondal",         "Dheeraj Goyal",        "Nidhi Rajora"]),
    ("Peter Rippon (CEO) Kirk Wylie Max Jeanniard Jonathan Senior Cris Conde",
                                                        ["Peter Rippon",          "Kirk Wylie",            "Max Jeanniard",        "Jonathan Senior",      "Cris Conde"]),
    # ── 11-word ──
    ("Bala Selvarajan Lee Hagelshaw Rajagopalan Sundararaghavan Srigiri Mahadevan Zhio Xiao Yan",
                                                        ["Bala Selvarajan",       "Lee Hagelshaw",         "Rajagopalan Sundararaghavan","Srigiri Mahadevan","Zhio Xiao Yan"]),
    ("Josh Martin Karlo Delos Reyes Dan Shores Scott Goodrich Randy Erb",
                                                        ["Josh Martin",           "Karlo Delos Reyes",     "Dan Shores",           "Scott Goodrich",       "Randy Erb"]),
    ("Prabhu Ramachandran Krishnamoorthi Rangasamy Yogendra Babu Rajavel Subramanian Suresh Babu Subramanian",
                                                        ["Prabhu Ramachandran",   "Krishnamoorthi Rangasamy","Yogendra Babu",      "Rajavel Subramanian",  "Suresh Babu Subramanian"]),
    ("Rohit Pandey Apurv Anand Tathagato Dastidar Rohit Kumar Pandey Apurv Anand",
                                                        ["Rohit Pandey",          "Apurv Anand",           "Tathagato Dastidar"]),  # deduped
    ("Thirukumaran Nagarajan Ashutosh Vikram Kartheeswaran K K Sharath Loganathan Vasu Devan",
                                                        ["Thirukumaran Nagarajan","Ashutosh Vikram",        "Kartheeswaran K K",    "Sharath Loganathan",   "Vasu Devan"]),
    ("Toby Gabriner (CEO) Aaron Bell Adam Berke Valentino Volonghi Peter Krivkovich",
                                                        ["Toby Gabriner",         "Aaron Bell",            "Adam Berke",           "Valentino Volonghi",   "Peter Krivkovich"]),
    ("Trung Nguyen Aleksander Larsen Jeffrey Zirlin Viet Anh Ho Tu Doan",
                                                        ["Trung Nguyen",          "Aleksander Larsen",     "Jeffrey Zirlin",       "Viet Anh Ho",          "Tu Doan"]),
    ("Yair Grindlinger Elad Horn Avner Gideoni Brenton Gumucio Roie Cohen Duwek",
                                                        ["Yair Grindlinger",      "Elad Horn",             "Avner Gideoni",        "Brenton Gumucio",      "Roie Cohen Duwek"]),
    # ── 12+ word ──
    ("Ilkka Paananen Lassi Leppinen Petri Styrman Visa Forsten Mikko Kodisoja Niko Derome",
                                                        ["Ilkka Paananen",        "Lassi Leppinen",        "Petri Styrman",        "Visa Forsten",         "Mikko Kodisoja",   "Niko Derome"]),
    ("Damien Scokin (CEO) Roberto Souviron Martín Rastellino Mariano Fiori Cristian Vilate Alejandro Tamer",
                                                        ["Damien Scokin",         "Roberto Souviron",      "Martín Rastellino",    "Mariano Fiori",        "Cristian Vilate",  "Alejandro Tamer"]),
    ("Fran Rosch (CEO) Jonathan Scudder Steve Ferris Lasse Andresen Hermann Svoren Victor Ake",
                                                        ["Fran Rosch",            "Jonathan Scudder",      "Steve Ferris",         "Lasse Andresen",       "Hermann Svoren",   "Victor Ake"]),
    ("Matt Cain (CEO) Chris Anderson Steve Yen Damien Katz James Phillips Dustin Sallings",
                                                        ["Matt Cain",             "Chris Anderson",        "Steve Yen",            "Damien Katz",          "James Phillips",   "Dustin Sallings"]),
    ("Stanislas Niox-Chateau Thomas Landais Ivan Schneider Franck Tetzlaff Steve Abou rjeily Jessy Bernal",
                                                        ["Stanislas Niox-Chateau","Thomas Landais",        "Ivan Schneider",       "Franck Tetzlaff",      "Steve Abourjeily", "Jessy Bernal"]),
    ("Dario Amodei Daniela Amodei Jack Clark Tom Brown Sam McCandlish Jared Kaplan Christopher Olah",
                                                        ["Dario Amodei",          "Daniela Amodei",        "Jack Clark",           "Tom Brown",            "Sam McCandlish",   "Jared Kaplan",  "Christopher Olah"]),
    ("Kruti Patel Goyal (CEO) Dev Tandon Haim Schoppik Jared Tarbell Robert Kalin Chris Maguire",
                                                        ["Kruti Patel Goyal",     "Dev Tandon",            "Haim Schoppik",        "Jared Tarbell",        "Robert Kalin",     "Chris Maguire"]),
    ("Lalit Ahuja Subashi Runie Trivedi Thomas W. Sisson II V. Bunty Bohra Vikram Ahuja",
                                                        ["Lalit Ahuja",           "Subashi Runie Trivedi", "Thomas W. Sisson II",  "V. Bunty Bohra",       "Vikram Ahuja"]),
    ("Nathan Sigworth Richard Bergström Armen Solakyan Lena Holzle-Johansson Marc Groz Patrick Schneider Peter Rice",
                                                        ["Nathan Sigworth",       "Richard Bergström",     "Armen Solakyan",       "Lena Holzle-Johansson","Marc Groz",        "Patrick Schneider","Peter Rice"]),
    ("Swapnil Kokate Tushar Gaware Dr. Shital Somani Kasat Nidhi Pant Ashwin Pawade Dr. Vaibhav Tidke Ganesh Bhere",
                                                        ["Swapnil Kokate",        "Tushar Gaware",         "Dr. Shital Somani Kasat","Nidhi Pant",          "Ashwin Pawade",    "Dr. Vaibhav Tidke","Ganesh Bhere"]),
]


def _q(db, sql: str, params: dict):
    return db.execute(text(sql), params)


def main() -> None:
    deleted = 0
    updated = 0
    inserted = 0
    not_found = 0

    # Snapshot before
    db = SessionLocal()
    before_total = db.execute(text("SELECT COUNT(*) FROM contacts")).scalar()
    db.close()

    db = SessionLocal()
    try:
        # ── Deletes ──
        for name in DELETES:
            result = _q(db, "DELETE FROM contacts WHERE name = :n", {"n": name})
            rc = result.rowcount
            if rc:
                print(f"  DELETE {rc}x: {name!r}")
                deleted += rc
            else:
                print(f"  NOT FOUND (delete): {name!r}")

        # ── Splits ──
        for blob_name, persons in SPLITS:
            # Handle trailing invisible chars with LIKE for the ZWJ blob
            if "Andy Pavlo" in blob_name:
                row = _q(db,
                    "SELECT id, company_id, source, role, scraped_pattern FROM contacts WHERE name LIKE :n",
                    {"n": blob_name + "%"},
                ).fetchone()
            else:
                row = _q(db,
                    "SELECT id, company_id, source, role, scraped_pattern FROM contacts WHERE name = :n",
                    {"n": blob_name},
                ).fetchone()

            if not row:
                print(f"  NOT FOUND: {blob_name!r}")
                not_found += 1
                continue

            contact_id = row[0]
            company_id = row[1]
            source     = row[2] or "scraping"
            role       = row[3] or "Founder"
            pattern    = row[4] or "firstname"

            domain_row = _q(db, "SELECT domain FROM companies WHERE id = :id", {"id": company_id}).fetchone()
            if not domain_row:
                print(f"  NO DOMAIN for company_id={company_id} blob={blob_name!r}")
                continue
            domain = domain_row[0]

            # Existing emails for this company
            existing = {r[0] for r in _q(db,
                "SELECT email FROM contacts WHERE company_id = :cid", {"cid": company_id}
            ).fetchall()}

            # Update person 1 in-place
            p1 = persons[0]
            p1_email = f"{first_name(p1)}@{domain}"
            _q(db, "UPDATE contacts SET name = :n, email = :e WHERE id = :id",
               {"n": p1, "e": p1_email, "id": contact_id})
            existing.add(p1_email)
            updated += 1

            # Insert persons 2-N
            for person in persons[1:]:
                email = f"{first_name(person)}@{domain}"
                if email in existing:
                    print(f"  SKIP dup email {email!r} for {person!r}")
                    continue
                existing.add(email)
                _q(db,
                    "INSERT INTO contacts (company_id, name, email, role, email_verified,"
                    " email_confidence, scraped_pattern, source, created_at)"
                    " VALUES (:cid, :n, :e, :role, 0, 60, :pat, :src, CURRENT_TIMESTAMP)",
                    {"cid": company_id, "n": person, "e": email,
                     "role": role, "pat": pattern, "src": source},
                )
                inserted += 1

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    # Snapshot after
    db2 = SessionLocal()
    after_total = db2.execute(text("SELECT COUNT(*) FROM contacts")).scalar()
    db2.close()

    total_net = inserted - deleted
    print("\n=== blob split complete ===")
    print(f"  contacts before:   {before_total}")
    print(f"  contacts after:    {after_total}")
    print(f"  deleted:           {deleted}  (non-person blobs)")
    print(f"  updated:           {updated}  (person-1 renamed in-place)")
    print(f"  inserted:          {inserted}  (persons 2-N as new rows)")
    print(f"  not_found:         {not_found}")
    print(f"  net change:        {'+' if total_net >= 0 else ''}{total_net}")


def main_extra() -> None:
    """Fix the 11 remaining even-word blobs missed in the first pass."""
    from sqlalchemy import text as _t
    EXTRA_DELETES = [
        # Role description, not a founder blob
        # (Kalyan is a real person but parenthetical cleanup only)
    ]
    EXTRA_SPLITS = [
        ("Greg Bell (CEO) Vern Paxson Seth Hall Robin Sommer",
            ["Greg Bell", "Vern Paxson", "Seth Hall", "Robin Sommer"]),
        ("Amit Kumar Jeff Winner Eckart Walther Geraud Boyer",
            ["Amit Kumar", "Jeff Winner", "Eckart Walther", "Geraud Boyer"]),
        ("Glen Wise Brian Fishman Philip Brennan Declan Cummings",
            ["Glen Wise", "Brian Fishman", "Philip Brennan", "Declan Cummings"]),
        ("Julien Hammerson (CEO) Ian Taylor Philip Goffin",
            ["Julien Hammerson", "Ian Taylor", "Philip Goffin"]),
        ("Tom Kemp Adam Au Paul Moore",
            ["Tom Kemp", "Adam Au", "Paul Moore"]),
        ("Geir Engdahl Dr. John Markus Lervik",
            ["Geir Engdahl", "Dr. John Markus Lervik"]),
        ("Aniket Deb Ankit Tomar Sachin Agrawal",
            ["Aniket Deb", "Ankit Tomar", "Sachin Agrawal"]),
        ("Amr Awadallah Jeff Hammerbacher Tom Reilly",
            ["Amr Awadallah", "Jeff Hammerbacher", "Tom Reilly"]),
        ("Matt Martin Gary Lerhaupt Mike Grinolds",
            ["Matt Martin", "Gary Lerhaupt", "Mike Grinolds"]),
        ("Bill Ready (CEO) Bryan Johnson",
            ["Bill Ready", "Bryan Johnson"]),
        ("Kalyan Krishnamurthy (CEO since 2017)",
            ["Kalyan Krishnamurthy"]),  # strip parenthetical, single person
    ]

    deleted = 0
    updated = 0
    inserted = 0
    not_found = 0

    db2 = SessionLocal()
    before = db2.execute(_t("SELECT COUNT(*) FROM contacts")).scalar()
    db2.close()

    db2 = SessionLocal()
    try:
        for blob_name, persons in EXTRA_SPLITS:
            row = db2.execute(
                _t("SELECT id, company_id, source, role, scraped_pattern FROM contacts WHERE name = :n"),
                {"n": blob_name},
            ).fetchone()
            if not row:
                print(f"  NOT FOUND: {blob_name!r}")
                not_found += 1
                continue
            contact_id, company_id = row[0], row[1]
            source = row[2] or "scraping"
            role   = row[3] or "Founder"
            pattern= row[4] or "firstname"

            domain = db2.execute(_t("SELECT domain FROM companies WHERE id = :id"), {"id": company_id}).scalar()
            if not domain:
                print(f"  NO DOMAIN for {blob_name!r}")
                continue

            existing = {r[0] for r in db2.execute(
                _t("SELECT email FROM contacts WHERE company_id = :cid"), {"cid": company_id}
            ).fetchall()}

            p1 = persons[0]
            p1_email = f"{first_name(p1)}@{domain}"
            db2.execute(
                _t("UPDATE contacts SET name = :n, email = :e WHERE id = :id"),
                {"n": p1, "e": p1_email, "id": contact_id},
            )
            existing.add(p1_email)
            updated += 1

            for person in persons[1:]:
                email = f"{first_name(person)}@{domain}"
                if email in existing:
                    print(f"  SKIP dup {email!r} for {person!r}")
                    continue
                existing.add(email)
                db2.execute(
                    _t("INSERT INTO contacts (company_id, name, email, role, email_verified,"
                       " email_confidence, scraped_pattern, source, created_at)"
                       " VALUES (:cid, :n, :e, :role, 0, 60, :pat, :src, CURRENT_TIMESTAMP)"),
                    {"cid": company_id, "n": person, "e": email,
                     "role": role, "pat": pattern, "src": source},
                )
                inserted += 1

        db2.commit()
    except Exception:
        db2.rollback()
        raise
    finally:
        db2.close()

    db2 = SessionLocal()
    after = db2.execute(_t("SELECT COUNT(*) FROM contacts")).scalar()
    db2.close()

    net = inserted - deleted
    print("\n=== extra-blob fix complete ===")
    print(f"  contacts before:   {before}")
    print(f"  contacts after:    {after}")
    print(f"  deleted:           {deleted}")
    print(f"  updated:           {updated}")
    print(f"  inserted:          {inserted}")
    print(f"  not_found:         {not_found}")
    print(f"  net change:        {'+' if net >= 0 else ''}{net}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "extra":
        main_extra()
    else:
        main()
