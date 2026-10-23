"""Export implemented internal RAG endpoints without reading environment/secrets."""
import json
from export_document_contract import ROOT, document_contract

if __name__ == "__main__":
    (ROOT / "contracts/rag-indexing.openapi.json").write_text(json.dumps(document_contract("/internal/rag/"),ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
