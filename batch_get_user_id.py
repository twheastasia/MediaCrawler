# -*- coding: utf-8 -*-
"""
从 SQLite 数据库的 xhs_note 和 xhs_note_comment 表中提取所有 user_id，
合并去重后保存到 user_id.json 文件。
"""

import json
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "database", "sqlite_tables.db")
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "user_id.json")


def get_user_ids_from_db(db_path: str) -> list[str]:
    """从 xhs_note 和 xhs_note_comment 表中读取所有 user_id，合并去重后返回排序列表。"""
    user_ids: set[str] = set()

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()

        for table in ("xhs_note", "xhs_note_comment"):
            cursor.execute(
                f"SELECT DISTINCT user_id FROM {table} WHERE user_id IS NOT NULL AND user_id != ''"
            )
            rows = cursor.fetchall()
            for (uid,) in rows:
                user_ids.add(uid.strip())

    return sorted(user_ids)


def save_user_ids(user_ids: list[str], output_path: str) -> None:
    """将 user_id 列表序列化为 JSON 文件。"""
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(user_ids, f, ensure_ascii=False, indent=2)
    print(f"已保存 {len(user_ids)} 个 user_id 到 {output_path}")


def main() -> None:
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"数据库文件不存在：{DB_PATH}")

    print(f"正在读取数据库：{DB_PATH}")
    user_ids = get_user_ids_from_db(DB_PATH)
    print(f"合并去重后共 {len(user_ids)} 个 user_id")

    save_user_ids(user_ids, OUTPUT_PATH)


if __name__ == "__main__":
    main()
