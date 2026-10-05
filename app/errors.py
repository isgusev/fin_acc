"""Единый формат ошибок приложения.

API отдаёт: {"error": {"code": "...", "message": "...", "field": "..."}}
"""

from typing import Any


class AppError(Exception):
    code = "APP_ERROR"
    status_code = 400

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "field": self.field}}


class ValidationAppError(AppError):
    code = "VALIDATION_ERROR"
    status_code = 422


class NotFoundError(AppError):
    code = "NOT_FOUND"
    status_code = 404


class UnauthorizedError(AppError):
    code = "UNAUTHORIZED"
    status_code = 401


class ForbiddenError(AppError):
    code = "FORBIDDEN"
    status_code = 403


class CsrfError(AppError):
    code = "CSRF_ERROR"
    status_code = 403


class ConflictError(AppError):
    """Нарушение бизнес-правила: удаление счёта с операциями, дубль и т.п."""

    code = "CONFLICT"
    status_code = 409


class RateLimitedError(AppError):
    code = "TOO_MANY_ATTEMPTS"
    status_code = 429


# Перевод типовых ошибок Pydantic на русский
_PYDANTIC_MESSAGES: dict[str, str] = {
    "missing": "Обязательное поле",
    "string_too_short": "Слишком короткое значение",
    "string_too_long": "Слишком длинное значение",
    "greater_than": "Значение должно быть больше {gt}",
    "greater_than_equal": "Значение должно быть не меньше {ge}",
    "less_than": "Значение должно быть меньше {lt}",
    "less_than_equal": "Значение должно быть не больше {le}",
    "decimal_parsing": "Введите число",
    "decimal_max_places": "Не больше {decimal_places} знаков после запятой",
    "decimal_max_digits": "Слишком большое число",
    "decimal_whole_digits": "Слишком большое число",
    "int_parsing": "Введите целое число",
    "int_from_float": "Введите целое число",
    "float_parsing": "Введите число",
    "date_parsing": "Неверный формат даты",
    "date_from_datetime_parsing": "Неверный формат даты",
    "uuid_parsing": "Неверный идентификатор",
    "bool_parsing": "Неверное логическое значение",
    "value_error": "{msg}",
    "enum": "Недопустимое значение",
    "literal_error": "Недопустимое значение",
    "json_invalid": "Некорректный JSON",
    "model_attributes_type": "Некорректные данные",
    "dict_type": "Некорректные данные",
}


def humanize_pydantic_error(err: dict[str, Any]) -> tuple[str, str | None]:
    """Возвращает (сообщение, поле) для одной ошибки pydantic."""
    loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path", "form")]
    field = ".".join(loc) or None
    template = _PYDANTIC_MESSAGES.get(err.get("type", ""))
    ctx = dict(err.get("ctx") or {})
    if template is None:
        return err.get("msg", "Некорректное значение"), field
    if err.get("type") == "value_error" and loc and loc[-1] == "email":
        return "Некорректный адрес email", field
    if err.get("type") == "value_error":
        msg = str(ctx.get("error", err.get("msg", "")))
        return msg.removeprefix("Value error, "), field
    try:
        return template.format(**ctx), field
    except (KeyError, IndexError):
        return template, field
