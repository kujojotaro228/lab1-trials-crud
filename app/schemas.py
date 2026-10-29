
import unicodedata
from datetime import date
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Правило валідації варіанта 25: група тільки з переліку A, B, контроль
ALLOWED_GROUPS = ("A", "B", "контроль")
ALLOWED_STATUSES = ("screening", "active", "completed", "withdrawn")


class ParticipantIn(BaseModel):
    """Тіло POST і PUT. PUT — повна заміна, тому модель та сама."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    participant_code: str = Field(min_length=1, max_length=32)
    protocol: str = Field(min_length=1, max_length=64)
    randomization_group: str
    enrollment_date: date
    status: str
    # null, поки учасник не завершив участь; якщо поле не передано — null
    completion_date: Optional[date] = None

    @field_validator("participant_code", "protocol", "randomization_group", "status")
    @classmethod
    def _check_control_chars(cls, v: str) -> str:
        # PostgreSQL не приймає \x00 у текстових полях (помилка кодування -> 500),
        # а інші керівні символи (\t, \n, \x1b ...) у кодах і шифрах — сміттєві дані.
        bad = next((ch for ch in v if unicodedata.category(ch) == "Cc"), None)
        if bad is not None:
            raise ValueError(
                f"рядок містить недопустимий керівний символ U+{ord(bad):04X}")
        return v

    @field_validator("randomization_group")
    @classmethod
    def _check_group(cls, v: str) -> str:
        if v not in ALLOWED_GROUPS:
            raise ValueError(
                "група рандомізації має бути одним із значень: A, B, контроль; "
                f"отримано '{v}'"
            )
        return v

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        if v not in ALLOWED_STATUSES:
            raise ValueError(
                "статус має бути одним із значень: " + ", ".join(ALLOWED_STATUSES)
                + f"; отримано '{v}'"
            )
        return v


def cross_field_errors(p: ParticipantIn) -> List[Dict[str, str]]:
    """Перевірки, що стосуються кількох полів одразу."""
    errors = []
    if p.completion_date is not None and p.completion_date < p.enrollment_date:
        errors.append({
            "field": "completion_date",
            "location": "body",
            "message": "дата завершення не може бути раніше дати включення "
                       f"({p.enrollment_date.isoformat()})",
        })
    return errors


_MESSAGES = {
    "missing": "поле обовʼязкове",
    "string_type": "очікується рядок",
    "string_too_short": "рядок не може бути порожнім",
    "string_too_long": "рядок задовгий (максимум {max_length} символів)",
    "date_type": "очікується дата у форматі YYYY-MM-DD",
    "date_parsing": "очікується дата у форматі YYYY-MM-DD",
    "date_from_datetime_parsing": "очікується дата у форматі YYYY-MM-DD",
    "date_from_datetime_inexact": "очікується дата без часу у форматі YYYY-MM-DD",
    "extra_forbidden": "невідоме поле",
    "json_invalid": "тіло запиту не є коректним JSON",
    "model_attributes_type": "тіло запиту має бути JSON-обʼєктом",
    "dict_type": "тіло запиту має бути JSON-обʼєктом",
    "int_parsing": "очікується ціле число",
    "int_type": "очікується ціле число",
    "greater_than_equal": "значення має бути не менше за {ge}",
    "less_than_equal": "значення має бути не більше за {le}",
}


def translate_pydantic_errors(errors: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Перетворює помилки pydantic/FastAPI на список {field, location, message}."""
    out = []
    for e in errors:
        loc = list(e.get("loc") or ())
        location = str(loc[0]) if loc else "body"
        typ = e.get("type", "")
        ctx = e.get("ctx") or {}

        parts = [str(x) for x in loc[1:]]
        if typ == "json_invalid" or not parts:
            field = location
        else:
            field = ".".join(parts)
        if location == "path" and field == "participant_id":
            field = "id"

        if typ == "value_error":
            msg = str(ctx.get("error") or e.get("msg", ""))
        elif typ in _MESSAGES:
            try:
                msg = _MESSAGES[typ].format(**ctx)
            except (KeyError, IndexError, ValueError):
                msg = _MESSAGES[typ]
        else:
            msg = str(e.get("msg", "невалідне значення"))
        out.append({"field": field, "location": location, "message": msg})
    return out
