"""Rule tester: check and try a condition or formula with sample values."""
from typing import Any, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core import expressions
from core.db import get_db
from core.security import get_current_user
from core.timeutil import today_for_company_id

router = APIRouter(prefix="/rules", tags=["Rules"])


class EvaluateIn(BaseModel):
    expression: str = Field(..., max_length=expressions.MAX_LENGTH)
    variables: Optional[dict[str, Any]] = None
    strict: bool = False


@router.get("/functions")
def functions(_: dict = Depends(get_current_user)):
    return [{"name": name, "help": fn.help, "min_args": fn.min_args, "max_args": fn.max_args}
            for name, fn in sorted(expressions.FUNCTIONS.items())]


@router.post("/evaluate")
def evaluate(req: EvaluateIn, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Returns {result, type, fields} or 400 {detail, position}."""
    try:
        today = today_for_company_id(db, current_user["company_id"])
        value = expressions.evaluate(req.expression, req.variables or {}, today=today, strict=req.strict)
        fields = expressions.variables_used(req.expression)
    except expressions.ExpressionError as e:
        return JSONResponse(status_code=400, content={"detail": e.message, "position": e.position})
    kind = ("yes/no" if isinstance(value, bool) else "number" if hasattr(value, "quantize")
            else "date" if hasattr(value, "isoformat") else "list" if isinstance(value, list)
            else "empty" if value is None else "text")
    return {"result": expressions.to_json(value), "type": kind, "fields": fields}
