"""Response models and long-form docs for the API's OpenAPI spec. The spec is meant to be
handed to a person or an agent on its own, so it explains the data, not just the shapes."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from .. import models

# Statuses that mean the student is finished with an item. The explorer's `DONE` mirrors this.
DONE_STATUSES = ("turned_in", "completed", "graded", "done", "returned", "handed_in")

# Bookkeeping timestamps are stored as UTC ISO strings and returned verbatim.
Timestamp = Annotated[str, Field(json_schema_extra={"format": "date-time"},
                                 examples=["2026-10-06T14:00:00+00:00"])]


class Tracked(BaseModel):
    """Fields the API adds to every stored record."""

    active: bool = Field(description="False once the record stopped appearing on its source "
                                     "(deleted or unassigned there). Inactive records are "
                                     "hidden unless you pass `include_inactive=true`.")
    first_seen_at: Timestamp = Field(description="When the scraper first saw this record (UTC).")
    last_seen_at: Timestamp = Field(description="When the scraper last saw this record (UTC).")


# Named like the models they extend, so the spec's schemas read `Item`, not `ItemOut`.
class Course(models.Course, Tracked):
    __doc__ = models.Course.__doc__


class Item(models.Item, Tracked):
    __doc__ = models.Item.__doc__


class Grade(models.Grade, Tracked):
    __doc__ = models.Grade.__doc__


class RunCounts(BaseModel):
    courses: int | None = Field(description="Courses the run saved.")
    items: int | None = Field(description="Items the run saved.")
    grades: int | None = Field(description="Grades the run saved.")


class LastRun(BaseModel):
    run_id: int = Field(description="Id for `GET /runs/{run_id}`.")
    started_at: Timestamp = Field(description="When the run started (UTC).")
    finished_at: Timestamp | None = Field(description="When it finished (UTC); null while running.")
    ok: bool | None = Field(description="True if it succeeded, false if it failed, null while "
                                        "running. A failed run leaves the source's previous "
                                        "data in place.")
    error: str | None = Field(description="Error message from a failed run, as "
                                          "`ExceptionType: message`, then indented `while …` "
                                          "lines saying what the scraper was doing, any "
                                          "`caused by …` lines, and the `last page:` URL.")
    counts: RunCounts
    has_log: bool = Field(description="Whether the run kept a log (`GET /runs/{run_id}/log`). "
                                      "Only failed runs do, for each source's last 20 runs.")


class Run(LastRun):
    source: str = Field(description="The source this run scraped.", examples=["google_classroom"])


class RunDetail(Run):
    log: str | None = Field(description="For a failed run: the error, a timestamped trail of "
                                        "the scraper's log lines and browser activity (tabs, "
                                        "navigations, XHR responses, HTTP errors, failed "
                                        "requests; URLs only, with secrets masked), and the "
                                        "traceback. Null otherwise.")


class SourceStatus(BaseModel):
    source: str = Field(description="Source name, as used in every `source` field and filter.",
                        examples=["google_classroom"])
    enabled: bool = Field(description="Whether scheduled and manual syncs include this source.")
    last_run: LastRun | None = Field(description="The most recent scrape of this source; null "
                                                 "if it has never run.")
    last_success_at: Timestamp | None = Field(description="When the last successful run finished "
                                                          "(UTC). This is how fresh the data is.")
    login_command: str | None = Field(
        description="Set when the last run got stuck on a sign-in challenge: the command that "
                    "finishes the sign-in by hand (run from a checkout of the repository). "
                    "Show it to a human; an agent can't complete it.",
    )


class SyncRequest(BaseModel):
    request_id: int = Field(description="Id to poll with `GET /sync/{request_id}`.")
    sources: list[str] | None = Field(description="Sources to sync; null means every enabled source.")
    status: Literal["pending", "running", "done", "failed"] = Field(
        description="`pending`: waiting for the scraper. `running`: a scrape picked it up. "
                    "`done`: every requested source succeeded. `failed`: at least one failed "
                    "(see `error` and `GET /sources`).",
    )
    requested_at: Timestamp = Field(description="When it was queued (UTC).")
    started_at: Timestamp | None = Field(description="When a scrape picked it up (UTC).")
    finished_at: Timestamp | None = Field(description="When that scrape finished (UTC).")
    error: str | None = Field(description="Why it failed, as `source: error` for each failed "
                                          "source, separated by `; `.")


class ClearedSyncRequests(BaseModel):
    deleted: int = Field(description="How many requests were deleted or cancelled.")


class Health(BaseModel):
    status: Literal["ok"]


class Error(BaseModel):
    detail: str = Field(description="Human-readable explanation.")


def errors(*codes: int) -> dict[int | str, dict]:
    """OpenAPI `responses` entries for the given error status codes."""
    text = {
        401: "Bearer token missing or wrong (only when the server sets `LIFEAPI_API_TOKEN`).",
        404: "No record with that id.",
        400: "Bad request, e.g. an unknown source name.",
        503: "The scraper hasn't created the database yet.",
    }
    return {c: {"model": Error, "description": text[c]} for c in codes}


TAGS = [
    {"name": "items", "description": "Assignments, quizzes, questions, materials and "
                                     "announcements from every source, in one shape."},
    {"name": "grades", "description": "Course grades with category breakdowns and individual "
                                      "scores, and the overall GPA (Infinite Campus)."},
    {"name": "courses", "description": "Classes the student is enrolled in, per source."},
    {"name": "status", "description": "Which sources exist, how fresh their data is, and "
                                      "manual sync requests."},
]

DESCRIPTION = f"""
One student's schoolwork, collected from several school platforms into a single read-only
API. A scraper signs in to each platform on a schedule (every couple of hours) and saves what
it finds; this API serves the saved copy. It never contacts the platforms itself.

## Common questions

| Question | Call |
|---|---|
| What's due soon? | `GET /items/upcoming?days=7` |
| What's overdue? | `GET /items/missing` |
| Any new announcements? | `GET /announcements?days=3` |
| How am I doing in my classes? | `GET /grades` |
| What's my GPA? | `GET /gpa` (a bare number to three decimal places, a percentage that can exceed 100) |
| Find a specific assignment | `GET /items?q=essay` |
| Everything for one class | `GET /courses`, then `GET /items?source=…&course_id=…` |
| What did I get on X? | `GET /items?q=…` (`score`, `points_possible`), or `GET /grades` (`entries`) |
| How up to date is this? | `GET /sources` (`last_success_at`) |
| Refresh now | `POST /sync`, then poll `GET /sync/{{request_id}}` until `done` or `failed` |

## Sources

| `source` | Platform | Provides |
|---|---|---|
| `google_classroom` | Google Classroom | courses, assignments, questions, materials, announcements, comments |
| `ap_classroom` | College Board AP Classroom | courses, assignments, quizzes (progress checks), videos |
| `vhl` | Vista Higher Learning (VHL Central) | courses, assignments grouped by due date |
| `infinite_campus` | Infinite Campus (school SIS) | courses and grades only, no items |

The same real-world class appears once per platform that has it, with different ids, so
match across sources by `course_name` if you need to.

## Data model

- **Item**: anything with a title that lives in a class: `kind` is `assignment`, `quiz`,
  `question`, `material` or `announcement`.
- **Course**: a class on one source.
- **Grade**: one course's grade for one term and grading task (e.g. `MP1` /
  `MARKING PERIOD`), with `categories` and per-assignment `entries`.

Every record is identified by **`(source, id)`**; ids are only unique within a source.
`course_id` refers to a course in the same source. `url` is a deep link to the record on its
platform: give it to a person who wants to open or submit the work. Anything platform-specific
is in `extra` (documented per source on each schema).

**Status.** `status` keeps each platform's own wording in snake_case (e.g. `assigned`,
`missing`, `turned_in`, `turned_in_late`, `graded`, `completed`, `partial_late`). These count
as finished: {", ".join(f"`{s}`" for s in DONE_STATUSES)}. `/items/upcoming` and
`/items/missing` already apply this, so prefer them over filtering `status` yourself.

**Time.** `due_at` and `posted_at` are ISO 8601 with a UTC offset, in the school's local time.
When a platform gives only a date, deadlines become 23:59 and post dates 00:00 local time;
`due_text` keeps the original wording. Record bookkeeping timestamps (`first_seen_at`,
`last_seen_at`, run and sync times) are UTC. For datetime filters, include an offset
(`2026-10-06T00:00:00-04:00`); a value without one is read as UTC, and a bare date
(`2026-10-06`) as midnight UTC.

**Deletions.** A record that disappears from its platform is kept with `active: false` and
hidden from every list unless you pass `include_inactive=true`. If a source's scrape fails,
its previous data stays as it was; check `GET /sources` before trusting stale data.

## Authentication

If the server sets `LIFEAPI_API_TOKEN`, every endpoint except `/health` and `/gpa` requires
`Authorization: Bearer <token>`. Otherwise no auth is needed.

## Errors

Errors are JSON `{{"detail": "…"}}`: 401 bad or missing token, 404 unknown id, 400 unknown
source in a sync request, 422 invalid parameter (`detail` is then a list of problems), 503
no data yet.
"""
