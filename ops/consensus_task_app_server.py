from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    from ops.consensus_task_app.app import app

    uvicorn.run(
        app,
        host=os.getenv("OCR_CONSENSUS_HOST", "0.0.0.0"),
        port=int(os.getenv("OCR_CONSENSUS_PORT", "8090")),
        reload=False,
    )
