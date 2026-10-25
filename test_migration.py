"""自测脚本：模拟旧版 memo_state.json 的待办迁移到 config.toml。"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plugin  # noqa: E402

tmp = Path(tempfile.mkdtemp())
# 造一份旧格式数据文件
legacy = {
    "todos": {
        "m1a2b": {
            "id": "m1a2b", "user_id": "10000", "content": "买牛奶",
            "note": "脱脂的", "platform": "qq", "source_stream_id": "stream_x",
            "user_nickname": "小明", "created_at": 1761000000.0, "done": False,
            "done_at": 0.0, "remind_count": 1, "last_reminded_at": 1761001000.0,
            "next_remind_at": 1761003000.0,
        }
    },
    "last_mention": {"10000": 1761001000.0},
    "nicknames": {"10000": "小明"},
}
(tmp / "memo_state.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

p = plugin.create_plugin()
# 手动接线：config 默认实例 + store（config 是只读属性，底层字段是 _plugin_config_instance）
p._plugin_config_instance = plugin.MemoConfig()
store = plugin.MemoStore(tmp / "memo_state.json")
store.load()
p._store = store

# _save_todos_to_disk 用 __file__ 所在目录的 config.toml，先指到临时目录
config_file = tmp / "config.toml"
config_file.write_text('# 注释测试\nenabled = true\n\n[plugin]\nconfig_version = "1.0.0"\n\n[todos]\n', encoding="utf-8")
plugin.MemoTodoPlugin._config_path.__doc__  # noqa
p.__class__._config_path = property(lambda self: config_file)

p._migrate_legacy_todos()
p._store.save()  # 模拟 on_load 中迁移后的数据文件重写
p._save_todos_to_disk()  # 幂等再写一次

text = config_file.read_text(encoding="utf-8")
assert text.startswith("# 注释测试"), "注释丢失！"
assert "[todos.m1a2b]" in text, "待办表未写入"
assert 'content = "买牛奶"' in text
assert 'note = "脱脂的"' in text
state = json.loads((tmp / "memo_state.json").read_text(encoding="utf-8"))
assert "todos" not in state and state["last_mention"], "数据文件未清理或节流状态丢失"
assert len(p.config.todos) == 1 and p.config.todos["m1a2b"].remind_count == 1
print("MIGRATION_OK 注释保留 + 待办入表 + 状态文件清理")
