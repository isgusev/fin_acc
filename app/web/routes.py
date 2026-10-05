"""HTML-интерфейс (Jinja2): объединяет маршруты всех разделов."""

from fastapi import APIRouter

from app.web import admin, auth, finance, planning

web_router = APIRouter(include_in_schema=False)
web_router.include_router(auth.router)
web_router.include_router(finance.router)
web_router.include_router(planning.router)
web_router.include_router(admin.router)
