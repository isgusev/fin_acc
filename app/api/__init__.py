from fastapi import APIRouter, Depends

from app.api import admin, auth, finance
from app.deps import csrf_protect

api_router = APIRouter(prefix="/api/v1", dependencies=[Depends(csrf_protect)])
api_router.include_router(auth.router)
api_router.include_router(finance.router)
api_router.include_router(admin.router)
