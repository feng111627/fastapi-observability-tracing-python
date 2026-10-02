"""本地启动入口：python main.py"""

from __future__ import annotations

import uvicorn

from app.main import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
