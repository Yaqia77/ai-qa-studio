# -*- coding: utf-8 -*-
"""App 自动化请求/响应模型（自动生成 OpenAPI 文档的类型契约），当前为预留位（脚手架）。"""
from typing import Any

from pydantic import BaseModel, Field


class AppStep(BaseModel):
    """一个 App 步骤：动作 + 定位器 + 取值（三者必要性由动作决定，见 app_case_store.ACTIONS）。"""

    action: str = Field(description="动作：launch/tap/long_press/input/swipe/key_press/assert_visible/assert_text/screenshot/wait")
    locator: str | None = Field(default=None, description="定位器：id=/xpath=/text=/accessibility_id=/class=/uiautomator= 前缀")
    value: Any = Field(default=None, description="取值/期望值（支持 {{var}} 变量）")
    desc: str = Field(default="", description="步骤备注")


class AppExtract(BaseModel):
    """变量提取：source 形如 text|<locator> / attr:<name>|<locator>。"""

    var: str = Field(description="变量名")
    source: str = Field(description="提取来源表达式")


class AppCaseOut(BaseModel):
    """一条 App 用例（对应 app_case_store.validate_case 清洗后的结构）。"""

    case_id: str
    module: str
    name: str
    desc: str = ""
    priority: str
    run: bool
    platform: str = "android"
    app: str = ""
    steps: list[AppStep]
    extract: list[AppExtract] = Field(default_factory=list)
    pre_scripts: list[str] = Field(default_factory=list)
    post_scripts: list[str] = Field(default_factory=list)


class AppCasePage(BaseModel):
    total: int
    items: list[AppCaseOut]


class AppCaseCreate(BaseModel):
    """新增 App 用例（编号唯一；steps 至少一步）。"""

    case_id: str = Field(description="用例编号（唯一）")
    steps: list[AppStep] = Field(description="步骤列表（至少一步）")
    name: str = Field(default="新建App用例", description="用例名称")
    module: str = Field(default="未分类", description="模块名")
    desc: str = Field(default="", description="用例描述")
    priority: str = Field(default="P2", description="P0~P5")
    run: bool = Field(default=True, description="是否执行")
    platform: str = Field(default="android", description="android/ios")
    app: str = Field(default="", description="被测应用包名 / BundleID")
    extract: list[AppExtract] = Field(default_factory=list)
    pre_scripts: list[str] = Field(default_factory=list)
    post_scripts: list[str] = Field(default_factory=list)


class AppCaseUpdate(BaseModel):
    """更新 App 用例（只传需改字段；case_id 不可改）。"""

    name: str | None = None
    module: str | None = None
    desc: str | None = None
    priority: str | None = None
    run: bool | None = None
    platform: str | None = None
    app: str | None = None
    steps: list[AppStep] | None = None
    extract: list[AppExtract] | None = None
    pre_scripts: list[str] | None = None
    post_scripts: list[str] | None = None


class AppCaseMove(BaseModel):
    """迁移 App 用例到另一个 App 项目：目标查重 → 写入目标 → 源删除。"""

    to_file: str = Field(description="目标 App 项目的用例 JSON 路径（相对项目根）")
    new_module: str | None = Field(default=None, description="迁移后归入的模块名（留空=保留原模块）")
