"""Cleaning tests built from the real shapes seen in Evolution Golf's tickets."""

from evogolf_support.corpus.clean import clean_body, redact, strip_quoted, strip_signature


def test_strips_quoted_reply_history():
    raw = (
        "Morning mate,\n\n"
        "Yep that makes sense now! Thank you very much!\n\n"
        "On Wed, 26 Aug 2026 at 16:29, Simon Tillson <Simon.Tillson@motocaddy.com> wrote:\n"
        "> Hi Brad\n"
        "> I've investigated it and it looks like this is a trolley.\n"
    )
    out = strip_quoted(raw)
    assert "makes sense now" in out
    assert "investigated it" not in out
    assert "Simon Tillson" not in out


def test_strips_zendesk_footer():
    raw = (
        "Hi Brad,\n\n"
        "I can confirm DPD collected the trolley this morning.\n\n"
        "Open Ticket #1248 Requester Jayman Patel Assignee Evolution Golf CCs -\n"
        "Followers - Group Support Organisation - Brand\n"
    )
    out = clean_body(raw)
    assert "DPD collected the trolley" in out
    assert "Open Ticket" not in out
    assert "Assignee" not in out


def test_strips_signoff_and_name():
    raw = (
        "Thanks for the prompt response. To advise I have just posted the size 9\n"
        "shoes back to yourselves.\n\n"
        "Kind regards\n"
        "Jayman\n"
    )
    out = strip_signature(raw)
    assert "posted the size 9" in out
    assert "Kind regards" not in out
    assert "Jayman" not in out


def test_early_thanks_is_not_treated_as_signoff():
    raw = "Thanks\n\n" + "I ordered the wrong model and need a refund. " * 12
    out = strip_signature(raw)
    assert "wrong model" in out


def test_redacts_pii_but_keeps_order_references():
    raw = (
        "Order 27641 was sent to SW1A 1AA, call me on 07700 900123 "
        "or email jayman@example.com. Card 4111 1111 1111 1111."
    )
    out = redact(raw)
    assert "[POSTCODE]" in out
    assert "[PHONE]" in out
    assert "[EMAIL]" in out
    assert "[CARD]" in out
    # The order reference is context the bot needs - it must survive.
    assert "27641" in out
    assert "example.com" not in out


def test_full_pipeline_on_a_realistic_agent_reply():
    raw = (
        "Hi Jayman,\r\n\r\n"
        "Thanks for getting back to me. I've raised a collection with DPD for\r\n"
        "Thursday 3rd September, and we'll refund the 9.95 postage once the\r\n"
        "trolley is back with us.\r\n\r\n"
        "Many thanks\r\n"
        "Brad\r\n"
        "Evolution Golf | 01234 567890\r\n\r\n"
        "On Fri, 28 Aug 2026 at 12:20, Jayman Patel <jayman@example.com> wrote:\r\n"
        "> Someone should be at home on the 3rd September\r\n"
    )
    out = clean_body(raw)
    assert "raised a collection with DPD" in out
    assert "refund the 9.95 postage" in out
    assert "Many thanks" not in out
    assert "01234 567890" not in out
    assert "Someone should be at home" not in out
    assert "\r" not in out


def test_empty_body_is_safe():
    assert clean_body("") == ""
    assert clean_body("   \n\n  ") == ""
