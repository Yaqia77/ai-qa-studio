"""config.yaml 环境段写回：行级替换保留其余章节注释，校验通过才落盘。

设计对齐 excel_writer 的安全约定：
- 只重写 default_env 行与 env: 区块，report/log 等其他章节及其注释原样保留
  （env: 区块内的行内注释会丢失，前端已提示）；
- 写前先证实原文件可被 yaml 解析（recognize 不了原样绝不动笔）；
- 生成的新文本必须：yaml 可解析 + env/default_env 语义与预期完全一致 + 其余章节未被扰动，三关全过才落盘；
- 落盘前备份 config.yaml.bak（gitignore），tmp 临时文件 + os.replace 原子替换。
"""
import json
import os
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "config.yaml"


class ConfigWriteError(Exception):
    """kind: invalid（值非法/新文本校验不过）/ not_found / locked（文件被占用）"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def save_envs(default_env: str, envs: list[dict]) -> None:
    """把环境配置写回 config.yaml；payload 非法或产物校验不过时原文件字节不动。"""
    expected = _validate(default_env, envs)
    if not CONFIG_PATH.is_file():
        raise ConfigWriteError("not_found", "配置文件不存在: config/config.yaml")

    text = CONFIG_PATH.read_text(encoding="utf-8")
    try:
        base = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigWriteError("invalid", f"当前 config.yaml 无法解析，已拒绝保存以免恶化: {e}")
    if not isinstance(base, dict):
        raise ConfigWriteError("invalid", "config.yaml 顶层结构不是映射，无法安全改写")
    old_rest = {k: v for k, v in base.items() if k not in ("env", "default_env")}

    new_text = _replace_default_env(text, default_env)
    new_text = _replace_env_section(new_text, expected)

    # 三重校验：可解析 + 目标段语义一致 + 其余章节未被扰动
    try:
        check = yaml.safe_load(new_text)
    except yaml.YAMLError as e:
        raise ConfigWriteError("invalid", f"生成的配置文本解析失败（内部 bug，已拒绝落盘）: {e}")
    if not isinstance(check, dict):
        raise ConfigWriteError("invalid", "生成的配置顶层结构异常，已拒绝落盘")
    if check.get("env") != _expected_env_dict(expected) or check.get("default_env") != default_env:
        raise ConfigWriteError("invalid", "写回结果与环境预期不一致，已拒绝落盘")
    rest = {k: v for k, v in check.items() if k not in ("env", "default_env")}
    if rest != old_rest:
        raise ConfigWriteError("invalid", "写回影响到了 env/default_env 之外的配置段，已拒绝落盘")

    _atomic_write(new_text)


def _validate(default_env: str, envs: list[dict]) -> list[dict]:
    """校验并归一化，返回干净的 env 列表（name 保序去重）。"""
    if not isinstance(envs, list) or not envs:
        raise ConfigWriteError("invalid", "至少需要一个环境")
    de = str(default_env or "").strip()
    if not de:
        raise ConfigWriteError("invalid", "default_env 不能为空")
    out: list[dict] = []
    seen: set[str] = set()
    for raw in envs:
        name = str(raw.get("name") or "").strip()
        if not name or re.search(r"[\r\n]", name):
            raise ConfigWriteError("invalid", f"环境名非法: {name!r}")
        if name in seen:
            raise ConfigWriteError("invalid", f"环境名重复: {name}")
        seen.add(name)
        base_url = str(raw.get("base_url") or "").strip()
        if not re.match(r"^https?://\S+$", base_url):
            raise ConfigWriteError("invalid", f"环境 {name} 的 base_url 必须以 http(s):// 开头")
        timeout = raw.get("timeout")
        if timeout is not None:
            try:
                timeout = float(timeout)
            except (TypeError, ValueError):
                raise ConfigWriteError("invalid", f"环境 {name} 的 timeout 必须是数字")
            if timeout <= 0:
                raise ConfigWriteError("invalid", f"环境 {name} 的 timeout 必须大于 0")
        headers = raw.get("default_headers") or {}
        if not isinstance(headers, dict):
            raise ConfigWriteError("invalid", f"环境 {name} 的 default_headers 必须是对象")
        clean_headers: dict[str, str] = {}
        for k, v in headers.items():
            k, v = str(k), str(v)
            if not k.strip() or "\n" in k or "\n" in v or "\r" in k or "\r" in v:
                raise ConfigWriteError("invalid", f"环境 {name} 的请求头 {k!r} 非法（键为空或含换行）")
            clean_headers[k.strip()] = v
        out.append({"name": name, "base_url": base_url, "timeout": timeout,
                    "verify_ssl": bool(raw.get("verify_ssl", True)),
                    "default_headers": clean_headers})
    if de not in seen:
        raise ConfigWriteError("invalid", f"default_env {de!r} 必须是环境之一: {sorted(seen)}")
    return out


def _scalar(value) -> str:
    """YAML 标量输出：安全形态直接 plain（与存量文件书写习惯一致），否则 json 双引号。

    只处理字符串标量——布尔/数字由调用方内联。形似 true/false/null/数字的字符串
    （如请求头值 "no"、"123"）会走 json 双引号，避免被 YAML 误解析成非字符串类型。
    """
    s = str(value)
    dangerous = (bool(re.match(r"(?i)^(true|false|yes|no|on|off|null|~)$", s))
                 or bool(re.match(r"^-?[\d.]", s)))
    plain_ok = (re.match(r"^[A-Za-z_\u4e00-\u9fa5][A-Za-z0-9_\-./: {}\u4e00-\u9fa5]*$", s)
                and ": " not in s and " #" not in s and not s.endswith(":"))
    if not dangerous and plain_ok:
        return s
    return json.dumps(s, ensure_ascii=False)


def _expected_env_dict(envs: list[dict]) -> dict:
    """按 _serialize_envs 的落盘语义构造期望的 env 映射，供 yaml 读回后逐项比对。

    须与序列化保持一致：timeout 整数化、None 时整键省略、verify_ssl 恒有、default_headers 恒有（空则 {}）。
    """
    d: dict = {}
    for e in envs:
        item: dict = {"base_url": e["base_url"]}
        t = e["timeout"]
        if t is not None:
            item["timeout"] = int(t) if float(t).is_integer() else float(t)
        item["verify_ssl"] = e["verify_ssl"]
        item["default_headers"] = e["default_headers"]
        d[e["name"]] = item
    return d


def _serialize_envs(envs: list[dict]) -> str:
    lines = ["env:"]
    for e in envs:
        lines.append(f"  {_scalar(e['name'])}:")
        lines.append(f"    base_url: {_scalar(e['base_url'])}")
        if e["timeout"] is not None:
            t = e["timeout"]
            lines.append(f"    timeout: {int(t) if float(t).is_integer() else t}")
        lines.append(f"    verify_ssl: {'true' if e['verify_ssl'] else 'false'}")
        if e["default_headers"]:
            lines.append("    default_headers:")
            for k, v in e["default_headers"].items():
                lines.append(f"      {_scalar(k)}: {_scalar(v)}")
        else:
            lines.append("    default_headers: {}")
    return "\n".join(lines) + "\n"


def _replace_default_env(text: str, default_env: str) -> str:
    pat = re.compile(r"^default_env:.*$", re.MULTILINE)
    if pat.search(text):
        return pat.sub(f"default_env: {default_env}", text, count=1)
    return f"default_env: {default_env}\n\n" + text


def _replace_env_section(text: str, envs: list[dict]) -> str:
    """env: 顶层键区块整段替换；保留区块尾部空行以维持段落间距。不存在则文件末尾追加。"""
    block = _serialize_envs(envs)  # 以 \n 结尾
    lines = text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^env:", line):
            start = i
            break
    if start is None:
        if text and not text.endswith("\n"):
            text += "\n"
        return text + "\n" + block
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^\S", lines[j]):  # 下一个顶格行（含 # 注释行）即边界
            end = j
            break
    # 回退收集区块尾部（下一顶格行之前）的空行，原样保留段间距
    trail = ""
    k = end - 1
    while k > start and lines[k].strip() == "":
        trail = lines[k] + trail
        k -= 1
    return "".join(lines[:start]) + block + trail + "".join(lines[end:])


def _atomic_write(new_text: str) -> None:
    # 备份原文件（.bak 已 gitignore；出错可人工回滚）
    try:
        bak = CONFIG_PATH.with_suffix(".yaml.bak")
        bak.write_text(CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        pass  # 备份失败不阻断保存主流程
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".platform-write.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fp:
            fp.write(new_text)
        os.replace(tmp, CONFIG_PATH)
    except PermissionError:
        raise ConfigWriteError("locked", "config.yaml 正被其他程序占用，请关闭后重试")
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
