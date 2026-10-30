"""Lecture ICS : extrait réel de Zeus et cas Outlook (fuseaux Windows, répétitions, statuts)."""

import asyncio
import traceback
from datetime import date, datetime, timedelta

import httpx
import pytest

from fouettard.planning.ics import IcsError, fetch_ics, parse_ics
from fouettard.timeutils import PARIS

FIRST, LAST = date(2026, 11, 1), date(2027, 7, 1)


def calendar(*events, timezone=""):
    return "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:test", timezone, *events,
                        "END:VCALENDAR"]).replace("\r\n\r\n", "\r\n")


ROMANCE_TIMEZONE = "\r\n".join([
    "BEGIN:VTIMEZONE", "TZID:Romance Standard Time",
    "BEGIN:STANDARD", "DTSTART:16010101T030000", "TZOFFSETFROM:+0200", "TZOFFSETTO:+0100",
    "RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=10", "END:STANDARD",
    "BEGIN:DAYLIGHT", "DTSTART:16010101T020000", "TZOFFSETFROM:+0100", "TZOFFSETTO:+0200",
    "RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=3", "END:DAYLIGHT",
    "END:VTIMEZONE"])


def event(uid, summary, start, end, *extra, tz="Romance Standard Time"):
    return "\r\n".join(["BEGIN:VEVENT", f"UID:{uid}", f"SUMMARY:{summary}",
                        f"DTSTART;TZID={tz}:{start}", f"DTEND;TZID={tz}:{end}",
                        "DTSTAMP:20261030T100000Z", *extra, "END:VEVENT"])


# Extrait du vrai flux Zeus (groupe 941)
ZEUS_SAMPLE = calendar(
    "BEGIN:VEVENT\r\nCLASS:PUBLIC\r\nDESCRIPTION:\r\nDTEND:20261110T110000Z\r\nDTSTAMP:20261030T200231Z\r\n"
    "DTSTART:20261110T080000Z\r\nLOCATION:A208\r\nSEQUENCE:0\r\nSUMMARY:Mathématiques\r\n"
    "UID:867ba384-961d-4490-ad4f-7cb3379bf8c7\r\nEND:VEVENT",
    "BEGIN:VEVENT\r\nCLASS:PUBLIC\r\nDESCRIPTION:\r\nDTEND:20270115T190000Z\r\nDTSTAMP:20261030T200231Z\r\n"
    "DTSTART:20270111T060000Z\r\nLOCATION:B06\\, B105\\, A01\\, B02\\, B03\\, B04\\, B05\\, B07\\, B08\\, B106\\, B10\r\n"
    " 7\\, B108\\, B103\\, A02\r\nSEQUENCE:0\r\nSUMMARY:ASSERTIVITE\r\n"
    "UID:63bf5e6a-24a7-400d-b93f-318b439eddc6\r\nEND:VEVENT",
    "BEGIN:VEVENT\r\nCLASS:PUBLIC\r\nDESCRIPTION:\r\nDTEND:20270329T180000Z\r\nDTSTAMP:20261030T200231Z\r\n"
    "DTSTART:20270329T040000Z\r\nLOCATION:\r\nSEQUENCE:0\r\nSUMMARY:FERIE\r\n"
    "UID:bffc52e1-bac2-40e8-8a57-d92481b05ddc\r\nEND:VEVENT",
)


def test_zeus_utc_times_become_paris_with_daylight_saving():
    events = parse_ics(ZEUS_SAMPLE, "zeus", FIRST, LAST)
    maths = next(e for e in events if e.title == "Mathématiques")
    assert maths.start == datetime(2026, 11, 10, 9, 0, tzinfo=PARIS)  # hiver : UTC+1
    assert maths.start.utcoffset() == timedelta(hours=1)
    ferie = next(e for e in events if e.title == "FERIE")
    assert ferie.start.hour == 6  # après le passage à l'heure d'été : UTC+2
    assert ferie.start.utcoffset() == timedelta(hours=2)


def test_zeus_folded_location_is_rebuilt():
    assertivite = next(e for e in parse_ics(ZEUS_SAMPLE, "zeus", FIRST, LAST)
                       if e.title == "ASSERTIVITE")
    assert "B107" in assertivite.location
    assert assertivite.location.count(",") == 13


def test_windows_timezone_with_description():
    text = calendar(event("a", "Réunion", "20261215T100000", "20261215T110000"),
                    timezone=ROMANCE_TIMEZONE)
    found = parse_ics(text, "outlook", FIRST, LAST)[0]
    assert found.start.hour == 10
    assert found.start.utcoffset() == timedelta(hours=1)


def test_windows_timezone_summer_with_description():
    text = calendar(event("a", "Réunion", "20270615T100000", "20270615T110000"),
                    timezone=ROMANCE_TIMEZONE)
    found = parse_ics(text, "outlook", FIRST, LAST)[0]
    assert found.start.hour == 10
    assert found.start.utcoffset() == timedelta(hours=2)


def test_windows_timezone_without_description():
    text = calendar(event("a", "Réunion", "20270615T100000", "20270615T110000"))
    found = parse_ics(text, "outlook", FIRST, LAST)[0]
    assert (found.start.hour, found.start.utcoffset()) == (10, timedelta(hours=2))


def test_central_europe_timezone_used_by_the_real_outlook_feed():
    text = calendar(event("a", "Réunion", "20261103T130000", "20261103T133000",
                          "STATUS:CONFIRMED", "X-MICROSOFT-CDO-BUSYSTATUS:TENTATIVE",
                          tz="Central Europe Standard Time"))
    found = parse_ics(text, "outlook", FIRST, LAST)[0]
    assert (found.start.hour, found.start.utcoffset()) == (13, timedelta(hours=1))
    assert found.tentative


def test_other_windows_timezone_is_converted_to_paris():
    text = calendar(event("a", "Call", "20261215T100000", "20261215T110000",
                          tz="GMT Standard Time"))
    assert parse_ics(text, "outlook", FIRST, LAST)[0].start.hour == 11  # Londres + 1 h


def test_recurrence_with_excluded_and_modified_occurrences():
    text = calendar(
        event("serie", "Point hebdo", "20261102T100000", "20261102T110000",
              "RRULE:FREQ=WEEKLY;COUNT=4", "EXDATE;TZID=Romance Standard Time:20261109T100000"),
        event("serie", "Point hebdo", "20261117T150000", "20261117T160000",
              "RECURRENCE-ID;TZID=Romance Standard Time:20261116T100000"),
        timezone=ROMANCE_TIMEZONE)
    events = parse_ics(text, "outlook", FIRST, LAST)
    assert [e.start.date() for e in events] == [date(2026, 11, 2), date(2026, 11, 17),
                                                date(2026, 11, 23)]
    assert events[1].start.hour == 15


def test_cancelled_occurrence_of_a_series_is_dropped():
    text = calendar(
        event("serie", "Point hebdo", "20261102T100000", "20261102T110000", "RRULE:FREQ=WEEKLY;COUNT=2"),
        event("serie", "Point hebdo", "20261109T100000", "20261109T110000",
              "RECURRENCE-ID;TZID=Romance Standard Time:20261109T100000", "STATUS:CANCELLED"),
        timezone=ROMANCE_TIMEZONE)
    assert [e.start.date() for e in parse_ics(text, "outlook", FIRST, LAST)] == [date(2026, 11, 2)]


def test_moved_occurrence_keeps_the_same_key():
    series = event("serie", "Point", "20261102T100000", "20261102T110000", "RRULE:FREQ=WEEKLY;COUNT=2")
    before = parse_ics(calendar(series, timezone=ROMANCE_TIMEZONE), "outlook", FIRST, LAST)
    after = parse_ics(calendar(series, event("serie", "Point", "20261110T150000", "20261110T160000",
                                             "RECURRENCE-ID;TZID=Romance Standard Time:20261109T100000"),
                               timezone=ROMANCE_TIMEZONE), "outlook", FIRST, LAST)
    assert {e.key for e in before} == {e.key for e in after}


@pytest.mark.parametrize("extra", [
    ("STATUS:CANCELLED",), ("X-MICROSOFT-CDO-BUSYSTATUS:FREE",), ("TRANSP:TRANSPARENT",)])
def test_cancelled_or_free_events_are_ignored(extra):
    text = calendar(event("a", "Bloc", "20261215T100000", "20261215T110000", *extra),
                    timezone=ROMANCE_TIMEZONE)
    assert parse_ics(text, "outlook", FIRST, LAST) == []


def test_outlook_cancelled_title_prefix_is_ignored():
    text = calendar(event("a", "Annulé : Réunion", "20261215T100000", "20261215T110000"),
                    timezone=ROMANCE_TIMEZONE)
    assert parse_ics(text, "outlook", FIRST, LAST) == []


def test_busy_status_wins_over_transparency():
    text = calendar(event("a", "Bloc", "20261215T100000", "20261215T110000",
                          "TRANSP:TRANSPARENT", "X-MICROSOFT-CDO-BUSYSTATUS:BUSY"),
                    timezone=ROMANCE_TIMEZONE)
    assert len(parse_ics(text, "outlook", FIRST, LAST)) == 1


@pytest.mark.parametrize("extra", [("STATUS:TENTATIVE",), ("X-MICROSOFT-CDO-BUSYSTATUS:TENTATIVE",)])
def test_tentative_event_is_kept_and_marked(extra):
    text = calendar(event("a", "Peut-être", "20261215T100000", "20261215T110000", *extra),
                    timezone=ROMANCE_TIMEZONE)
    assert parse_ics(text, "outlook", FIRST, LAST)[0].tentative


def test_all_day_event():
    text = calendar("BEGIN:VEVENT\r\nUID:a\r\nSUMMARY:Journée\r\nDTSTART;VALUE=DATE:20261215\r\n"
                    "DTEND;VALUE=DATE:20261216\r\nDTSTAMP:20261030T100000Z\r\nEND:VEVENT")
    found = parse_ics(text, "outlook", FIRST, LAST)[0]
    assert found.all_day and found.start == datetime(2026, 12, 15, tzinfo=PARIS)


def test_garbage_raises_without_details():
    with pytest.raises(IcsError):
        parse_ics("BEGIN:VCALENDAR\r\nnot valid\r\nBEGIN", "outlook", FIRST, LAST)


SECRET = "https://outlook.example/owa/calendar/s3cret-token/calendar.ics"


def fetch_with(handler, url=SECRET):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_ics(client, url)
    return asyncio.run(run())


@pytest.mark.parametrize("handler", [
    lambda request: httpx.Response(404, text="Not found " + SECRET),
    lambda request: httpx.Response(200, text="<html>connexion</html>"),
    lambda request: (_ for _ in ()).throw(httpx.ConnectError("échec vers " + SECRET)),
])
def test_fetch_errors_never_contain_the_url(handler):
    with pytest.raises(IcsError) as caught:
        fetch_with(handler)
    assert "s3cret" not in str(caught.value)
    assert "s3cret" not in "".join(traceback.format_exception(caught.value))


def test_fetch_accepts_webcal_links():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text="BEGIN:VCALENDAR\r\nEND:VCALENDAR")

    fetch_with(handler, "webcal://outlook.example/cal.ics")
    assert seen == ["https://outlook.example/cal.ics"]
