#!/usr/bin/env python3
"""微信镜像工作流入口。"""

import sys

from wechat_mirror.pipeline import main


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"微信镜像更新失败：{error}", file=sys.stderr)
        sys.exit(1)
