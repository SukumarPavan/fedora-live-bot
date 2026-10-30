from deskbot.text_utils import (clean_for_tts, is_hallucination, mentions_name, pick_voice,
                                pop_leading_tag, split_sentences, tag_pending)


def test_split_streaming_sentences():
    s, rest = split_sentences("Hello there friend. How are you")
    assert s == ["Hello there friend."]
    assert rest.strip() == "How are you"


def test_split_waits_for_more_after_trailing_dot():
    s, rest = split_sentences("The value is 3.")
    assert s == [] and rest == "The value is 3."


def test_short_fragments_merge_forward():
    s, rest = split_sentences("Hi! Nice to see you again today. ")
    assert s == ["Hi! Nice to see you again today."]


def test_long_run_without_punctuation_is_cut():
    text = "word " * 60
    s, rest = split_sentences(text)
    assert s and len(s[0]) <= 165


def test_telugu_danda_and_newlines():
    s, rest = split_sentences("నమస్కారం, ఎలా ఉన్నారు? I am fine\nnext")
    assert s[0].startswith("నమస్కారం") and "I am fine" in s


def test_tags():
    assert pop_leading_tag("[happy] Hi") == ("happy", "Hi")
    assert pop_leading_tag("  [SKIP]") == ("skip", "")
    assert pop_leading_tag("Hi [happy]") == (None, "Hi [happy]")
    assert tag_pending("[hap") and not tag_pending("[happy] x") and not tag_pending("Hello")


def test_clean_for_tts():
    assert clean_for_tts("[happy] **Bold** see https://x.y/z 🤖") == "Bold see link"


def test_hallucinations():
    assert is_hallucination("Thank you.")
    assert is_hallucination(" . ")
    assert not is_hallucination("Thank you for helping me with this code")


def test_voice_switch_by_script():
    assert pick_voice("Hello there", "en", "te", True) == "en"
    assert pick_voice("మీరు ఎలా ఉన్నారు", "en", "te", True) == "te"
    assert pick_voice("మీరు ఎలా ఉన్నారు", "en", "te", False) == "en"


def test_mentions_name():
    assert mentions_name("hey ultron what's up", "Ultron Jr")
    assert not mentions_name("what is the time", "Ultron Jr")
