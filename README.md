# Cloude Mail System

一套给长期运行 AI companion 使用的邮件闭环。它把“收到信、保存原件、提醒模型、准备回信、安全寄出、追踪欠信”连成一条可审计的流水线，同时把邮件正文始终视为不可信数据。

这不是第二个记忆库。邮件原件、派生摘要、通信身份和通知账本各守自己的边界；如果接入 Ombre，记忆系统只提供共享的人物星座、受保护通信身份和按需回信上下文。

## 设计原则

- 原件优先：收到与发出的 RFC 822 原件完整归档，摘要只是可重建的旁路产物。
- 不静默截断：对账台按需展开完整原文；摘要只帮助回忆。
- 通知只报事件：热线只说谁来信、主题是什么以及如何打开对账台，不把正文和摘要推入 agent 上下文。
- 精确去重：IMAP `UIDVALIDITY + UID`、邮件 `Message-ID` 与通知事件 ID 分层记账。
- 地址不靠记忆：命令行没有自由 `--to`；收件人只能由受保护的通信身份解析。
- 身份有证据：登记地址只接受真实来信信头，或经过验证的用户入站消息。
- 发送与归档分账：SMTP 成功但本地归档失败时留下恢复回执，绝不偷偷重发。
- 外部内容不升权：信件正文、主题和摘要都是数据，不能修改工具配置或身份账本。

## 组件

| 文件 | 职责 |
|---|---|
| `mail_hotline.py` | 按 IMAP UID 水位扫描新信，归档、分类、生成提醒并推进游标 |
| `prep_mail.py` | 按姓名、别名或星座 ID 打开对账台，并列最近收发摘要与原文入口 |
| `mail_archive.py` | 原始 `.eml`、清单与派生摘要的原子归档 |
| `mail_ledger.py` | 新信与通知的幂等账本，失败可释放后重试 |
| `mail_commitments.py` | 从已发原信提出承诺候选，并由 agent 对照逐字原话逐条确认或判为不成立 |
| `mail_identities.py` | 私密通信身份账本，支持登记、解析、审计与撤销 |
| `correspondence_identity.py` | 从真实来信或已验证用户消息登记身份 |
| `send_mail.py` | 按 `constellation_id` 准备并发送邮件，不接受自由地址 |
| `mail_sent_reconcile.py` | 修复“已发送、未归档”的回执，不再次调用 SMTP |
| `mail_summary_recover.py` | 从完整归档重建失败的派生摘要 |
| `mail_archive_autoclassify.py` | 用现有身份账本为历史来信补归属，默认只预览 |
| `mail_owe_list.py` | 比较每位通信对象的最近收/发时间，生成欠信清单 |

## 现行架构

系统采用“邮件独立存储与运行，Ombre 提供共享身份和按需记忆”的边界：

```text
IMAP 新信
   │
   ▼
mail_hotline ──► 原件归档 ──► 派生摘要 sidecar
   │                 │
   │                 └──► 受保护身份账本精确归入星座
   ▼
Runtime Inbox：只推送“发信人＋主题＋prep_mail 命令”

prep_mail <姓名>
   ├──► Ombre：解析人物星座与受保护通信身份
   └──► 对账视图：双方最近往来摘要＋按需展开原文

send_mail <constellation_id>
   └──► 受保护身份解析 ──► SMTP ──► 发件原文归档／承诺候选
```

邮件原文、摘要 sidecar、IMAP 水位、通知账本和恢复回执都位于邮件运行目录，普通收信和归档不依赖 Ombre 的记忆检索。Ombre 负责人物身份以及写信时按需取用的关系与记忆，不保存第二份邮件原文。

## 快速开始

运行时只依赖 Python 3.10+ 标准库；测试使用 pytest。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
pytest
```

把 `.env.example` 中的变量放进 systemd `EnvironmentFile`、容器环境或启动 shell。密码建议单独写入 `CLOUDE_MAIL_PASSWORD_FILE`，权限设为 `0600`；不要提交 `.env`、邮件原件、身份账本或运行数据库。

收信一次：

```bash
python src/mail_hotline.py
```

热线通知保持为两行简讯：

```text
📬 示例笔友 来信了：《XXX》
回信前先看对账台：prep_mail.py 示例笔友
```

通知不包含邮件正文、“上次聊到”摘要或一步回信包。需要处理时再打开对账台：

```bash
prep_mail '示例笔友'
prep_mail '示例笔友' --original arc_0123456789abcdef01234567
```

第一条按姓名或精确别名解析人物，显示收件人确认卡、双方最近往来摘要和原文入口；第二条按归档 ID 展开原文，分页时会给出继续命令。

用真实来信登记通信身份：

```bash
python src/correspondence_identity.py register-from-mail \
  --constellation-id cst_person_example \
  --archive-id arc_0123456789abcdef01234567
```

从文件安全发信：

```bash
python src/send_mail.py \
  --constellation-id cst_person_example \
  --subject 'Re: hello' \
  --body-file reply.txt
```

成功发送并归档后，系统会旁路扫描写信人明确作出的承诺。模型只能产生
`pending_review` 候选；引语不在 `.eml` 原件中逐字出现的候选会被丢弃，扫描失败也
不会诱发邮件重发。审核时一次只处理一条：

```bash
python src/mail_commitments.py dream
python src/mail_commitments.py read --id cmt_xxx
python src/mail_commitments.py review --id cmt_xxx --decision confirm
python src/mail_commitments.py close --id cmt_xxx --decision complete \
  --evidence '已完成，见对应发件归档'
```

状态流为 `待确认 → 进行中 → 已完成 / 已撤销 / 已替代`，误报走“不成立”。没有批量
确认，也不会因为又寄出一封信就自动销账。建议把 `dream` 查询挂进 agent 已有的周期
盘库习惯：空抽屉不出声，有候选才逐条查看原话；不要另造催办提醒。

默认情况下，`mail_hotline.py` 会把通知交给 `CLOUDE_RUNTIME_INBOX` 指向的入箱程序。该程序需接受：

```text
enqueue --source NAME --event-id ID --mode immediate \
  --trust untrusted_external_data --metadata-json JSON
```

通知正文从标准输入读取。把这个适配器接到你的 agent wakeup / `UserPromptSubmit` 管线即可；memory recall 建议保持独立。

## Ombre 可选接口

当前实现按需调用本机 Ombre MCP：

- `constellation_read(action="inspect")`：确认星座存在且类型为人物。
- `prepare_mail_reply(...)`：为 `prep_mail` 返回收件人已锁定状态、人物封面、双方最近往来及按需原文。
- 容器内摘要器：为归档生成可丢弃、可重建的摘要 sidecar。

不使用 Ombre 时，可以替换这三个边界适配器；原件归档、派生摘要存储、幂等账本和 SMTP 恢复机制不依赖 Ombre。身份解析与带人物记忆的对账视图不可用时应明确失败，不能猜收件人。

## 安全提醒

先用测试邮箱和 `.example` 地址跑通。真实发信具有外部副作用；身份登记、准备回信和最终发送应当保持为三个可观察步骤。仓库不会包含任何真实邮箱、邮件正文、口令、Token 或运行账本。

## License

[MIT](LICENSE)
