from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(APIModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
