"""UI 自动化请求/响应模型（自动生成 OpenAPI 文档的类型契约）。"""
from typing import Any

from pydantic import BaseModel, Field


# ==================== UI 用例（JSON 载体，结构化步骤） ====================


class UiStep(BaseModel):
    """一个 UI 步骤：动作 + 定位器 + 取值（三者必要性由动作决定，见 ui_case_store.ACTIONS）。"""

    action: str = Field(description="动作键（完整集合与中文名见 ui_case_store.ACTIONS，随 /api/ui-cases/meta 下发；如 goto/click/fill/upload/drag_to/switch_frame/expect_* 等）")
    locator: str | None = Field(default=None, description="定位器：css=(缺省)/xpath=/text=/role=/testid=/placeholder=/alt= 前缀")
    value: Any = Field(default=None, description="取值/期望值（支持 {{var}} 变量）")
    desc: str = Field(default="", description="步骤备注")


class UiExtract(BaseModel):
    """变量提取：source 形如 text|<locator> / input_value|<locator> / value|<locator> / attr|<sel>:<name> / url / title / storage:<key> / cookie:<name>。"""

    var: str = Field(description="变量名")
    source: str = Field(description="提取来源表达式")


class UiCaseOut(BaseModel):
    """一条 UI 用例（对应 ui_case_store.validate_case 清洗后的结构）。"""

    case_id: str
    module: str
    name: str
    desc: str = ""
    priority: str
    run: bool
    browser: str = "chromium"
    headless: bool = True
    steps: list[UiStep]
    extract: list[UiExtract] = Field(default_factory=list)
    pre_scripts: list[str] = Field(default_factory=list)
    post_scripts: list[str] = Field(default_factory=list)
    session: str | None = Field(default=None, description="引用的登录态会话 id（开机加载其 storage_state）")
    retry: int = Field(default=0, description="失败重试次数 0–3（任一次通过即判通过，采集模式不重试）")


class UiCasePage(BaseModel):
    total: int
    items: list[UiCaseOut]


class UiCaseCreate(BaseModel):
    """新增 UI 用例（编号唯一；steps 至少一步）。"""

    case_id: str = Field(description="用例编号（唯一）")
    steps: list[UiStep] = Field(description="步骤列表（至少一步）")
    name: str = Field(default="新建UI用例", description="用例名称")
    module: str = Field(default="未分类", description="模块名")
    desc: str = Field(default="", description="用例描述")
    priority: str = Field(default="P2", description="P0~P5")
    run: bool = Field(default=True, description="是否执行")
    browser: str = Field(default="chromium", description="chromium/firefox/webkit")
    headless: bool = Field(default=True, description="无头模式")
    extract: list[UiExtract] = Field(default_factory=list)
    pre_scripts: list[str] = Field(default_factory=list)
    post_scripts: list[str] = Field(default_factory=list)
    session: str | None = Field(default=None, description="引用的登录态会话 id（可选）")
    retry: int = Field(default=0, ge=0, le=3, description="失败重试次数（0=不重试）")


class UiCaseUpdate(BaseModel):
    """更新 UI 用例（只传需改字段；case_id 不可改）。"""

    name: str | None = None
    module: str | None = None
    desc: str | None = None
    priority: str | None = None
    run: bool | None = None
    browser: str | None = None
    headless: bool | None = None
    steps: list[UiStep] | None = None
    extract: list[UiExtract] | None = None
    pre_scripts: list[str] | None = None
    post_scripts: list[str] | None = None
    session: str | None = Field(default=None, description="引用的登录态会话 id（传空串=取消引用）")
    retry: int | None = Field(default=None, ge=0, le=3, description="失败重试次数（不传=保持原值）")


class UiCaseMove(BaseModel):
    """迁移 UI 用例到另一个 UI 项目：目标查重 → 写入目标 → 源删除。"""

    to_file: str = Field(description="目标 UI 项目的用例 JSON 路径（相对项目根）")
    new_module: str | None = Field(default=None, description="迁移后归入的模块名（留空=保留原模块）")


class UiImportReq(BaseModel):
    """批量导入 UI 用例（逐条走单条创建同一校验；编号重复跳过，字段非法逐条报因）。"""

    cases: list[dict] = Field(default_factory=list, description="用例 JSON 数组，字段与单条创建接口同构")


# ==================== UI 登录态会话（storage_state 复用，全局注册表） ====================


class UiSessionOut(BaseModel):
    """一条登录态会话（注册表元数据 + 运行态；绝不包含 storage_state 实体内容）。"""

    id: str
    name: str
    desc: str = ""
    env: str
    browser: str = "chromium"
    headless: bool = True
    file: str
    capture_case: str
    ttl_seconds: int = 3600
    created_at: str | None = None
    captured_at: str | None = None
    last_error: str | None = None


class UiSessionCreate(BaseModel):
    """新建登录态会话（id 唯一；env 必须存在于 config.yaml；capture_case 为一条 UI 用例编号）。"""

    id: str = Field(description="会话唯一名（不含 / \\ ? # 与换行，直接寻址 + 参与实体文件名）")
    env: str = Field(description="绑定环境名（决定 base_url/域名归属）")
    file: str = Field(description="采集用例所在的 UI 项目 JSON 路径（相对项目根）")
    capture_case: str = Field(description="采集登录态的用例编号（登录成功后导出 storage_state）")
    name: str = Field(default="新建登录态", description="会话名称")
    desc: str = Field(default="", description="描述")
    browser: str = Field(default="chromium", description="chromium/firefox/webkit")
    headless: bool = Field(default=True, description="采集时无头开关")
    ttl_seconds: int = Field(default=3600, description="复用有效期秒（<=0=永不过期，仅手动采集）")


class UiSessionUpdate(BaseModel):
    """更新会话（只传需改字段；id 不可改）。改 env/browser/file/capture_case/ttl_seconds 会作废旧登录态。"""

    name: str | None = None
    desc: str | None = None
    env: str | None = None
    browser: str | None = None
    headless: bool | None = None
    file: str | None = None
    capture_case: str | None = None
    ttl_seconds: int | None = None
