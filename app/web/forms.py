"""Вспомогательные функции HTML-форм: разбор значений, валидация, ошибки, безопасный next."""

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError
from starlette.datastructures import FormData

from app.deps import CSRF_FORM_FIELD
from app.errors import AppError, NotFoundError, humanize_pydantic_error

GENERAL_ERROR = "Проверьте правильность заполнения формы"


@dataclass
class FormState:
    """Состояние формы для шаблона: введённые значения, ошибки полей и общая ошибка."""

    values: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    action: str = ""
    edit_id: object | None = None

    def v(self, name: str) -> str:
        return self.values.get(name, "")

    def checked(self, name: str) -> bool:
        return self.values.get(name, "") not in ("", "0", "false", "off")

    def apply(self, exc: Exception) -> None:
        """Переносит ошибку в состояние формы. NotFound без поля — пробрасывается дальше."""
        if isinstance(exc, FormInvalid):
            self.errors.update(exc.errors)
            self.error = exc.message
        elif isinstance(exc, AppError):
            if isinstance(exc, NotFoundError) and exc.field is None:
                raise exc
            if exc.field:
                self.errors[exc.field] = exc.message
            self.error = exc.message
        else:
            raise exc


class FormInvalid(Exception):
    def __init__(self, errors: dict[str, str], message: str | None = None) -> None:
        super().__init__(message or GENERAL_ERROR)
        self.errors = errors
        self.message = message or GENERAL_ERROR


def form_values(form: FormData) -> dict[str, str]:
    """Строковые значения формы (без CSRF-токена). Пароли шаблоны обратно не выводят."""
    return {k: v for k, v in form.multi_items() if isinstance(v, str) and k != CSRF_FORM_FIELD}


def norm_money(v: str | None) -> str | None:
    """«1 234,56» → «1234.56». Пустая строка остаётся пустой (→ None в схеме)."""
    if v is None:
        return None
    for ch in (" ", " ", " ", " ", "_"):
        v = v.replace(ch, "")
    return v.replace(",", ".").strip()


def checkbox(values: dict[str, str], name: str) -> bool:
    return values.get(name, "") not in ("", "0", "false", "off")


def opt(values: dict[str, str], name: str) -> str | None:
    """Значение поля или None, если оно пустое."""
    v = values.get(name, "").strip()
    return v or None


def validate[M: BaseModel](model: type[M], data: dict[str, Any]) -> M:
    """Строит Pydantic-схему; ошибки валидации → FormInvalid с русскими сообщениями.

    Ключи со значением None отбрасываются: обязательное поле получит ошибку «Обязательное
    поле», необязательное — значение по умолчанию.
    """
    data = {k: v for k, v in data.items() if v is not None}  # None → значение по умолчанию
    try:
        return model.model_validate(data)
    except ValidationError as e:
        errors: dict[str, str] = {}
        general: list[str] = []
        for err in e.errors():
            msg, fld = humanize_pydantic_error(dict(err))
            if fld == "email" and err.get("type") == "value_error":
                msg = "Введите корректный email"  # сообщение email-validator — на английском
            if fld:
                errors.setdefault(fld, msg)
            else:
                general.append(msg)
        raise FormInvalid(errors, general[0] if general else None) from e


def safe_next(nxt: str | None, default: str = "/") -> str:
    """Разрешаем только локальные пути (защита от open redirect)."""
    if (
        nxt
        and nxt.startswith("/")
        and not nxt.startswith("//")
        and "\\" not in nxt
        and "\n" not in nxt
        and "\r" not in nxt
    ):
        return nxt
    return default
