from mw.bus import EventBus
from mw.events import Event, BIN_FULL, ELIMINATION, FAULT, CAT_ENTER
from mw.alerts import alert_message, Alerts

def test_alert_message_mapping():
    assert "bin" in alert_message(Event(BIN_FULL, 1.0)).lower()
    assert alert_message(Event(ELIMINATION, 1.0, {})) is not None
    assert "fault" in alert_message(Event(FAULT, 1.0, {"bitmap": 2})).lower()
    assert alert_message(Event(CAT_ENTER, 1.0)) is None  # not alert-worthy

def test_alerts_dispatches_via_notify():
    bus = EventBus(); sent = []
    a = Alerts(bus, notify=sent.append)
    bus.publish(Event(BIN_FULL, 1.0))
    bus.publish(Event(CAT_ENTER, 2.0))   # ignored
    a.run_once()
    assert len(sent) == 1 and "bin" in sent[0].lower()
