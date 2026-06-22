# Telegram Tap-to-Label (interactive ID for unknown visits) Plan

> **For agentic workers:** Implement task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** When an eliminated visit can't be auto-identified, push its photo(s) to
Telegram with inline buttons **[Ella] [Ucok] [Garfield] [skip]**. One tap attributes
the visit to that cat (human-authoritative) — no more gallery hunting. Taps are
owner-allowlisted like every other inbound action.

**Architecture:** Extends the existing `TelegramBot` (already long-polls getUpdates and
allowlists the owner). Add (1) a `send_photo` multipart helper + an inline-keyboard
"who was this?" prompt, (2) `callback_query` handling in `process()` (a tap is a
callback, not a message), and (3) a label callback that writes a human attribution via
`store`. `EliminationNotifier`, on a couldn't-ID visit, fires the prompt instead of a
dead-end text alert.

**Tech Stack:** Python 3.10 stdlib only (`urllib` multipart — no `requests`), sqlite3,
pytest. Matches existing `mw/telegram_bot.py` / `mw/alerts.py` conventions.

## Global Constraints

- ALLOWLIST applies to taps too: a `callback_query` is honored only if
  `callback_query.from.id` equals the owner chat_id. Non-owner taps are answered with a
  terse "not authorized" and otherwise ignored.
- No new dependencies. Multipart/form-data is built by hand with `urllib.request`.
- Human attribution must be AUTHORITATIVE and survive the later auto-sweep — write it
  through `set_capture_label(..., source="human")` so `visit_established_cat` protects it
  (the auto-labeler never overrides a human label).
- `callback_data` ≤ 64 bytes: use the compact form `lbl:<vid>:<CatName>` (and
  `lbl:<vid>:skip`).
- Keep HTTP wrappers thin and untested (like the existing `_http_send`); unit-test the
  pure logic (callback parsing, allowlist, dispatch) with fakes.

---

### Task 1: `store.human_attribute_visit`

**Files:** Modify `mw/store.py`; test in `tests/test_store.py` (append).

**Interfaces:**
- Produces: `human_attribute_visit(conn, visit_id, cat_id) -> bool` — records a HUMAN
  label so the visit attributes to `cat_id` authoritatively. Returns True on success,
  False if the visit has no captures to attach the label to.

- [ ] **Step 1: failing test**

```python
def test_human_attribute_visit(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ucok", "Ella"])
    vid = store.open_visit(conn, 1000.0); store.mark_elimination(conn, vid, 55)
    store.insert_capture(conn, 1000.0, vid, "cam", "/g/a.jpg")
    store.insert_capture(conn, 1001.0, vid, "cam", "/g/b.jpg")
    eid = store.cat_id_by_name(conn, "Ella")
    assert store.human_attribute_visit(conn, vid, eid) is True
    # visit attributed to Ella, and it's human-established (auto-labeler won't override)
    assert store.get_visit(conn, vid)["cat_id"] == eid
    assert store.visit_established_cat(conn, vid) == "Ella"

def test_human_attribute_visit_no_captures(tmp_path):
    conn = store.connect(str(tmp_path / "t.db")); store.init_db(conn)
    store.seed_cats(conn, ["Ella"])
    vid = store.open_visit(conn, 1000.0)
    assert store.human_attribute_visit(conn, vid, store.cat_id_by_name(conn, "Ella")) is False
```

- [ ] **Step 2: run, verify fail.**
- [ ] **Step 3: implement** (place near `sync_visit_cat`):

```python
def human_attribute_visit(conn, visit_id, cat_id):
    """Attribute a whole visit to a cat from a HUMAN decision (the Telegram tap
    when auto-ID failed). Writes the human label on the visit's first capture, which
    (a) syncs visits.cat_id via set_capture_label and (b) makes visit_established_cat
    return this cat so the auto-labeler never overrides it. Returns False if the
    visit has no captures."""
    with _lock:
        row = conn.execute(
            "SELECT id FROM captures WHERE visit_id=? ORDER BY id LIMIT 1",
            (visit_id,)).fetchone()
    if row is None:
        return False
    set_capture_label(conn, row["id"], cat_id, source="human")  # syncs the visit too
    return True
```

- [ ] **Step 4: run, verify pass.**  **Step 5: commit.**

---

### Task 2: TelegramBot — photo prompt + callback (tap) handling

**Files:** Modify `mw/telegram_bot.py`; test `tests/test_telegram_bot.py` (append).

**Interfaces:**
- Produces:
  - module fn `send_label_request(token, chat_id, vid, frame_paths, cats, when) -> None`
    — sends up to 3 photos (the spread frames) then a message
    "🐈 Who used the box at {when}? (couldn't auto-ID)" with an inline keyboard of
    `cats` + a skip button; `callback_data` = `lbl:{vid}:{cat}`.
  - `TelegramBot` gains an optional `label_cb` ctor arg: `label_cb(vid:int, cat:str) ->
    str` (returns a confirmation line). `process()` now also handles `callback_query`
    updates: allowlist on `from.id`, parse `lbl:<vid>:<cat>`, call `label_cb` (or report
    "skipped"), and acknowledge.

- [ ] **Step 1: failing tests** (pure logic, fake sender):

```python
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
```

- [ ] **Step 2: run, verify fail.**
- [ ] **Step 3: implement.** Add `label_cb=None` to `__init__`. In `process()`, before the
  `message` handling, branch on `callback_query`:

```python
cq = u.get("callback_query")
if cq is not None:
    self._answer_callback(cq.get("id"))            # dismiss Telegram's spinner
    frm = str((cq.get("from") or {}).get("id"))
    if frm != self.allowed:
        continue                                   # allowlist taps too
    data = cq.get("data") or ""
    if data.startswith("lbl:"):
        _, vid, cat = data.split(":", 2)
        if cat == "skip" or self.label_cb is None:
            self._reply(f"⏭️ Skipped visit {vid}")
        else:
            self._reply(self.label_cb(int(vid), cat))
    continue
```

Add thin HTTP helpers (untested, network): `_answer_callback(cq_id)` →
POST `/answerCallbackQuery` with `callback_query_id`. And module-level
`send_label_request(...)` using a `_post_photo(token, chat_id, path, caption, markup)`
multipart helper:

```python
def _post_photo(token, chat_id, path, caption=None, reply_markup=None):
    boundary = "----meowant" + str(abs(hash(path)) % 10**8)
    parts = []
    def field(name, value):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    field("chat_id", str(chat_id))
    if caption: field("caption", caption)
    if reply_markup is not None: field("reply_markup", _json.dumps(reply_markup))
    with open(path, "rb") as f:
        img = f.read()
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
                  f"filename=\"f.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n").encode() + img + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendPhoto", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    urllib.request.urlopen(req, timeout=20)


def send_label_request(token, chat_id, vid, frame_paths, cats, when):
    """Up to 3 photos, then a buttons message asking who used the box."""
    import os
    for p in [p for p in frame_paths if os.path.exists(p)][:3]:
        try:
            _post_photo(token, chat_id, p)
        except Exception as e:
            print(f"[telegram] photo {p} failed: {e}", file=sys.stderr)
    row = [{"text": c, "callback_data": f"lbl:{vid}:{c}"} for c in cats]
    markup = {"inline_keyboard": [row, [{"text": "skip", "callback_data": f"lbl:{vid}:skip"}]]}
    try:
        _http_send_markup(token, chat_id,
                          f"🐈 Who used the box at {when}? (couldn't auto-ID)", markup)
    except Exception as e:
        print(f"[telegram] label-request failed: {e}", file=sys.stderr)
```

Add `_http_send_markup(token, chat_id, text, markup)` = like `_http_send` but with a
`reply_markup` form field (json). Add `_answer_callback` thin wrapper. NOTE: do not break
the existing `_http_send` signature (used by command replies).

- [ ] **Step 4: run, verify pass** (the 3 new tests + existing bot tests).  **Step 5: commit.**

---

### Task 3: notifier fires the prompt on couldn't-ID; wire it up

**Files:** Modify `mw/elim_notify.py`, `meowantd.py`; test `tests/test_elim_notify.py`.

**Interfaces:**
- `EliminationNotifier` gains optional `ask_who=None` (callable
  `(vid, frame_paths, when)`). When a visit stays unidentified after sampling AND
  `ask_who` is set, call it (sending the photo prompt) INSTEAD of the dead-end
  "couldn't ID" text. Identified visits still send the named text alert.

- [ ] **Step 1: failing test** — extend the unidentified test: pass `ask_who` and assert
  it's called with the visit id and frame paths, and that no plain "couldn't ID" text
  was sent.

```python
def test_unidentified_triggers_ask_who(tmp_path):
    conn, _, sent = _setup(tmp_path, cat=None)
    asked = []
    # rebuild notifier with ask_who
    from mw.elim_notify import EliminationNotifier
    n = EliminationNotifier(conn, _Labeler(conn, None), notify=sent.append,
                            now_fn=lambda: 10_000.0, settle_s=15,
                            ask_who=lambda vid, paths, when: asked.append(vid))
    v = store.open_visit(conn, 9_000.0); store.mark_elimination(conn, v, 55)
    store.insert_capture(conn, 9_100.0, v, "cam", "/g/x.jpg")
    store.close_visit(conn, v, 9_900.0, 900)
    n.run_once()
    assert asked == [v]                      # prompt fired
    assert sent == []                        # no dead-end text
    assert store.get_visit(conn, v)["notified"] == 1
```

- [ ] **Step 2: run, verify fail.**
- [ ] **Step 3: implement** in `run_once`, after re-reading `fresh`:

```python
cat_id = fresh["cat_id"]
if cat_id:
    self.notify(self._alert_text(fresh))
elif self.ask_who is not None:
    paths = [c["path"] for c in store.captures_for_visit(self.conn, v["id"])]
    self.ask_who(v["id"], paths, time.strftime("%H:%M", time.localtime(self.now())))
else:
    self.notify(self._alert_text(fresh))     # fallback: dead-end text
store.mark_notified(self.conn, v["id"])
```

Add `ask_who=None` to `__init__`.

- [ ] **Step 4: wire `meowantd.py`.** The TelegramBot and the notifier must share the
  token/chat. Build them so the bot has a `label_cb` and the notifier has `ask_who`:

```python
# in the telegram block: give the bot a label callback
_valid_cats = [c for c in store.gallery_counts(conn).keys()]
def _label_cb(vid, cat):
    cid = store.cat_id_by_name(conn, cat)
    if cid and store.human_attribute_visit(conn, vid, cid):
        return f"✓ Visit {vid} labeled {cat}"
    return f"⚠️ Couldn't label visit {vid} as {cat}"
bot = TelegramBot(tg_token, tg_chat, {...commands...}, label_cb=_label_cb)
...
# give the notifier the photo-prompt (only when Telegram is configured)
from mw.telegram_bot import send_label_request
elim_notifier.ask_who = lambda vid, paths, when: send_label_request(
    tg_token, tg_chat, vid, paths, _valid_cats, when)
```

(Construct `elim_notifier` where it is today, then set `.ask_who` after the telegram
creds are confirmed; leave it None when Telegram is unconfigured so the text fallback
holds.)

- [ ] **Step 5: full suite green** — `pytest -q`.  **Step 6: commit.**

## Self-Review
- Coverage: human attribution + established ✔, tap dispatch ✔, tap allowlist ✔, skip ✔,
  unidentified→ask_who ✔. HTTP/multipart are thin untested wrappers (per constraints).
- Types consistent: `human_attribute_visit(conn,vid,cat_id)`,
  `send_label_request(token,chat_id,vid,paths,cats,when)`, `label_cb(vid,cat)`,
  `ask_who(vid,paths,when)`.
- DEPLOY NOTE: no backfill needed (notified already backfilled). Restart with
  `launchctl kickstart -k gui/$UID/com.meowant.daemon` (plain stop/start races KeepAlive).
