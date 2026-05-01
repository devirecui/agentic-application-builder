import os
import re
from pathlib import Path


def parse_resume(path: str) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Resume not found at {path}")

    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        text = _extract_pdf(path)
    elif ext in (".docx", ".doc"):
        text = _extract_docx(path)
    else:
        raise ValueError(f"Unsupported resume format: {ext}")

    return _structure_text(text)


def _extract_pdf(path: str) -> str:
    import pdfplumber
    parts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            parts.append(t)
    return "\n".join(parts)


def _extract_docx(path: str) -> str:
    from docx import Document
    doc = Document(path)
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def _structure_text(text: str) -> dict:
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    name = lines[0] if lines else ""
    email = _find(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    phone = _find(r"(\+?\d[\d\s().-]{8,}\d)", text)
    linkedin = _find(r"(linkedin\.com/in/[\w-]+)", text, flags=re.I)

    sections = _split_sections(lines)

    return {
        "name": name,
        "contact": {
            "email": email,
            "phone": phone,
            "linkedin": linkedin,
        },
        "summary": sections.get("summary", ""),
        "experience": sections.get("experience", []),
        "skills": sections.get("skills", []),
        "education": sections.get("education", []),
        "certifications": sections.get("certifications", []),
        "raw_text": text,
    }


def _find(pattern: str, text: str, flags: int = 0) -> str:
    m = re.search(pattern, text, flags)
    return m.group(0) if m else ""


SECTION_KEYWORDS = {
    "summary": ["summary", "profile", "objective", "about"],
    "experience": ["experience", "employment", "work history", "professional experience"],
    "skills": ["skills", "technical skills", "core competencies", "technologies"],
    "education": ["education", "academic"],
    "certifications": ["certifications", "certificates", "licenses"],
}


def _split_sections(lines: list[str]) -> dict:
    out = {k: [] for k in SECTION_KEYWORDS}
    out["summary"] = ""
    current = None

    for line in lines:
        low = line.lower().strip(":").strip()
        matched = None
        for key, words in SECTION_KEYWORDS.items():
            if low in words or any(low.startswith(w) and len(low) <= len(w) + 2 for w in words):
                matched = key
                break
        if matched:
            current = matched
            continue
        if current == "summary":
            out["summary"] = (out["summary"] + " " + line).strip()
        elif current:
            out[current].append(line)

    if isinstance(out["skills"], list) and len(out["skills"]) == 1:
        out["skills"] = [s.strip() for s in re.split(r"[,;|·•]", out["skills"][0]) if s.strip()]

    return out
