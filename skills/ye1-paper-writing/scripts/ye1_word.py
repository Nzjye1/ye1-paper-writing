#!/usr/bin/env python3
"""Create, inspect, annotate and revise DOCX manuscripts without replacing inputs.

Requires python-docx >=1.2. The change engine deliberately handles ordinary,
direct text runs only; it refuses ambiguous or structurally complex anchors.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Inches, Pt, RGBColor
from docx.text.paragraph import Paragraph
from docx.text.run import Run

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
T, R, RPR, P = (qn(x) for x in ("w:t", "w:r", "w:rPr", "w:p"))
DEL, INS, MOVEFROM = (qn(x) for x in ("w:del", "w:ins", "w:moveFrom"))


def fail(message: str) -> None:
    raise ValueError(message)


def read_json(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def safe_destination(path: str, sources: list[str]) -> Path:
    out = Path(path).expanduser()
    if out.exists():
        fail(f"Output already exists; choose a new path: {out}")
    if any(out.resolve() == Path(src).expanduser().resolve() for src in sources):
        fail("Output must not be an input file")
    if not out.parent.is_dir():
        fail(f"Output directory does not exist: {out.parent}")
    if out.suffix.lower() != ".docx":
        fail("Word output must end in .docx")
    return out


def save_new(doc, path: Path) -> None:
    created = False
    try:
        with path.open("xb") as output:
            created = True
            doc.save(output)
    except Exception:
        if created and path.exists():
            path.unlink()
        raise


def paragraphs(doc):
    # Document-order indices include paragraphs inside tables, but not header/footer.
    return list(doc.element.body.iter(P))


def belongs_to(node, paragraph) -> bool:
    ancestor = node.getparent()
    while ancestor is not None and ancestor.tag != P:
        if ancestor.tag in (DEL, MOVEFROM):
            return False
        ancestor = ancestor.getparent()
    return ancestor is paragraph


def visible_text(paragraph) -> str:
    chunks = []
    for node in paragraph.iter():
        if not belongs_to(node, paragraph):
            continue
        if node.tag == T:
            chunks.append(node.text or "")
        elif node.tag == qn("w:tab"):
            chunks.append("\t")
        elif node.tag in (qn("w:br"), qn("w:cr")):
            chunks.append("\n")
    return "".join(chunks)


def inspect_document(doc) -> dict:
    ps = paragraphs(doc)
    anchors = {}
    active = set()
    for index, paragraph in enumerate(ps, 1):
        for node in paragraph.iter():
            if node is paragraph or not belongs_to(node, paragraph):
                continue
            ident = node.get(qn("w:id"))
            if node.tag == qn("w:commentRangeStart") and ident is not None:
                active.add(ident)
                anchors.setdefault(ident, {"paragraphs": [], "text": ""})
            elif node.tag == qn("w:commentRangeEnd") and ident is not None:
                active.discard(ident)
            elif node.tag == T and active:
                for key in active:
                    anchor = anchors[key]
                    if index not in anchor["paragraphs"]:
                        anchor["paragraphs"].append(index)
                    anchor["text"] += node.text or ""
    comments = []
    for comment in doc.comments:
        key = str(comment.comment_id)
        comments.append({
            "id": key, "author": comment.author, "text": comment.text,
            "anchor": anchors.get(key),
        })
    tracked = len(doc.element.body.xpath('.//w:ins | .//w:del | .//w:moveFrom | .//w:moveTo'))
    return {
        "paragraphs": [{"index": i, "text": visible_text(p),
                        "in_table": any(a.tag == qn("w:tbl") for a in p.iterancestors())}
                       for i, p in enumerate(ps, 1)],
        "comments": comments,
        "existing_revision_elements": tracked,
        "scope": "Main document and tables only; inspect header, footer, footnotes, drawings and modern comment threads separately.",
    }


def simple_run(node) -> bool:
    return (node.tag == R and any(c.tag == T for c in node)
            and all(c.tag in (RPR, T) for c in node))


def run_text(run) -> str:
    return "".join((child.text or "") for child in run if child.tag == T)


def clone_run(run, text: str):
    cloned = copy.deepcopy(run)
    for child in list(cloned):
        if child.tag != RPR:
            cloned.remove(child)
    t = OxmlElement("w:t")
    t.text = text
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    cloned.append(t)
    return cloned


def find_group(paragraph, quote: str):
    if not quote or "\n" in quote or "\t" in quote:
        fail("Anchor must be a nonempty single-line text string")
    groups, group = [], []
    for child in paragraph:
        if simple_run(child):
            group.append(child)
        else:
            if group:
                groups.append(group)
                group = []
    if group:
        groups.append(group)
    matches = []
    for group in groups:
        text = "".join(run_text(run) for run in group)
        pos = 0
        while True:
            offset = text.find(quote, pos)
            if offset < 0:
                break
            matches.append((group, offset, offset + len(quote)))
            pos = offset + 1
    if len(matches) != 1:
        fail(f"Anchor must match exactly once within ordinary adjacent runs; found {len(matches)}. "
             "Use a longer quote or handle fields, links or existing revisions with a capable Word tool.")
    return matches[0]


def split_range(paragraph, group, start: int, end: int):
    """Replace affected runs by equivalent runs and return selected contiguous fragments."""
    pieces = []
    offset = 0
    for run in group:
        text = run_text(run)
        left, right = offset, offset + len(text)
        a, b = max(start, left), min(end, right)
        if a < b:
            prefix = text[:a-left]
            chosen = text[a-left:b-left]
            suffix = text[b-left:]
            replacements = [(prefix, False), (chosen, True), (suffix, False)]
            pos = paragraph.index(run)
            for fragment, selected in replacements:
                if fragment:
                    new_run = clone_run(run, fragment)
                    paragraph.insert(pos, new_run)
                    pos += 1
                    if selected:
                        pieces.append(new_run)
            paragraph.remove(run)
        offset = right
    if not pieces:
        fail("Could not split matched anchor")
    return pieces


def index_paragraph(doc, value):
    ps = paragraphs(doc)
    if not isinstance(value, int) or value < 1 or value > len(ps):
        fail(f"Invalid paragraph index: {value!r}")
    return ps[value - 1]


def apply_comments(doc, specification, author: str) -> int:
    comments = specification.get("comments", [])
    if not comments:
        fail("No comments supplied")
    for item in comments:
        p = index_paragraph(doc, item.get("paragraph"))
        message = item.get("text", "")
        if not isinstance(message, str) or not message.strip():
            fail("Each comment needs text")
        para = Paragraph(p, doc._body)
        quote = item.get("quote")
        if quote:
            group, start, end = find_group(p, quote)
            selected = split_range(p, group, start, end)
            runs = [Run(el, para) for el in selected]
        else:
            runs = para.runs
            if not runs:
                fail("Paragraph has no directly annotatable runs; supply a different anchor or use Word")
        doc.add_comment(runs=runs, text=message, author=author, initials="Y")
    return len(comments)


def next_revision_id(doc) -> int:
    ids = []
    for el in doc.element.body.xpath('.//w:ins | .//w:del | .//w:moveFrom | .//w:moveTo'):
        try:
            ids.append(int(el.get(qn("w:id"))))
        except (TypeError, ValueError):
            pass
    return max(ids, default=-1) + 1


def change_wrapper(tag: str, ident: int, author: str):
    el = OxmlElement(tag)
    el.set(qn("w:id"), str(ident))
    el.set(qn("w:author"), author)
    el.set(qn("w:date"), datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return el


def apply_changes(doc, specification, tracked: bool, author: str) -> int:
    changes = specification.get("changes", [])
    if not changes:
        fail("No changes supplied")
    ident = next_revision_id(doc)
    for item in changes:
        paragraph = index_paragraph(doc, item.get("paragraph"))
        old, new = item.get("old"), item.get("new")
        if not isinstance(old, str) or not isinstance(new, str):
            fail("Each change needs string fields 'old' and 'new'")
        if old == new:
            fail("Old and new text are identical")
        if any(c in new for c in "\n\r\t"):
            fail("Replacement must stay within one paragraph and cannot contain tabs")
        group, start, end = find_group(paragraph, old)
        selected = split_range(paragraph, group, start, end)
        insertion_index = paragraph.index(selected[0])
        sample = selected[0]
        if tracked:
            deleted = change_wrapper("w:del", ident, author)
            ident += 1
            for run in selected:
                paragraph.remove(run)
                for node in run:
                    if node.tag == T:
                        node.tag = qn("w:delText")
                deleted.append(run)
            paragraph.insert(insertion_index, deleted)
            insertion_index += 1
            if new:
                added = change_wrapper("w:ins", ident, author)
                ident += 1
                added.append(clone_run(sample, new))
                paragraph.insert(insertion_index, added)
        else:
            new_run = clone_run(sample, new) if new else None
            for run in selected:
                paragraph.remove(run)
            if new_run is not None:
                paragraph.insert(insertion_index, new_run)
    return len(changes)


def create_document(specification, base: Path):
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Arial"
    normal.font.size = Pt(11)
    for level in (1, 2, 3):
        doc.styles[f"Heading {level}"].font.color.rgb = RGBColor(0, 0, 0)
    title_style = doc.styles["Title"]
    title_style.font.color.rgb = RGBColor(0, 0, 0)
    title_style.font.underline = False
    style_ppr = title_style.element.pPr
    if style_ppr is not None:
        border = style_ppr.find(qn("w:pBdr"))
        if border is not None:
            style_ppr.remove(border)
    title = specification.get("title", "")
    if title:
        doc.add_heading(title, 0)
    blocks = specification.get("blocks", [])
    if not blocks:
        fail("Document needs at least one block")
    for block in blocks:
        kind = block.get("type")
        if kind == "heading":
            doc.add_heading(block.get("text", ""), level=max(1, min(3, int(block.get("level", 1)))))
        elif kind == "paragraph":
            doc.add_paragraph(block.get("text", ""))
        elif kind == "table":
            rows = block.get("rows", [])
            if not rows or not all(isinstance(r, list) and len(r) == len(rows[0]) for r in rows):
                fail("Table rows must be nonempty lists of equal length")
            if block.get("caption"):
                doc.add_paragraph(block["caption"])
            table = doc.add_table(rows=len(rows), cols=len(rows[0]))
            table.style = "Table Grid"
            for i, row in enumerate(rows):
                for j, cell in enumerate(row):
                    table.cell(i, j).text = str(cell)
            if len(rows[0]) > 5:
                # Wide tables need enough space for their longest ordinary words;
                # equal-width columns can split labels such as "Laparoscopic".
                weights = []
                for j in range(len(rows[0])):
                    tokens = [token for row in rows for token in str(row[j]).split()]
                    weights.append(max(6, min(18, max((len(t) for t in tokens), default=0))))
                section = doc.sections[-1]
                available = section.page_width - section.left_margin - section.right_margin
                table.autofit = False
                for j, weight in enumerate(weights):
                    width = Emu(int(available * weight / sum(weights)))
                    table.columns[j].width = width
                    for cell in table.columns[j].cells:
                        cell.width = width
                        for paragraph in cell.paragraphs:
                            for run in paragraph.runs:
                                run.font.size = Pt(9.5)
            doc.add_paragraph()
        elif kind == "figure":
            path = Path(block.get("path", ""))
            if not path.is_absolute():
                path = base / path
            doc.add_picture(str(path), width=Inches(min(float(block.get("width_inches", 5.8)), 6.0)))
            if block.get("caption"):
                doc.add_paragraph(block["caption"])
        else:
            fail(f"Unsupported block type: {kind!r}")
    return doc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("input")
    create = sub.add_parser("create")
    create.add_argument("--spec", required=True)
    create.add_argument("--out", required=True)
    comment = sub.add_parser("comment")
    comment.add_argument("input")
    comment.add_argument("--spec", required=True)
    comment.add_argument("--out", required=True)
    comment.add_argument("--author", default="Ye1")
    revise = sub.add_parser("revise")
    revise.add_argument("input")
    revise.add_argument("--spec", required=True)
    revise.add_argument("--marked", required=True)
    revise.add_argument("--clean", required=True)
    revise.add_argument("--author", default="Ye1")
    args = ap.parse_args()
    if args.command == "inspect":
        print(json.dumps(inspect_document(Document(args.input)), ensure_ascii=False, indent=2))
    elif args.command == "create":
        dest = safe_destination(args.out, [args.spec])
        doc = create_document(read_json(args.spec), Path(args.spec).resolve().parent)
        save_new(doc, dest)
        print(json.dumps({"output": str(dest)}, ensure_ascii=False))
    elif args.command == "comment":
        dest = safe_destination(args.out, [args.input, args.spec])
        doc = Document(args.input)
        n = apply_comments(doc, read_json(args.spec), args.author)
        save_new(doc, dest)
        print(json.dumps({"output": str(dest), "comments_added": n}, ensure_ascii=False))
    elif args.command == "revise":
        marked = safe_destination(args.marked, [args.input, args.spec, args.clean])
        clean = safe_destination(args.clean, [args.input, args.spec, args.marked])
        spec = read_json(args.spec)
        doc_marked, doc_clean = Document(args.input), Document(args.input)
        old_tracks = inspect_document(doc_marked)["existing_revision_elements"]
        count = apply_changes(doc_marked, spec, True, args.author)
        apply_changes(doc_clean, spec, False, args.author)
        marked_text = [visible_text(p) for p in paragraphs(doc_marked)]
        clean_text = [visible_text(p) for p in paragraphs(doc_clean)]
        if marked_text != clean_text:
            fail("Marked and clean views differ; no files written")
        save_new(doc_marked, marked)
        try:
            save_new(doc_clean, clean)
        except Exception:
            marked.unlink(missing_ok=True)
            raise
        print(json.dumps({"marked": str(marked), "clean": str(clean), "changes": count,
                          "preexisting_revision_elements": old_tracks}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
