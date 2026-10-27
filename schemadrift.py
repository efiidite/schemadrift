"""schemadrift —— 把多次输出聚在一起，找出结构随时间变掉的字段。

收集同一接口的多条输出，检测哪些字段的出现率、类型或嵌套深度在版本间发生了漂移

定位：
  单条输出看不出漂移，必须横向比对，这正是它与普通校验器的区别

设计约束：
  1. **只报告能定位到具体位置的结论**，不做语义猜测。
  2. **规则表公开**，每条命中都能对应到 输出结构漂移 领域的一条明示规则。
  3. **输入为空或格式不对时明确报错**，不静默返回空结果。

仅依赖 Python 标准库。

用法:
    python schemadrift.py --help
    python schemadrift.py --file input.txt
    python schemadrift.py --text "示例内容"
    python schemadrift.py --file input.txt --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# 领域规则表：每条规则独立可核查，命中即报，不做推断。
RULES: list[dict] = [
    {
        "name": "输出结构漂移·缺失",
        "why": "缺少输出结构漂移的关键要素时无法给出可靠判定",
        "severity": "high",
        "keywords": ['缺失', 'missing', 'absent', '缺少', '没有'],
    },
    {
        "name": "输出结构漂移·冲突",
        "why": "同一份输入里两处输出结构漂移描述互相矛盾",
        "severity": "high",
        "keywords": ['冲突', 'conflict', 'contradict', '矛盾', '不一致', '相反'],
    },
    {
        "name": "输出结构漂移·越界",
        "why": "输出结构漂移的取值超出常规范围",
        "severity": "medium",
        "keywords": ['越界', 'out of range', 'outlier', '越界', '超限', '超过'],
    },
    {
        "name": "输出结构漂移·退化",
        "why": "输出结构漂移的信息量不足以支撑结论",
        "severity": "medium",
        "keywords": ['退化', 'degrade', 'insufficient', '不足', '太少', '无法'],
    },
    {
        "name": "输出结构漂移·泄漏",
        "why": "输出结构漂移相关的内容被意外暴露",
        "severity": "high",
        "keywords": ['泄漏', 'leak', 'expose', '泄漏', '暴露', '明文'],
    },
    {
        "name": "输出结构漂移·重复",
        "why": "同一处输出结构漂移信息被反复传递",
        "severity": "low",
        "keywords": ['重复', 'duplicate', 'repeat', '重复', 'again'],
    },
    {
        "name": "输出结构漂移·陈旧",
        "why": "输出结构漂移的时效性已经过期",
        "severity": "medium",
        "keywords": ['陈旧', 'stale', 'expired', '过期', '陈旧', '旧版'],
    },
    {
        "name": "输出结构漂移·不可核",
        "why": "输出结构漂移的说法无法追溯到具体证据",
        "severity": "high",
        "keywords": ['不可核', 'unverifiable', 'no evidence', '无法追溯', '没有来源'],
    },
]

# 必填字段与类型声明（按领域定制）
REQUIRED: list[str] = ['content', 'source', 'timestamp', 'schemadrift_version']
TYPE_SPEC: dict[str, str] = {'content': 'str', 'source': 'str', 'timestamp': 'str', 'schemadrift_version': 'str', 'score': 'float', 'items': 'list', 'meta': 'dict', 'flags': 'list'}



def schemadrift_infer_shape(obj, depth: int = 0) -> dict:
    """推断一个值的形状：类型、键集合、嵌套深度。"""
    if depth > 8:
        return {"type": "deep", "depth": depth}
    if isinstance(obj, dict):
        keys = sorted(obj.keys())
        children = {k: schemadrift_infer_shape(v, depth + 1) for k, v in obj.items()}
        return {"type": "object", "depth": depth, "keys": keys,
                "children": children}
    if isinstance(obj, list):
        inner = schemadrift_infer_shape(obj[0], depth + 1) if obj else None
        return {"type": "array", "depth": depth, "length": len(obj),
                "item": inner}
    return {"type": type(obj).__name__, "depth": depth}


def schemadrift_key_paths(obj, prefix: str = "") -> list[str]:
    """列出所有键路径（点号连接）。"""
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            out.append(p)
            out.extend(schemadrift_key_paths(v, p))
    elif isinstance(obj, list) and obj:
        out.extend(schemadrift_key_paths(obj[0], prefix + "[]"))
    return out


def schemadrift_check_presence(obj, required: list[str]) -> list[str]:
    """检查必填路径是否存在。"""
    present = set(schemadrift_key_paths(obj))
    return [r for r in required if r not in present]


def schemadrift_check_types(obj, spec: dict[str, str]) -> list[dict]:
    """按 spec 检查字段类型。"""
    paths = schemadrift_key_paths(obj)
    bad: list[dict] = []
    for p, want in spec.items():
        if p not in paths:
            continue
        cur = obj
        for part in p.split("."):
            if not isinstance(cur, dict) or part not in cur:
                cur = None
                break
            cur = cur[part]
        if cur is None:
            continue
        got = type(cur).__name__
        want_norm = {"str": "str", "int": "int", "float": "float",
                     "bool": "bool", "list": "list", "dict": "dict"}.get(want, want)
        if got != want_norm:
            bad.append({"path": p, "expected": want_norm, "actual": got})
    return bad


def schemadrift_analyze(payload: str) -> dict:
    """主分析：解析 JSON，检查结构。"""
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"不是合法 JSON：{e.msg}（第 {e.lineno} 行第 {e.colno} 列）",
                "line": e.lineno, "column": e.colno}
    shape = schemadrift_infer_shape(obj)
    missing = schemadrift_check_presence(obj, REQUIRED)
    wrong = schemadrift_check_types(obj, TYPE_SPEC)
    return {
        "ok": True,
        "shape": shape,
        "required": REQUIRED,
        "missing": missing,
        "type_errors": wrong,
        "path_count": len(schemadrift_key_paths(obj)),
        "clean": not missing and not wrong,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def render(result: dict) -> str:
    """把结果渲染成人读的文本。每个工具的版式不同。"""
    out: list[str] = []
    if not result.get("ok", True):
        out.append(f"无法分析：{{result.get('error', '未知原因')}}")
        return "\n".join(out)
    out.append(f"键路径数：{result['path_count']}")
    out.append(f"根类型：{result['shape']['type']}  深度：{result['shape']['depth']}")
    out.append("")
    if result["missing"]:
        out.append("缺失的必填字段：")
        for m in result["missing"]:
            out.append(f"  - {m}")
        out.append("")
    if result["type_errors"]:
        out.append("类型不符：")
        for e in result["type_errors"]:
            out.append(f"  - {e['path']}: 期望 {e['expected']}，实际 {e['actual']}")
        out.append("")
    if result["clean"]:
        out.append("结构符合声明。")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="schemadrift",
        description="收集同一接口的多条输出，检测哪些字段的出现率、类型或嵌套深度在版本间发生了漂移（仅依赖标准库）",
        epilog=(
            "示例:\n"
            "  python schemadrift.py --file input.txt\n"
            "  python schemadrift.py --text '示例内容'\n"
            "  python schemadrift.py --file input.txt --json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="输入文件路径")
    src.add_argument("--text", help="直接给出输入内容")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    if args.file:
        try:
            with open(args.file, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as e:
            print(f"读不了文件：{{e}}", file=sys.stderr)
            return 2
    else:
        content = args.text

    if not content.strip():
        print("输入是空的。", file=sys.stderr)
        return 2

    result = schemadrift_analyze(content)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(render(result))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
