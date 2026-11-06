# interface-auto 接入 Jenkins CI/CD 操作文档

本文档说明如何把本项目从「本地手动跑 `python main.py`」升级为「Jenkins 自动定时/触发执行、自动出 Allure 中文报告、失败自动告警」。

配套已入库的改动：

| 文件 | 作用 |
|------|------|
| `Jenkinsfile` | 声明式流水线定义（被 Jenkins 读取执行） |
| `allurerc.mjs` | Allure 3 配置：报告默认中文 + 单文件 HTML（便于归档） |
| `tools/gen_secret.py` | CI 里从环境变量（Jenkins 凭据）生成 `data/secret_vars.xlsx`，凭证不入 Git |
| `jenkins/docker-compose.yml` | 本地用 Docker 一键起 Jenkins 的编排（选装 Docker 时用） |

---

## 零、本机还没有 Jenkins？（当前环境无 Java、无 Docker）

Jenkins 运行依赖 Java。本机实测**既无 `java` 也无 `docker`**，按省事程度排序：

**方式 A（推荐·零依赖）：Jenkins Windows `.msi` 安装包**
- 下载页：`https://www.jenkins.io/download/`（Windows LTS 安装包，**自带 JRE**，无需另装 Java）
- 安装时自动注册为 Windows 服务，装完自动开浏览器到 `http://localhost:8080`
- 首次进入选 **Install suggested plugins**，再创建管理员账号

**方式 B：Docker（需先装 Docker Desktop）**
- 装好 Docker Desktop 后，在项目根执行：`docker compose -f jenkins/docker-compose.yml up -d`
- 打开 `http://localhost:8080`，初始密码见 `docker logs jenkins`

**方式 C：WAR + JDK（已有 Java 时）**
- `java -jar jenkins.war --httpPort=8080`

> **构建节点（真正跑 pytest/allure 的机器）**：本项目用例步骤是 Windows PowerShell 写的。最顺的做法是在**你这台已装 Python/Node/Allure 的 Windows 主机**上跑——用方式 A 装好 Jenkins 后直接用 built-in 节点即可（`Jenkinsfile` 已设为 `agent any`）。若用 Docker 起 Jenkins（方式 B），容器内默认没有 Python/Node，需要另挂一个 Windows 节点或在镜像里补依赖。

---

## 一、目标与能力对照

| # | 目标 | 实现方式 |
|---|------|----------|
| 1 | 提交代码后自动触发 | GitHub Webhook + Jenkins GitHub 插件（无公网回调时用 `pollSCM` 兜底） |
| 2 | 手动 / 定时执行 | 流水线 `parameters`（手动选范围/环境）+ `triggers{ cron('H 2 * * *') }`（每晚全量） |
| 3 | 自动出并归档 Allure 报告 | `allure generate`（读 `allurerc.mjs`→中文单文件）+ `archiveArtifacts` |
| 4 | 失败告警 | `post.failure`：邮件（Email Extension）+ 企业微信/钉钉机器人（curl） |
| 5 | 构建历史 | Jenkins 原生保留，配合 `buildDiscarder` 保留最近 30 次 |

---

## 二、前置条件（Jenkins 所在机器 / 构建节点）

构建节点（跑用例的机器）需要具备：

1. **Jenkins**（建议 2.4xx LTS 以上）。
2. **Git**（已在 PATH）。
3. **Python 3.10+**（已在 PATH，用于建虚拟环境）。
4. **Node.js + Allure 3 CLI**：流水线里执行 `allure generate`。
   - 本项目实测 `allure --version` 为 `3.18.0`（npm 全局安装）。
   - 安装：`npm install -g allure`（Allure 3），确保 `allure` 命令在节点 PATH 中。
5. （企业微信/钉钉告警）节点能出网访问机器人 Webhook 域名。

> Windows 节点用 PowerShell 步骤（当前 `Jenkinsfile` 即按此写）；Linux 节点见「八、跨平台调整」。

---

## 三、安装 Jenkins 插件

`Manage Jenkins → Plugins → Available plugins`，安装（装完重启）：

- **Git** / **GitHub**（或 **GitHub Integration**）：拉代码 + Webhook 触发
- **Pipeline**（多环境自带）：执行 `Jenkinsfile`
- **Credentials Binding**：`withCredentials` 注入凭据
- **Email Extension (emailext)**：邮件告警
- （可选）**Allure Plugin**：若想要 Jenkins 内嵌 Allure 趋势页；本方案用 `allure generate`+归档，插件非必需

---

## 四、创建凭据（Manage Jenkins → Credentials → system → Global）

| ID（自定义保持一致） | 类型 | 内容 |
|------|------|------|
| `interface-auto-token` | Secret text | 完整 Authorization 头值，形如 `Basic bW9iaWxl...`（**会过期的登录态**，见第六节） |
| `interface-auto-userid` | Secret text | 业务 `userId`，如 `1310974415610162457` |
| `notify-webhook` | Secret text | 企业微信/钉钉机器人 Webhook 地址（不配群通知可留空占位） |

> ⚠️ 安全：**真实凭证只存 Jenkins 凭据库**，绝不写进 Git。`data/secret_vars.xlsx` 已被 `.gitignore` 忽略；流水线每次从凭据生成、跑完即删（见 `Jenkinsfile` 的 `always` 清理）。

---

## 五、创建流水线任务并跑通

1. `New Item → Pipeline`，命名如 `interface-auto-api-regression`。
2. Pipeline 配置：
   - Definition: **Pipeline script from SCM** → Git
   - Repository URL: `https://github.com/Yaqia77/interface-auto`
   - Credentials: 选一个 GitHub 的凭据（私有仓库需用户名/令牌；公有仓库无需）
   - Branch Specifier: `*/feat/album-atlas-list-cases`（或你的目标分支，`*/main` 等）
   - Script Path: `Jenkinsfile`
3. （可选）额外行为去掉「Build when a change is pushed to GitHub」勾选，改由 Webhook（第七节）。
4. **先手动「Build Now」跑一次**：首次运行才会加载 `Jenkinsfile` 里的 `triggers`/`parameters`。跑通后：
   - 会看到参数 `ENV_NAME / SCOPE / MODULE`；
   - 构建页 `Build Artifacts` 里有 `allure-report/index.html`（双击即中文报告）。

---

## 六、凭证时效与 Allure 中文关键点

- **Basic 凭证会过期**：`interface-auto-token` 是登录态串，内含时间戳，过期后接口返回 `01000002 鉴权失败`，用例会红。长期方案二选一：
  1. 定期手动更新 Jenkins 凭据里的 token；
  2. 在 `api_cases.xlsx` 前置一条「登录」用例，用「变量提取」把新 token 写入变量池（框架已支持 `{{token}}` 覆盖），彻底免维护。
- **报告中文**：`allurerc.mjs` 里 `reportLanguage:"zh"`，且 `allure` 命令在项目根执行会自动读取它；`Jenkinsfile` 又显式传了 `--config allurerc.mjs`，双保险。
- **单文件报告**：`singleFile:true` 让 `allure generate` 产出一个自包含 `index.html`，`archiveArtifacts` 归档后从构建页直接下载打开，无需额外起服务。

---

## 七、配置「提交即触发」（GitHub Webhook）

前提：Jenkins 地址对 GitHub 可达（公网或内网穿透/防火墙放行）。

1. Jenkins 装好 **GitHub** 插件后，任务会暴露 `https://<jenkins>/github-webhook/`。
2. GitHub 仓库 `Settings → Webhooks → Add webhook`：
   - Payload URL: `https://<你的Jenkins>/github-webhook/`
   - Content type: `application/json`
   - Secret: 与 Jenkins（GitHub Integration 插件里的 hook secret）一致
   - Events: **Just the push event**（或 Push + Pull Request）
3. 保存后 GitHub 会发送 ping；Jenkins 任务对应分支有新提交即触发流水线。

**无公网回调时的兜底**：放开 `Jenkinsfile` 里 `pollSCM('H/5 * * * *')` 注释（每 5 分钟检查一次 Git 有无新提交），配合 GitHub Personal Access Token 凭据即可，无需 Jenkins 被外部访问。

---

## 八、失败告警配置

### 邮件
- `Manage Jenkins → System → Extended E-mail`：填 SMTP（发件账号/服务器），保存。
- `Jenkinsfile` 的 `emailext(... to: 'qa-team@example.com')` 改成你的收件组。

### 企业微信 / 钉钉机器人
- 群里添加「自定义机器人」，拿到 Webhook，存进凭据 `notify-webhook`。
- `post.failure` 里已用统一 `text` 结构 POST（企业微信与钉钉的 `text` 消息格式相同）：
  ```json
  {"msgtype":"text","text":{"content":"【接口自动化失败】... 链接: <BUILD_URL>"}}
  ```
- 若机器人开了**加签/关键词**校验：钉钉加签需在 URL 拼 `&timestamp=&sign=`；企业微信关键词需保证 content 命中。按需扩展 `Jenkinsfile`。

---

## 九、跨平台调整（Linux 节点）

当前 `Jenkinsfile` 用 `powershell` 与 `.venv/Scripts`。Linux 节点改：
- `agent { label 'linux' }`（或 `any`）；
- `powershell '...'` → `sh '...'`；
- `.venv/Scripts/python.exe` → `.venv/bin/python`；
- `curl.exe` → `curl`；
- 路径分隔用正斜杠（本项目步骤已统一正斜杠，迁移成本低）。

---

## 十、常见问题排查

| 现象 | 原因 / 解决 |
|------|-------------|
| `allure : 无法识别` / `allure: command not found` | 节点未装或未在 PATH：`npm i -g allure`，或把 node 全局 bin 加入 PATH |
| 报告仍是英文 | 未在项目根执行 `allure`（没读到 `allurerc.mjs`）；确认 `--config allurerc.mjs` 或工作目录 |
| 用例全红、返回 `鉴权失败` | `interface-auto-token` 过期，见第六节 |
| `变量 {token} 未定义` | `gen_secret.py` 未注入（凭据 ID 不匹配）或 CI 未生成 `secret_vars.xlsx`；查「注入凭证」阶段日志 |
| Webhook 不触发 | Jenkins 对 GitHub 不可达；改用 `pollSCM`；或检查 Secret/事件配置 |
| 报告没有归档 | 「执行接口回归」失败导致 `allure-results` 为空——`catchError` 已保证仍继续生成报告，确认「生成/归档」阶段是否执行 |
| 首次不显示定时/参数 | `triggers`/`parameters` 需第一次构建后才生效，先手动跑一次 |

---

## 十一、上线检查清单（Checklist）

- [ ] 节点已装 Git / Python / Node / Allure3，命令行可用
- [ ] 插件：Git、GitHub、Pipeline、Credentials Binding、Email Extension
- [ ] 凭据：`interface-auto-token`、`interface-auto-userid`、`notify-webhook`
- [ ] Pipeline 任务 SCM 指向仓库、Script Path=`Jenkinsfile`、分支正确
- [ ] 手动首跑通过，构建产物含中文 `allure-report/index.html`
- [ ] Webhook（或 `pollSCM`）配置并可触发
- [ ] 定时 `cron('H 2 * * *')` 生效（任务页可见下次触发时间）
- [ ] 故意制造失败，验证邮件 + 群机器人告警到达
