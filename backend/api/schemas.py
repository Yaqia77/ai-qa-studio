"""接口自动化请求/响应模型（自动生成 OpenAPI 文档的类型契约）。"""
from typing import Any

from pydantic import BaseModel, Field


class CaseOut(BaseModel):
    """一条用例（对应 core.models.TestCase 的对外视图）。"""

    case_id: str
    module: str
    api_name: str
    case_desc: str
    priority: str
    run: bool
    method: str
    url: str
    headers: dict | None = None
    params: dict | None = None
    body: Any = None
    extract: dict | None = None
    asserts: list | None = None
    timeout: float | None = None
    pre_scripts: list[str] = Field(default_factory=list, description="前置公共脚本名（Excel 逗号分隔列）")
    post_scripts: list[str] = Field(default_factory=list, description="后置公共脚本名")
    source: str = Field(description="来源 Excel 文件名")
    row: int = Field(description="来源 Excel 行号")


class CasePage(BaseModel):
    total: int
    items: list[CaseOut]


class CaseUpdate(BaseModel):
    """用例更新（只传需要改的字段；case_id 不可改，它是写回定位的行锁锚点）。"""

    module: str | None = None
    api_name: str | None = None
    case_desc: str | None = None
    priority: str | None = Field(default=None, description="P0~P5")
    run: bool | None = Field(default=None, description="是否执行（写回 Y/N）")
    method: str | None = Field(default=None, description="GET/POST/PUT/DELETE/PATCH")
    url: str | None = None
    headers: dict | None = None
    params: dict | None = None
    body: Any = None
    extract: dict | None = None
    asserts: list | None = Field(default=None, description="[[操作符, 取值路径, 期望值], ...]")
    timeout: float | None = Field(default=None, description="秒，正数；传 null 清空用 exclude 控制")
    pre_scripts: list[str] | None = Field(default=None, description="前置公共脚本名列表（写回「前置脚本」列，逗号分隔）")
    post_scripts: list[str] | None = Field(default=None, description="后置公共脚本名列表")


class CaseCreate(BaseModel):
    """新增用例（整行写入 Excel 末行；编号唯一）。"""

    case_id: str = Field(description="用例编号（唯一，写回定位的行锁锚点）")
    api_name: str = Field(description="接口名称（必填）")
    url: str = Field(description="请求路径（必填）")
    method: str = Field(description="GET/POST/PUT/DELETE/PATCH")
    asserts: list | None = Field(default=None, description="[[操作符, 取值路径, 期望值], ...]，至少一条")
    module: str = "未分类"
    case_desc: str = "新建用例"
    priority: str = Field(default="P2", description="P0~P5")
    run: bool = Field(default=True, description="是否执行（写回 Y/N）")
    headers: dict | None = None
    params: dict | None = None
    body: Any = None
    extract: dict | None = None
    timeout: float | None = None
    pre_scripts: list[str] | None = Field(default=None, description="前置公共脚本名列表")
    post_scripts: list[str] | None = Field(default=None, description="后置公共脚本名列表")


class CaseBatchCreate(BaseModel):
    """智能导入确认落盘：批量用例（前端预览确认后原样回传）。"""

    cases: list[CaseCreate] = Field(description="预览确认后的用例列表")


class CaseMove(BaseModel):
    """迁移用例到另一个项目（目标文件）：目标查重 → 写入目标 → 源删除。"""

    to_file: str = Field(description="目标项目的用例 Excel 路径（相对项目根）")
    new_module: str | None = Field(default=None, description="迁移后归入的模块名（留空=保留原模块）")


# ==================== 公共脚本（前后置处理器，设计文档 2.4） ====================


class ScriptCreate(BaseModel):
    """新建公共脚本（步骤为声明式定义，动作白名单见 core/scripts.py）。"""

    name: str = Field(description="脚本名（唯一；不能含逗号/空格，用例按名引用）")
    remark: str = Field(default="", description="用途说明")
    steps: list[dict[str, Any]] = Field(
        description="步骤链，每步 {action, ...}，action 取 request/sleep/set_var/assert")


class ScriptUpdate(BaseModel):
    """更新脚本（只传改动字段；已被引用的脚本改名会被拦下）。"""

    name: str | None = None
    remark: str | None = None
    steps: list[dict[str, Any]] | None = None


class ScriptOut(BaseModel):
    """脚本对外视图（附引用它的用例编号，供页面做引用保护提示）。"""

    name: str
    remark: str = ""
    steps: list[dict[str, Any]] = Field(default_factory=list)
    used_by: list[str] = Field(default_factory=list, description="引用该脚本的用例编号")
    updated_at: str = ""


# ==================== 接口定义库（设计文档 2.2） ====================


class ApiDefCreate(BaseModel):
    """新建接口定义（接口名唯一，同一 方法+路径 也不允许用两个名字重复登记）。"""

    name: str = Field(description="接口名称（与用例的「接口名称」列对齐）")
    method: str = Field(description="GET/POST/PUT/DELETE/PATCH")
    path: str = Field(description="请求路径（/ 或 http(s):// 开头；路径参数写 {{id}} 占位符）")
    module: str = Field(default="未分类", description="所属模块")
    remark: str = Field(default="", description="用途说明")
    headers: dict | None = Field(default=None, description="推荐请求头")
    params: dict | None = Field(default=None, description="推荐查询参数（示例值）")
    body: Any = Field(default=None, description="推荐请求体（示例值）")
    extract: dict | None = Field(default=None, description="推荐变量提取，如 {\"token\": \"$.data.token\"}")
    asserts: list | None = Field(default=None, description="推荐断言 [[操作符, 取值路径, 期望值], ...]")


class ApiDefUpdate(BaseModel):
    """更新接口定义（只传改动字段；被用例同名引用的接口不可改名/不可删）。"""

    name: str | None = None
    method: str | None = None
    path: str | None = None
    module: str | None = None
    remark: str | None = None
    headers: dict | None = None
    params: dict | None = None
    body: Any = None
    extract: dict | None = None
    asserts: list | None = None


class ApiDefOut(BaseModel):
    """接口定义对外视图（used_by 为同名的存量用例编号，即该接口的用例覆盖情况）。"""

    name: str
    module: str = "未分类"
    method: str
    path: str
    remark: str = ""
    headers: dict | None = None
    params: dict | None = None
    body: Any = None
    extract: dict | None = None
    asserts: list | None = None
    source: str = Field(default="manual", description="来源：manual/excel/openapi")
    used_by: list[str] = Field(default_factory=list, description="接口名称同名的用例编号")
    updated_at: str = ""


class OpenApiImport(BaseModel):
    """导入 OpenAPI / Swagger 文档原文（JSON 或 YAML 均可）。"""

    text: str = Field(description="文档原文（整份 spec，不是 URL）")
    overwrite: bool = Field(default=False, description="同名接口已存在时是否覆盖（false 则逐条跳过）")


class SkeletonCreate(BaseModel):
    """由接口定义生成用例骨架（一行一个用例编号）。"""

    case_ids: list[str] = Field(description="要生成的用例编号列表（每个编号写入一行）")
    case_desc: str | None = Field(default=None, description="用例描述，留空用「接口名 正常流程」")
    priority: str = Field(default="P2", description="P0~P5")
    run: bool = Field(default=True, description="是否执行（Y/N）")
    dry_run: bool = Field(default=True, description="默认只返回预览行，确认后再落 Excel")
