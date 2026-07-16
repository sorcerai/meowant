from mw.bus import EventBus
from mw.events import Event, BIN_FULL, CHUTE_FULL, ELIMINATION, FAULT, CAT_ENTER
from mw.alerts import alert_message, Alerts, make_notify

def test_alert_message_mapping():
    # BIN_FULL is no longer an instant alert — BoxHealthWatch owns it.
    assert alert_message(Event(BIN_FULL, 1.0)) is None
    assert alert_message(Event(ELIMINATION, 1.0, {})) is None  # named alert via EliminationNotifier
    assert alert_message(Event(FAULT, 1.0, {"bitmap": 2})) is None  # BoxHealthWatch owns it now
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
    assert calls == [("tg", "hello", "T", ["42"])]   # recipients passed as a list


def test_make_notify_multi_recipient_dedup(monkeypatch):
    # owner + sitter, owner first, duplicate dropped.
    cfg = {"alerts.telegram_bot_token": "T", "alerts.telegram_chat_id": "42",
           "alerts.telegram_chat_ids": ["77", "42"]}
    calls = []
    import mw.alerts as alerts
    monkeypatch.setattr(alerts, "telegram_notify", lambda m, t, c: calls.append(c))
    make_notify(lambda k: cfg.get(k))("x")
    assert calls == [["42", "77"]]


def test_make_notify_owner_only_excludes_sitter(monkeypatch):
    # Routine pings: owner_only must drop the extra (sitter) recipients.
    cfg = {"alerts.telegram_bot_token": "T", "alerts.telegram_chat_id": "42",
           "alerts.telegram_chat_ids": ["77"]}
    calls = []
    import mw.alerts as alerts
    monkeypatch.setattr(alerts, "telegram_notify", lambda m, t, c: calls.append(c))
    make_notify(lambda k: cfg.get(k), owner_only=True)("x")
    assert calls == [["42"]]                         # sitter 77 excluded


def test_telegram_notify_sends_to_all_and_any_success(monkeypatch):
    import mw.alerts as alerts
    import urllib.request
    n = {"calls": 0}
    def fake(req, timeout=5):
        n["calls"] += 1
        if n["calls"] == 1:
            raise RuntimeError("first recipient down")
        return object()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    ok = alerts.telegram_notify("hi", "TOK", ["bad", "good"])
    assert ok is True and n["calls"] == 2          # tried both, one success -> True


def test_telegram_notify_false_only_if_all_fail(monkeypatch):
    import mw.alerts as alerts
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert alerts.telegram_notify("hi", "TOK", ["a", "b"]) is False


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


# ---- cascade fallback: a failed channel must not eat the alert (Jul 15) ----

def test_cascade_telegram_fails_ntfy_succeeds(monkeypatch):
    cfg = {"alerts.telegram_bot_token": "T", "alerts.telegram_chat_id": "42",
           "alerts.ntfy_topic": "topic-x"}
    ntfy_calls = []
    notify = make_notify(lambda k: cfg.get(k),
                         _telegram=lambda m, t, c: False,
                         _ntfy=lambda m, topic: ntfy_calls.append((m, topic)) or True)
    assert notify("uh oh") is True
    assert ntfy_calls == [("uh oh", "topic-x")]


def test_cascade_both_channels_fail_macos_best_effort_and_false(monkeypatch):
    import mw.alerts as alerts
    cfg = {"alerts.telegram_bot_token": "T", "alerts.telegram_chat_id": "42",
           "alerts.ntfy_topic": "topic-x"}
    macos_calls = []
    monkeypatch.setattr(alerts, "macos_notify", lambda m: macos_calls.append(m))
    notify = make_notify(lambda k: cfg.get(k),
                         _telegram=lambda m, t, c: False,
                         _ntfy=lambda m, topic: False)
    assert notify("uh oh") is False
    assert macos_calls == ["uh oh"]           # local best-effort trace still fires


def test_cascade_telegram_succeeds_ntfy_not_called():
    cfg = {"alerts.telegram_bot_token": "T", "alerts.telegram_chat_id": "42",
           "alerts.ntfy_topic": "topic-x"}
    ntfy_calls = []
    notify = make_notify(lambda k: cfg.get(k),
                         _telegram=lambda m, t, c: True,
                         _ntfy=lambda m, topic: ntfy_calls.append((m, topic)) or True)
    assert notify("all good") is True
    assert ntfy_calls == []                   # short-circuited: never tried


def test_cascade_telegram_raises_falls_back_to_ntfy():
    cfg = {"alerts.telegram_bot_token": "T", "alerts.telegram_chat_id": "42",
           "alerts.ntfy_topic": "topic-x"}
    def boom(m, t, c):
        raise RuntimeError("dns blip")
    ntfy_calls = []
    notify = make_notify(lambda k: cfg.get(k),
                         _telegram=boom,
                         _ntfy=lambda m, topic: ntfy_calls.append((m, topic)) or True)
    assert notify("uh oh") is True
    assert ntfy_calls == [("uh oh", "topic-x")]


def test_bin_full_no_longer_instant_alert():
    # BoxHealthWatch owns bin-full messaging now; Alerts must not double-ping it.
    assert alert_message(Event(BIN_FULL, 0.0)) is None

def test_fault_no_longer_instant_alert():
    # BoxHealthWatch owns fault messaging now (re-nag + UNUSABLE escalation);
    # Alerts must not double-ping it, exactly like BIN_FULL.
    assert alert_message(Event(FAULT, 0.0, {"bitmap": 1})) is None
