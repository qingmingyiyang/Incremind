from core.search_and_recall.evidence_windows import split_evidence_chunks


def test_chunks_keep_offsets_bounds_and_one_sentence_overlap():
    paragraphs = ["第%d段。" % i + ("这是一句用于验证原文坐标和分块重叠的合成文字。" * 25) for i in range(8)]
    text = "\n\n".join(paragraphs)
    chunks = split_evidence_chunks(text)
    assert len(chunks) > 1
    assert chunks[0].start == 0 and chunks[-1].end == len(text)
    for chunk in chunks:
        assert chunk.text == text[chunk.start:chunk.end]
        assert len(chunk.text) <= 800
    for left, right in zip(chunks, chunks[1:]):
        assert left.start < right.start < left.end < right.end
        overlap = text[right.start:left.end].strip()
        assert overlap.endswith("。")
        assert overlap.count("。") == 1
        assert len(left.text) >= 400


def test_chunks_cover_unpunctuated_text_and_short_tail():
    text = "无" * 2050
    chunks = split_evidence_chunks(text)
    assert chunks[0].start == 0 and chunks[-1].end == len(text)
    assert all(len(c.text) <= 800 for c in chunks)
    assert all(a.end >= b.start for a,b in zip(chunks,chunks[1:]))
    assert split_evidence_chunks("") == ()
    assert split_evidence_chunks("短句。")[0].text == "短句。"


def test_contextual_selection_reads_late_answer_and_keeps_prefix_out_of_evidence():
    from backend.memory_app.v2.contextual_chunks import select_contextual_windows
    text = ("设备登记记录完整。" * 90 + "\n\n") * 12 + "最后核验：备用端口编号为海风二十六。"
    selected = select_contextual_windows(text, "海风站备用端口编号是什么？", title="海风站", summary="合成概览前缀")
    assert "备用端口编号为海风二十六" in selected.excerpt
    assert "合成概览前缀" not in selected.excerpt
    assert all(w.text == text[w.start:w.end] for w in selected.windows)
    assert sum(len(w.text) for w in selected.windows) <= 800


def test_short_content_uses_original_evidence_selection():
    from backend.memory_app.v2.contextual_chunks import select_contextual_windows
    from core.search_and_recall.evidence_windows import select_evidence_windows
    text = "短资料有alpha；另一句beta。"
    assert select_contextual_windows(text,"alpha",title="标题",summary="摘要") == select_evidence_windows(text,"alpha",title="标题",max_chars=10800)


def test_vector_choice_maps_back_to_original_offsets():
    from backend.memory_app.v2.contextual_chunks import select_contextual_windows
    text = "初始登记。" * 200 + "\n\n" + "末尾的隐喻材料。" * 80
    chunks = split_evidence_chunks(text)
    index = len(chunks)-1
    selected = select_contextual_windows(text, "unseen-query", title="无关标题", summary="无关摘要", chunks=chunks, vector_scores={index: .97})
    assert selected.windows == (chunks[index],)
    assert selected.excerpt == text[chunks[index].start:chunks[index].end]


def test_long_document_tail_answer_survives_actual_ladder_and_coordinates(tmp_path):
    import json
    from pathlib import Path
    from tools.memory_eval import seed
    fixture = json.loads((Path(__file__).parents[2]/"fixtures/memory_eval/corpus.json").read_text(encoding="utf8"))
    item = next(q for q in fixture["questions"] if q["id"] == "q-long-document-6")
    document = next(d for d in fixture["documents"] if d["id"] in item["expected_ids"])
    query, ids = seed(tmp_path, {"documents": [document], "insights": []})
    plan = query.prepare_ask(item["project_id"], item["question"])
    selected = [c for c in plan["chosen"] if c["id"] == ids[document["id"]] and c["layer"] == "L1"]
    assert len(selected) == 1
    assert item["answer_text"] in selected[0]["excerpt"]
    markdown = query.documents.markdown(ids[document["id"]])
    assert all(w.text == markdown[w.start:w.end] for w in selected[0]["windows"])
    assert selected[0]["coordinate_space"] == "document_markdown_v1"
    assert query.models.attempts == 0
