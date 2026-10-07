"""Normalized data model shared by the scraper (writer) and the API (reader).

Every source maps its data onto these few shapes. Anything source-specific that doesn't
fit a dedicated field goes into `extra`.

Field descriptions double as the API's OpenAPI documentation, so write them for someone
reading the API, not the scraper.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ItemKind(str, Enum):
    """What sort of thing an item is. Only `assignment`, `quiz` and `question` are work to
    hand in; `material` is reference content and `announcement` is a stream post."""

    ASSIGNMENT = "assignment"
    QUIZ = "quiz"
    QUESTION = "question"
    MATERIAL = "material"
    ANNOUNCEMENT = "announcement"


class Attachment(BaseModel):
    """A file or link attached to an item by the teacher (the student's own submitted work
    is in `extra.submitted_work` for Google Classroom)."""

    title: str | None = Field(None, description="Display name of the attachment.")
    url: str | None = Field(None, description="Link to open the attachment.")
    type: str | None = Field(
        None,
        description="Free-form label as the source shows it, e.g. `Google Docs`, `PDF`, "
                    "`YouTube video`, `Link`.",
        examples=["Google Docs"],
    )


class Comment(BaseModel):
    """A comment on an item (Google Classroom class or private comments)."""

    author: str | None = Field(None, description="Commenter's display name.")
    text: str = Field(description="Comment text.")
    posted_at: str | None = Field(
        None,
        description="When it was posted, exactly as the source displays it (often partial "
                    "or relative, e.g. `Sep 3`). Not machine-parseable.",
        examples=["Sep 3"],
    )
    private: bool = Field(False, description="True for a private comment between the student "
                                             "and teacher; false for a class comment.")


class Course(BaseModel):
    """A class the student is enrolled in on one source. The same real-world class appears
    once per source that has it, with different ids."""

    source: str = Field(description="Source that this record came from.", examples=["google_classroom"])
    id: str = Field(description="Course id, unique only within `source`.", examples=["712345678901"])
    name: str = Field(description="Course name as the source shows it.", examples=["AP Macroeconomics"])
    section: str | None = Field(None, description="Section or period, if the source has one.",
                                examples=["Period 3"])
    teacher: str | None = Field(None, description="Teacher's display name.")
    url: str | None = Field(None, description="Deep link to the course on the source.")
    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="Source-specific fields. `ap_classroom`: `section_id`. `vhl`: `program`, "
                    "`course_id` (VHL's course number). `infinite_campus`: `course_number`, "
                    "`room`, `school`.",
    )


class Item(BaseModel):
    """An assignment, quiz, question, material or announcement from any source."""

    source: str = Field(description="Source that this record came from.", examples=["google_classroom"])
    id: str = Field(description="Item id, unique only within `source`. Use `(source, id)` "
                                "as the key.", examples=["798765432109"])
    kind: ItemKind = Field(description="What sort of item this is.")
    title: str = Field(description="Title as the source shows it.", examples=["Unit 2 Problem Set"])
    url: str | None = Field(
        None,
        description="Deep link to the item on the source. This is the link to give a person "
                    "who wants to open or submit the work.",
    )
    course_id: str | None = Field(None, description="`id` of the course (in the same `source`) "
                                                    "this item belongs to.")
    course_name: str | None = Field(None, description="Course name, copied here for convenience.")
    description: str | None = Field(None, description="Instructions or post body, as plain text.")
    author: str | None = Field(None, description="Who posted it (usually the teacher).")
    posted_at: datetime | None = Field(
        None,
        description="When the item was posted, ISO 8601 with UTC offset. A date-only value "
                    "from the source becomes 00:00 local time.",
    )
    due_at: datetime | None = Field(
        None,
        description="Deadline, ISO 8601 with UTC offset (the school's local time). A date-only "
                    "deadline becomes 23:59 local time. Null when there's no deadline or it "
                    "couldn't be parsed; see `due_text`.",
        examples=["2026-09-23T23:59:00-04:00"],
    )
    due_text: str | None = Field(None, description="The deadline exactly as the source showed it, "
                                                   "kept when it can't be parsed precisely.")
    status: str | None = Field(
        None,
        description="Submission state, in the source's own wording (snake_case). Seen values: "
                    "`assigned`, `missing`, `turned_in`, `turned_in_late`, `graded` "
                    "(google_classroom); `assigned`, `completed`, `graded` (ap_classroom); "
                    "`completed`, `partial`, `partial_late`, `missing` (vhl). Null for "
                    "announcements, materials and anything without a state. Statuses that "
                    "count as finished: `turned_in`, `completed`, `graded`, `done`, `returned`, "
                    "`handed_in`.",
        examples=["assigned"],
    )
    points_possible: float | None = Field(None, description="Maximum score, if graded.")
    score: float | None = Field(None, description="Score earned, once graded.")
    attachments: list[Attachment] = Field(default_factory=list,
                                          description="Teacher-provided files and links.")
    comments: list[Comment] = Field(default_factory=list, description="Class and private comments.")
    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="Source-specific fields; keys vary by source and may be absent. "
                    "**google_classroom**: `topic`, `category`, `edited`, `comment_count`, "
                    "`links` (links found in the description), `submitted_work` (the student's "
                    "own attachments), plus cache bookkeeping (`list_signature`, "
                    "`detail_fetched_at`). **ap_classroom**: `type`, `ap_status`, `starts_at`, "
                    "`submitted_at`, `unit`, `progress`, `timer_minutes`, `hard_timer`, `secure`, "
                    "`allow_late_submission`, `awaiting_scoring`, `scored_at`, `actions`; videos "
                    "have `watched_percentage`, `video_id`, `resource_id`. **vhl** (each item is "
                    "a group of activities due on one date): `activities_assigned`, "
                    "`activities_completed`, `vhl_status`, `estimated_time`, "
                    "`availability_message`, `can_be_started`, `proctored`, `calendar_labels`, "
                    "`calendar_url`.",
    )


class GradeEntry(BaseModel):
    """A single graded thing inside a course grade (e.g. one assignment's score)."""

    name: str = Field(description="Assignment name.")
    url: str | None = Field(None, description="Deep link to the assignment on the source.")
    category: str | None = Field(None, description="Grading category it counts toward, "
                                                   "matching a `categories[].name`.",
                                 examples=["Homework"])
    due_at: str | None = Field(None, description="Due date as the source reports it "
                                                 "(Infinite Campus: ISO 8601 UTC).",
                               examples=["2026-10-01T03:59:00.000Z"])
    score: str | None = Field(None, description="Score as displayed (may be a letter or mark, "
                                                "not just a number).", examples=["95"])
    points_earned: float | None = Field(None, description="Points earned.")
    points_possible: float | None = Field(None, description="Points possible.")
    percent: float | None = Field(None, description="Score as a percentage, 0–100.")
    flags: list[str] = Field(default_factory=list, description="Markers such as `missing`, "
                                                               "`late`, `exempt`, `incomplete`.")
    comments: str | None = Field(None, description="Teacher's comment on this score.")


class Grade(BaseModel):
    """A course grade for one grading task in one term (e.g. "MP1 / MARKING PERIOD"). There is
    one record per course × term × task.

    Overall GPAs (e.g. cumulative) use the same shape: `gpa` is set and `course_id` is null."""

    source: str = Field(description="Source that this record came from.", examples=["infinite_campus"])
    id: str = Field(description="Grade id, unique only within `source`.")
    course_name: str = Field(description="Course name as the source shows it.")
    course_id: str | None = Field(None, description="`id` of the course in the same `source`.")
    term: str | None = Field(None, description="Term name, e.g. `MP1`.", examples=["MP1"])
    task: str | None = Field(None, description="Grading task, e.g. `MARKING PERIOD` or "
                                               "`FINAL AVERAGE`.", examples=["MARKING PERIOD"])
    teacher: str | None = Field(None, description="Teacher's display name.")
    letter: str | None = Field(None, description="Letter grade, if posted or computed.",
                               examples=["A-"])
    percent: float | None = Field(None, description="Grade as a percentage, 0–100.")
    gpa: float | None = Field(None, description="Set only on overall GPA records (no course), on "
                                                "the school's own scale (may be 0–100).",
                              examples=[99.15])
    url: str | None = Field(None, description="Deep link to this grade on the source.")
    categories: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Per-category breakdown. Each has `name`, `weight` (percent of the grade), "
                    "`excluded`, `letter`, `percent`, `points_earned`, `points_possible`.",
        examples=[[{"name": "Homework", "weight": 20.0, "excluded": False, "letter": "B",
                    "percent": 85.0, "points_earned": 170.0, "points_possible": 200.0}]],
    )
    entries: list[GradeEntry] = Field(default_factory=list,
                                      description="Individual assignment scores.")
    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="Source-specific fields. `infinite_campus`: `points_earned`, "
                    "`points_possible`, `term_gpa`, `weight_percent`, `modified_at`, `posted` "
                    "(true once the teacher has posted the grade, rather than it being an "
                    "in-progress calculation). On GPA records: `type` (e.g. `cumulative`), "
                    "`weighted`, and `rank`/`out_of` when the school publishes them.",
    )


class ScrapeResult(BaseModel):
    courses: list[Course] = Field(default_factory=list)
    items: list[Item] = Field(default_factory=list)
    grades: list[Grade] = Field(default_factory=list)
