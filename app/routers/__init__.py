from fastapi import FastAPI

from app.routers import health


def register(app: FastAPI) -> None:
    app.include_router(health.router)
