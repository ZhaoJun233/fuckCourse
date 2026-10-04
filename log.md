# fuckCourse 更新日志

## 未发布：v3.1.0 审查后修正

### 修复
- config: 超星/WE Learn 保存账号、雨课堂读写共享 JSON 时，不再将读取错误或非对象 JSON 当成空配置。语法损坏先独占创建唯一备份；备份失败停止写入，保留原件和历史备份。
- chaoxing: 用兼容 PriorityQueue 的停止项回收 task/retry 全部线程，不依赖 Python 3.13 的 shutdown；完整结算重试队列，保持原有五次重试上限。
- zhs: Hike 完成阈值向上取整对齐整数上报，循环内检查现有时间限额；连续五次服务端进度未超过最高已确认进度时明确失败退出，不再无限零增量上报。
- zhs: PPT 本地缓存使用完整 URL 的 SHA-256 身份，上传缓存使用内容 SHA-256 身份，避免跨目录同名/同大小文件错复用；检查真实路径并拒绝缓存目录及子路径的 symlink/reparse point。该检查不是对恶意并发文件系统替换的原子防护。
- welearn: 通过 JSON 解析判断提交成功；失败只重试用户原分数，不再改为 100，也不重复提交已经成功的结果。
- yuketang: 任意页面缺失或解码失败时整体失败，回收所有图像；全部页面解码并保存成功后才原子替换 PDF，避免留下残缺成功文件。
- packaging: 逐文件源码/资源白名单替代平台目录递归收集；保留 frozen 字体资源路径所需映射表，归档检查拒绝配置、日志、备份和非白名单平台文件。
- ci/tests: 用实际实现测试替换公式复制测试；增加配置故障注入、队列生命周期、Hike、缓存边界、PDF、打包与探针回归。EXE 验证包含归档审计、菜单退出和四平台离线导入，检查返回码、完成标记和超时。
- ci/tests: 修复 Python 3.10 配置读取故障注入未生效的问题：该版本 pathlib 缓存 io.open，测试改为拦截公开 Path.open，并断言原异常向上传播；不跳过测试，不修改配置保存的业务行为。

### 验证范围
- 离线测试及导入探针不执行真实登录、学习、答题提交或模型请求，不能作为真实平台业务流程通过的证明。
- 旧版 Python 的兼容性需以 CI 矩阵运行结果为准；无 shutdown 队列和 Python 3.10 语法检查仅是本地补充证据。
- 本节源码修正已纳入本次更新；尚未打新标签或发布新的 Release。下面 v3.1.0 的历史描述不代表其未覆盖边界已经修复。

## v3.1.0 (2026-09-30)

### 修复
- packaging: 新增维护标准 `fuckCourse.spec` 文件，显式收集动态依赖（requests, PIL, openai, concurrent 等），修复打包单文件 EXE 启动报 No module named 崩溃的问题。
- chaoxing: 修复课程 ID 过滤无匹配时静默退化为全选全刷所有课程的风险，改为安全终止并提示。
- chaoxing: 修复章节工作线程异常未调用 `task_done()` 导致主调度队列永久挂死的问题。
- chaoxing: 修复未开放章节 tries 计数被注释导致无限重试死循环的问题。
- chaoxing: 恢复言溪题库、LIKE知识库等接口的 TLS 证书标准验证，消除安全隐患并补充请求超时。
- zhs: 修复 Hike 视频到达终点时因时刻比较判断导致的无限请求死循环。
- zhs: 修复 AI 视频在低倍速下累计时间取整为 0 导致进度不推进的死循环问题；为 AI 答题设置最大重试上限，杜绝无限消耗 API Token。
- zhs: 考试心跳线程改为 daemon 线程，并增加生命周期退出清理机制，修复异常流程导致进程无法退出的问题。
- zhs: PPT 处理中对 URL 提取路径进行目录边界安全校验，彻底消除 `../` 目录逃逸漏洞风险。
- zhs: 修复超长 Prompt 截断切片使用浮点数导致 `TypeError` 的问题；修复 `extra_body` 覆盖控制字段引发的模型与传输模式冲突；规范化 OpenAI API URL 拼接。
- yuketang: PPT 图片抓取使用独立轻量请求，切断向外部 CDN 跨域泄漏 `sessionid` 与 `csrftoken` 的隐私隐患；补充课件下载缺页检测与告警。
- welearn: 修复刷时长中途网络异常中断时仍无条件上报为已完成的伪成功缺陷；保护课程模式自定义正确率不被二次覆盖。
- config: 修复共享 JSON 配置文件在解析损坏时被单平台覆盖抹除非相关数据的问题，增加自动备份机制。
- runtime: 增加 Python 3.10+ 对 `queue.ShutDown` 与 `typing.Self` 的向后兼容回退；多平台 `logging.basicConfig` 增加 `force=True` 防止日志处理器冲突。

---

## v3.1.0-dev (2026-06-27)

### 新增
- zhs: AI 课程接入主流程 — 菜单 `[3]` 交互式课程选择，统一展示知到/共享课/AI 课程
- zhs: `_interactive_select_courses()` — 运行时获取全部课程列表，编号展示，支持多选/全选/退出

### 重构
- zhs: `--aicourse` / `--noexam` CLI 参数移除，AI 课程改为自动检测
- zhs: `--fetch` CLI 参数移除，`execution.json` 文件缓存机制移除，改为运行时交互式选择
- zhs: `FUCKCOURSE_EXEC` 环境变量移除
- zhs: `_validate_ai_config()` / `_validate_openai_config()` / `_validate_ppt_config()` 提取为模块级函数
- zhs: `fuckWhatever()` 增加 `aiConfig` 参数，自动包含 AI 课程
- zhs: `import re` 移除（不再需要 execution.json 类型回退推断）

### 修复
- zhs: AI 课程与普通课程统一交互式选择，无需手动 `--fetch` 生成缓存文件

### 性能
- zhs: AI 配置验证失败在自动检测路径中只警告跳过，不阻塞普通课程

### 其他
- 版本号 v3.0.0-dev → v3.1.0-dev
- README: 新增 AI 配置文档；移除 yuketang CLI 参数示例（统一菜单驱动）；移除 execution.json 文档

---

## v3.0.0-dev (2026-06-14)

### 修复
- PyInstaller frozen: 子模块加载从 `subprocess.run()` 改为进程内 `exec(compile(...))`，解决 exe 在其他电脑上子进程双开竞争 stdin 导致闪退的问题
- zhs: `exit` 注入 `exec()` 命名空间，修复 `name 'exit' is not defined`
- PyInstaller spec: 使用 `sys.prefix` 动态定位 DLL，避免硬编码 `CONDA_PREFIX`
- PyInstaller spec: `collect_submodules()` 递归收集 chardet / charset_normalizer / fontTools / Crypto 子模块，根除 `No module named` 错误

### 架构
- 新增 `_run_frozen()` — frozen 模式用 `exec()` 进程内加载模块脚本，`_ns` 注入 `exit` 兼容 zhs
- 新增 `_setup_env()` — 统一环境变量注入 + `logs/` 目录创建
- 开发模式保留 `_run_subprocess()`，通过 `_python_exe()` 调用 conda python

## v3.0.0 (2026-06-13)

### 新增
- yuketang: 微信扫码登录，启动自动检测 cookies 过期/缺失 → 弹 QR 码登录
- yuketang: 终端 ASCII 渲染 QR 码（移植自 zhs `showImage`，内嵌到 `yuketang_login.py`）
- yuketang: `yuketang_login.py` — `qr_login()` 完整扫码流程（pre-info → 显示 QR → 轮询 → 保存）
- yuketang: `fetch_classroom_list()` — `/v2/api/web/courses/list?identity=2` 获取学生全部课程
- yuketang: `validate_cookies()` — `/v2/api/web/userinfo` 验证 cookies
- yuketang: 交互式课堂选择（多选逗号分隔 / 全选 `a` / 退出 `q`）
- yuketang: 交互式课件选择（多选逗号分隔 / 全选 `a` / 退出 `q`）
- yuketang: 多课堂时先逐个选课 → 收集完毕后统一下载
- yuketang: `university_id` 自动提取，从课程 API `course.university_id` 获取，过滤为 0 的非正式课程
- yuketang: 配置文件 `yuketang_config.py` → `yuketang_config.json`（`cookies` / `university_id` / `classroom_ids`）
- yuketang: 接入主系统统一启动器 `main.py` 菜单 `[4]`
- yuketang: 接入共享 `config.json` / `cookies.json`（通过 `FUCKCOURSE_CONFIG` / `FUCKCOURSE_COOKIES` 环境变量）
- yuketang: 日志输出到 `logs/yuketang.log`（通过 `FUCKCOURSE_LOG_DIR` 环境变量）
- yuketang: `yuketang-ppt-downloader/` 重命名为 `yuketang/`，`download_ppt.py` → `main.py`

### 反向工程
- yuketang: QR 登录 API — `GET /api/v3/user/login/pre-info` + `POST /api/v3/user/login`
- yuketang: 课程列表 API — `/v2/api/web/courses/list?identity=2`（identity=1 教的课，2 听的课）
- yuketang: 课件列表 API — `/v2/api/web/logs/learn/{id}?actype=14`
- yuketang: PPT 下载 API — `/api/v3/classroom-report/student/ppt`

## v2.2.2 (2026-06-12)

### 新增
- 统一日志目录 `logs/`，所有平台日志输出到根目录
- welearn: 接入 `logging` 模块，文件日志 `logs/welearn.log`
- welearn: 关键操作（登录、提交、异常）写入日志，静默异常补 `exc_info`

### 修复
- 编译后日志不可见：chaoxing/zhs 日志原本在模块目录（`_MEIPASS` 内），改为 `FUCKCOURSE_LOG_DIR` 环境变量指向 `DATA_DIR/logs/`
- chaoxing: `logger.py` 日志路径改为 `{LOG_DIR}/chaoxing.log`
- zhs: `logger.py` 日志路径改为 `{LOG_DIR}/zhs_logs/`

### 结构
```
logs/
├── chaoxing.log
├── welearn.log
└── zhs_logs/
    ├── debug.log / info.log / warning.log / error.log / critical.log
```

## v2.2.1 (2026-06-12)

### 新增
- welearn: 时长模式进度条 (`progressbar_view`)，多线程每行独立进度条，格式 `|#####     | 50.0% (15/30秒)`
- welearn: 课程目录树 (`tree_view`)，3 级结构 课程→单元→SCO，`[未开放]`/`[已完成]` 标签
- welearn: `print_course_tree()` 递归打印，SCO 数据缓存避免重复请求

### 修复
- PyInstaller: frozen 模式下拆分为 `APP_DIR`（`_MEIPASS`，代码）+ `DATA_DIR`（`sys.executable`，用户数据），修复 `[WinError 267] 目录名称无效`
- main.py: 图标路径修正为 `icon/icon.ico`
- main.py: `interrupted by user` / `按任意键` 提示统一左对齐

### 踩坑：PyInstaller 路径问题
- **v2.1.0 的"修复"引入新 bug**：v2.1.0 把 ROOT 从 `__file__` 改成 `sys.executable` 以解决 config 找不到，但这导致模块目录（chaoxing/zhs/welearn）也指向 exe 目录——而 PyInstaller 打包的 data 实际解压在 `_MEIPASS`，于是报 `目录名称无效`
- **正确做法**：frozen 下必须两条路径——代码目录用 `sys._MEIPASS`，用户数据目录用 `os.path.dirname(sys.executable)`，不能混用
- **图标**：`.gitignore` 忽略了 `icon/` 目录，编译命令路径也写错了（`icon\fuckCourse.ico` 实际是 `icon/icon.ico`），spec 没配 `icon=` 参数

## v2.2.0 (2026-06-12)

### 新增
- chaoxing: 课程目录树形视图 (`tree_view`)，对齐 ZHS 风格
- chaoxing: `decode_course_point()` 保留章节层级，新增 `_extract_section_tree()` 递归构建章→节→子节结构
- chaoxing: `print_course_tree()` / `_print_section()` 递归打印，已完成项标注 `[已完成]`
- chaoxing: `tree_view` 配置开关，默认开启，关闭则跳过打印

### 优化
- chaoxing: 树形打印增加分隔线，层级间距对齐 ZHS，不再紧凑

## v2.1.0 (2026-06-12)

### 新增
- 统一 config.json / cookies.json 架构，三平台各自读写自己的 section
- 首次运行自动创建 config.json 并写入完整默认字段
- 账号密码登录后自动回写 config，下次免输入
- WE Learn 自动登录：config 凭据 → 交互输入，去掉 cookie 登录步骤
- WE Learn 登录移到模式选择前面

### 修复
- chaoxing: config.json 不存在时不再返回 None，改为创建文件
- chaoxing: use_cookies 默认值改为 True，二次运行免登录
- chaoxing: cookie 过期自动提示重新输入并保存
- chaoxing: config 写入所有完整字段（tiku/notification）
- welearn: sso_login 中 extraCheck 为 null 时崩溃
- welearn: 每次登录后无条件保存 cookies
- PyInstaller: frozen 模式下 ROOT 改用 sys.executable，修复 exe 找不到 config/cookies

### 清理
- 删除 config_bridge.py
- 删除 chaoxing/config_template.ini
- 删除 zhs/meta.json（去掉更新检测）
- 删除各子目录残留的 config.ini / cookies.json / cookies.txt
- welearn: 删除开发者姓名、QQ、邮箱等菜单文案

## v2.0.0 (2026-06-10)

### 新增
- 统一启动器 main.py，菜单选择三平台
- subprocess 调度，stdin/stdout/stderr 透传
- 环境变量 FUCKCOURSE_CONFIG / FUCKCOURSE_COOKIES 传递路径
- WE Learn cookie 持久化

### 技术栈
- Python 3.13 (Conda: fuckcourse)
- PyInstaller 6.20.0 打包为单文件 exe
