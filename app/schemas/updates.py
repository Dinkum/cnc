from typing import Annotated, TypeVar

from pydantic import BeforeValidator
from pydantic.json_schema import SkipJsonSchema


T = TypeVar("T")


def _reject_null(value: T) -> T:
    if value is None:
        raise ValueError("cannot be null; omit this field to keep its current value")
    return value


# None is an internal default for omitted fields, never an accepted wire value.
# Keep null out of OpenAPI as well as runtime validation.
UpdateValue = Annotated[T | SkipJsonSchema[None], BeforeValidator(_reject_null)]
