"""«Прогноз»: накопления на конец каждого месяца нарастающим итогом."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_user
from app.models import User
from app.services import forecast as forecast_svc
from app.web.templating import render

router = APIRouter()


@router.get("/forecast")
def forecast_page(
    request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> Response:
    return render(request, "forecast.html", {"fc": forecast_svc.build(db, user)})
