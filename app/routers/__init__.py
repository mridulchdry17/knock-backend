from fastapi import FastAPI

from app.routers import auth, health


def register(app: FastAPI) -> None:
    app.include_router(health.router)
    app.include_router(auth.bootstrap)
    app.include_router(auth.api)
