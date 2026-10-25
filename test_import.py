import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import plugin  # noqa: E402

p = plugin.create_plugin()
tools = [n for n in dir(p) if n.startswith("memo_") and not n.startswith("memo_page")]
apis = [n for n in dir(p) if n.startswith("api_")]
print("PLUGIN_OK tools:", tools, "apis:", apis)
assert len(tools) == 4, f"expect 4 tools, got {tools}"
assert sorted(apis) == ["api_page_add_todo", "api_page_remove_todo", "api_page_set_todo", "api_page_state"], apis

assert "todos" in plugin.MemoConfig.model_fields
print("CONFIG_MODEL_OK")
