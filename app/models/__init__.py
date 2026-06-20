"""ORM models. Importing this package registers all tables on Base.metadata."""
from app.models.campaign import Campaign
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_failure import EmailFailure
from app.models.email_log import EmailLog
from app.models.global_lock import GlobalContactLock
from app.models.platform_company_lock import PlatformCompanyLock
from app.models.refresh_token import RefreshToken
from app.models.send_queue import SendQueue
from app.models.session import Session
from app.models.template import Template
from app.models.today_batch_item import TodayBatchItem
from app.models.user import User
from app.models.user_company_lock import UserCompanyLock
from app.models.user_contact_cooldown import UserContactCooldown
from app.models.user_contact_map import UserContactMap
from app.models.user_contact_note import UserContactNote
from app.models.user_excluded_domain import UserExcludedDomain
from app.models.waitlist import WaitlistEntry

__all__ = [
    "Campaign",
    "Company",
    "Contact",
    "EmailFailure",
    "EmailLog",
    "GlobalContactLock",
    "PlatformCompanyLock",
    "RefreshToken",
    "SendQueue",
    "Session",
    "Template",
    "TodayBatchItem",
    "User",
    "UserCompanyLock",
    "UserContactCooldown",
    "UserContactMap",
    "UserContactNote",
    "UserExcludedDomain",
    "WaitlistEntry",
]
