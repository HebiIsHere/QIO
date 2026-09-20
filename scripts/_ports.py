"""端口可连通性探测（stdlib）。

注意：本机某些受限环境下 `Get-NetTCPConnection` 会安静地返回空列表
（看起来「没有监听」，实际服务还在），所以停止/启动脚本一律用真实 TCP
连接来确认端口状态，而不是相信端口表。
"""

from __future__ import annotations

import socket


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    with socket.socket() as sock:
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False
