"""A step-by-step record of one source's run, saved with the run when it fails.

It collects every `lifeapi.*` log line (debug included, whatever the console shows) and
what the browser did: tabs opening and closing, navigations, XHR/fetch responses, HTTP
errors, failed requests and page errors. Only URLs are recorded, never request or response
bodies, and credential-looking query parameters are masked, because sign-in redirects
carry codes and tokens.
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from patchright.async_api import BrowserContext, Page

MAX_LINES = 3000  # the most recent ones are kept
MAX_URL = 300
_SECRET_PARAM = re.compile(
    r"token|code|pass|secret|session|saml|ticket|sig|key|nonce|assertion|^state$|auth(?!user)", re.I
)
_RESPONSE_TYPES = {"document", "xhr", "fetch"}


def redact(url: str) -> str:
    """`url` with credential-looking query values masked and the fragment dropped if it
    carries parameters (OAuth implicit flows put tokens there). Truncated for the log."""
    try:
        parts = urlsplit(url)
        query = urlencode(
            [(k, "redacted" if _SECRET_PARAM.search(k) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)],
            safe="/:,",
        )
        fragment = "redacted" if "=" in parts.fragment else parts.fragment
        url = urlunsplit(parts._replace(query=query, fragment=fragment))
    except ValueError:
        pass
    return url if len(url) <= MAX_URL else url[:MAX_URL] + "…"


class _Handler(logging.Handler):
    def __init__(self, trail: "Trail"):
        super().__init__(logging.DEBUG)
        self.trail = trail

    def emit(self, record: logging.LogRecord) -> None:
        try:
            name = record.name.removeprefix("lifeapi.")
            self.trail.add(f"{record.levelname.lower()} {name}: {record.getMessage()}")
        except Exception:
            self.handleError(record)


class Trail:
    """Use as `with Trail(ctx) as trail:` around one source's scrape."""

    def __init__(self, context: BrowserContext):
        self.context = context
        self.lines: deque[str] = deque(maxlen=MAX_LINES)
        self.dropped = 0
        self.last_url: str | None = None
        self._start = time.monotonic()
        self._tabs: dict[int, int] = {}  # id(page) -> tab number
        self._listeners: list[tuple[Any, str, Callable]] = []
        self._handler = _Handler(self)
        self._closed = False

    def add(self, text: str) -> None:
        if len(self.lines) == self.lines.maxlen:
            self.dropped += 1
        self.lines.append(f"{time.monotonic() - self._start:8.2f}s  {text}")

    def text(self) -> str:
        head = [f"[{self.dropped} earlier lines dropped]"] if self.dropped else []
        return "\n".join(head + list(self.lines))

    # -- wiring -----------------------------------------------------------------------

    def __enter__(self) -> "Trail":
        logging.getLogger("lifeapi").addHandler(self._handler)
        self._on(self.context, "page", self._watch)
        for page in self.context.pages:
            self._watch(page, opened=False)
        return self

    def __exit__(self, *exc: Any) -> None:
        self._closed = True
        logging.getLogger("lifeapi").removeHandler(self._handler)
        for emitter, event, fn in self._listeners:
            try:
                emitter.remove_listener(event, fn)
            except Exception:
                pass
        self._listeners.clear()

    def _on(self, emitter: Any, event: str, fn: Callable) -> None:
        def safe(*args: Any) -> None:
            if self._closed:
                return
            try:
                fn(*args)
            except Exception as e:  # never let the trail break the scrape
                self.add(f"(trail: couldn't record {event}: {e})")

        emitter.on(event, safe)
        self._listeners.append((emitter, event, safe))

    def _watch(self, page: Page, opened: bool = True) -> None:
        n = self._tabs.setdefault(id(page), len(self._tabs) + 1)
        tab = f"tab {n}"
        if opened:
            self.add(f"{tab} opened")
        elif page.url and page.url != "about:blank":
            self.add(f"{tab} already open at {redact(page.url)}")

        def navigated(frame: Any) -> None:
            if frame == page.main_frame:
                self.last_url = redact(frame.url)
                self.add(f"{tab} → {self.last_url}")

        def response(r: Any) -> None:
            if r.request.resource_type in _RESPONSE_TYPES or r.status >= 400:
                self.add(f"{tab} ← {r.status} {r.request.method} {redact(r.url)}")

        def failed(req: Any) -> None:
            self.add(f"{tab} ✗ {req.method} {redact(req.url)}: {req.failure}")

        def console(msg: Any) -> None:
            if msg.type == "error":
                self.add(f"{tab} console error: {msg.text[:500]}")

        self._on(page, "framenavigated", navigated)
        self._on(page, "response", response)
        self._on(page, "requestfailed", failed)
        self._on(page, "console", console)
        self._on(page, "pageerror", lambda err: self.add(f"{tab} page error: {str(err)[:500]}"))
        self._on(page, "close", lambda _: self.add(f"{tab} closed"))
