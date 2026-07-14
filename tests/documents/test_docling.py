import json

from hyperextract.documents.docling import (
    load_docling_document,
    plan_document_chunks,
    render_chunk_context,
)
from hyperextract.documents.models import DocumentChunk, DocumentOutline, OutlineNode


def _document():
    def item(ref, label, text, page, **extra):
        return {
            "self_ref": ref,
            "parent": {"$ref": "#/body"},
            "children": [],
            "content_layer": "body",
            "label": label,
            "prov": [
                {
                    "page_no": page,
                    "bbox": {
                        "l": 0,
                        "t": 10,
                        "r": 100,
                        "b": 0,
                        "coord_origin": "BOTTOMLEFT",
                    },
                    "charspan": [0, len(text)],
                }
            ],
            "orig": text,
            "text": text,
            **extra,
        }

    return {
        "schema_name": "DoclingDocument",
        "version": "1.0.0",
        "name": "Course",
        "origin": {
            "mimetype": "application/pdf",
            "binary_hash": 1,
            "filename": "course.pdf",
        },
        "body": {
            "self_ref": "#/body",
            "name": "_root_",
            "label": "unspecified",
            "content_layer": "body",
            "children": [
                {"$ref": "#/texts/0"},
                {"$ref": "#/texts/1"},
                {"$ref": "#/texts/2"},
                {"$ref": "#/texts/3"},
                {"$ref": "#/texts/4"},
            ],
        },
        "furniture": {
            "self_ref": "#/furniture",
            "name": "_root_",
            "label": "unspecified",
            "children": [],
        },
        "groups": [],
        "texts": [
            item("#/texts/0", "section_header", "Chapter One", 1, level=1),
            item("#/texts/1", "text", "Definition alpha.", 1),
            item("#/texts/2", "page_footer", "1", 1, content_layer="furniture"),
            item("#/texts/3", "section_header", "Chapter Two", 2, level=1),
            item("#/texts/4", "text", "Definition beta.", 2),
        ],
        "pictures": [],
        "tables": [],
        "key_value_items": [],
        "form_items": [],
        "field_regions": [],
        "field_items": [],
        "pages": {},
    }


def test_docling_outline_and_chunks_never_cross_chapters(tmp_path):
    path = tmp_path / "course.json"
    path.write_text(json.dumps(_document()), encoding="utf-8")

    outline, blocks = load_docling_document(path)
    chunks = plan_document_chunks(outline, blocks, target_tokens=100, max_tokens=200)

    assert [node.title for node in outline.nodes[1:]] == ["Chapter One", "Chapter Two"]
    assert [block.text for block in blocks] == ["Definition alpha.", "Definition beta."]
    assert len(chunks) == 2
    assert chunks[0].top_level_id != chunks[1].top_level_id
    assert chunks[0].source_refs[0].page_no == 1


def test_docling_rejects_plain_json(tmp_path):
    path = tmp_path / "not-docling.json"
    path.write_text('{"text":"hello"}', encoding="utf-8")

    try:
        load_docling_document(path)
    except ValueError as error:
        assert "DoclingDocument" in str(error)
    else:
        raise AssertionError("Expected invalid input to fail")


def test_flat_pdf_heading_levels_are_inferred_from_numbering(tmp_path):
    document = _document()
    texts = [document["texts"][0]]
    texts[0]["text"] = texts[0]["orig"] = "Part One"
    for index in range(1, 11):
        heading = document["texts"][0].copy()
        heading.update(
            self_ref=f"#/texts/{len(texts)}",
            text=f"1.{index} Topic {index}",
            orig=f"1.{index} Topic {index}",
            level=1,
        )
        texts.append(heading)
        body = document["texts"][1].copy()
        body.update(
            self_ref=f"#/texts/{len(texts)}",
            text=f"Definition {index}.",
            orig=f"Definition {index}.",
        )
        texts.append(body)
    document["texts"] = texts
    document["body"]["children"] = [{"$ref": item["self_ref"]} for item in texts]
    path = tmp_path / "flat.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    outline, _ = load_docling_document(path)

    assert outline.nodes[1].title == "Part One"
    assert outline.nodes[1].level == 1
    assert all(node.level == 2 for node in outline.nodes[2:])


def test_oversized_content_strictly_respects_max_tokens(tmp_path):
    document = _document()
    document["texts"][1]["text"] = document["texts"][1]["orig"] = "知识定义。" * 500
    path = tmp_path / "long.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    outline, blocks = load_docling_document(path)
    chunks = plan_document_chunks(outline, blocks, target_tokens=60, max_tokens=80)

    assert len(chunks) > 2
    assert max(chunk.token_count for chunk in chunks) <= 80


def test_compact_context_keeps_current_chapter_outline_but_not_other_descendants():
    outline = DocumentOutline(
        document_name="Course",
        nodes=[
            OutlineNode(id="root", title="Course", level=0),
            OutlineNode(
                id="chapter-a", title="Chapter A", level=1, parent_id="root", order=1
            ),
            OutlineNode(
                id="a-1", title="A One", level=2, parent_id="chapter-a", order=2
            ),
            OutlineNode(
                id="a-2", title="A Two", level=2, parent_id="chapter-a", order=3
            ),
            OutlineNode(
                id="chapter-b", title="Chapter B", level=1, parent_id="root", order=4
            ),
            OutlineNode(
                id="b-1", title="B One", level=2, parent_id="chapter-b", order=5
            ),
        ],
    )
    chunk = DocumentChunk(
        id="chunk-a",
        index=0,
        outline_id="a-1",
        top_level_id="chapter-a",
        outline_path=["Chapter A", "A One"],
        covered_outline_ids=["a-1"],
        covered_outline_paths=[["Chapter A", "A One"]],
        text="Definition",
        token_count=2,
    )

    context = render_chunk_context(outline, chunk, compact_outline=True)

    assert "Chapter A" in context
    assert "A One" in context
    assert "A Two" in context
    assert "Chapter B" in context
    assert "B One" not in context
