import uvicorn

from app.main import app

if __name__ == "__main__":
    # log_config=None：不让 uvicorn 安装自己的明文日志 handler，
    # 服务器日志全部传播到根 logger，走统一的 JSON 输出。
    uvicorn.run(app, host="127.0.0.1", port=8000, log_config=None)
