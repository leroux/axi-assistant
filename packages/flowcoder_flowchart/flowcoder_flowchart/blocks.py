"""Block types for flowchart workflows.

Defines the BlockType enum, all block models, and the Block discriminated union.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class BlockType(StrEnum):
    START = "start"
    END = "end"
    PROMPT = "prompt"
    BRANCH = "branch"
    VARIABLE = "variable"
    BASH = "bash"
    COMMAND = "command"
    REFRESH = "refresh"


class VariableType(StrEnum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    JSON = "json"


class Position(BaseModel):
    x: float
    y: float


class BlockBase(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = ""
    session: str = "default"
    position: Position | None = None


class StartBlock(BlockBase):
    type: Literal[BlockType.START] = BlockType.START


class EndBlock(BlockBase):
    type: Literal[BlockType.END] = BlockType.END


class PromptBlock(BlockBase):
    type: Literal[BlockType.PROMPT] = BlockType.PROMPT
    prompt: str
    output_variable: str | None = None
    output_schema: dict[str, Any] | None = None


class BranchBlock(BlockBase):
    type: Literal[BlockType.BRANCH] = BlockType.BRANCH
    condition: str


class VariableBlock(BlockBase):
    type: Literal[BlockType.VARIABLE] = BlockType.VARIABLE
    variable_name: str
    variable_value: str = ""
    variable_type: VariableType = VariableType.STRING


class BashBlock(BlockBase):
    type: Literal[BlockType.BASH] = BlockType.BASH
    command: str
    capture_output: bool = True
    output_variable: str | None = None
    output_type: VariableType = VariableType.STRING
    continue_on_error: bool = False
    working_directory: str | None = None
    exit_code_variable: str | None = None


class CommandBlock(BlockBase):
    type: Literal[BlockType.COMMAND] = BlockType.COMMAND
    command_name: str
    arguments: str = ""
    inherit_variables: bool = False
    merge_output: bool = False


class RefreshBlock(BlockBase):
    type: Literal[BlockType.REFRESH] = BlockType.REFRESH
    target_session: str | None = None


Block = Annotated[
    StartBlock
    | EndBlock
    | PromptBlock
    | BranchBlock
    | VariableBlock
    | BashBlock
    | CommandBlock
    | RefreshBlock,
    Field(discriminator="type"),
]
