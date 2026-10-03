"""本地开发入口。

关键：``log_config=None`` 禁止 uvicorn 用自带的明文 logging 配置覆盖
``app.main`` 在导入时完成的 JSON 配置，保证本服务所有输出（含框架日志）
统一为单行 JSON。等价的命令行启动方式：
``uvicorn app.main:app --port 8000 --log-config '{}'``。
"""

import uvicorn

from app.main import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_config=None)
