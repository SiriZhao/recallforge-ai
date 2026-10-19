"""Explicit molecular formula accounting, not structural chemistry validation."""
from pydantic import BaseModel, ConfigDict, Field


class ChemicalSpecies(BaseModel):
    model_config = ConfigDict(extra='forbid')
    formula: str = Field(min_length=1, max_length=160)
    coefficient: int = Field(default=1, ge=1, le=1000, strict=True)
    # Separate charge avoids guessing whether Fe3+ means charge or atom count.
    charge: int = Field(default=0, ge=-20, le=20, strict=True)


class ChemicalReaction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reactants: list[ChemicalSpecies] = Field(min_length=1, max_length=32)
    products: list[ChemicalSpecies] = Field(min_length=1, max_length=32)
    conditions: str = ''
