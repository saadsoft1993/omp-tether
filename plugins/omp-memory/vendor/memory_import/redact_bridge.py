"""Redaction for the memory importer: the Hindsight proxy's own rules, called as a filter.

Reads `{"texts": [...]}` on stdin, writes `{"results": [{"text", "counts", "residual"}, ...]}` on stdout. `text` is
`hindsight_proxy.redact.redact_text`; `residual` is what the Hindsight import's own residual scan
(`hindsight_import.import_mnemopi.import_scan`, which tolerates public claude.ai artifact ids) still finds after
redaction, as `[kind, offset]` pairs. A text the proxy rejects (too large) comes back as `{"error": "..."}`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hindsight_import.import_mnemopi import import_scan
from hindsight_proxy.redact import RejectBody, redact_text, residual_findings


def one(text: str) -> dict:
	try:
		redacted, counts = redact_text(text)
	except RejectBody as err:
		return {"error": f"{type(err).__name__}: {err}"}
	residual = [[kind, offset] for kind, offset in import_scan(redacted, residual_findings)]
	return {"text": redacted, "counts": dict(counts), "residual": residual}


def main() -> int:
	payload = json.load(sys.stdin)
	texts = payload["texts"]
	json.dump({"results": [one(text) for text in texts]}, sys.stdout, ensure_ascii=False)
	return 0


if __name__ == "__main__":
	sys.exit(main())
