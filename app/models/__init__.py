"""ORM models. Importing this package registers all tables on Base.metadata."""
from app.models.campaign import Campaign
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_log import EmailLog
from app.models.global_lock import GlobalContactLock
from app.models.send_queue import SendQueue
from app.models.session import Session
from app.models.template import Template
from app.models.user import User
from app.models.user_contact_map import UserContactMap

__all__ = [
    "User",
    "Session",
    "Company",
    "Contact",
    "Template",
    "Campaign",
    "SendQueue",
    "UserContactMap",
    "GlobalContactLock",
    "EmailLog",
]
