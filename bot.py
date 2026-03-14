"""
NoneBot2 启动入口。

运行方式：
    nb run
或
    python bot.py
"""

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter


def main() -> None:
    """初始化 NoneBot 并加载 OneBot v11 适配器与本地插件。"""
    nonebot.init()
    driver = nonebot.get_driver()
    driver.register_adapter(OneBotV11Adapter)
    nonebot.load_plugins("plugins")
    nonebot.run()


if __name__ == "__main__":
    main()
