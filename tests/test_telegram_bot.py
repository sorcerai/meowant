"""TelegramBot: the chat_id allowlist (the security boundary), command dispatch,
and getUpdates offset advancement — all with fake getter/sender (no network)."""
from mw.telegram_bot import TelegramBot


def _bot(handlers=None, allowed="100"):
    sent = []
    handlers = handlers or {"/cats": lambda: "CATS-OK"}
    bot = TelegramBot("tok", allowed, handlers,
                      getter=lambda *a: [], sender=lambda t, c, m: sent.append((c, m)))
    return bot, sent


def _update(uid, chat_id, text):
    return {"update_id": uid, "message": {"chat": {"id": chat_id}, "text": text}}


def test_owner_command_answered():
    bot, sent = _bot()
    n = bot.process([_update(1, 100, "/cats")])
    assert n == 1
    assert sent == [("100", "CATS-OK")]


def test_intruder_is_dropped_and_owner_warned_once():
    bot, sent = _bot()
    # two messages from a stranger -> zero answered, owner warned exactly once
    n = bot.process([_update(1, 999, "/cats"), _update(2, 999, "/status")])
    assert n == 0
    warns = [m for c, m in sent if "unauthorized" in m.lower()]
    assert len(warns) == 1 and sent[0][0] == "100"   # warning goes to the OWNER chat


def test_intruder_never_gets_a_reply():
    bot, sent = _bot()
    bot.process([_update(1, 999, "/cats")])
    # nothing is ever sent to the intruder's chat id
    assert all(chat == "100" for chat, _ in sent)


def test_unknown_command_gets_help():
    bot, sent = _bot()
    bot.process([_update(1, 100, "/nope")])
    assert "Commands:" in sent[0][1] and "/cats" in sent[0][1]


def test_offset_advances_past_processed_updates():
    bot, _ = _bot()
    bot.process([_update(5, 100, "/cats"), _update(7, 100, "/cats")])
    assert bot._offset == 8          # max update_id + 1


def test_command_with_botname_suffix_dispatches():
    # Telegram appends @botname in groups: "/cats@meowantbot"
    bot, sent = _bot()
    bot.process([_update(1, 100, "/cats@meowantbot extra args")])
    assert sent == [("100", "CATS-OK")]


def test_handler_exception_is_caught():
    def boom():
        raise ValueError("kaboom")
    bot, sent = _bot(handlers={"/cats": boom})
    n = bot.process([_update(1, 100, "/cats")])
    assert n == 1 and "failed" in sent[0][1].lower()   # bot survives, owner told


def test_callback_tap_dispatches_label():
    labeled = []
    sent = []
    from mw.telegram_bot import TelegramBot
    bot = TelegramBot("tok", "100", {}, getter=lambda *a: [],
                      sender=lambda t, c, m: sent.append((c, m)),
                      label_cb=lambda vid, cat: labeled.append((vid, cat)) or f"✓ {cat}")
    upd = {"update_id": 1, "callback_query": {
        "id": "cbq1", "from": {"id": 100},
        "message": {"message_id": 9, "chat": {"id": 100}},
        "data": "lbl:54:Ella"}}
    bot.process([upd])
    assert labeled == [(54, "Ella")]
    assert any("✓ Ella" in m for _, m in sent)

def test_callback_tap_allowlist_blocks_stranger():
    labeled = []
    from mw.telegram_bot import TelegramBot
    bot = TelegramBot("tok", "100", {}, getter=lambda *a: [],
                      sender=lambda t, c, m: None,
                      label_cb=lambda vid, cat: labeled.append((vid, cat)) or "ok")
    upd = {"update_id": 1, "callback_query": {
        "id": "x", "from": {"id": 999},
        "message": {"message_id": 9, "chat": {"id": 999}}, "data": "lbl:54:Ella"}}
    bot.process([upd])
    assert labeled == []                     # stranger's tap ignored

def test_callback_skip_does_not_label():
    labeled = []
    from mw.telegram_bot import TelegramBot
    bot = TelegramBot("tok", "100", {}, getter=lambda *a: [],
                      sender=lambda t, c, m: None,
                      label_cb=lambda vid, cat: labeled.append((vid, cat)) or "ok")
    upd = {"update_id": 1, "callback_query": {
        "id": "x", "from": {"id": 100},
        "message": {"message_id": 9, "chat": {"id": 100}}, "data": "lbl:54:skip"}}
    bot.process([upd])
    assert labeled == []                     # skip is a no-op label-wise


def test_dispatch_passes_arg_to_handler_that_accepts_one():
    from mw.telegram_bot import TelegramBot
    seen = []
    bot = TelegramBot("tok", "123", {
        "/feed": lambda arg="": seen.append(arg) or f"fed {arg}",
        "/cats": lambda: "cats",                  # zero-arg still works
    })
    assert bot._dispatch("/feed 3") == "fed 3"
    assert seen == ["3"]
    assert bot._dispatch("/cats") == "cats"        # unchanged contract
