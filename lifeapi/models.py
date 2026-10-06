"""Normalized data model shared by the scraper (writer) and the API (reader).

Every source maps its data onto these few shapes. Anything source-specific that doesn't
fit a dedicated field goes into `extra`.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ItemKind(str, Enum):
    ASSIGNMENT = "assignment"
    QUIZ = "quiz"
    QUESTION = "question"
    MATERIAL = "material"
    ANNOUNCEMENT = "announcement"


class Attachment(BaseModel):
    title: str | None = None
    url: str | None = None
    type: str | None = None  # e.g. "drive", "youtube", "link", "form", "file"


class Comment(BaseModel):
    author: str | None = None
    text: str
    posted_at: str | None = None  # as displayed by the source; often relative ("Sep 3")
    private: bool = False


class Course(BaseModel):
    source: str
    id: str
    name: str
    section: str | None = None
    teacher: str | None = None
    url: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class Item(BaseModel):
    """An assignment, announcement, material, etc. from any source."""

    source: str
    id: str  # unique within the source
    kind: ItemKind
    title: str
    url: str | None = None
    course_id: str | None = None
    course_name: str | None = None
    description: str | None = None
    author: str | None = None
    posted_at: datetime | None = None
    due_at: datetime | None = None
    due_text: str | None = None  # raw due string, kept when it can't be parsed precisely
    status: str | None = None  # e.g. "assigned", "missing", "turned_in", "graded", "done"
    points_possible: float | None = None
    score: float | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    comments: list[Comment] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


class GradeEntry(BaseModel):
    """A single graded thing inside a course grade (e.g. one assignment's score)."""

    name: str
    url: str | None = None
    category: str | None = None
    due_at: str | None = None
    score: str | None = None
    points_earned: float | None = None
    points_possible: float | None = None
    percent: float | None = None
    flags: list[str] = Field(default_factory=list)  # e.g. "missing", "late", "exempt"
    comments: str | None = None


class Grade(BaseModel):
    """A course grade for one grading task/term (e.g. "Quarter 1 Grade")."""

    source: str
    id: str
    course_name: str
    course_id: str | None = None
    term: str | None = None
    task: str | None = None
    teacher: str | None = None
    letter: str | None = None
    percent: float | None = None
    url: str | None = None
    categories: list[dict[str, Any]] = Field(default_factory=list)
    entries: list[GradeEntry] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


class ScrapeResult(BaseModel):
    courses: list[Course] = Field(default_factory=list)
    items: list[Item] = Field(default_factory=list)
    grades: list[Grade] = Field(default_factory=list)
