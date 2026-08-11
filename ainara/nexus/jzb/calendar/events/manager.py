# Nexus bundle: jzb/calendar
#
# Interactive calendar manager. This skill is the DATA half; the rendering half
# is _components/EventsManager/index.html, loaded by com-ring in a sandboxed
# iframe served from Orakle's own origin. Because it shares that origin the
# component talks to /run/tools_calendar directly for every mutation, so this
# skill only has to supply the first paint.
#
# Discovery contract (ainara/framework/capabilities/nexus.py):
#   file at <bundle>/<subdir>/<file>.py, class named
#   Vendor.capitalize() + Bundle.capitalize() + Pascal(subdir) + Pascal(file)
#   -> JzbCalendarEventsManager -> id jzb_calendar_events_manager -> component
#   EventsManager, which must exist at <bundle>/_components/EventsManager/.
#
# Discovery happens at Orakle startup: restart Orakle after editing this file.

import json
import logging
from datetime import datetime, timedelta
from typing import Annotated, Optional

from ainara.framework.skill import Skill

logger = logging.getLogger(__name__)


class JzbCalendarEventsManager(Skill):
    """Puts the calendar on screen: an interactive board with day, week and
    month views for browsing and editing events."""

    # The embedding matcher cannot separate this from tools_calendar — they
    # share the whole domain vocabulary and score within ~0.03 on every
    # phrasing — so both reach the LLM as candidates and the LLM's choice is
    # decided by this text. It therefore leans on the MEDIUM (on screen /
    # view / open / board) rather than the subject matter, and spells out the
    # user's own phrasings verbatim.
    matcher_info = (
        "THE ONLY skill that puts the user's CALENDAR OR AGENDA ON SCREEN."
        " Use it whenever the user asks to see, open, pull up, bring up, look"
        " at, browse or manage their calendar or agenda, or asks for a day"
        " view, week view or month view. It opens an interactive calendar"
        " board where events are laid out on a real time grid and can be"
        " added, edited and deleted by hand. Whenever the request is about"
        " VIEWING or WORKING WITH the calendar rather than being told one"
        " specific fact, prefer this over any text answer. Examples: 'pull up"
        " my calendar', 'open my calendar', 'show me my calendar', 'bring up"
        " the calendar', 'let me see my calendar', 'open my agenda', 'show me"
        " my agenda', 'open the calendar view', 'show me my week', 'show me"
        " this month', 'what does Tuesday look like', 'I want to edit my"
        " calendar', 'open the calendar board', 'put my schedule on screen'."
        " Keywords: calendar, agenda, open calendar, show calendar, pull up"
        " calendar, see my calendar, open agenda, calendar view, calendar"
        " board, day view, week view, month view, planner, timetable, manage"
        " events, edit calendar, on screen."
    )

    def __init__(self):
        super().__init__()
        self.logger = logger

    async def run(
        self,
        days: Annotated[
            int,
            "How many days ahead the board should load on first paint."
            " Defaults to 30.",
        ] = 30,
        category: Annotated[
            Optional[str],
            "Optionally narrow the first paint to one category"
            " (personal/work/family/health/shift/holiday/other).",
        ] = None,
    ) -> str:
        """Return the initial calendar payload as a JSON *string*.

        Nexus skills must return a string: the middleware json.loads() it and
        the component receives the decoded object via postMessage.
        """
        # Imported here rather than at module scope so a broken calendar skill
        # surfaces as a rendered error instead of dropping this whole bundle
        # out of discovery at startup.
        try:
            from ainara.orakle.skills.tools.calendar import ToolsCalendar
        except Exception as e:
            logger.exception("EventsManager: cannot import ToolsCalendar")
            return json.dumps(
                {"error": f"tools_calendar unavailable: {e}", "events": []}
            )

        today = datetime.now()
        to_date = today + timedelta(days=days)

        try:
            data = await ToolsCalendar().get_events(
                from_date=today.strftime("%Y-%m-%d"),
                to_date=to_date.strftime("%Y-%m-%d"),
                category=category,
            )
        except Exception as e:
            logger.exception("EventsManager: get_events failed")
            return json.dumps(
                {"error": f"failed to load events: {e}", "events": []}
            )

        data["range"] = {
            "from_date": today.strftime("%Y-%m-%d"),
            "to_date": to_date.strftime("%Y-%m-%d"),
            "category": category,
        }
        return json.dumps(data, default=str)
