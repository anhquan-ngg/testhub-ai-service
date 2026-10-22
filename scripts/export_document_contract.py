"""Export only the implemented extraction API and its referenced schemas."""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.main import create_app


def document_contract(prefix: str = "/internal/documents/") -> dict:
    """Derive the versioned contract without starting providers or reading .env."""
    schema = create_app().openapi()
    schema["paths"] = {k: v for k, v in schema["paths"].items() if k.startswith(prefix)}
    models = schema.get("components", {}).get("schemas", {})
    wanted: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            reference = value.get("$ref", "")
            if isinstance(reference, str) and reference.startswith("#/components/schemas/"):
                name = reference.rsplit("/", 1)[-1]
                if name not in wanted:
                    wanted.add(name)
                    visit(models[name])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(schema["paths"])
    schema["components"]["schemas"] = {k: v for k, v in models.items() if k in wanted}
    return schema


if __name__ == "__main__":
    target = ROOT / "contracts/document-extraction.openapi.json"
    target.write_text(json.dumps(document_contract(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
