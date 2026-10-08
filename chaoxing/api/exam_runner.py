"""Interactive exam workflow. Starting and submitting are separate opt-in gates."""
import json
import os
from pathlib import Path
import secrets

import requests

from api.exam import ExamClient, ExamError, answer_fields


def write_review(path, rows):
    """Only questions/answers/status: no cookies, signed URLs or account fields."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        raise ExamError("无法写入本地检查记录，已停止；若已开始考试，请及时在官方客户端核对。") from None


def run_exam_session(client, exam, tiku, *, ask=input, emit=print, review_dir=None):
    meta = client.prepare(exam)
    emit(f"所选考试：{exam['name']}（{exam['course']['title']}）")
    emit("开始后可能立即计时。AI 答案不保证正确；逐题暂存不等于交卷。")
    if tiku.DISABLE:
        raise ExamError("未启用答题接口，未启动考试。请先配置超星题库/AI。")
    if ask("输入「开始考试」才会开始；回车取消：").strip() != "开始考试":
        emit("已取消，未启动考试。")
        return
    code = ask("请输入老师提供的考试码：").strip() if meta["need_code"] else ""
    if meta["need_code"] and not code:
        raise ExamError("考试码为空，未启动考试。")
    if review_dir is None:
        config = os.environ.get("FUCKCOURSE_CONFIG")
        root = Path(config).resolve().parent if config else Path(__file__).resolve().parents[2]
        review_dir = root / "exam_reviews"
    review = Path(review_dir) / f"exam-{secrets.token_hex(8)}.json"
    write_review(review, [{"status": "开始请求待核对"}])
    emit(f"检查记录：{review.resolve()}")
    client.start(meta, confirmed=True, code=code)
    sheet = client.sheet()
    rows = []
    write_review(review, rows)
    emit(f"共 {len(sheet)} 题；检查记录：{review.resolve()}")
    seen = set()
    unresolved = 0
    for number, (index, completed) in enumerate(sheet.items(), 1):
        if completed:
            rows.append({"number": number, "index": index, "status": "保留服务器已答题目"})
            write_review(review, rows)
            emit(f"[{number}/{len(sheet)}] 已有暂存答案，未覆盖。")
            continue
        emit(f"[{number}/{len(sheet)}] 正在读取题目…")
        paper = client.fetch(index)
        question = paper["question"]
        if question["id"] in seen:
            raise ExamError("不同题号返回同一道题，已停止；未交卷。")
        seen.add(question["id"])
        row = {"number": number, "index": index, "type": question["type"],
               "title": question["title"], "options": question["options"], "answer": None,
               "status": "待查询"}
        rows.append(row)
        write_review(review, rows)
        emit(f"[{number}/{len(sheet)}] {question['title']}\n正在查询答题接口…")
        answer = None
        if question["type"] != "unsupported":
            try:
                answer = tiku.query(dict(question))
            except Exception:
                # Existing providers may include keys/URLs in exception messages.
                emit("答题接口失败，未猜测答案。")
        row["answer"] = answer if isinstance(answer, str) else None
        if answer_fields(question, answer) is None:
            unresolved += 1
            row["status"] = "答案未匹配，需人工完成"
            write_review(review, rows)
            emit(f"[{number}/{len(sheet)}] 未匹配，保留原状，不随机作答。")
            continue
        row["status"] = "待暂存（若请求异常，请在平台核对）"
        write_review(review, rows)
        emit(f"[{number}/{len(sheet)}] 参考答案：{answer}\n正在暂存…")
        client.save(answer)
        row["status"] = "平台确认已暂存"
        write_review(review, rows)
        emit(f"[{number}/{len(sheet)}] 暂存成功。")
    emit(f"检查记录：{review.resolve()}")
    if unresolved:
        emit(f"{unresolved} 题需人工完成；未交卷。请及时返回官方客户端，考试计时可能仍在继续。")
        return
    emit("答案已暂存，但未交卷。请核查答案；等待期间考试计时可能继续。")
    if ask("输入「提交考试」才会交卷；回车仅保留暂存：").strip() != "提交考试":
        emit("未交卷，请及时在官方客户端检查并完成考试。")
        return
    latest = client.sheet()
    if set(latest) != set(sheet) or not all(latest.values()):
        raise ExamError("平台答题卡仍有未答题目或发生变化，未交卷，请人工检查。")
    # Refresh server-generated timing fields; never synthesize a remaining time.
    client.fetch(next(reversed(latest)))
    client.submit(confirmed=True)
    rows.append({"status": "平台确认交卷成功"})
    write_review(review, rows)
    emit("平台确认交卷成功。")


def run_exam_mode(chaoxing, course_ids=None, *, list_only=False, ask=input, emit=print,
                  client=None, review_dir=None):
    owned_session = None
    if client is None:
        from api.base import SessionManager
        source = SessionManager.get_session()
        # Unlike chapter sessions, never retry state-changing exam requests.
        owned_session = requests.Session()
        owned_session.headers.update(source.headers)
        owned_session.cookies.update(source.cookies)
        client = ExamClient(owned_session)
    try:
        emit("独立考试模式：不会刷视频、直播或章节测验。正在只读查询考试列表…")
        courses = chaoxing.get_course_list()
        if course_ids:
            courses = [course for course in courses if course["courseId"] in course_ids]
        if not courses:
            emit("未找到所选课程，未进入考试。")
            return
        exams = []
        for course in courses:
            emit(f"正在查询：{course['title']}")
            try:
                exams.extend(client.list_exams(course))
            except ExamError as error:
                emit(f"{course['title']}：{error}")
        if not exams:
            emit("没有读取到考试记录；若上方有失败提示，请在官方客户端核对。")
            return
        for i, exam in enumerate(exams, 1):
            emit(f"[{i}] {exam['course']['title']} / {exam['name']} / {exam['status']} / {exam['remaining']}")
        if list_only:
            emit("只读查询结束，未进入或启动任何考试。")
            return
        selected = ask("请选择一个考试序号（0/回车返回）：").strip()
        if not selected or selected == "0":
            return
        if not selected.isdigit() or not 1 <= int(selected) <= len(exams):
            raise ExamError("考试序号无效，未进入考试。")
        run_exam_session(client, exams[int(selected) - 1], chaoxing.tiku,
                         ask=ask, emit=emit, review_dir=review_dir)
    except ExamError as error:
        emit(f"已停止：{error}")
        emit("若已经开始考试，计时可能继续；请及时在官方客户端核对。")
    except KeyboardInterrupt:
        emit("已停止自动答题，未主动交卷。计时可能继续；如中断发生在交卷请求期间，请核对平台状态。")
    finally:
        if owned_session:
            owned_session.close()
