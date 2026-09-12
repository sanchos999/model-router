#!/usr/bin/env python3
"""R15: DOCX + PDF generator for the Model Router manual (Router Control Center).

Reads docs/RUKOVODSTVO-R15.md, embeds fresh screenshots from state/e2e-shots,
writes:
  docs/Model_Router_v1_0_Rukovodstvo_R15.docx
  docs/Model_Router_v1_0_Rukovodstvo_R15.pdf
"""
import os
import re
import sys

from docx import Document
from docx.shared import Cm, Pt

ROOT = "/home/sanchos/model-router"
MD = os.path.join(ROOT, "docs/RUKOVODSTVO-R15.md")
SHOTS = os.path.join(ROOT, "state/e2e-shots")
OUT_DOCX = os.path.join(ROOT, "docs/Model_Router_v1_0_Rukovodstvo_R15.docx")
OUT_PDF = os.path.join(ROOT, "docs/Model_Router_v1_0_Rukovodstvo_R15.pdf")

# section -> screenshot (fresh R15 browser-E2E shots)
SHOT_MAP = {
    # R15: single fresh monitoring shot available from e2e
    "47. Production monitoring (R15)": "r15_monitoring.png",
    "5. Провайдеры": "r14_b_providers.png",
    "7. Каталог моделей": "r14_c_models.png",
    "36. Как Router принимает решение (R15)": "r14_e_routing.png",
    "40. Классы задач и уровни (tiers)": "r14_f_task_classes.png",
    "41. Временные правила (бывшие «Переопределения»)": "r14_g_rule_wizard.png",
    "42. Симулятор": "r14_h_simulator.png",
    "43. Журнал изменений (аудит)": "r14_i_audit.png",
    "44. Точки восстановления": "r14_j_settings.png",
}


def add_shots(doc, section_title: str) -> bool:
    fn = SHOT_MAP.get(section_title)
    if not fn:
        return False
    path = os.path.join(SHOTS, fn)
    if not os.path.exists(path):
        return False
    doc.add_picture(path, width=Cm(16))
    doc.paragraphs[-1].alignment = 1
    cap = doc.add_paragraph(f"Рисунок: {fn}")
    cap.alignment = 1
    for r in cap.runs:
        r.font.size = Pt(8)
        r.font.italic = True
    return True


def build_docx() -> int:
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "DejaVu Sans"
    st.font.size = Pt(10)

    title = doc.add_heading("Model Router v1.0 — Руководство администратора", 0)
    title.alignment = 1
    sub = doc.add_paragraph("Редакция R15 Router Control Center · 12.09.2026 · коммит fd14de4")
    sub.alignment = 1

    n_shots = 0
    for line in open(MD, encoding="utf-8").read().splitlines():
        if not line.strip():
            continue
        if line.startswith("## "):
            cur_section = line[3:].strip()
            doc.add_heading(cur_section, level=1)
            if add_shots(doc, cur_section):
                n_shots += 1
        elif line.startswith("    "):
            p = doc.add_paragraph(line.strip())
            for r in p.runs:
                r.font.name = "DejaVu Sans Mono"
                r.font.size = Pt(9)
        elif line.startswith("- ") or re.match(r"^\d+\.\s", line.strip()):
            doc.add_paragraph(line.strip(),
                              style="List Bullet" if line.startswith("- ") else "List Number")
        else:
            doc.add_paragraph(line.strip())
    doc.save(OUT_DOCX)
    return n_shots


def build_pdf() -> int:
    """PDF via PyMuPDF Story: text from the MD + embedded screenshots.
    Images referenced RELATIVE to the pymupdf.Archive dir (skill rule)."""
    import pymupdf

    html = ['<h1 style="text-align:center">Model Router v1.0 — '
            'Руководство администратора</h1>'
            '<p style="text-align:center">Редакция R15 Router Control Center · '
            '12.09.2026 · коммит fd14de4</p>']
    n_shots = 0
    for line in open(MD, encoding="utf-8").read().splitlines():
        if not line.strip():
            continue
        esc = (line.strip().replace("&", "&amp;").replace("<", "&lt;")
               .replace(">", "&gt;"))
        if line.startswith("## "):
            sec = line[3:].strip()
            html.append(f"<h2>{esc}</h2>")
            fn = SHOT_MAP.get(sec)
            if fn and os.path.exists(os.path.join(SHOTS, fn)):
                html.append(f'<p><img src="{fn}" style="width:400px"/></p>')
                n_shots += 1
        elif line.startswith("    "):
            html.append(f"<pre>{esc.strip()}</pre>")
        else:
            html.append(f"<p>{esc}</p>")
    story = pymupdf.Story("".join(html), archive=pymupdf.Archive(SHOTS))
    writer = pymupdf.DocumentWriter(OUT_PDF)
    mediabox = pymupdf.paper_rect("a4")
    where = mediabox + (36, 36, -36, -36)
    more = 1
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    d = pymupdf.open(OUT_PDF)
    return d.page_count


if __name__ == "__main__":
    n = build_docx()
    print(f"DOCX: {OUT_DOCX} ({os.path.getsize(OUT_DOCX)} bytes, {n} screenshots)")
    pages = build_pdf()
    print(f"PDF: {OUT_PDF} pages={pages} size={os.path.getsize(OUT_PDF)} shots={n}")
    sys.exit(0 if pages and n >= 7 else 1)
