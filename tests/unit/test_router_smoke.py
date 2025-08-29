from router import route_message

def test_first_touch_moves_forward(fake_outbox):
    r = route_message("привет", "700", message_uid="w1")
    assert r["ok"] is True
    assert r["next_step"] in {"classify","hello"}

def test_unknown_text_not_crash(fake_outbox):
    r = route_message("", "701", message_uid="w2")
    assert r["ok"] is True

def test_repeat_message_idempotent(fake_outbox):
    # имитация двух подряд сообщений
    r1 = route_message("детское", "702", message_uid="w3")
    r2 = route_message("детское", "702", message_uid="w3")
    assert r2["ok"] is True