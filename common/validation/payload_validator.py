from typing import TypeVar

from pydantic import BaseModel, ValidationError

from common.models.errors import PayloadValidationError


SchemaT = TypeVar("SchemaT", bound=BaseModel)


class PayloadValidator:
    def validate(self, payload: dict, schema: type[SchemaT]) -> SchemaT:
        try:
            return schema.model_validate(payload)
        except ValidationError as exc:
            raise PayloadValidationError(str(exc)) from exc


