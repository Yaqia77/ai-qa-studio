// interface-auto —— Jenkins 声明式流水线
//
// 能力对应接入目标：
//   1) Git 提交自动触发   —— 依赖 Jenkins GitHub 插件的 Webhook（见 JENKINS-CICD.md），或下方 pollSCM 兜底
//   2) 手动 / 定时执行     —— parameters + triggers{ cron(...) }（每晚全量回归）
//   3) 生成并归档 Allure   —— allure 命令生成中文单文件报告 + archiveArtifacts
//   4) 失败告警            —— post.failure：邮件 + 企业微信/钉钉机器人（curl）
//   5) 构建历史            —— Jenkins 原生保留（配合 artifact）
//
// 需在 Jenkins 里预先创建的凭据（Credentials）：
//   - interface-auto-token   （Secret text）登录态完整 Authorization 头值（Basic xxx）
//   - interface-auto-userid  （Secret text）业务 userId
//   - notify-webhook         （Secret text）企业微信/钉钉机器人 Webhook 地址（可选，不配则跳过群通知）
pipeline {
    // 本地单节点（内置 built-in 节点）直接用 any 即可开箱跑；
    // 若有多台机器，可改成 agent { label 'windows' } 并给目标节点打标签
    agent any

    options {
        timestamps()                 // 日志带时间
        disableConcurrentBuilds()    // 同一分支串行，避免报告目录互踩
        buildDiscarder(logRotator(numToKeepStr: '30')) // 保留最近 30 次构建历史
        timeout(time: 30, unit: 'MINUTES')
    }

    // 触发方式：定时（每晚 02:00 全量）；GitHub Webhook 由插件在服务端配置，这里用 pollSCM 作为无公网回调时的兜底
    triggers {
        cron('H 2 * * *')        // 每天凌晨约 02:00 跑一次全量回归
        // pollSCM('H/5 * * * *') // 无 Webhook 时：每 5 分钟拉一次，检测有新提交则触发
    }

    parameters {
        string(name: 'ENV_NAME', defaultValue: 'test', description: '运行环境（对应 config.yaml 的 env 段，如 test）')
        choice(name: 'SCOPE', choices: ['ALL', 'P0', 'P0,P1', 'SMOKE'], description: '执行范围：ALL=全量；P0/P0,P1=按优先级；SMOKE=只跑 P0')
        string(name: 'MODULE', defaultValue: '', description: '按模块名筛选（包含匹配，留空不筛选）')
    }

    environment {
        // 相对工作区的路径：pytest 直接产出到固定目录，便于 allure 生成与归档
        ALLURE_RESULTS = 'allure-results'
        ALLURE_REPORT  = 'allure-report'
        // 依据 SCOPE 参数映射成 pytest 命令行参数
        PYTEST_FILTER  = "${params.SCOPE == 'SMOKE' ? '--priority=P0' : (params.SCOPE == 'ALL' ? '' : "--priority=${params.SCOPE}")}"
        MODULE_FILTER  = "${params.MODULE?.trim() ? "--module=${params.MODULE}" : ''}"
    }

    stages {
        stage('检出代码') {
            steps {
                checkout scm
            }
        }

        stage('准备环境') {
            steps {
                // 复用项目虚拟环境；不存在则创建并装依赖（Linux 把 .venv/Scripts 换成 .venv/bin）
                powershell '''
                    if (-not (Test-Path ".venv")) {
                        python -m venv .venv
                    }
                    ./.venv/Scripts/python.exe -m pip install -q --disable-pip-version-check -r requirements.txt
                    # 打印版本，确认 allure / node 在该节点可用（Allure 报告生成依赖 allure 命令）
                    allure --version
                '''
            }
        }

        stage('注入凭证') {
            steps {
                // 凭据以环境变量注入，不落 SCM；gen_secret.py 生成运行所需的 data/secret_vars.xlsx
                withCredentials([
                    string(credentialsId: 'interface-auto-token',  variable: 'API_TOKEN'),
                    string(credentialsId: 'interface-auto-userid', variable: 'API_USER_ID')
                ]) {
                    powershell './.venv/Scripts/python.exe tools/gen_secret.py'
                }
            }
        }

        stage('执行接口回归') {
            steps {
                // 直接调 pytest（框架通过 conftest 的自定义参数加载 Excel 用例），产出 allure-results
                // catchError 让失败也继续走「生成报告」，最后再依据结果置红
                catchError(buildResult: 'UNSTABLE', stageResult: 'FAILURE') {
                    powershell {
                        scriptText = """
                            ./.venv/Scripts/python.exe -m pytest tests -v --tb=short `
                                --env=${ENV.ENV_NAME} `
                                --alluredir=${ENV.ALLURE_RESULTS} --clean-alluredir `
                                ${ENV.PYTEST_FILTER} ${ENV.MODULE_FILTER}
                        """
                        // pytest 退出码非 0（有失败）时，让 catchError 捕获但不立即中断整条流水线
                    }
                }
            }
        }

        stage('生成 Allure 报告') {
            steps {
                // Allure 3 会自动读取项目根 allurerc.mjs（reportLanguage=zh + singleFile=true）
                powershell "allure generate ${ENV.ALLURE_RESULTS} --output ${ENV.ALLURE_REPORT} --config allurerc.mjs"
            }
        }

        stage('归档报告') {
            steps {
                // 单文件报告直接可下载查看；同时归档原始 allure-results 便于二次生成
                archiveArtifacts artifacts: "${ALLURE_REPORT}/index.html, ${ALLURE_RESULTS}/**", fingerprint: false,
                                   allowEmptyArchive: true
            }
        }
    }

    post {
        failure {
            // —— 邮件告警（需 Email Extension 插件；未配置收件人则自动跳过）——
            emailext(
                subject: "[失败] interface-auto 接口回归 #${BUILD_NUMBER} @ ${ENV_NAME}",
                body: "构建 ${BUILD_NUMBER} 失败（环境：${ENV_NAME}，范围：${SCOPE}）。<br/>" +
                      "详情见：${BUILD_URL}<br/>Allure 报告：${BUILD_URL}artifact/${ALLURE_REPORT}/index.html",
                to: 'qa-team@example.com'
            )
            // —— 企业微信 / 钉钉机器人告警（配了 notify-webhook 凭据才发）——
            script {
                withCredentials([string(credentialsId: 'notify-webhook', variable: 'HOOK')]) {
                    if (env.HOOK?.trim()) {
                        def text = "【接口自动化失败】\\n项目: interface-auto\\n分支: ${env.BRANCH_NAME ?: 'N/A'}\\n" +
                                   "构建: #${env.BUILD_NUMBER}\\n环境: ${params.ENV_NAME} / 范围: ${params.SCOPE}\\n" +
                                   "链接: ${env.BUILD_URL}"
                        // 企业微信与钉钉都用 markdown/text JSON；这里用兼容的 text 结构
                        def body = groovy.json.JsonOutput.toJson([msgtype: 'text', text: [content: text]])
                        writeFile file: '.notify.json', text: body
                        powershell "curl.exe -s -X POST -H 'Content-Type: application/json' --data '@.notify.json' $env:HOOK"
                    }
                }
            }
        }
        success {
            echo "构建成功，Allure 中文报告已归档：${env.BUILD_URL}artifact/${env.ALLURE_REPORT}/index.html"
        }
        always {
            // 清理本次运行产物（保留在 artifact 中），避免污染工作区；凭证文件删除防泄露
            powershell '''
                Remove-Item -Recurse -Force allure-results, allure-report -ErrorAction SilentlyContinue
                Remove-Item -Force data/secret_vars.xlsx -ErrorAction SilentlyContinue
            '''
        }
    }
}
