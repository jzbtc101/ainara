# Ainara AI Companion Framework Project
# Copyright (C) 2025 Rubén Gómez - khromalabs.org
#
# This file is dual-licensed under:
# 1. GNU Lesser General Public License v3.0 (LGPL-3.0)
#    (See the included LICENSE_LGPL3.txt file or look into
#    <https://www.gnu.org/licenses/lgpl-3.0.html> for details)
# 2. Commercial license
#    (Contact: rgomez@khromalabs.org for licensing options)
#
# You may use, distribute and modify this code under the terms of either license.
# This notice must be preserved in all copies or substantial portions of the code.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# Lesser General Public License for more details.

"""iCalendar parsing for tools_calendar's feed import.

Turns an .ics document into a flat list of concrete occurrences, already
normalized to the naive local wall-clock time the rest of the calendar skill
stores. Recurrence is EXPANDED here rather than translated into the skill's
own recur_rules model on purpose: real feeds use BYMONTH, BYMONTHDAY, BYSETPOS,
multiple BYDAY values and COUNT/UNTIL, none of which that model can express,
so translating would silently produce wrong dates.

Kept out of the skills package proper (capability discovery globs '*/*.py'
under the skills directory, so a helper sitting one level deeper is not
mistaken for a skill).
"""

import logging
from datetime import date, datetime, time, timedelta

from dateutil.rrule import rrulestr
from dateutil.tz import tzlocal
from icalendar import Calendar

logger = logging.getLogger(__name__)

# A malformed or unbounded rule (FREQ=SECONDLY with no UNTIL/COUNT) would
# otherwise expand until memory runs out.
MAX_OCCURRENCES_PER_EVENT = 2000


def _to_naive_local(value):
    """Normalize an icalendar date/datetime to naive local wall-clock time.

    Returns (datetime, is_all_day). Everything else in tools_calendar compares
    against datetime.now(), which is naive, so an aware value has to be
    converted rather than merely stripped of its tzinfo.
    """
    # datetime is a subclass of date, so it has to be tested first.
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone().replace(tzinfo=None), False
        return value, False
    if isinstance(value, date):
        return datetime.combine(value, time.min), True
    raise TypeError(f"unsupported date value: {value!r}")


def _exdates(comp):
    """Collect EXDATE values, which icalendar may hand back singly or as a list."""
    raw = comp.get("EXDATE")
    if not raw:
        return set()
    items = raw if isinstance(raw, list) else [raw]
    out = set()
    for item in items:
        for d in getattr(item, "dts", []):
            try:
                out.add(_to_naive_local(d.dt)[0])
            except TypeError:
                continue
    return out


def _span(comp, start_raw):
    """Work out (start, end, all_day) for one VEVENT component."""
    start, all_day = _to_naive_local(start_raw)

    end_prop = comp.get("DTEND")
    if end_prop is not None:
        end, end_all_day = _to_naive_local(end_prop.dt)
        if all_day or end_all_day:
            # DTEND is exclusive for all-day events: an event on the 14th
            # carries DTEND=15th. Pull it back so it renders on its own day
            # instead of bleeding into the next one.
            end = max(end - timedelta(days=1), start)
            end = datetime.combine(end.date(), time(23, 59))
        return start, end, all_day

    dur_prop = comp.get("DURATION")
    if dur_prop is not None:
        return start, start + dur_prop.dt, all_day

    if all_day:
        return start, datetime.combine(start.date(), time(23, 59)), True
    return start, start + timedelta(hours=1), False


def _text(comp, key):
    value = comp.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _base_fields(comp):
    return {
        "title": _text(comp, "SUMMARY") or "(untitled)",
        "description": _text(comp, "DESCRIPTION"),
        "location": _text(comp, "LOCATION"),
    }


def parse_ics(text, window_start, window_end,
              max_occurrences=MAX_OCCURRENCES_PER_EVENT):
    """Expand an .ics document into concrete occurrences inside a window.

    Args:
        text: the raw .ics document.
        window_start / window_end: naive local datetimes bounding the import.
        max_occurrences: per-rule safety cap.

    Returns:
        (events, stats) where events is a list of dicts carrying uid, title,
        description, location, start_dt, end_dt (ISO strings) and all_day.
    """
    cal = Calendar.from_ical(text)
    stats = {"vevents": 0, "masters": 0, "overrides": 0,
             "expanded": 0, "skipped": 0, "errors": 0}

    masters, overrides = [], []
    for comp in cal.walk("VEVENT"):
        stats["vevents"] += 1
        try:
            if comp.get("DTSTART") is None:
                stats["skipped"] += 1
                continue
            if str(comp.get("STATUS") or "").upper() == "CANCELLED":
                stats["skipped"] += 1
                continue
            (overrides if comp.get("RECURRENCE-ID") is not None
             else masters).append(comp)
        except Exception:
            stats["errors"] += 1

    out = []
    # A RECURRENCE-ID event replaces one occurrence of its series, so the
    # matching generated occurrence has to be suppressed below.
    replaced = set()

    for comp in overrides:
        try:
            uid = str(comp.get("UID") or "")
            rid, _ = _to_naive_local(comp.get("RECURRENCE-ID").dt)
            replaced.add((uid, rid))
            start, end, all_day = _span(comp, comp.get("DTSTART").dt)
            if window_start <= start <= window_end:
                out.append({**_base_fields(comp), "uid": uid,
                            "start_dt": start.isoformat(),
                            "end_dt": end.isoformat(),
                            "all_day": all_day})
            stats["overrides"] += 1
        except Exception as e:
            logger.warning(f"ICS: skipping malformed override: {e}")
            stats["errors"] += 1

    for comp in masters:
        try:
            uid = str(comp.get("UID") or "")
            start, end, all_day = _span(comp, comp.get("DTSTART").dt)
            duration = end - start
            fields = _base_fields(comp)
            rrule = comp.get("RRULE")
            stats["masters"] += 1

            if rrule is None:
                if window_start <= start <= window_end:
                    out.append({**fields, "uid": uid,
                                "start_dt": start.isoformat(),
                                "end_dt": end.isoformat(),
                                "all_day": all_day})
                    stats["expanded"] += 1
                continue

            excluded = _exdates(comp)

            # Expand in the event's OWN timezone, not in local time. Two
            # reasons: a recurring 09:00 Madrid meeting must stay 09:00 Madrid
            # rather than drifting when Madrid and the local zone change DST on
            # different dates; and dateutil rejects an rrule whose UNTIL is UTC
            # while its dtstart is naive, which is exactly what feeds contain.
            raw_start = comp.get("DTSTART").dt
            rr_start = raw_start if isinstance(raw_start, datetime) else start
            if rr_start.tzinfo is not None:
                lo = window_start.replace(tzinfo=tzlocal())
                hi = window_end.replace(tzinfo=tzlocal())
            else:
                lo, hi = window_start, window_end

            rule = rrulestr(rrule.to_ical().decode(), dtstart=rr_start)

            count = 0
            # between() is inclusive of the bounds and already ordered; the
            # cap guards against a rule with neither UNTIL nor COUNT.
            for occ in rule.between(lo, hi, inc=True):
                if count >= max_occurrences:
                    logger.warning(
                        f"ICS: occurrence cap hit for uid={uid}; truncating.")
                    break
                if occ.tzinfo is not None:
                    occ = occ.astimezone().replace(tzinfo=None)
                if occ in excluded or (uid, occ) in replaced:
                    continue
                out.append({**fields, "uid": uid,
                            "start_dt": occ.isoformat(),
                            "end_dt": (occ + duration).isoformat(),
                            "all_day": all_day})
                count += 1
            stats["expanded"] += count
        except Exception as e:
            logger.warning(f"ICS: skipping malformed event: {e}")
            stats["errors"] += 1

    return out, stats


def read_ics_bytes(raw):
    """Decode an .ics payload, or pull the first .ics out of a .zip export.

    Google's "export" gives a zip containing one .ics per calendar, while a
    subscribed secret URL gives the .ics directly; accept either.
    """
    import io
    import zipfile

    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".ics")]
            if not names:
                raise ValueError("zip archive contains no .ics file")
            raw = z.read(names[0])
    return raw.decode("utf-8", "replace")
