# fuckCourse v3.1.1

超星学习通 / WE Learn / 智慧树 / 雨课堂 四合一自动刷课工具。

## 项目结构

```
├── main.py                  # 统一启动器，菜单选择平台 (subprocess 调度)
├── config.json              # 统一配置（首次运行自动生成）
├── cookies.json             # 统一 cookies（登录后自动保存）
├── requirements.txt         # Python 依赖
├── log.md                   # 更新日志
├── logs/                    # 运行日志（自动生成）
│   ├── chaoxing.log
│   ├── welearn.log
│   ├── yuketang.log
│   └── zhs_logs/
│
├── chaoxing/                # 超星学习通
│   ├── main.py              # 入口
│   ├── api/                 # 核心模块
│   │   ├── base.py          # 课程/视频/文档/作业处理
│   │   ├── answer.py        # 题库（多 provider）
│   │   ├── decode.py        # HTML 解析（课程列表/章节树/任务点）
│   │   ├── process.py       # 章节调度
│   │   ├── live.py          # 直播处理
│   │   ├── captcha.py       # 验证码识别
│   │   ├── cipher.py        # 加密
│   │   ├── cookies.py       # Cookie 管理
│   │   ├── notification.py  # 消息推送
│   │   └── ...
│   └── resource/            # 资源文件
│
├── welearn/                 # WE Learn
│   └── welearn_decompiled.py  # 一体版（SSO 登录 + 课程/时长刷取）
│
├── zhs/                     # 智慧树
│   ├── main.py              # 入口
│   ├── fucker.py            # 核心刷课逻辑
│   ├── utils.py             # 工具（进度条等）
│   ├── sign.py              # 签到
│   ├── push.py              # 推送通知
│   └── logger.py            # 日志
│
└── yuketang/                # 雨课堂 PPT 下载
    ├── main.py              # 入口
    └── yuketang_login.py    # 微信扫码登录 + 配置管理
```

> **关于 JSON 文件**：`config.json` / `cookies.json` 均存放在 exe 同目录（或项目根目录）。frozen 模式下通过环境变量 `FUCKCOURSE_*` 传递路径，子模块优先使用环境变量指向的根目录文件。
>
> 超星与 WE Learn 保存账号、雨课堂更新共享配置时，读取失败或 JSON 根不是对象会停止写入。JSON 语法损坏时，先在原目录创建唯一的 `<文件名>.corrupt.<随机标识>.bak`，保存原始字节；仅备份成功后才允许重新初始化。备份失败不会覆盖原文件，已有备份也不会被替换。重新初始化不等于恢复损坏文件中的其他平台字段，需从备份手动恢复；备份可能含凭据，应妥善保管。


## 功能

| 平台 | 课程刷取 | 时长刷取 | 自动答题 | PPT 下载 | 自动签到 | 通知推送 |
|------|:---:|:---:|:---:|:---:|:---:|:---:|
| 超星学习通 | Y | Y | Y | — | Y | Y |
| WE Learn | Y | Y | Y | — | — | — |
| 智慧树 | Y | Y | Y | — | — | Y |
| 雨课堂 | — | — | — | Y | — | — |

## 运行
1.release下载exe文件,双击运行 

2.或者运行python脚本
```bash
pip install -r requirements.txt
python main.py
```

## 使用流程

启动后进入主菜单选择平台，每个平台内部有自己的交互菜单（选择课程/时长模式、输入 ID 等）：

```
==================================================
             fuckCourse v3.1.1
             designed by snake
==================================================

  [1] 超星学习通 (Chaoxing)
  [2] WE Learn (SFLEP)
  [3] 智慧树 (ZHS)
  [4] 雨课堂 (Yuketang)
  [0] 退出

  请选择平台 (0-4):
```

所有配置从 `config.json` 读取，无需传参。各平台运行完毕后返回主菜单。

## 登录策略

```
启动 → cookies.json 存在且有效？
  ├─ 是 → 跳过登录，直接刷课
  └─ 否 → config.json 有 username/password？
           ├─ 是 → 自动登录 → 保存 cookies.json
           └─ 否 → 交互输入 → 登录 → 保存 cookies.json + 回写 config.json
```

切换账号：删除 `cookies.json`，或在 `config.json` 中修改 `username`/`password` 后重启。

## 配置

直接编辑 exe 同目录下的 `config.json`。首次运行任一平台后自动生成，账号密码登录成功后自动回写。

### chaoxing — 超星学习通

**common（基本配置）**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `username` | string | `""` | 手机号账号 |
| `password` | string | `""` | 登录密码 |
| `course_list` | array | `[]` | 课程 ID 列表，如 `["2151141"]`，留空手动选择 |
| `speed` | number | `1.0` | 视频倍速，最大 2 |
| `jobs` | number | `4` | 并发章节数 |
| `notopen_action` | string | `"retry"` | 未开放章节：`retry` 重试 / `continue` 跳过 |

超星账号密码登录使用连接 5 秒、读取 15 秒的请求超时；网络异常退出当前平台，不自动重试。登录失败时可选择重新输入账号密码或取消；密码输入不回显，新输入的凭据仅在密码登录成功后保存，失败或取消不会覆盖已有配置。

**tiku（题库配置）**

不配 `provider` 则不答题，测验直接跳过。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `provider` | string | `""` | 题库来源 |
| `submit` | boolean | `false` | 是否自动提交答题 |
| `cover_rate` | number | `0.8` | 最低覆盖率（搜到/总题数） |
| `delay` | number | `1.0` | 多题搜索间隔（秒） |
| `true_list` | string | `"正确,对,√,是"` | 判断题"对" |
| `false_list` | string | `"错误,错,×,否"` | 判断题"错" |

Provider 可选项：

| 值 | 说明 | 需额外配置 |
|----|------|-----------|
| `TikuYanxi` | 言溪题库 | `tokens` |
| `TikuLike` | LIKE 知识库 | `tokens`, `likeapi_*` |
| `TikuAdapter` | 自建题库 | `url` |
| `AI` | OpenAI 兼容 API | `endpoint`, `key`, `model` |
| `SiliconFlow` | 硅基流动 | `siliconflow_key`, `siliconflow_model` |

各 provider 专属字段：

| 字段 | provider | 说明 |
|------|----------|------|
| `tokens` | TikuYanxi / TikuLike | API Token，多个逗号分隔 |
| `likeapi_model` | TikuLike | 模型名，默认 `"glm-4.5-air"` |
| `url` | TikuAdapter | 适配器服务地址 |
| `endpoint` | AI | API 地址（如 `https://api.openai.com/v1`） |
| `key` | AI | API Key |
| `model` | AI | 模型名（如 `gpt-4o`） |
| `reasoning_effort` | AI | 推理档位（选填，如 `high`；需模型和 API 支持），留空不发送 |
| `http_proxy` | AI | HTTP 代理（选填） |
| `min_interval_seconds` | AI | 请求间隔，默认 `3` |
| `siliconflow_key` | SiliconFlow | API Key |
| `siliconflow_model` | SiliconFlow | 模型名，默认 `deepseek-ai/DeepSeek-V3` |

**notification（通知配置）**

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `provider` | `""` | `ServerChan` / `Qmsg` / `Bark` / `Telegram` |
| `url` | `""` | 通知服务 URL |

URL 格式：

| provider | 格式 |
|----------|------|
| ServerChan | `https://sctapi.ftqq.com/<key>.send` |
| Qmsg | `https://qmsg.zendee.cn/send/<key>` |
| Bark | `https://api.day.app/<key>/` |
| Telegram | `https://api.telegram.org/bot<token>/sendMessage`（需同时填 `tg_chat_id`） |

### welearn — WE Learn (SFLEP)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `username` | string | `""` | WE Learn 账号 |
| `password` | string | `""` | 登录密码 |
| `save_cookies` | boolean | `true` | 是否持久化 cookies |
| `tree_view` | boolean | `true` | 选课后打印课程目录树 |
| `progressbar_view` | boolean | `true` | 时长模式显示进度条 |

### zhs — 智慧树

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `username` | string | `""` | 智慧树账号 |
| `password` | string | `""` | 登录密码 |
| `qrlogin` | boolean | `true` | 优先二维码登录 |
| `save_cookies` | boolean | `true` | 是否持久化 cookies |
| `logLevel` | string | `"INFO"` | 日志级别：`"DEBUG"` / `"INFO"` |
| `proxies` | object | `{}` | 代理，如 `{"http": "http://127.0.0.1:8080"}` |

**qr_extra（二维码显示选项）**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `show_in_terminal` | boolean/null | `null` | `true` 终端 / `false` 弹窗 / `null` 自动 |
| `ensure_unicode` | boolean | `false` | 仅用 Unicode 字符打印 |

**pushplus（推送通知）**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enable` | boolean | `false` | 启用 PushPlus |
| `token` | string | `""` | PushPlus Token |

**bark（推送通知）**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enable` | boolean | `false` | 启用 Bark（iOS） |
| `token` | string | `""` | Bark URL |

**ai（AI 课程自动答题，需要有效的 API Key）**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | boolean | `true` | 是否启用 AI 课程自动处理 |
| `use_zhidao_ai` | boolean | `true` | `true` 用智慧树内置 AI / `false` 用外部 OpenAI |

**ai.openai（外部 OpenAI 兼容 API，use_zhidao_ai=false 时生效）**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `api_base` | string | `"https://api.openai.com"` | API 地址 |
| `api_key` | string | `"sk-"` | API Key |
| `model_name` | string | `"claude-3-5-sonnet-20240620"` | 模型名 |
| `extra_body` | object | `{}` | 额外请求参数，如 `{"reasoning_effort": "high"}`；需模型和 API 支持 |

**ai.ppt_processing（PPT 提供给 AI 作为参考材料，可选）**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `provide_to_ai` | boolean | `false` | 是否将 PPT 文本发给 AI 辅助答题 |
| `moonShot.base_url` | string | `"https://api.moonshot.cn/v1"` | MoonShot API 地址 |
| `moonShot.api_key` | string | `"sk-"` | MoonShot API Key |

AI 课程与普通课程统一展示在交互式列表中 — 菜单选择 `[3]` 后自动获取全部课程（知到/共享课/AI），编号展示供用户选择要刷的课程。

### yuketang — 雨课堂

yuketang 无账号密码，全部通过微信扫码登录。首次运行自动弹出终端 QR 码，扫码后 cookies 和 `university_id` 自动保存到共享 `cookies.json` / `config.json`。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `university_id` | string | `""` | 学校 ID（登录后自动获取） |

Cookies 存储在 `cookies.json` 的 `"yuketang"` 字段（cookie 字符串）。

启动后自动从 API 获取课堂列表，交互选择后统一下载所有课件。

登录策略：启动 → cookies 有效？→ 是 → 直接进入课堂选择 → 否 → 弹 QR 码 → 微信扫码 → 自动保存。

## 打包 exe

`fuckCourse.spec` 使用 `build_support.py` 中的逐文件源码和必要资源白名单，不递归收集平台目录中的日志、配置、Cookie、备份或缓存。新增平台源码或资源时，需显式更新白名单。

```bash
pip install -r requirements.txt
pip install pyinstaller
python -m PyInstaller --clean -y fuckCourse.spec
```

打包完成后，单文件可执行程序生成于 `dist/fuckCourse.exe`。请在分发前执行下列归档审计与离线验证；Git 忽略规则本身不提供打包防泄漏保护。

## 离线验证

```bash
python -m unittest discover -s tests -p "test_*.py"
python scripts/verify_binary.py dist/fuckCourse.exe --timeout 90
```

验证脚本先检查归档白名单、必需源码/字体资源及敏感文件名，再检查菜单退出和四个平台的依赖导入；非零退出、缺失完成标记或超时都会失败。每个平台探针在单独的进程内使用临时配置和日志目录，并禁止 Python 网络解析与连接。

探针只执行平台入口源码中的实际顶层导入声明及必要资源检查，不运行课程菜单、登录、学习、答题提交或模型请求。尤其 ZHS 入口包含顶层交互，不能将直接执行入口当作离线检查。探针通过不代表真实平台业务流程通过。

Hike 保留配置的完成百分比，按整数上报精度向上取整；连续五次回包未产生新进度会失败退出。WE Learn 提交失败只重试原分数。雨课堂任意缺页或坏页都会使本次 PDF 转换失败，不覆盖既有文件。

## 致谢

- 超星：基于 [Samueli924/chaoxing](https://github.com/Samueli924/chaoxing)
- WE Learn：基于 [Fanyuchang2026/welearn-helper](https://github.com/Fanyuchang2026/welearn-helper)
- 智慧树：基于 [VermiIIi0n/fuckZHS](https://github.com/VermiIIi0n/fuckZHS)
- 雨课堂：基于 [EdibleSalt/yuketang-ppt-downloader](https://github.com/EdibleSalt/yuketang-ppt-downloader)

## 架构说明

v3.0+ 双模式调度：
- **开发模式**：`subprocess.run()` 启动各平台，透传 stdin/stdout/stderr
- **frozen 模式**：`exec(compile(...))` 进程内加载模块脚本，避免双进程竞争 stdin

通过环境变量 `FUCKCOURSE_CONFIG`、`FUCKCOURSE_COOKIES`、`FUCKCOURSE_LOG_DIR` 传递根目录路径，各平台读写对应 section 和日志。开发模式使用独立子进程；frozen 模式共享解释器，不承诺模块缓存或全局状态完全隔离。

PyInstaller 打包时自动检测 `sys.frozen`：代码目录指向 `_MEIPASS`（解压的模块），用户数据（config/cookies/logs）指向 exe 所在目录。

## 免责声明

本工具仅供学习交流使用，请勿用于商业用途。使用本工具产生的任何后果由使用者自行承担。

## 许可

本项目基于 GPL-3.0 协议开源。包含以下上游代码：

| 组件 | 来源 | 原始协议 |
|------|------|----------|
| chaoxing | [Samueli924/chaoxing](https://github.com/Samueli924/chaoxing) | GPL-3.0 |
| welearn | [Fanyuchang2026/welearn-helper](https://github.com/Fanyuchang2026/welearn-helper) | MIT |
| zhs | [VermiIIi0n/fuckZHS](https://github.com/VermiIIi0n/fuckZHS) | 无 |
| yuketang | [EdibleSalt/yuketang-ppt-downloader](https://github.com/EdibleSalt/yuketang-ppt-downloader) | 无 |

详见 [LICENSE](LICENSE)。

