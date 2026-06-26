from mw.bus import EventBus
from mw.events import Event, BIN_FULL, CHUTE_FULL, ELIMINATION, FAULT, CAT_ENTER
from mw.alerts import alert_message, Alerts, make_notify

def test_alert_message_mapping():
    # BIN_FULL is no longer an instant alert — BoxHealthWatch owns it.
    assert alert_message(Event(BIN_FULL, 1.0)) is None
    assert alert_message(Event(ELIMINATION, 1.0, {})) is None  # named alert via EliminationNotifier
    assert "fault" in alert_message(Event(FAULT, 1.0, {"bitmap": 2})).lower()
    assert alert_message(Event(CAT_ENTER, 1.0)) is None  # not alert-worthy

def test_alerts_dispatches_via_notify():
    bus = EventBus(); sent = []
    a = Alerts(bus, notify=sent.append)
    bus.publish(Event(CHUTE_FULL, 1.0))  # still an instant alert
    bus.publish(Event(CAT_ENTER, 2.0))   # ignored
    a.run_once()
    assert len(sent) == 1 and "chute" in sent[0].lower()


def test_make_notify_prefers_telegram(monkeypatch):
    # Telegram chosen when token+chat_id set, even if an ntfy topic also exists.
    cfg = {"alerts.telegram_bot_token": "T", "alerts.telegram_chat_id": "42",
           "alerts.ntfy_topic": "some-topic"}
    calls = []
    import mw.alerts as alerts
    monkeypatch.setattr(alerts, "telegram_notify",
                        lambda m, t, c: calls.append(("tg", m, t, c)))
    monkeypatch.setattr(alerts, "ntfy_notify",
                        lambda *a, **k: calls.append(("ntfy",)))
    make_notify(lambda k: cfg.get(k))("hello")
    assert calls == [("tg", "hello", "T", "42")]


def test_make_notify_falls_back_to_ntfy(monkeypatch):
    # No telegram creds -> ntfy when a topic is set.
    cfg = {"alerts.ntfy_topic": "topic-x"}
    calls = []
    import mw.alerts as alerts
    monkeypatch.setattr(alerts, "ntfy_notify",
                        lambda m, topic: calls.append(("ntfy", m, topic)))
    make_notify(lambda k: cfg.get(k))("yo")
    assert calls == [("ntfy", "yo", "topic-x")]


def test_make_notify_falls_back_to_macos(monkeypatch):
    # No remote creds at all -> macOS desktop notify.
    calls = []
    import mw.alerts as alerts
    monkeypatch.setattr(alerts, "macos_notify", lambda m: calls.append(m))
    make_notify(lambda k: None)("desktop")
    assert calls == ["desktop"]


def test_bin_full_no_longer_instant_alert():
    # BoxHealthWatch owns bin-full messaging now; Alerts must not double-ping it.
    assert alert_message(Event(BIN_FULL, 0.0)) is None

def test_fault_still_instant_alert():
    msg = alert_message(Event(FAULT, 0.0, {"bitmap": 1}))
    assert msg is not None and "fault" in msg.lower()
