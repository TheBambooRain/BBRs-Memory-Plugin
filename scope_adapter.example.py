"""自定义平台 scope 适配器模板。

使用方法：
1. 复制到插件数据目录，并改名为 `scope_adapter.py`
   例如：
   .../plugin_data/astrbot_plugin_bbrsmemory_plugin/scope_adapter.py
2. 修改 resolve_scope(ctx)。
3. 重启 AstrBot。

返回 None 表示回退内置 AstrBot 逻辑。
"""

from __future__ import annotations


def resolve_scope(ctx: dict):
    """把当前平台身份转换成 scope 描述。

    ctx 由适配层传入，常见字段：
      umo / platform / kind / external_id
      sender_id / group_id / sender_name
      raw_event

    新平台可以完全忽略这些字段，根据自己的上下文取值。
    """
    # 示例 1：微信私聊，只有 usercode
    usercode = ctx.get("usercode") or ""
    group_code = ctx.get("group_code") or ""

    if group_code:
        return {
            "platform": "myapp",
            "kind": "group",
            "external_id": group_code,
            "binding_key": f"myapp:group:{group_code}",
            "alias_key": f"myapp:group:{group_code}",
            "label": "微信群",
        }

    if usercode:
        return {
            # 可选：直接指定稳定 scope_id；留空则按 platform/kind/external_id 查/建
            "scope_id": "",
            "platform": "myapp",
            "kind": "private",
            "external_id": usercode,
            "binding_key": f"myapp:private:{usercode}",
            "alias_key": f"myapp:private:{usercode}",
            "label": "微信私聊",
        }

    return None
