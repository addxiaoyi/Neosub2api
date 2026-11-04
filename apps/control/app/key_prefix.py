"""网关密钥前缀的唯一来源：签发、存库、回显掩码、日志脱敏正则都从这里取。

前缀值是存量密钥的口径，改它等于让库里所有 key 立刻失效，所以全仓只允许在这里定义一次。
"""

# 存库前缀在前缀之后保留的随机段位数；生成走 token_urlsafe(24)（32 位），这里只截前 6 位供界面辨认
PREFIX_RANDOM_KEEP = 6

KEY_PREFIX = "u2a_"
STORED_PREFIX_LEN = len(KEY_PREFIX) + PREFIX_RANDOM_KEEP


def mask_prefix(prefix: str) -> str:
    """回显前缀 + 随机段前 4 位 + ****；非前缀形态按前 4 位兜底，任何场景都不输出完整密钥。"""
    if prefix.startswith(KEY_PREFIX):
        return f"{KEY_PREFIX}{prefix[len(KEY_PREFIX):][:4]}****"
    return f"{prefix[:4]}****"
