"""Схемы справочников: страны, города, категории."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def _code(v: str | None) -> str | None:
    return v.upper() if isinstance(v, str) else v


class CountryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=128)
    code: str = Field(pattern="^[A-Za-z]{2,3}$")
    is_active: bool = True

    _up = field_validator("code", mode="after")(_code)


class CountryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(None, min_length=1, max_length=128)
    code: str | None = Field(None, pattern="^[A-Za-z]{2,3}$")
    is_active: bool | None = None

    _up = field_validator("code", mode="after")(_code)


class CountryOut(_Out):
    id: int
    name: str
    code: str
    is_active: bool


class CityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=128)
    country_id: int
    is_active: bool = True


class CityUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(None, min_length=1, max_length=128)
    country_id: int | None = None
    is_active: bool | None = None


class CityOut(_Out):
    id: int
    name: str
    country_id: int
    is_active: bool


class CategoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=128)
    slug: str = Field(pattern="^[a-z0-9]+(?:_[a-z0-9]+)*$", max_length=128)
    description: str | None = Field(None, max_length=2000)
    parent_id: int | None = None
    is_active: bool = True


class CategoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(None, min_length=1, max_length=128)
    slug: str | None = Field(None, pattern="^[a-z0-9]+(?:_[a-z0-9]+)*$", max_length=128)
    description: str | None = Field(None, max_length=2000)
    parent_id: int | None = None
    is_active: bool | None = None


class CategoryOut(_Out):
    id: int
    name: str
    slug: str
    description: str | None
    parent_id: int | None
    is_active: bool


class CategoryTree(BaseModel):
    id: int
    name: str
    slug: str
    description: str | None = None
    is_active: bool
    children: list["CategoryTree"] = Field(default_factory=list)


CategoryTree.model_rebuild()
