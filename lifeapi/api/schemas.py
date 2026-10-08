"""Response models and long-form docs for the API's OpenAPI spec. The spec is meant to be
handed to a person or an agent on its own, so it explains the data, not just the shapes."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field

from .. import models
from ..models import DONE_STATUSES  # re-exported: the API's finished statuses

# Bookkeeping timestamps are stored as UTC ISO strings and returned verbatim.
Timestamp = Annotated[str, Field(json_schema_extra={"format": "date-time"},
                                 examples=["2026-10-06T14:00:00+00:00"])]


class Tracked(BaseModel):
    """Fields the API adds to every stored record."""

    active: bool = Field(description="False once the record stopped appearing on its source "
                                     "(deleted or unassigned there). Inactive records are "
                                     "hidden unless you pass `include_inactive=true`.")
    first_seen_at: Timestamp = Field(description="When the scraper first saw this record (UTC). "
                                                 "For an assignment made in lifeapi, when it "
                                                 "was made.")
    last_seen_at: Timestamp = Field(description="When the scraper last saw this record (UTC). "
                                                "For an assignment made in lifeapi, when it was "
                                                "last edited.")


# Named like the models they extend, so the spec's schemas read `Item`, not `ItemOut`.
class Course(models.Course, Tracked):
    __doc__ = models.Course.__doc__


class UserMarks(BaseModel):
    """What the student added to an item in lifeapi. It's kept in lifeapi only: nothing is
    sent to the platform, and the scraper never changes it."""

    note: str | None = Field(description="The student's note, set with `PUT "
                                         "/items/{source}/{item_id}/note`.")
    due_at: datetime | None = Field(
        description="The deadline the student set (`PUT /items/{source}/{item_id}/due`), in the "
                    "school's local time. It replaces `source_due_at` as the item's `due_at`.",
        examples=["2026-09-25T23:59:00-04:00"])
    status: Literal["turned_in", "assigned"] | None = Field(
        None, description="On a scraped item, the student's own status (`PUT "
                          "/items/{source}/{item_id}/status`): `turned_in` or `assigned`. It's the "
                          "item's `status` while `source_status` disagrees on whether the work is "
                          "finished.")
    updated_at: Timestamp = Field(description="When the note, deadline or status was last "
                                              "changed (UTC).")


class Item(models.Item, Tracked):
    __doc__ = models.Item.__doc__ + (
        " Items include assignments the student made in lifeapi from an announcement (see "
        "`converted_from`), and carry the student's own note, deadline and status (`user`).")

    status: str | None = Field(
        None,
        description="The status that counts, in the platform's wording (snake_case): the "
                    "student's (`user.status`: `turned_in` or `assigned`) while it disagrees with "
                    "the platform's on whether the work is finished, else the platform's "
                    "(`source_status`). Every filter and list uses this one.",
        examples=["turned_in"])
    source_status: str | None = Field(
        description="The status as the platform has it, which lifeapi never changes. Null for "
                    "assignments made in lifeapi. "
                    + models.Item.model_fields["status"].description, examples=["assigned"])

    due_at: datetime | None = Field(
        None,
        description="The deadline that counts, ISO 8601 with UTC offset (the school's local "
                    "time): the student's own if they changed it (`user.due_at`), else the "
                    "source's (`source_due_at`). Every filter, sort and list uses this one. A "
                    "date-only deadline becomes 23:59 local time. Null when there's no deadline "
                    "or it couldn't be parsed; see `due_text`.",
        examples=["2026-09-25T23:59:00-04:00"],
    )
    source_due_at: datetime | None = Field(
        description="The deadline as the source has it, which lifeapi never changes. Equal to "
                    "`due_at` unless the student changed the deadline. Null for assignments made "
                    "in lifeapi and items without a deadline.",
        examples=["2026-09-23T23:59:00-04:00"])
    converted_from: str | None = Field(
        description="Set on assignments the student made in lifeapi (`POST /extra/assignments`): "
                    "the `id` of the announcement it came from, in the same `source`. Their `id` "
                    "starts with `lifeapi-`, `url` links to the announcement, and `status` is "
                    "`assigned` or `done`, as the student sets it. Null for everything scraped.")
    user: UserMarks | None = Field(description="The student's note, deadline and status for this "
                                               "item, or null if they set none.")


class Grade(models.Grade, Tracked):
    __doc__ = models.Grade.__doc__


class RunCounts(BaseModel):
    courses: int | None = Field(description="Courses the run saved.")
    items: int | None = Field(description="Items the run saved.")
    grades: int | None = Field(description="Grades the run saved.")


class LastRun(BaseModel):
    run_id: int = Field(description="Id for `GET /runs/{run_id}`.")
    partial: str | None = Field(description="The partial fetch this run did (one of the "
                                            "source's `partials`, e.g. `gpa`), or null for a "
                                            "full one.")
    started_at: Timestamp = Field(description="When the run started (UTC).")
    finished_at: Timestamp | None = Field(description="When it finished (UTC); null while running.")
    ok: bool | None = Field(description="True if it succeeded, false if it failed, null while "
                                        "running. A failed run leaves the source's previous "
                                        "data in place.")
    error: str | None = Field(description="Error message from a failed run, as "
                                          "`ExceptionType: message`, then indented `while …` "
                                          "lines saying what the scraper was doing, any "
                                          "`caused by …` lines, and the `last page:` URL.")
    failure: Literal["scraper", "site", "login"] | None = Field(
        description="Whose problem a failed run is; null unless `ok` is false. `scraper`: the "
                    "scraper broke (a bug, or the site changed under it), so its code needs "
                    "fixing. `site`: the site was down or unreachable (an HTTP 5xx, a network "
                    "error). Nothing needs fixing; the next run tries again. `login`: a sign-in "
                    "needs finishing by hand (see the source's `login_command`).",
    )
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


class PartialFetch(BaseModel):
    name: str = Field(description="Name to use as a schedule's `partial`.", examples=["gpa"])
    description: str = Field(description="What it fetches.", examples=["Overall GPA only"])


_INTERVAL = ("Minutes between scheduled fetches, counted from the start of the source's "
             "previous fetch (scheduled or manual).")
_PARTIAL = ("A partial fetch (one of the source's `partials`) to run between full fetches, or "
            "null to fetch everything every time.")
_FULL_EVERY = ("With `partial`: every this-many-th fetch is a full one and the rest are "
               "`partial` (4: full, partial, partial, partial, full, …). Null without `partial`.")


class ScheduleSettings(BaseModel):
    """How often to fetch a source, and how much each time."""

    interval_minutes: int = Field(ge=5, description=_INTERVAL + " At least 5.", examples=[30])
    partial: str | None = Field(None, description=_PARTIAL, examples=["gpa"])
    full_every: int | None = Field(None, ge=2, description=_FULL_EVERY + " Required with "
                                                          "`partial`, at least 2.", examples=[4])


class Schedule(BaseModel):
    """When a source is fetched on its own. Set it with `PUT /sources/{source}/schedule`."""

    interval_minutes: int = Field(description=_INTERVAL, examples=[30])
    partial: str | None = Field(description=_PARTIAL, examples=["gpa"])
    full_every: int | None = Field(description=_FULL_EVERY, examples=[4])
    default: bool = Field(description="True when no schedule has been set for this source, so it "
                                      "follows the server's default: a full fetch every "
                                      "`interval_minutes`.")
    next_fetch_at: Timestamp | None = Field(
        description="When the next scheduled fetch is due (UTC). A time in the past means it's "
                    "due now: it starts within about a minute, or after the run in progress. Null "
                    "for a disabled source, which is never fetched on a schedule.")
    next_fetch_partial: str | None = Field(description="The partial fetch the next scheduled "
                                                       "fetch will do, or null for a full one.")


_HEADED = ("Run this source in a headed (visible) browser instead of a headless one. Bot checks "
           "such as Cloudflare's spot headless browsers more easily. On a server without a "
           "screen, the scraper shows it on a virtual display, which costs a little more "
           "memory and CPU while the source runs.")


class BrowserSettings(BaseModel):
    """How the scraper runs the browser for a source."""

    headed: bool = Field(description=_HEADED, examples=[True])


class Browser(BaseModel):
    """How the scraper runs the browser for a source. Set it with `PUT
    /sources/{source}/browser`. The scraper's `--headed` flag and `LIFEAPI_HEADLESS=0` show
    the browser for every source, whatever this says."""

    headed: bool = Field(description=_HEADED)
    default: bool = Field(description="True when nothing has been set for this source, so "
                                      "`headed` is the source's own default.")


class SourceStatus(BaseModel):
    source: str = Field(description="Source name, as used in every `source` field and filter.",
                        examples=["google_classroom"])
    enabled: bool = Field(description="Whether scheduled and manual syncs include this source.")
    partials: list[PartialFetch] = Field(description="Partial fetches this source can do between "
                                                     "full ones, for its `schedule`. Most "
                                                     "sources have none.")
    schedule: Schedule
    browser: Browser
    last_run: LastRun | None = Field(description="The most recent scrape of this source, full or "
                                                 "partial; null if it has never run.")
    last_success_at: Timestamp | None = Field(description="When the last successful full run "
                                                          "finished (UTC). This is how fresh the "
                                                          "data is. Partial runs refresh only "
                                                          "their part (see each record's "
                                                          "`last_seen_at`).")
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


class HistoryRecord(BaseModel):
    """One change to a grade-related value. Rows are added only when a value changes, so each
    one holds until the next row for the same record."""

    source: str = Field(description="Source of the record.", examples=["infinite_campus"])
    kind: Literal["grade", "entry", "item"] = Field(
        description="`grade`: a `Grade` record (course grade or overall GPA). `entry`: one scored "
                    "assignment in that grade's `entries`. `item`: an `Item`'s status and score.")
    id: str = Field(description="The grade's or item's `id`. For an `entry`, its grade's `id`.",
                    examples=["gpa:6779:cumulative::w"])
    entry: str | None = Field(description="For an `entry`: which one, as its `url` (or `name|due_at` "
                                          "without one). Null otherwise.")
    label: str | None = Field(description="What it is, as it read when recorded: `course_name · "
                                          "term · task` for a grade (just the name for a GPA), the "
                                          "assignment name for an entry, the title for an item.",
                              examples=["AP MICROECONOMICS · MP1 · MARKING PERIOD"])
    value: dict[str, Any] = Field(
        description="The tracked fields from that moment, leaving out null ones. Grade: `letter`, "
                    "`percent`, `gpa`, `points_earned`, `points_possible`, `term_gpa`, `categories` "
                    "(each `name`, `letter`, `percent`, `points_earned`, `points_possible`). Entry: "
                    "`score`, `points_earned`, `points_possible`, `percent`, `flags`, `comments`. "
                    "Item: `status`, `score`, `points_possible`.",
        examples=[{"gpa": 99.15}])
    recorded_at: Timestamp = Field(description="When the scraper first saw this value (UTC). The "
                                               "change happened on the source between the previous "
                                               "fetch and this time.")


class GpaValue(BaseModel):
    value: float = Field(description="The GPA as a percentage, written to three decimal places "
                                     "(`99.150`). Pad it back to three places to show it.",
                         examples=[99.15])
    last_seen_at: Timestamp = Field(description="When the scraper last read this GPA from the "
                                                "source (UTC).")


class CountValue(BaseModel):
    value: int = Field(description="The count.", examples=[3])


class StatusValue(BaseModel):
    value: int = Field(description="How many enabled sources' most recent run failed.",
                       examples=[0])
    minutes: int | None = Field(description="Minutes since the stalest enabled source last "
                                            "finished a successful full run: the age of the "
                                            "oldest data. Null if a source has never succeeded.",
                                examples=[95])


class NoteSettings(BaseModel):
    note: str = Field(max_length=10_000, description="The note, as plain text. Blank clears it.",
                      examples=["Asked for an extension; she said Monday is fine."])


def _bare_date(value: Any) -> Any:
    """Keep "2026-10-12" a date: as a datetime it would be midnight, not the end of the day."""
    if isinstance(value, str) and len(value) == 10:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return value


DueDate = Annotated[datetime | date, BeforeValidator(_bare_date), Field(
    description="The new deadline, ISO 8601. Without an offset it's read as the school's local "
                "time, and a bare date (`2026-10-12`) as 23:59 that day.",
    examples=["2026-10-12T23:59:00-04:00"])]


class DueSettings(BaseModel):
    due_at: DueDate


class StatusSettings(BaseModel):
    turned_in: bool = Field(description="True: turned in (finished). False: not yet.")
    keep: bool = Field(False, description="Store it as the student's even when the platform "
                                          "already has it so, so it holds if the platform's "
                                          "status changes. A kept `turned_in` still shows the "
                                          "platform's wording (`graded`) while that's finished "
                                          "too.")


WorkKind = Literal["assignment", "quiz"]


class NewAssignment(BaseModel):
    """An assignment to make from an announcement. `GET /extra/drafts/{source}/{item_id}`
    suggests every field."""

    source: str = Field(description="The announcement's `source`.", examples=["google_classroom"])
    announcement_id: str = Field(description="The announcement's `id`.", examples=["798765432109"])
    title: str = Field(min_length=1, max_length=300, pattern=r"\S", examples=["Chapter 5 vocab quiz"])
    kind: WorkKind = Field("assignment", description="`quiz` for a quiz, test or exam.")
    due_at: DueDate | None = None
    description: str | None = Field(None, max_length=20_000)
    points_possible: float | None = Field(None, ge=0)
    note: str | None = Field(None, max_length=10_000, description="A note, as with `PUT "
                                                                  "/items/{source}/{item_id}/note`.")


class AssignmentEdit(BaseModel):
    """Fields to change on an assignment made in lifeapi. Leave a field out to keep it. Its
    deadline and note are changed like any item's (`/items/{source}/{item_id}/due` and
    `/note`)."""

    title: str | None = Field(None, min_length=1, max_length=300, pattern=r"\S")
    kind: WorkKind | None = None
    description: str | None = Field(None, max_length=20_000, description="Null clears it.")
    points_possible: float | None = Field(None, ge=0, description="Null clears it.")
    status: Literal["assigned", "done"] | None = Field(
        None, description="`done` counts as finished, so it leaves `/items/upcoming` and "
                          "`/items/missing`.")


class DateMention(BaseModel):
    text: str = Field(description="The date as written.", examples=["this Friday at 8am"])
    start: int = Field(description="Where `text` starts in the announcement's `description` "
                                   "(a character offset).")
    end: int = Field(description="Where it ends (exclusive).")
    at: datetime = Field(description="What it means, in the school's local time, counted from "
                                     "when the announcement was posted (so `tomorrow` is the day "
                                     "after the post).", examples=["2026-10-09T08:00:00-04:00"])
    time: Literal["text", "course", "default"] = Field(
        description="Where the time of day came from. `text`: it was written (`8am`, `tonight`). "
                    "`course`: only a date was written, so it's the time the course's deadlines "
                    "are usually set at. `default`: only a date, and no usual time, so 23:59.")


class AssignmentDraft(BaseModel):
    title: str = Field(description="Suggested title: the sentence that asks for the work, with "
                                   "its deadline and reminder wording cut out.",
                       examples=["Chapter 5 vocab quiz"])
    titles: list[str] = Field(description="Other plausible titles, best first: the course's own "
                                          "name for numbered work (`Lab 5`), and the post's "
                                          "first sentence.")
    kind: WorkKind = Field(description="`quiz` when the sentence names a quiz, test or exam.")
    due_at: datetime | None = Field(description="Suggested deadline (`due.at`), or null if the "
                                                "post has no date.")
    due: DateMention | None = Field(description="The date `due_at` came from.")
    description: str = Field(description="The post's text, whole: it's context even when the "
                                         "post asks for several things.")
    points_possible: float | None = Field(description="Points, when the post says (`20 points`).")
    sentence: str = Field(description="The sentence the draft came from.")


class RelatedItem(BaseModel):
    source: str
    id: str
    title: str
    kind: models.ItemKind
    due_at: datetime | None = Field(description="Its deadline (`due_at`).")
    score: float = Field(description="How much of the item's title the announcement contains, "
                                     "weighting rare words more, from 0.5 to 1.")


class ConversionDraft(BaseModel):
    """Machine-made suggestions for turning an announcement into assignments, to fill a form
    a person checks. Made from the announcement's text and the rest of its course; no AI
    service is involved, so it's instant and deterministic."""

    source: str = Field(description="The announcement's `source`.")
    id: str = Field(description="The announcement's `id`.")
    drafts: list[AssignmentDraft] = Field(
        description="One per piece of work the post seems to ask for (at most 5), best first. "
                    "Never empty: a post that asks for nothing still gets one from its first "
                    "sentence.")
    dates: list[DateMention] = Field(description="Every date and time in the post, in order, as "
                                                 "alternatives to each draft's `due_at`.")
    related: list[RelatedItem] = Field(
        description="Existing items in the course the post may be about (at most 3), most likely "
                    "first: work the platform already lists, or whose deadline the post moves. "
                    "Rather than make a duplicate, change one's deadline with `PUT "
                    "/items/{source}/{item_id}/due`.")
    moves_deadline: bool = Field(description="True when the post reads like it moves a deadline "
                                             "(`extended`, `postponed`, `moved to`), so `related` "
                                             "is probably the better target.")
    usual_time: str | None = Field(description="The local time (`HH:MM`) the course's deadlines are "
                                               "usually set at, used for dates written without one; "
                                               "null if there's no clear habit.", examples=["08:00"])
    converted: list[str] = Field(description="`id`s of assignments already made from this "
                                             "announcement.")


class CommandRequest(BaseModel):
    """A command for one item, in words, e.g. `{"source": "google_classroom", "item_id":
    "889402927132", "command": "due friday 5pm"}`."""

    command: str | None = Field(
        None, max_length=500,
        description="What to do, as you'd say it. Due dates: `set the due date to today at 11:59 "
                    "PM`, `due at 11:59`, `due oct 8 3 o'clock`, `due wednesday` (keeps the time), "
                    "`due at 5` (keeps the date), `due in 3 days`, `push it back a day`, `2 hours "
                    "earlier`, `+1 week`, `reset the due date`. Notes: `note: bring a calculator`, "
                    "`add note: …` (adds a line), `clear note`. Assignments made in lifeapi: `done`, "
                    "`not done`. And `undo`, `redo`. Typos, spoken numbers (`eleven fifty nine pm`) "
                    "and filler (`please`, `hey`) are fine. Optional when `url` ends with `##` and the "
                    "command.", examples=["set the due date to today at 11:59 PM"])
    source: str | None = Field(None, description="The item's `source`, with `item_id`.",
                               examples=["google_classroom"])
    item_id: str | None = Field(None, description="The item's `id`, with `source`.",
                                examples=["889402927132"])
    url: str | None = Field(
        None, max_length=4000,
        description="Instead of `source` and `item_id`: a link to the item, as copied from its "
                    "platform (a Google Classroom assignment link from any signed-in account works), "
                    "or from the explorer. It may end with `##` and the command, like the explorer's "
                    "command links.",
        examples=["https://classroom.google.com/c/ODU2MTYxODI5MTU3/a/ODg5NDAyOTI3MTMy/details"])
    dry_run: bool = Field(False, description="Say what the command would do without doing it.")


class FieldChange(BaseModel):
    field: Literal["due_at", "note", "status"] = Field(
        description="`due_at`: the student's own deadline (`user.due_at`), where null means the "
                    "platform's. `note`: the note. `status`: the student's status (`user.status`), where "
                    "null means the platform's, or an assignment made in lifeapi's own.")
    before: Any = Field(description="Its value before (`due_at` in the school's local time).")
    after: Any = Field(description="Its value after.")


class Action(BaseModel):
    """A command that ran (or would run, for `dry_run`). Every one can be undone with `POST
    /extra/commands/{action_id}/undo`, however long ago."""

    action_id: int | None = Field(description="Id for `GET /extra/commands/{action_id}` and undo. "
                                              "Null when nothing was changed (a dry run, or the item "
                                              "already was that way).")
    kind: Literal["command", "undo", "redo"] = Field(
        description="`undo` undid a command (`undo_of`); `redo` undid an undo, so the command is "
                    "back. Undoing an `undo` redoes; undoing a `command` or `redo` undoes.")
    source: str
    item_id: str
    title: str | None = Field(description="The item's title when the command ran.")
    command: str = Field(description="The command as given (`undo` for an undo). Changes made "
                                     "with `PUT`/`DELETE /items/{source}/{item_id}/due`, `/status` "
                                     "or `/note` are logged too, as `set the due date`, `reset the "
                                     "status`, `delete the note` and so on.")
    summary: str = Field(description="What it did, in a sentence to show the person, e.g. `Due date "
                                     "set to today, Thu Oct 8 at 11:59 PM (was tomorrow, Fri Oct 9 "
                                     "at 11:59 PM).`")
    notes: list[str] = Field(description="How it read anything ambiguous (`No am or pm said, so "
                                         "11:59 PM; …`, `Kept its date, Fri Oct 9.`, `Read it as "
                                         "“due wednesday”.`), and warnings (`That's in the past.`). "
                                         "Show these too.")
    changes: list[FieldChange]
    undo_of: int | None = Field(description="For an undo: the action it undid.")
    undone_by: int | None = Field(description="Set once this action has been undone: the undo's id.")
    created_at: Timestamp | None = Field(description="When it ran (UTC); null if it didn't.")


class CommandResult(Action):
    item: Item | None = Field(description="The item as it is now; null if it was deleted since.")


class Health(BaseModel):
    status: Literal["ok"]


class Error(BaseModel):
    detail: str = Field(description="Human-readable explanation.")


def errors(*codes: int) -> dict[int | str, dict]:
    """OpenAPI `responses` entries for the given error status codes."""
    text = {
        401: "Token missing or wrong (only when the server sets `LIFEAPI_API_TOKEN`).",
        404: "No record with that id.",
        400: "Bad request, e.g. an unknown source name or a partial fetch the source lacks.",
        409: "The item isn't the right kind for this, e.g. converting something that isn't an "
             "announcement, or editing a scraped item as if it were made in lifeapi.",
        503: "The scraper hasn't created the database yet.",
    }
    return {c: {"model": Error, "description": text[c]} for c in codes}


TAGS = [
    {"name": "items", "description": "Assignments, quizzes, questions, materials and "
                                     "announcements from every source, in one shape, with the "
                                     "student's own notes and deadlines."},
    {"name": "grades", "description": "Course grades with category breakdowns and individual "
                                      "scores, and the overall GPA (Infinite Campus)."},
    {"name": "history", "description": "Every change to grades, GPAs, assignment scores and "
                                       "item statuses, kept for good."},
    {"name": "courses", "description": "Classes the student is enrolled in, per source."},
    {"name": "values", "description": "Single numbers for widgets and displays, each as plain "
                                      "text (`/min/…`) or JSON (`/json/…`). No token needed."},
    {"name": "status", "description": "Which sources exist, how fresh their data is, how often "
                                      "they're fetched and with what browser, and manual sync "
                                      "requests."},
    {"name": "extra", "description": "Turning announcements into assignments (machine-made drafts, "
                                     "and assignments the student makes, which then appear among "
                                     "the items like any other), and commands in words, each "
                                     "undoable."},
]

DESCRIPTION = f"""
One student's schoolwork, collected from several school platforms into a single API. A
scraper signs in to each platform on a schedule (every couple of hours unless set otherwise)
and saves what it finds; this API serves the saved copy. It never contacts the platforms
itself. The student can add to it (notes, their own deadlines, assignments made from
announcements); those additions stay in lifeapi and never reach a platform.

## Common questions

| Question | Call |
|---|---|
| What's due soon? | `GET /items/upcoming?days=7` |
| What's overdue? | `GET /items/missing` |
| Any new announcements? | `GET /announcements?days=3` |
| How am I doing in my classes? | `GET /grades` |
| What's my GPA? | `GET /json/gpa` (`value` to three decimal places, a percentage that can exceed 100; `last_seen_at` says when it was last scraped) |
| Just a number for a widget | `GET /min/{{name}}` (plain text) or `GET /json/{{name}}` (`{{"value": …}}`), where `name` is `gpa`, `missing` (overdue in the last `days`, default 7), `next` (due in the next `days`, default 7) or `status` (failing sources; the JSON adds `minutes` since the stalest source's last successful run) |
| How has my GPA changed? | `GET /history?gpa=true` (one row per change) |
| How did my grade in X move? | `GET /grades` for its `id`, then `GET /history?id=…` (the grade and its scored assignments) |
| Find a specific assignment | `GET /items?q=essay` |
| Everything for one class | `GET /courses`, then `GET /items?source=…&course_id=…` |
| What did I get on X? | `GET /items?q=…` (`score`, `points_possible`), or `GET /grades` (`entries`) |
| How up to date is this? | `GET /sources` (`last_success_at`, and `schedule.next_fetch_at` for the next refresh) |
| Refresh now | `POST /sync`, then poll `GET /sync/{{request_id}}` until `done` or `failed` |
| Refresh a source more or less often | `PUT /sources/{{source}}/schedule` |
| Remember something about an assignment | `PUT /items/{{source}}/{{item_id}}/note` |
| I got an extension | `PUT /items/{{source}}/{{item_id}}/due` (`DELETE` undoes it) |
| I turned it in (or haven't, whatever the platform says) | `PUT /items/{{source}}/{{item_id}}/status` with `{{"turned_in": true}}` (`DELETE` undoes it) |
| Keep a deadline or status even if the platform changes it | `PUT /items/{{source}}/{{item_id}}/due` with the deadline it has; `/status` with `"keep": true` |
| An announcement says something is due | `GET /extra/drafts/{{source}}/{{item_id}}`, then `POST /extra/assignments` |
| Mark one of those done | `PATCH /extra/assignments/{{source}}/{{item_id}}` with `{{"status": "done"}}` |
| Do something said in words ("due friday 5pm", "push it back a day", "undo") | `POST /extra/commands` |

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
as finished: {", ".join(f"`{s}`" for s in DONE_STATUSES)}. The student can mark an item
turned in or not in lifeapi (see below), so `status` can differ from `source_status`. `/items/upcoming` and
`/items/missing` already apply this, so prefer them over filtering `status` yourself.

**Time.** `due_at` and `posted_at` are ISO 8601 with a UTC offset, in the school's local time.
When a platform gives only a date, deadlines become 23:59 and post dates 00:00 local time;
`due_text` keeps the original wording. Record bookkeeping timestamps (`first_seen_at`,
`last_seen_at`, run and sync times) are UTC. For datetime filters, include an offset
(`2026-10-06T00:00:00-04:00`); a value without one is read as UTC, and a bare date
(`2026-10-06`) as midnight UTC.

**History.** Records hold their current values only, but every change to a grade-related value
(grade letters and percents, GPAs, category totals, assignment scores, item statuses and
scores) is also kept in `GET /history`, for good. Scrape runs (`GET /runs`) are kept for 180
days.

**The student's additions.** Any item can carry a note, the student's own deadline and
their own status, in `user`. A changed deadline becomes the item's `due_at` everywhere (lists, filters, sorting,
`/items/upcoming`, `/items/missing`, the `next` and `missing` counts), while `source_due_at`
always keeps the platform's. An item with `user.due_at` set has a deadline the student chose;
say so when it matters ("due Monday, moved from Friday"). Likewise `user.status` (`turned_in`
or `assigned`) is the item's `status`, in every list and count, while the platform's
(`source_status`) disagrees on whether it's finished: the student says they turned it in (on
paper, say) and the platform hasn't caught up, or the reverse. Once the platform agrees, its
own wording (`graded`) shows again. lifeapi never tells the platform. Assignments made from an
announcement (`converted_from` set, `id` starting `lifeapi-`) live in the announcement's
source and course, link to the announcement, have no `source_due_at`, and are `assigned`
until the student marks them `done`. `GET /extra/drafts/{{source}}/{{item_id}}` suggests them:
titles, kinds and deadlines read from the announcement's text (dates count from when it was
posted) and the course's habits (its usual due time, how it names numbered work), plus
existing items the announcement may be about. It's a deterministic text analyser, not an AI
model, so check its suggestions before saving them; they're meant to fill a form.

**Commands.** `POST /extra/commands` takes an item (by `source` and `item_id`, or by its link)
and a command in words: due dates (`set the due date to today at 11:59 PM`, `due at 11:59`,
`due oct 8 3 o'clock`, `push it back a day`, `reset the due date`), notes (`note: …`), `done`
and `not done` (`turned in`, `assigned`…), `undo`. What isn't said is kept (`due at 5pm` keeps the date), a time without am/pm is read
the way a student would mean it, and the answer says what was done and how anything
ambiguous was read; show `summary` and `notes` to the person. Every command can be undone,
however old (`POST /extra/commands/{{action_id}}/undo`, or the command `undo`). Changes made
with `PUT`/`DELETE /items/{{source}}/{{item_id}}/note`, `/due` and `/status` are logged and
undoable the same way.

**Deletions.** A record that disappears from its platform is kept with `active: false` and
hidden from every list unless you pass `include_inactive=true`. If a source's scrape fails,
its previous data stays as it was; check `GET /sources` before trusting stale data. A failed
run's `failure` says whose problem it is: `site` (the site was down; the next run retries),
`login` (a person has to finish a sign-in) or `scraper` (the scraper needs fixing).

## Schedules

Each source is fetched every `interval_minutes` (its `schedule` in `GET /sources`). Some
sources can also do a cheaper **partial fetch** that reads only part of their data (listed in
the source's `partials`): `infinite_campus` has `gpa`, which reads only the overall GPA. A
schedule can alternate them: `{{"interval_minutes": 30, "partial": "gpa", "full_every": 4}}`
reads the GPA every 30 minutes and everything every 4th time (every 2 hours). A partial fetch
updates only what it reads and marks nothing inactive. `PUT /sources/{{source}}/schedule` sets
a schedule and `DELETE` on the same path restores the default. Manual syncs (`POST /sync`)
are always full and count as a fetch: the next scheduled one is `interval_minutes` after them.

Each source's `browser` says whether it runs in a headed (visible) browser rather than a
headless one, which gets past some sites' bot checks. `vhl` is headed by default, since
VHL Central sits behind Cloudflare. `PUT /sources/{{source}}/browser` with `{{"headed": true}}`
or `false` changes it, and `DELETE` restores the source's default.

## Authentication

If the server sets `LIFEAPI_API_TOKEN`, every endpoint except `/health`, `/min/…` and `/json/…`
requires the token, as `Authorization: Bearer <token>` or as the `token` query parameter
(`?token=<token>`). Otherwise no auth is needed.

## Errors

Errors are JSON `{{"detail": "…"}}`: 401 bad or missing token, 404 unknown id or source, 400
unknown source in a sync request or a schedule that doesn't fit its source, 409 an item of the
wrong kind (converting something that isn't an announcement, or editing a scraped item through
`/extra/assignments`), 422 invalid parameter or body (`detail` is then a list of problems), 503
no data yet.
"""
