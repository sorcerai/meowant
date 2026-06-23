from mw.bus import EventBus
from mw.events import Event, BIN_FULL, ELIMINATION, FAULT, CAT_ENTER
from mw.alerts import alert_message, Alerts, make_notify

def test_alert_message_mapping():
    assert "bin" in alert_message(Event(BIN_FULL, 1.0)).lower()
    assert alert_message(Event(ELIMINATION, 1.0, {})) is None  # named alert via EliminationNotifier
    assert "fault" in alert_message(Event(FAULT, 1.0, {"bitmap": 2})).lower()
    assert alert_message(Event(CAT_ENTER, 1.0)) is None  # not alert-worthy

def test_alerts_dispatches_via_notify():
    bus = EventBus(); sent = []
    a = Alerts(bus, notify=sent.append)
    bus.publish(Event(BIN_FULL, 1.0))
    bus.publish(Event(CAT_ENTER, 2.0))   # ignored
    a.run_once()
    assert len(sent) == 1 and "bin" in sent[0].lower()


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
