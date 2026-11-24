#!/usr/bin/env python3
"""Snipaste镜像工作流入口。"""

import sys

from snipaste_mirror.pipeline import main


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"Snipaste镜像更新失败：{error}", file=sys.stderr)
        sys.exit(1)
