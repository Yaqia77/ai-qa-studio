"""生成（或重置）接口用例 Excel 模板：data/api_cases.xlsx。

包含 3 个工作表：
  - 用例:     9 条演示用例，覆盖 GET/POST/PUT/DELETE、变量提取、Token 串联、
              查询参数、反向断言、跳过标记等全部能力
  - 全局变量: 预置 username / password / title
  - 填写说明: 字段说明、断言语法、注意事项

用法: python tools/gen_template.py [输出路径]
"""
import json
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "data" / "api_cases.xlsx"

CASE_HEADERS = ["用例编号", "模块", "接口名称", "用例描述", "优先级", "是否执行", "请求方式",
                "请求路径", "请求头", "查询参数", "请求体", "变量提取", "断言", "超时(秒)",
                "前置脚本", "后置脚本"]

# 每行字段顺序与 CASE_HEADERS 一致；JSON 列写紧凑 JSON 文本
DEMO_CASES = [
    ["login_001", "演示-认证", "登录", "模拟登录：从回显响应中提取 token", "P0", "Y", "POST",
     "https://httpbin.org/post", "", "",
     '{"username":"{{username}}","password":"{{password}}","token":"auto-login-token-abc123"}',
     '{"token":"$.json.token"}',
     '[["eq","status_code",200],["eq","$.json.username","{{username}}"]]', ""],
    ["bearer_001", "演示-认证", "Token鉴权", "引用上一步提取的 token 访问鉴权接口", "P0", "Y", "GET",
     "https://httpbin.org/bearer",
     '{"Authorization":"Bearer {{token}}"}', "", "", "",
     '[["eq","status_code",200],["eq","$.authenticated",true]]', ""],
    ["get_user_001", "演示-用户", "查询用户", "GET 查询用户详情，提取用户 ID 供后续用例使用", "P0", "Y", "GET",
     "https://jsonplaceholder.typicode.com/users/1", "", "", "",
     '{"user_id":"$.id"}',
     '[["eq","status_code",200],["eq","$.id",1],["not_null","$.email"]]', ""],
    ["search_posts_001", "演示-用户", "搜索文章", "GET 带查询参数（引用 user_id）并提取第一篇文章 ID", "P1", "Y", "GET",
     "https://jsonplaceholder.typicode.com/posts", "",
     '{"userId":"{{user_id}}"}', "",
     '{"post_id":"$[0].id"}',
     '[["eq","status_code",200],["len_eq","$",10]]', ""],
    ["create_post_001", "演示-文章CRUD", "创建文章", "POST 创建文章，请求体与断言引用变量", "P0", "Y", "POST",
     "https://jsonplaceholder.typicode.com/posts", "", "",
     '{"userId":"{{user_id}}","title":"{{title}}","body":"接口自动化演示内容"}', "",
     '[["eq","status_code",201],["eq","$.userId","{{user_id}}"]]', ""],
    ["update_post_001", "演示-文章CRUD", "更新文章", "PUT 更新搜索到的文章并断言 ID 一致", "P1", "Y", "PUT",
     "https://jsonplaceholder.typicode.com/posts/{{post_id}}", "", "",
     '{"id":"{{post_id}}","title":"更新后的标题"}', "",
     '[["eq","status_code",200],["eq","$.id","{{post_id}}"]]', ""],
    ["delete_post_001", "演示-文章CRUD", "删除文章", "DELETE 删除搜索到的文章（引用 post_id）", "P2", "Y", "DELETE",
     "https://jsonplaceholder.typicode.com/posts/{{post_id}}", "", "", "", "",
     '[["eq","status_code",200]]', ""],
    ["not_found_001", "演示-异常", "404场景", "反向用例：断言 404 状态码", "P2", "Y", "GET",
     "https://httpbin.org/status/404", "", "", "", "",
     '[["eq","status_code",404]]', ""],
    ["skipped_001", "演示-异常", "跳过演示", "是否执行=N 时跳过该用例", "P3", "N", "GET",
     "https://httpbin.org/get", "", "", "", "",
     '[["eq","status_code",200]]', ""],
]

GLOBAL_VARS = [
    ["username", "test001", "测试账号"],
    ["password", "Aa123456", "测试密码"],
    ["title", "接口自动化演示标题", "创建文章的标题变量"],
]

GUIDE_SECTIONS = [
    ("一、用例表字段说明", [
        ["字段", "必填", "说明"],
        ["用例编号", "是", "全局唯一，如 login_001；报告与日志中用它定位用例"],
        ["模块", "是", "模块名，用于报告分组与 --module 筛选"],
        ["接口名称", "是", "接口的业务名称"],
        ["用例描述", "是", "一句话描述本用例验证点"],
        ["优先级", "是", "P0/P1/P2/P3/P4/P5"],
        ["是否执行", "是", "Y 执行 / N 跳过（报告中显示 skipped）"],
        ["请求方式", "是", "GET/POST/PUT/DELETE/PATCH"],
        ["请求路径", "是", "http 开头的完整 URL 直接使用；相对路径会拼接 config.yaml 的 base_url，支持 {{变量}}"],
        ["请求头", "否", "JSON 对象，与环境默认请求头合并（用例优先），值支持 {{变量}}"],
        ["查询参数", "否", "JSON 对象，URL 查询参数，值支持 {{变量}}"],
        ["请求体", "否", "JSON 对象/数组（以 { 或 [ 开头必须合法 JSON），或纯文本；值支持 {{变量}}"],
        ["变量提取", "否", 'JSON 对象：{"变量名":"$.data.token"}，从响应体按 JSONPath 提取，供后续用例引用'],
        ["断言", "是", "JSON 数组，见下方断言语法；期望值支持 {{变量}}"],
        ["超时(秒)", "否", "正数；缺省使用 config.yaml 中的 timeout"],
        ["前置脚本", "否", "逗号分隔的公共脚本名，在主请求之前按序执行（任一步失败即中断，主请求不发）"],
        ["后置脚本", "否", "逗号分隔的公共脚本名，无论主流程成败都在最后执行（用于清理造出的数据）"],
    ]),
    ("二、断言语法", [
        ["格式", "", '[["操作符","取值路径","期望值"], ...]，多条断言全部通过才算通过'],
        ["取值路径", "", "status_code=响应状态码；response_text=响应原文；$.xxx=响应体 JSONPath（$ 为整个 body）；var.xxx=变量池取值"],
        ["eq / neq", "", "相等 / 不等（数字与字符串会自动智能比较，如 200 与 \"200\" 视为相等）"],
        ["contains", "", "包含：字符串为子串包含；列表为成员包含"],
        ["gt / ge / lt / le", "", "数值大于/大于等于/小于/小于等于"],
        ["regex", "", "正则匹配（期望值为正则表达式）"],
        ["not_null", "", "非空（实际值不为 null 且不为空串），可省略期望值"],
        ["len_eq", "", "长度相等（适用于列表/字符串，如断言返回列表条数）"],
        ["示例", "", '[["eq","status_code",200],["eq","$.code",0],["contains","$.message","成功"],["not_null","$.data.id"]]'],
    ]),
    ("三、变量机制", [
        ["写法", "", "{{变量名}}，可用在 请求路径/请求头/查询参数/请求体/断言期望值"],
        ["优先级", "", "运行时变量提取 > 凭证文件 secret_vars.xlsx > 全局变量表 > config.yaml（提供 base_url）"],
        ["类型保留", "", 'JSON 列中占位符必须写在引号内，如 {"id":"{{post_id}}"}；当整个值就是一个占位符时，替换后保留原始类型（数字/布尔）'],
        ["依赖顺序", "", "用例严格按行号顺序串行执行，后面的用例可使用前面用例提取的变量（如先登录提 token 再访问鉴权接口）"],
        ["筛选提醒", "", "按 --module/--priority/--keyword 筛选时，被依赖的用例（如提取 token 的登录用例）需同时被选中，否则会报「变量未定义」"],
    ]),
    ("四、注意事项", [
        ["1", "", "演示用例使用公开 API（httpbin.org / jsonplaceholder.typicode.com），可直接运行验证框架"],
        ["2", "", "实际使用时：替换为被测系统接口；域名可配置到 config.yaml 的 base_url，用例里只写相对路径"],
        ["3", "", "删除演示用例后保留表头即可；新增用例直接在表格末尾追加一行"],
        ["4", "", "运行方式：python main.py（详见项目 README 或 main.py 文件头注释）"],
        ["5", "", "公共脚本：在平台「公共脚本」页维护（存 data/scripts.json），步骤只支持 request/sleep/set_var/assert 四种声明式动作，不执行任意代码"],
    ]),
]


def _j(value) -> str:
    """紧凑 JSON 文本（用于演示用例的 JSON 列，保证写出的就是标准 JSON）。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


SECRET_TEMPLATE_VARS = [
    ["username", "<填写真实账号>", "测试账号（会覆盖用例 Excel 全局变量表中的同名变量）"],
    ["password", "<填写真实密码>", "测试密码"],
]

SECRET_GUIDE = [
    ["使用步骤", ""],
    ["1", "复制本文件，重命名为 secret_vars.xlsx（与本文件同目录 data/ 下）"],
    ["2", "在 secret_vars.xlsx 中填入真实账号密码等敏感信息"],
    ["3", "secret_vars.xlsx 已被 .gitignore 忽略，不会被提交到 Git 仓库"],
    ["4", "运行时自动加载（路径由 config.yaml 的 secret_vars_file 配置），变量优先级高于用例 Excel 的全局变量表"],
    ["5", "本模板文件仅作样板，程序不会加载它；可按需增删变量行（如 token、验证码等）"],
]


def build_secret_template() -> Workbook:
    """生成凭证变量模板：全局变量 Sheet + 使用说明 Sheet。"""
    wb = Workbook()

    ws = wb.active
    ws.title = "全局变量"
    header_fill = PatternFill("solid", fgColor="C00000")
    header_font = Font(bold=True, color="FFFFFF")
    for col, header in enumerate(["变量名", "值", "备注"], start=1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
    for row_no, var in enumerate(SECRET_TEMPLATE_VARS, start=2):
        for col, value in enumerate(var, start=1):
            ws.cell(row=row_no, column=col, value=value)
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 26
    ws.column_dimensions["C"].width = 52

    ws2 = wb.create_sheet("使用说明")
    for row_no, line in enumerate(SECRET_GUIDE, start=1):
        for col, value in enumerate(line, start=1):
            ws2.cell(row=row_no, column=col, value=value)
    ws2.column_dimensions["A"].width = 12
    ws2.column_dimensions["B"].width = 90
    return wb


def build_workbook() -> Workbook:
    wb = Workbook()

    # ---- Sheet1: 用例 ----
    ws = wb.active
    ws.title = "用例"
    header_fill = PatternFill("solid", fgColor="4472C4")
    header_font = Font(bold=True, color="FFFFFF")
    wrap = Alignment(vertical="center", wrap_text=True)

    for col, header in enumerate(CASE_HEADERS, start=1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for row_no, case in enumerate(DEMO_CASES, start=2):
        for col, value in enumerate(case, start=1):
            cell = ws.cell(row=row_no, column=col, value=value)
            cell.alignment = wrap

    widths = [16, 14, 12, 32, 8, 9, 10, 46, 30, 16, 46, 28, 52, 9, 22, 22]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    # ---- Sheet2: 全局变量 ----
    ws2 = wb.create_sheet("全局变量")
    for col, header in enumerate(["变量名", "值", "备注"], start=1):
        cell = ws2.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
    for row_no, var in enumerate(GLOBAL_VARS, start=2):
        for col, value in enumerate(var, start=1):
            ws2.cell(row=row_no, column=col, value=value)
    ws2.column_dimensions["A"].width = 18
    ws2.column_dimensions["B"].width = 26
    ws2.column_dimensions["C"].width = 30
    ws2.freeze_panes = "A2"

    # ---- Sheet3: 填写说明 ----
    ws3 = wb.create_sheet("填写说明")
    section_font = Font(bold=True, size=12)
    row = 1
    for title, rows in GUIDE_SECTIONS:
        cell = ws3.cell(row=row, column=1, value=title)
        cell.font = section_font
        row += 1
        for line in rows:
            for col, value in enumerate(line, start=1):
                c = ws3.cell(row=row, column=col, value=value)
                c.alignment = wrap
            row += 1
        row += 1  # 章节间空一行
    ws3.column_dimensions["A"].width = 20
    ws3.column_dimensions["B"].width = 8
    ws3.column_dimensions["C"].width = 100

    return wb


def main() -> None:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        answer = input(f"文件已存在: {output}\n将覆盖重置为模板（演示用例 + 全局变量），继续? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("已取消")
            return
    wb = build_workbook()
    wb.save(output)
    print(f"模板已生成: {output}")
    print(f"共写入 {len(DEMO_CASES)} 条演示用例、{len(GLOBAL_VARS)} 个全局变量")

    # 凭证变量模板（直接覆盖：仅是样板文件，程序不加载）
    secret_output = output.parent / "secret_vars_template.xlsx"
    build_secret_template().save(secret_output)
    print(f"凭证模板已生成: {secret_output}")
    print("使用方法: 复制该文件为 secret_vars.xlsx，填入真实账号密码（该文件不入 Git 仓库）")


if __name__ == "__main__":
    main()
