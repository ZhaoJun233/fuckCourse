# 超星独立考试协议与验证边界

独立考试入口位于 `chaoxing/api/exam.py`，交互流程位于 `chaoxing/api/exam_runner.py`。
主菜单 `[5]` 与刷课调度分开；`python chaoxing/main.py --exam-list` 仅查询列表。

## 接口

所有考试请求限定到 `https://mooc1-api.chaoxing.com`，页面跳转按所处阶段限制路径：

| 用途 | 路径 |
| --- | --- |
| 列表 | `/exam-ans/exam/phone/task-list` |
| 封面 | `/exam-ans/android/mtaskmsgspecial`、`/exam-ans/exam/phone/task-exam` |
| 开始 | `/exam-ans/exam/phone/start` |
| 单题页面 | `/exam-ans/exam/test/reVersionTestStartNew` |
| 答题卡 | `/exam-ans/exam/phone/loadAnswerStatic` |
| 暂存与交卷 | `/exam-ans/exam/test/reVersionSubmitTestNew` |

字段与请求签名格式参考 [yatori-dev/yatori-go-core](https://github.com/yatori-dev/yatori-go-core) 的超星考试实现。该项目使用 MIT 许可证，适配代码的许可声明保留在 `exam.py` 中。请求签名是接口协议字段，与需要本人完成的诚信承诺签名不同。

开始与交卷分别要求完整确认词 `开始考试`、`提交考试`。暂存使用 `tempSave=true`，交卷使用 `tempSave=false`；计时与后续令牌取自服务端响应。考试客户端不自动重试请求，网络异常时要求使用官方客户端核对结果。

## 验证与限制

离线测试覆盖列表、重定向限制、页面解析、答案匹配、确认流程、暂存和交卷请求格式。真实账号仅验证课程与考试列表，未通过真实考试验证开始、暂存或交卷成功；这些行为仍需用户在使用时核对平台结果。

仅支持未监考的移动端单题页面。承诺签名、人脸、验证码、指定 IP、客户端限制和监考要求会停止流程；图片、公式、未解密文字或无法唯一匹配的答案不会猜测。已有服务端暂存答案不会覆盖，最终答题卡有未答题目时不交卷。

选择考试后若平台返回“章节任务点未完成”或“考试尚未开始”，程序会显示对应原因，未启动考试。请完成课程任务点或等待考试开放，并在官方客户端确认；不会通过修改参数跳过前置条件。EXE 子功能结束后会等待按回车，再返回主菜单，避免停止提示被清屏。

`exam_reviews/` 保存题干、参考答案和处理状态用于核查，不写入账号、Cookie 或请求参数。记录可能包含考试内容，已加入 Git 忽略列表，不随构建发布。
