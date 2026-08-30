from pathlib import Path
from docx import Document
from docx.shared import Inches

def create_report(results: list[dict], destination: Path) -> None:
    doc = Document(); doc.add_heading('Website Test Evidence Report', 0); doc.add_paragraph(f"{len(results)} tests recorded.")
    for index, result in enumerate(results, 1):
        doc.add_page_break(); doc.add_heading(f"{index}. {result.get('title', 'Unnamed test')}", 1); doc.add_paragraph(f"Status: {result.get('status','unknown').upper()}")
        if result.get('error'): doc.add_heading('What happened', 2); doc.add_paragraph(str(result['error']))
        doc.add_heading('Browser evidence', 2); attachments = result.get('attachments', [])
        if not attachments: doc.add_paragraph('No verified evidence was captured.')
        for image in attachments:
            path = Path(image)
            if path.is_file(): doc.add_picture(str(path), width=Inches(6.2)); doc.add_paragraph(path.name)
    destination.parent.mkdir(parents=True, exist_ok=True); doc.save(destination)
