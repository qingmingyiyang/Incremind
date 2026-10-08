from core.search_and_recall.evidence_windows import select_evidence_windows


def test_late_unique_term_keeps_original_offsets_and_budget():
    content = "a" * 5200 + " tailmarker evidence " + "b" * 600
    result = select_evidence_windows(content, "tailmarker evidence", max_chars=2400)
    assert result.match_in == "content"
    assert "tailmarker evidence" in result.excerpt
    assert len(result.excerpt) <= 2400
    assert all(window.text == content[window.start:window.end] for window in result.windows)
    assert result.windows[0].start <= 5200 < result.windows[0].end


def test_distant_terms_are_disjoint_and_do_not_send_intervening_text():
    content = "earlymarker " + "PRIVATE_MIDDLE" * 400 + " latemarker"
    result = select_evidence_windows(content, "earlymarker latemarker", max_chars=2400)
    assert len(result.windows) == 2
    assert "earlymarker" in result.excerpt and "latemarker" in result.excerpt
    assert len(result.excerpt) <= 2400
    assert "PRIVATE_MIDDLE" * 100 not in result.excerpt
    assert result.windows[0].end < result.windows[1].start


def test_unicode_casefold_and_title_only_match_preserve_coordinates():
    content = "🙂\r\nStraße 后段"
    hit = select_evidence_windows(content, "STRASSE", max_chars=30, max_windows=1)
    assert hit.match_in == "content"
    assert hit.windows[0].text == content[hit.windows[0].start:hit.windows[0].end]
    title = select_evidence_windows(content, "titleword", title="Titleword heading", max_chars=10)
    assert title.match_in == "title" and title.excerpt == content[:10]
    assert select_evidence_windows(content, "unmatched", max_chars=10).score == 0


def test_single_unicode_and_long_end_match_have_nonempty_exact_windows():
    for term in ("税", "🙂", "é", "x" * 190):
        content = "a" * 5200 + term
        hit = select_evidence_windows(content, term, max_chars=280, max_windows=1)
        assert term in hit.excerpt
        assert hit.windows[0].text == content[hit.windows[0].start:hit.windows[0].end]


def test_small_budget_never_adds_unaffordable_second_window():
    content = "alpha" + "x" * 500 + "beta"
    hit = select_evidence_windows(content, "alpha beta", max_chars=30)
    assert len(hit.excerpt) <= 30
    assert len(hit.windows) == 1
