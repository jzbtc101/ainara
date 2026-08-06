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

import json
import logging
import platform
import subprocess
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional

from ainara.framework.config import ConfigManager
from ainara.framework.skill import Skill

_TOAST_PS_SCRIPT = r"""param(
    [string]$Title,
    [string]$Message
)
# Raw WinRT ToastNotificationManager::CreateToastNotifier(appId) requires a
# registered AUMID (normally via a Start Menu shortcut) to actually render;
# without one it fails silently. NotifyIcon's balloon tip needs no such
# registration and Windows 10/11 render it as a real toast notification.
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = [System.Drawing.SystemIcons]::Information
$notify.Visible = $true
$notify.BalloonTipTitle = $Title
$notify.BalloonTipText = $Message
$notify.ShowBalloonTip(10000)
Start-Sleep -Seconds 5
$notify.Dispose()
"""

# -Command with trailing positional args doesn't reliably bind to a
# scriptblock's param() (here-strings get mis-tokenized); -File does, so the
# script is written out once and invoked by path instead.
_TOAST_PS_SCRIPT_PATH = Path(tempfile.gettempdir()) / "ainara_toast_notify.ps1"


class ToolsCalendar(Skill):
    """Native local calendar with full event and recurrence management"""

    matcher_info = (
        "Use this skill when the user wants to manage calendar events,"
        " appointments, reminders, or recurring schedules. This skill handles"
        " creating, reading, updating, and deleting events, as well as setting"
        " up repeating patterns. Examples include: 'add a meeting Friday 3pm',"
        " 'what have I got tomorrow?', 'clear my Thursday afternoon',"
        " 'set up a recurring event every 2 weeks on Tuesday',"
        " 'remind me every Monday at 9am', 'show me this week',"
        " 'cancel next Tuesday's appointment', 'edit all future occurrences'.\n\n"
        "Keywords: calendar, event, appointment, meeting, reminder, schedule,"
        " recurring, repeat, weekly, monthly, daily, every x weeks, agenda,"
        " add event, delete event, update event, show week, show month,"
        " what's on, upcoming, all day, multi-day, recurrence, exception,"
        " shift pattern, rotation."
    )

    DB_FILE = "ainara_calendar.db"

    RECURRENCE_FREQUENCIES = ["daily", "weekly", "every_x_weeks", "monthly", "custom"]

    CATEGORIES = ["personal", "work", "family", "health", "shift", "holiday", "other"]

    def __init__(self):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self.config = ConfigManager()
        # Use the project's platform-specific data directory (e.g. Saved
        # Games\Ainara\Data on Windows) rather than a hardcoded home-dir
        # path, so this stays off any OneDrive-synced folder.
        self.db_path = (
            Path(self.config.get_default_data_dir()) / "calendar" / self.DB_FILE
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

        lookahead = self.config.get(
            "skills.tools_calendar.reminder_lookahead_minutes", 15
        )
        check_interval = self.config.get(
            "skills.tools_calendar.reminder_check_interval_minutes", 5
        )
        self.default_schedule = {
            "trigger": "interval",
            "minutes": check_interval,
            "kwargs": {"action": "check_reminders"},
        }
        self._reminder_lookahead_minutes = lookahead

    def _get_conn(self) -> sqlite3.Connection:
        """Returns a database connection with row factory set"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Initialise the SQLite database schema"""
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    title       TEXT NOT NULL,
                    description TEXT,
                    location    TEXT,
                    start_dt    TEXT NOT NULL,
                    end_dt      TEXT NOT NULL,
                    all_day     INTEGER DEFAULT 0,
                    category    TEXT DEFAULT 'personal',
                    priority    TEXT DEFAULT 'normal',
                    notes       TEXT,
                    recur_rule_id INTEGER,
                    is_exception  INTEGER DEFAULT 0,
                    created_at  TEXT DEFAULT (datetime('now')),
                    updated_at  TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS recur_rules (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    base_event_id INTEGER NOT NULL,
                    frequency   TEXT NOT NULL,
                    interval    INTEGER DEFAULT 1,
                    days_of_week TEXT,
                    end_date    TEXT,
                    max_count   INTEGER,
                    exceptions  TEXT DEFAULT '[]',
                    created_at  TEXT DEFAULT (datetime('now'))
                );

                CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_dt);
                CREATE INDEX IF NOT EXISTS idx_events_recur  ON events(recur_rule_id);

                CREATE TABLE IF NOT EXISTS notified_reminders (
                    event_id    INTEGER NOT NULL,
                    start_dt    TEXT NOT NULL,
                    notified_at TEXT DEFAULT (datetime('now')),
                    PRIMARY KEY (event_id, start_dt)
                );
            """)
            conn.commit()
        finally:
            conn.close()

    # ─────────────────────────────────────────────
    # INTERNAL HELPERS
    # ─────────────────────────────────────────────

    def _expand_recurrences(
        self, base_event: dict, rule: dict, from_dt: datetime, to_dt: datetime
    ) -> List[dict]:
        """Expand a recurrence rule into concrete event occurrences within range"""
        occurrences = []
        exceptions = json.loads(rule["exceptions"] or "[]")
        freq = rule["frequency"]
        interval = rule["interval"] or 1
        days_of_week = json.loads(rule["days_of_week"] or "[]")

        start = datetime.fromisoformat(base_event["start_dt"])
        end = datetime.fromisoformat(base_event["end_dt"])
        duration = end - start

        rule_end = datetime.fromisoformat(rule["end_date"]) if rule["end_date"] else None
        max_count = rule["max_count"]
        count = 0

        def emit(dt: datetime):
            nonlocal count
            occ = dict(base_event)
            occ["start_dt"] = dt.isoformat()
            occ["end_dt"] = (dt + duration).isoformat()
            occ["is_occurrence"] = True
            occurrences.append(occ)
            count += 1

        if freq == "monthly":
            current = start
            while current <= to_dt:
                if rule_end and current > rule_end:
                    break
                if max_count and count >= max_count:
                    break
                if current >= from_dt and current.isoformat() not in exceptions:
                    emit(current)
                month = current.month + interval
                year = current.year + (month - 1) // 12
                month = (month - 1) % 12 + 1
                try:
                    current = current.replace(year=year, month=month)
                except ValueError:
                    break
            return occurrences

        # daily / weekly / every_x_weeks / custom: step one day at a time so a
        # multi-day 'days_of_week' filter (e.g. Monday AND Wednesday) can
        # match more than one weekday per cycle. The previous implementation
        # advanced the cursor by whole weeks, so it could only ever land on
        # the same weekday as the first occurrence, silently dropping every
        # other day in the list.
        #
        # Weekly-style frequencies gate on whole-week cadence measured from
        # the start date's own week (Monday-aligned); daily gates on a plain
        # N-day cadence from the start date.
        week_anchor = start - timedelta(days=start.weekday())
        current_day = start
        while current_day <= to_dt:
            if rule_end and current_day > rule_end:
                break
            if max_count and count >= max_count:
                break

            if freq == "daily":
                cadence_ok = (current_day.date() - start.date()).days % interval == 0
                day_ok = (not days_of_week) or (
                    current_day.strftime("%A").lower() in days_of_week
                )
            else:
                weeks_since_anchor = (
                    current_day.date() - week_anchor.date()
                ).days // 7
                cadence_ok = weeks_since_anchor % interval == 0
                if days_of_week:
                    day_ok = current_day.strftime("%A").lower() in days_of_week
                else:
                    day_ok = current_day.weekday() == start.weekday()

            if (
                cadence_ok
                and day_ok
                and current_day >= from_dt
                and current_day.isoformat() not in exceptions
            ):
                emit(current_day)

            current_day += timedelta(days=1)

        return occurrences

    def _split_series_for_edit(
        self,
        cursor: sqlite3.Cursor,
        event: dict,
        rule: dict,
        original_dt: datetime,
        fields: dict,
    ) -> Dict[str, Any]:
        """Split a recurring series at `original_dt` for an 'all_future' edit.

        Truncates the existing rule so it stops generating occurrences at/after
        `original_dt`, then creates a new base event + rule (carrying `fields`'
        overrides) that continues the same cadence from `original_dt` onward.
        Any 'this_only' exception rows already at/after the split point are
        reassigned to the new rule so they aren't orphaned or duplicated.
        """
        base_start = datetime.fromisoformat(event["start_dt"])
        base_end = datetime.fromisoformat(event["end_dt"])
        duration = base_end - base_start
        cutoff = original_dt - timedelta(seconds=1)

        # Never let truncation push the old rule's end *later* than it
        # already was (e.g. splitting past a series that already ended).
        old_end = datetime.fromisoformat(rule["end_date"]) if rule["end_date"] else None
        truncated_end = cutoff if (old_end is None or cutoff < old_end) else old_end

        # The new rule only needs to cover whatever occurrences are left.
        old_max_count = rule["max_count"]
        new_max_count = None
        if old_max_count:
            prior = self._expand_recurrences(event, rule, base_start, cutoff)
            new_max_count = max(old_max_count - len(prior), 0)

        old_exceptions = json.loads(rule["exceptions"] or "[]")
        moved_exceptions = [
            x for x in old_exceptions if datetime.fromisoformat(x) >= original_dt
        ]
        kept_exceptions = [
            x for x in old_exceptions if datetime.fromisoformat(x) < original_dt
        ]

        cursor.execute(
            "UPDATE recur_rules SET end_date=?, exceptions=? WHERE id=?",
            (truncated_end.isoformat(), json.dumps(kept_exceptions), rule["id"]),
        )

        occ_start = datetime.fromisoformat(fields.get("start_dt", original_dt.isoformat()))
        occ_end = (
            datetime.fromisoformat(fields["end_dt"])
            if "end_dt" in fields
            else occ_start + duration
        )
        new_base = {
            **event,
            **fields,
            "start_dt": occ_start.isoformat(),
            "end_dt": occ_end.isoformat(),
        }

        cursor.execute("""
            INSERT INTO events
                (title, description, location, start_dt, end_dt,
                 category, priority, notes)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            new_base["title"], new_base.get("description"), new_base.get("location"),
            new_base["start_dt"], new_base["end_dt"], new_base.get("category"),
            new_base.get("priority"), new_base.get("notes"),
        ))
        new_base_id = cursor.lastrowid

        cursor.execute("""
            INSERT INTO recur_rules
                (base_event_id, frequency, interval, days_of_week,
                 end_date, max_count, exceptions)
            VALUES (?,?,?,?,?,?,?)
        """, (
            new_base_id, rule["frequency"], rule["interval"], rule["days_of_week"],
            rule["end_date"], new_max_count, json.dumps(moved_exceptions),
        ))
        new_rule_id = cursor.lastrowid

        cursor.execute(
            "UPDATE events SET recur_rule_id=? WHERE id=?", (new_rule_id, new_base_id)
        )
        cursor.execute(
            "UPDATE events SET recur_rule_id=?"
            " WHERE recur_rule_id=? AND is_exception=1 AND start_dt>=?",
            (new_rule_id, rule["id"], original_dt.isoformat()),
        )

        return {"success": True, "new_event_id": new_base_id, "new_rule_id": new_rule_id}

    # ─────────────────────────────────────────────
    # PUBLIC SKILL METHODS
    # ─────────────────────────────────────────────

    async def create_event(
        self,
        title: Annotated[str, "Title or name of the event"],
        start_dt: Annotated[str, "Start date/time in ISO format (YYYY-MM-DDTHH:MM)"],
        end_dt: Annotated[str, "End date/time in ISO format (YYYY-MM-DDTHH:MM)"],
        description: Annotated[Optional[str], "Optional description"] = None,
        location: Annotated[Optional[str], "Optional location"] = None,
        all_day: Annotated[Optional[bool], "True if this is an all-day event"] = False,
        category: Annotated[Optional[str], "Category: personal/work/family/health/shift/holiday/other"] = "personal",
        priority: Annotated[Optional[str], "Priority: low/normal/high"] = "normal",
        notes: Annotated[Optional[str], "Additional notes"] = None,
    ) -> Dict[str, Any]:
        """Creates a single calendar event"""
        self.logger.info(f"CALENDAR CREATE: {title} @ {start_dt}")
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO events
                    (title, description, location, start_dt, end_dt,
                     all_day, category, priority, notes)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (title, description, location, start_dt, end_dt,
                  int(all_day), category, priority, notes))
            conn.commit()
            event_id = cursor.lastrowid
            return {
                "success": True,
                "event_id": event_id,
                "message": f"Event '{title}' created on {start_dt}",
            }
        except Exception as e:
            return {"success": False, "error": "Failed to create event", "details": str(e)}
        finally:
            conn.close()

    async def create_recurring_event(
        self,
        title: Annotated[str, "Title of the recurring event"],
        start_dt: Annotated[str, "First occurrence start datetime (ISO format)"],
        end_dt: Annotated[str, "First occurrence end datetime (ISO format)"],
        frequency: Annotated[str, "Recurrence frequency: daily/weekly/every_x_weeks/monthly"],
        interval: Annotated[int, "Repeat every N units (e.g. 2 for every 2 weeks)"] = 1,
        days_of_week: Annotated[Optional[List[str]], "Specific days e.g. ['monday','wednesday']"] = None,
        end_date: Annotated[Optional[str], "Stop recurring after this date (ISO format)"] = None,
        max_count: Annotated[Optional[int], "Stop after this many occurrences"] = None,
        description: Annotated[Optional[str], "Optional description"] = None,
        location: Annotated[Optional[str], "Optional location"] = None,
        category: Annotated[Optional[str], "Category: personal/work/family/health/shift/holiday/other"] = "personal",
        priority: Annotated[Optional[str], "Priority: low/normal/high"] = "normal",
        notes: Annotated[Optional[str], "Additional notes"] = None,
    ) -> Dict[str, Any]:
        """Creates a recurring calendar event with a recurrence rule"""
        self.logger.info(f"CALENDAR RECURRING CREATE: {title} freq={frequency} interval={interval}")
        conn = self._get_conn()
        try:
            cursor = conn.cursor()

            # Insert the base event
            cursor.execute("""
                INSERT INTO events
                    (title, description, location, start_dt, end_dt,
                     category, priority, notes)
                VALUES (?,?,?,?,?,?,?,?)
            """, (title, description, location, start_dt, end_dt,
                  category, priority, notes))
            base_id = cursor.lastrowid

            # Insert the recurrence rule
            cursor.execute("""
                INSERT INTO recur_rules
                    (base_event_id, frequency, interval, days_of_week,
                     end_date, max_count)
                VALUES (?,?,?,?,?,?)
            """, (base_id, frequency, interval,
                  json.dumps(days_of_week or []),
                  end_date, max_count))
            rule_id = cursor.lastrowid

            # Link rule back to base event
            cursor.execute(
                "UPDATE events SET recur_rule_id=? WHERE id=?",
                (rule_id, base_id)
            )
            conn.commit()
            return {
                "success": True,
                "event_id": base_id,
                "rule_id": rule_id,
                "message": (
                    f"Recurring event '{title}' created. "
                    f"Repeats {frequency} every {interval} unit(s)."
                ),
            }
        except Exception as e:
            return {"success": False, "error": "Failed to create recurring event", "details": str(e)}
        finally:
            conn.close()

    async def get_events(
        self,
        from_date: Annotated[str, "Start of date range (ISO format YYYY-MM-DD)"],
        to_date: Annotated[str, "End of date range (ISO format YYYY-MM-DD)"],
        category: Annotated[Optional[str], "Filter by category"] = None,
    ) -> Dict[str, Any]:
        """Retrieves all events (including recurring occurrences) within a date range"""
        self.logger.info(f"CALENDAR GET: {from_date} to {to_date}")
        conn = self._get_conn()
        try:
            from_dt = datetime.fromisoformat(from_date)
            to_dt = datetime.fromisoformat(to_date).replace(hour=23, minute=59)

            cursor = conn.cursor()

            # Fetch one-off events in range
            query = """
                SELECT * FROM events
                WHERE start_dt BETWEEN ? AND ?
                AND (recur_rule_id IS NULL OR is_exception=1)
            """
            params = [from_date, to_date + "T23:59"]
            if category:
                query += " AND category=?"
                params.append(category)

            cursor.execute(query, params)
            events = [dict(row) for row in cursor.fetchall()]

            # Fetch and expand recurring events
            cursor.execute("SELECT * FROM recur_rules")
            rules = [dict(row) for row in cursor.fetchall()]

            for rule in rules:
                cursor.execute(
                    "SELECT * FROM events WHERE id=?", (rule["base_event_id"],)
                )
                base = cursor.fetchone()
                if base:
                    base_dict = dict(base)
                    if category and base_dict.get("category") != category:
                        continue
                    occurrences = self._expand_recurrences(
                        base_dict, rule, from_dt, to_dt
                    )
                    events.extend(occurrences)

            events.sort(key=lambda e: e["start_dt"])
            return {
                "success": True,
                "count": len(events),
                "events": events,
            }
        except Exception as e:
            return {"success": False, "error": "Failed to retrieve events", "details": str(e)}
        finally:
            conn.close()

    async def update_event(
        self,
        event_id: Annotated[int, "ID of the event to update"],
        title: Annotated[Optional[str], "New title"] = None,
        start_dt: Annotated[Optional[str], "New start datetime (ISO format)"] = None,
        end_dt: Annotated[Optional[str], "New end datetime (ISO format)"] = None,
        description: Annotated[Optional[str], "New description"] = None,
        location: Annotated[Optional[str], "New location"] = None,
        category: Annotated[Optional[str], "New category"] = None,
        priority: Annotated[Optional[str], "New priority"] = None,
        notes: Annotated[Optional[str], "New notes"] = None,
        edit_mode: Annotated[Optional[str], "For recurring: 'this_only', 'all_future', or 'all'"] = "this_only",
        occurrence_dt: Annotated[
            Optional[str],
            "Required for edit_mode='this_only' or 'all_future' on a recurring"
            " event: the ISO datetime of the specific occurrence being edited"
            " (as returned by get_events). Without it, the series' first"
            " occurrence is assumed.",
        ] = None,
    ) -> Dict[str, Any]:
        """Updates an existing event. For recurring events, choose to edit just this occurrence or all future ones."""
        self.logger.info(f"CALENDAR UPDATE: event_id={event_id} mode={edit_mode}")
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM events WHERE id=?", (event_id,))
            event = cursor.fetchone()
            if not event:
                return {"success": False, "error": f"Event ID {event_id} not found"}

            event = dict(event)
            fields = {}
            if title:       fields["title"] = title
            if start_dt:    fields["start_dt"] = start_dt
            if end_dt:      fields["end_dt"] = end_dt
            if description: fields["description"] = description
            if location:    fields["location"] = location
            if category:    fields["category"] = category
            if priority:    fields["priority"] = priority
            if notes:       fields["notes"] = notes
            fields["updated_at"] = datetime.now().isoformat()

            if event["recur_rule_id"] and edit_mode == "this_only":
                # All occurrences of a recurring event share the base event's
                # id, since they're expanded on the fly rather than stored as
                # rows. occurrence_dt is what disambiguates *which* occurrence
                # is being edited; without it we can only assume the series'
                # own start (first occurrence).
                base_start = datetime.fromisoformat(event["start_dt"])
                base_end = datetime.fromisoformat(event["end_dt"])
                duration = base_end - base_start

                original_dt = occurrence_dt or event["start_dt"]
                occ_start = datetime.fromisoformat(original_dt)
                occ_end = occ_start + duration

                # Keep the occurrence's own date/time unless the caller is
                # explicitly moving it.
                fields.setdefault("start_dt", occ_start.isoformat())
                fields.setdefault("end_dt", occ_end.isoformat())

                # Create an exception event for this single occurrence
                base = {**event, **fields}
                cursor.execute("""
                    INSERT INTO events
                        (title, description, location, start_dt, end_dt,
                         category, priority, notes, recur_rule_id, is_exception)
                    VALUES (?,?,?,?,?,?,?,?,?,1)
                """, (
                    base["title"], base.get("description"), base.get("location"),
                    base["start_dt"], base["end_dt"], base.get("category"),
                    base.get("priority"), base.get("notes"), event["recur_rule_id"]
                ))
                # Add the original occurrence start as an exception in the rule
                cursor.execute(
                    "SELECT * FROM recur_rules WHERE id=?", (event["recur_rule_id"],)
                )
                rule = dict(cursor.fetchone())
                exceptions = json.loads(rule["exceptions"] or "[]")
                exceptions.append(occ_start.isoformat())
                cursor.execute(
                    "UPDATE recur_rules SET exceptions=? WHERE id=?",
                    (json.dumps(exceptions), rule["id"])
                )
            elif event["recur_rule_id"] and edit_mode == "all_future":
                # Split the series: the old rule stops at the occurrence
                # before this one, and a new rule (carrying the requested
                # changes) picks up from here with the same cadence.
                original_dt = datetime.fromisoformat(occurrence_dt or event["start_dt"])
                cursor.execute(
                    "SELECT * FROM recur_rules WHERE id=?", (event["recur_rule_id"],)
                )
                rule = dict(cursor.fetchone())
                split_result = self._split_series_for_edit(
                    cursor, event, rule, original_dt, fields
                )
                if not split_result["success"]:
                    return split_result
            else:
                # Update the base event directly (covers 'all' and non-recurring events)
                set_clause = ", ".join(f"{k}=?" for k in fields)
                cursor.execute(
                    f"UPDATE events SET {set_clause} WHERE id=?",
                    (*fields.values(), event_id)
                )

            conn.commit()
            return {"success": True, "message": f"Event ID {event_id} updated ({edit_mode})"}
        except Exception as e:
            return {"success": False, "error": "Failed to update event", "details": str(e)}
        finally:
            conn.close()

    async def delete_event(
        self,
        event_id: Annotated[int, "ID of the event to delete"],
        delete_mode: Annotated[Optional[str], "For recurring: 'this_only' or 'all_future' or 'all'"] = "this_only",
        occurrence_dt: Annotated[
            Optional[str],
            "ISO datetime of the specific occurrence, required for"
            " delete_mode='this_only' or 'all_future' on a recurring event",
        ] = None,
    ) -> Dict[str, Any]:
        """Deletes an event. For recurring events, choose scope of deletion."""
        self.logger.info(f"CALENDAR DELETE: event_id={event_id} mode={delete_mode}")
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM events WHERE id=?", (event_id,))
            event = cursor.fetchone()
            if not event:
                return {"success": False, "error": f"Event ID {event_id} not found"}

            event = dict(event)

            if event["recur_rule_id"] and delete_mode == "this_only" and occurrence_dt:
                # Add occurrence to exceptions list
                cursor.execute(
                    "SELECT * FROM recur_rules WHERE id=?", (event["recur_rule_id"],)
                )
                rule = dict(cursor.fetchone())
                exceptions = json.loads(rule["exceptions"] or "[]")
                exceptions.append(occurrence_dt)
                cursor.execute(
                    "UPDATE recur_rules SET exceptions=? WHERE id=?",
                    (json.dumps(exceptions), rule["id"])
                )
            elif event["recur_rule_id"] and delete_mode == "all_future":
                if not occurrence_dt:
                    return {
                        "success": False,
                        "error": "occurrence_dt is required for delete_mode='all_future'",
                    }
                cursor.execute(
                    "SELECT * FROM recur_rules WHERE id=?", (event["recur_rule_id"],)
                )
                rule = dict(cursor.fetchone())
                cutoff = datetime.fromisoformat(occurrence_dt) - timedelta(seconds=1)
                # Never push the rule's end *later* than it already was.
                old_end = (
                    datetime.fromisoformat(rule["end_date"]) if rule["end_date"] else None
                )
                new_end = cutoff if (old_end is None or cutoff < old_end) else old_end
                cursor.execute(
                    "UPDATE recur_rules SET end_date=? WHERE id=?",
                    (new_end.isoformat(), rule["id"])
                )
                # Drop any single-occurrence exceptions from this point on;
                # they'd otherwise keep showing up even though the series
                # that spawned them no longer generates that far.
                cursor.execute(
                    "DELETE FROM events WHERE recur_rule_id=? AND is_exception=1 AND start_dt>=?",
                    (event["recur_rule_id"], occurrence_dt)
                )
            elif event["recur_rule_id"] and delete_mode == "all":
                # Delete rule and all related events
                cursor.execute(
                    "DELETE FROM events WHERE id=? OR recur_rule_id=?",
                    (event_id, event["recur_rule_id"])
                )
                cursor.execute(
                    "DELETE FROM recur_rules WHERE id=?", (event["recur_rule_id"],)
                )
            else:
                cursor.execute("DELETE FROM events WHERE id=?", (event_id,))

            conn.commit()
            return {
                "success": True,
                "message": f"Event ID {event_id} deleted (mode: {delete_mode})",
            }
        except Exception as e:
            return {"success": False, "error": "Failed to delete event", "details": str(e)}
        finally:
            conn.close()

    def _show_desktop_notification(self, title: str, message: str) -> bool:
        """Fire a native OS desktop notification. Windows only for now."""
        if platform.system() != "Windows":
            self.logger.warning(
                "Desktop notifications are only implemented for Windows;"
                f" skipping notification '{title}'."
            )
            return False
        try:
            if not _TOAST_PS_SCRIPT_PATH.exists():
                _TOAST_PS_SCRIPT_PATH.write_text(_TOAST_PS_SCRIPT, encoding="utf-8")
            subprocess.run(
                [
                    "powershell", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass",
                    "-File", str(_TOAST_PS_SCRIPT_PATH),
                    "-Title", title, "-Message", message,
                ],
                capture_output=True, timeout=10, check=True,
            )
            return True
        except Exception as e:
            self.logger.error(f"Failed to show desktop notification: {e}")
            return False

    async def check_reminders(self) -> Dict[str, Any]:
        """Finds upcoming events starting soon and fires a desktop
        notification for each one not already notified about."""
        now = datetime.now()
        window_end = now + timedelta(minutes=self._reminder_lookahead_minutes)

        events_result = await self.get_events(
            from_date=now.strftime("%Y-%m-%d"),
            to_date=window_end.strftime("%Y-%m-%d"),
        )
        if not events_result.get("success"):
            return events_result

        conn = self._get_conn()
        notified = []
        try:
            cursor = conn.cursor()
            for event in events_result["events"]:
                start = datetime.fromisoformat(event["start_dt"])
                if not (now <= start <= window_end):
                    continue

                cursor.execute(
                    "SELECT 1 FROM notified_reminders WHERE event_id=? AND start_dt=?",
                    (event["id"], event["start_dt"]),
                )
                if cursor.fetchone():
                    continue

                minutes_until = max(int((start - now).total_seconds() // 60), 0)
                message = f"Starts in {minutes_until} min"
                if event.get("location"):
                    message += f" — {event['location']}"

                if self._show_desktop_notification(
                    title=f"Upcoming: {event['title']}", message=message
                ):
                    cursor.execute(
                        "INSERT OR IGNORE INTO notified_reminders (event_id, start_dt)"
                        " VALUES (?, ?)",
                        (event["id"], event["start_dt"]),
                    )
                    notified.append({
                        "event_id": event["id"],
                        "title": event["title"],
                        "start_dt": event["start_dt"],
                    })
            conn.commit()
            return {"success": True, "notified": notified}
        except Exception as e:
            return {"success": False, "error": "Failed to check reminders", "details": str(e)}
        finally:
            conn.close()

    async def run(
        self,
        action: Annotated[str, "Action to perform: create/create_recurring/get/update/delete/check_reminders"],
        title: Annotated[Optional[str], "Event title"] = None,
        start_dt: Annotated[Optional[str], "Start datetime ISO format"] = None,
        end_dt: Annotated[Optional[str], "End datetime ISO format"] = None,
        from_date: Annotated[Optional[str], "Range start for get action (YYYY-MM-DD)"] = None,
        to_date: Annotated[Optional[str], "Range end for get action (YYYY-MM-DD)"] = None,
        event_id: Annotated[Optional[int], "Event ID for update/delete"] = None,
        frequency: Annotated[Optional[str], "Recurrence frequency"] = None,
        interval: Annotated[Optional[int], "Recurrence interval"] = 1,
        days_of_week: Annotated[Optional[List[str]], "Days of week for recurrence"] = None,
        end_date: Annotated[Optional[str], "Recurrence end date"] = None,
        max_count: Annotated[Optional[int], "Max recurrence count"] = None,
        description: Annotated[Optional[str], "Event description"] = None,
        location: Annotated[Optional[str], "Event location"] = None,
        all_day: Annotated[Optional[bool], "All day event flag"] = False,
        category: Annotated[Optional[str], "Event category"] = "personal",
        priority: Annotated[Optional[str], "Event priority"] = "normal",
        notes: Annotated[Optional[str], "Event notes"] = None,
        edit_mode: Annotated[Optional[str], "Edit mode for recurring: this_only/all_future/all"] = "this_only",
        occurrence_dt: Annotated[
            Optional[str],
            "Specific occurrence datetime (ISO format), required for"
            " action='update' or action='delete' with edit_mode='this_only'"
            " or 'all_future' on a recurring event",
        ] = None,
    ) -> Dict[str, Any]:
        """
        Main calendar skill entry point. Routes to the correct action.

        Examples:
            action="create", title="Team standup", start_dt="2026-07-28T09:00", end_dt="2026-07-28T09:30"
            action="create_recurring", title="Night Shift", frequency="every_x_weeks", interval=9
            action="get", from_date="2026-07-28", to_date="2026-08-03"
            action="update", event_id=5, title="Updated title", edit_mode="this_only", occurrence_dt="2026-08-04T09:00:00"
            action="update", event_id=5, start_dt="2026-08-11T10:00", edit_mode="all_future", occurrence_dt="2026-08-11T09:00:00"
            action="delete", event_id=5, delete_mode="all"
            action="delete", event_id=5, delete_mode="all_future", occurrence_dt="2026-08-11T09:00:00"
        """
        self.logger.info(f"CALENDAR RUN: action={action}")

        if action == "create":
            return await self.create_event(
                title=title, start_dt=start_dt, end_dt=end_dt,
                description=description, location=location,
                all_day=all_day, category=category,
                priority=priority, notes=notes,
            )
        elif action == "create_recurring":
            return await self.create_recurring_event(
                title=title, start_dt=start_dt, end_dt=end_dt,
                frequency=frequency, interval=interval,
                days_of_week=days_of_week, end_date=end_date,
                max_count=max_count, description=description,
                location=location, category=category,
                priority=priority, notes=notes,
            )
        elif action == "get":
            return await self.get_events(
                from_date=from_date, to_date=to_date, category=category
            )
        elif action == "update":
            return await self.update_event(
                event_id=event_id, title=title, start_dt=start_dt,
                end_dt=end_dt, description=description, location=location,
                category=category, priority=priority, notes=notes,
                edit_mode=edit_mode, occurrence_dt=occurrence_dt,
            )
        elif action == "delete":
            return await self.delete_event(
                event_id=event_id, delete_mode=edit_mode,
                occurrence_dt=occurrence_dt,
            )
        elif action == "check_reminders":
            return await self.check_reminders()
        else:
            return {
                "success": False,
                "error": f"Unknown action '{action}'",
                "valid_actions": [
                    "create", "create_recurring", "get", "update", "delete",
                    "check_reminders",
                ],
            }
