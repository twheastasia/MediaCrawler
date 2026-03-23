# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/config/xhs_config.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。


# Xiaohongshu platform configuration

# Sorting method, the specific enumeration value is in media_platform/xhs/field.py
SORT_TYPE = "popularity_descending"

# Specify the note URL list, which must carry the xsec_token parameter
XHS_SPECIFIED_NOTE_URL_LIST = [
    "https://www.xiaohongshu.com/explore/64b95d01000000000c034587?xsec_token=AB0EFqJvINCkj6xOCKCQgfNNh8GdnBC_6XecG4QOddo3Q=&xsec_source=pc_cfeed"
    # ........................
]

# Specify the creator URL list, which needs to carry xsec_token and xsec_source parameters.
# 支持完整 URL（含 xsec_token）或纯 user_id（24 位 hex）两种格式。
# 默认从项目根目录的 user_id.json 读取，排除 .batch_progress_creator 中已处理的 id。

import json as _json
import os as _os


def _load_pending_creator_ids() -> list[str]:
    """从 user_id.json 加载待处理 user_id，自动排除 .batch_progress_creator 中已完成的。

    支持通过环境变量切片，用于多机并行：
      XHS_CREATOR_START  起始索引（1-based，含），默认 1
      XHS_CREATOR_END    结束索引（1-based，含），默认最后一条
    """
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    user_id_file = _os.path.join(root, "user_id.json")
    progress_file = _os.path.join(root, ".batch_progress_creator")

    if not _os.path.exists(user_id_file):
        # user_id.json 不存在时回退到空列表（可在此处手动填写 URL）
        return []

    with open(user_id_file, encoding="utf-8") as f:
        all_ids: list[str] = _json.load(f)

    # 读取切片范围（1-based）
    start_idx = int(_os.environ.get("XHS_CREATOR_START", "1"))
    end_idx = int(_os.environ.get("XHS_CREATOR_END", str(len(all_ids))))
    # 转为 0-based slice
    all_ids = all_ids[start_idx - 1 : end_idx]

    done_ids: set[str] = set()
    if _os.path.exists(progress_file):
        with open(progress_file, encoding="utf-8") as f:
            done_ids = {line.strip() for line in f if line.strip()}

    pending = [uid for uid in all_ids if uid not in done_ids]
    return pending


XHS_CREATOR_ID_LIST = _load_pending_creator_ids()
