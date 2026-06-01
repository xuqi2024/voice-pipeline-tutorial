# 导入统一日志模块
from app.core.logger import setup_logging, get_logger

# 设置日志（只调用一次）
setup_logging()

import socket
import uvicorn
from .core.config import settings

# 设置日志
logger = get_logger(__name__)


def get_local_ip() -> str:
    """获取本机IP地址"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    try:
        logger.start(f"开发环境服务启动，监听地址: {settings.host}:{settings.port}")
        logger.info(f"API文档: http://{settings.host}:{settings.port}/voiceprint/docs")
        logger.info("=" * 60)
        local_ip = get_local_ip()
        logger.info(
            f"声纹接口地址: http://{local_ip}:{settings.port}/voiceprint/health?key="
            + settings.api_token
        )
        logger.info("=" * 60)

        # reload=False 确保 uvicorn 进程退出时容器也退出，触发 Docker 自动重启
        uvicorn.run(
            "app.application:app",
            host=settings.host,
            port=settings.port,
            reload=False,
            workers=1,
            access_log=False,
            log_level="info",
        )
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在退出服务。")
    except Exception as e:
        logger.fail(f"服务启动失败: {e}")
        raise
