"""One-shot: delete the 26 'dot_local' malformed-email contacts identified
by scripts/audit_contact_emails.py on 2026-06-21.

Pattern: the email-guesser kept a trailing dot for names whose first token
was a title or initial (e.g. 'Dr.', 'K.', 'M.', 'Late.'), producing an
effectively-empty localpart ('dr.@…', 'k.@…'). None of these are
deliverable; Gmail would bounce them. Their presence in the pool was
crashing the frontend's strict Zod email validator on /today.

FK behavior for dependent tables:
    user_contact_cooldown      ondelete=CASCADE   (auto)
    user_contact_notes         ondelete=CASCADE   (auto)
    today_batch_items.to_…     ondelete=CASCADE   (auto)
    send_queue.replied_to_…    ondelete=SET NULL  (auto)
    user_contact_map           no cascade         → DELETE manually first
    send_queue.contact_id      no cascade         → DELETE manually first
    email_logs.contact_id      no cascade         → ABORT if any (audit trail)

We deliberately refuse to mutate email_logs — those are audit records.

Run from project root with venv active:

    python -m scripts.cleanup_dot_local_contacts
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.base import _WRITE_ENGINE

# Frozen list. These IDs were identified on 2026-06-21 from a 4,575-contact
# pool. Re-running this script is a no-op once they're deleted.
BAD_IDS: list[int] = [
    737, 908, 916, 976, 1185, 1248, 1392, 1625, 2131, 2284, 2518, 2777,
    2943, 3344, 3353, 3359, 3365, 3377, 3404, 3486, 3491, 3622, 3821,
    3871, 4025, 4323,
]


def main() -> None:
    ids_csv = ",".join(str(i) for i in BAD_IDS)
    with Session(_WRITE_ENGINE) as db:
        print("=== DEPENDENT ROW COUNTS (before delete) ===")
        for tbl, col in [
            ("user_contact_map", "contact_id"),
            ("send_queue", "contact_id"),
            ("today_batch_items", "to_contact_id"),
            ("email_logs", "contact_id"),
            ("user_contact_cooldown", "contact_id"),
            ("user_contact_notes", "contact_id"),
        ]:
            n = db.execute(
                text(f"SELECT COUNT(*) FROM {tbl} WHERE {col} IN ({ids_csv})")
            ).scalar()
            print(f"  {tbl:25s} {col:15s} {n} rows")

        email_log_refs = db.execute(
            text(f"SELECT COUNT(*) FROM email_logs WHERE contact_id IN ({ids_csv})")
        ).scalar()
        if email_log_refs:
            print(
                f"\nABORT: {email_log_refs} email_logs rows reference these "
                f"contacts — refusing to mutate audit history."
            )
            return
        print("\nemail_logs refs = 0 → safe to proceed")

        print("\n=== DELETING DEPENDENTS ===")
        for sql, label in [
            (f"DELETE FROM user_contact_map WHERE contact_id IN ({ids_csv})", "user_contact_map"),
            (f"DELETE FROM send_queue WHERE contact_id IN ({ids_csv})", "send_queue"),
        ]:
            r = db.execute(text(sql))
            print(f"  {label:25s} → {r.rowcount} rows affected")

        print("\n=== DELETING CONTACTS ===")
        r = db.execute(text(f"DELETE FROM contacts WHERE id IN ({ids_csv})"))
        print(f"  contacts                 → {r.rowcount} rows deleted")

        db.commit()
        print("\n=== COMMITTED ===")

        n = db.execute(
            text(f"SELECT COUNT(*) FROM contacts WHERE id IN ({ids_csv})")
        ).scalar()
        print(f"contacts remaining with bad ids: {n} (should be 0)")


if __name__ == "__main__":
    main()
