from fastapi import FastAPI

from app.routers import (
    admin,
    auth,
    contacts,
    health,
    inbox,
    onboarding,
    preferences,
    templates,
    today,
    waitlist,
)


def register(app: FastAPI) -> None:
    app.include_router(health.router)
    app.include_router(auth.bootstrap)
    app.include_router(auth.api)
    app.include_router(waitlist.router)
    app.include_router(onboarding.router)
    app.include_router(admin.router)
    app.include_router(preferences.router)
    app.include_router(contacts.router)
    app.include_router(today.router)
    app.include_router(inbox.router)
    app.include_router(templates.router)
