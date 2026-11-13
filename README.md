# Σcholar Lineage Studio —— 证据可追溯的协作综述工作台（插件 + Web）

按通用 AI 技能目录组织的综述协作插件：`SKILL.md` + `references/` + `assets/`，附带可直接运行的 Flask + SQLite Web 界面。围绕「文献建库 → 统一提取 → 人工核验 → 章节组织 → 分支审核与合并 → 标签快照」，把证据卡片和变更历史落到同一个可移植项目中。

**Σcholar Lineage Studio（溯文）**是本项目的对外品牌；内部技术包名与协作 API 保持 `ReviewGit` 兼容。本目录是**规范版插件及继续开发目录**，不回写早期原型。它既可以复制到支持该技能目录结构的 AI 助手使用，也能独立运行本地 Web 应用；不是 DSH 原生 bundle，不依赖 Git 二进制。

> 本地启动：`python scripts/launch.py`，浏览器打开 `http://127.0.0.1:5000`。依赖只有 Python 3.8+ 与 Flask。
>
> **Token 赞助与致谢：本项目获得玻尔（Bohrium）赞助的 Token 支持，谨致谢意。**

## 系统结构

![ReviewGit 当前实现架构示意（AI 生成）](assets/reviewgit-structure.png)

> 此图是 **AI 生成的示意插画**（Doubao-Seedream 5.5），用于快速建立整体结构印象。图中文字、数字与排版可能存在生成误差，**以正文、REST API 表和代码为准**。它对应 2026-11-12 的本地实现：Flask 单进程 + SQLite、Mock 提取器、本地单项目协作内核。图中底部虚线区列出尚未实现的能力（真实 LLM Provider、PDF 全文解析、句级引用绑定、用户与角色鉴权、PostgreSQL / 多项目、历史版本导出），它们属于后续扩展，不是当前可用功能。

## 特性

- **文献与模板建库**：按标题、作者、年份、DOI 与摘要建立文献；自定义字段模板并选择生效口径。当前以摘要作为演示全文，不冒充 PDF 全文解析。
- **证据卡片与章节组织**：区分论断与逐字摘录，记录来源、状态与自定义字段；支持通过、驳回、修订以及把已核验卡片挂到章节。
- **原文摘录硬闸门**：人工通过与修订核验检查摘录是否为原文子串；向 main 合并时再次检查所有已核验卡片，未通过原文校验的摘录不能被标为已核验。
- **ChangeUnit 原子变更**：卡片字段、模板字段、章节节点、段落均使用稳定 `unit_key`，以规范化 JSON 的 SHA-256 标识内容，而不是整篇覆盖。
- **真正的分支隔离**：记录分支负责人、用途、起点与当前 head。非 main 提交只修改自身完整快照，不提前改动主干的活动卡片、章节或段落。
- **三方合并**：沿提交 DAG 找共同祖先，处理无关修改、相同修改、增加与删除；仅当双方以不同结果修改同一单元时才报告冲突。
- **MR 审核与内联评论**：默认至少一位非 MR 作者的审核人批准，可配置更高门槛；评论必须锚定本次源分支实际修改的 `unit_key`。
- **有时效的审核**：历史审核保留，但源分支的新提交使旧批准不再计入新 head；`request_changes`、过期预览和未解决冲突均阻断合并。
- **三选一冲突解决**：展示 base / ours / theirs，支持保留目标、保留来源与手工内容；解决人、时间与内容哈希均留痕，重复预览不会无故清除解决记录。
- **标签与非破坏性回滚**：标签保存固定提交的完整快照，不随后续提交改变。回滚生成反向提交；当前 MVP 仅允许回滚 main 当前 head，同单元已被后续修改时拒绝静默覆盖。
- **追加式审计与双父提交**：分支、提交、审核评论、冲突解决、合并、标签和回滚均记录操作者和对象；合并提交保留两个 parent。
- **自包含、可移植**：运行时路径由 `__file__` 推导，可以通过 `REVIEWGIT_DB` 指定数据库。启动和种子脚本使用同一个数据库路径，分发包不含数据库或缓存。

协作层不调用 Git 二进制，也不把整篇综述当普通文本做字符级合并：每张卡片的核心字段、模板字段、章节节点和文稿段落均有稳定 `unit_key`。

## 目录结构

```text
reviewgit/
├── SKILL.md                         # 技能入口与标准工作流、判定规则
├── README.md                        # 本文件
├── references/
│   ├── extraction-template.md       # 统一提取模板
│   ├── evidence-card-spec.md        # 证据卡片与逐字摘录规范
│   ├── git-workflow.md              # 提交 / 审核 / 合并约定
│   └── roles.md                     # 分工角色说明
├── assets/
│   ├── review-outline-template.md
│   ├── evidence-card-template.md
│   ├── division-of-labor-log-template.md
│   └── reviewgit-structure.png      # 当前实现架构图（与代码一致）
├── scripts/
│   ├── launch.py                    # 启动服务，支持自定义数据库与端口
│   └── package.py                   # 重建分发快照，排除运行时文件
└── web/
    ├── app.py                       # Flask 原功能入口与核验摘录校验
    ├── collaboration.py             # ChangeUnit / DAG / MR / 冲突 / 标签 / 回滚
    ├── seed.py                      # 聚酰亚胺建模演示语料
    ├── requirements.txt             # Flask
    ├── templates/index.html         # 五个标签页的原生 HTML/JS 界面
    ├── tests/
    │   ├── test_collaboration.py     # Flask + SQLite 隔离回归
    │   └── test_http_smoke.py        # 真实进程与 HTTP 流程
    └── reviewgit.db                 # 运行时自动生成，不进入分发包
```

## 快速开始

需要 Python 3.8+ 与 Flask：

```bash
python -m pip install -r web/requirements.txt
python scripts/launch.py
# 重建演示语料后再启动
python scripts/launch.py --seed
# 指定端口
python scripts/launch.py --port 8000
```

浏览器打开 `http://127.0.0.1:5000`。数据库首次启动自动创建，也可以使用 `REVIEWGIT_DB` 指定另一个 SQLite 文件。没有数据库时自动灌入 21 篇文献、84 张卡片和 8 节框架的聚酰亚胺演示语料；启动器与种子脚本沿用同一个自定义路径。

**`python scripts/launch.py --seed` 会删除并重建指定的数据库，请在测试副本使用。分发包默认不含数据库。**

## 界面工作流

1. 在「文献 / 提取模板 / 提取与核验 / 综述框架」准备文献、卡片和章节。
2. 在「协作与版本」新建有个人/主题/章节用途的分支，选择卡片论断或章节标题，形成带作者和说明的提交。
3. 以所选分支为源、`main` 为目标发起合并请求。
4. 重新检测冲突。审核人必须不同于 MR 作者；可提交定位到 `unit_key` 的评论，再「批准」或「要求修改」。
5. 有冲突时对比 base / ours / theirs，选择「保留目标 / 保留来源 / 手工内容」。所有活动冲突必须解决。
6. 合并到 `main` 后卡片列表才反映源分支的变更；标签可以固定任意主干提交的快照。
7. 主干提交历史中选择「回滚此提交」。若该单元后来又被修改，回滚返回冲突错误而不会静默覆盖。

## ChangeUnit

哈希为规范化 JSON（键排序、UTF-8、无多余空白）的 SHA-256。每个提交还保存完整快照，标签保存所指向的快照，不依赖重新猜测内容。

| 类型 | `unit_key` 示例 | 哈希内容 |
|---|---|---|
| card_attr | `card_attr:12:claim` | 一个卡片核心字段值 |
| card_field | `card_field:12:主要发现` | 一个模板字段值 |
| block | `block:5` | 段落的 `section_id/ord/text` |
| sentence | `sentence:9` | 预留的句子单元内容 |
| citation | `citation:9` | 预留的引用绑定内容 |
| structure | `structure:3` | 章节的 `title/parent_id/ord` |
| metadata | `metadata:topic:7` | 其他元数据 JSON |

提交示例：

```http
POST /api/branches/2/commits
Content-Type: application/json

{
  "author": "张三",
  "message": "修订卡片 12 的论断",
  "changes": [{
    "unit_type": "card_attr",
    "unit_key": "card_attr:12:claim",
    "old_content": "修改前的论断",
    "new_content": "修改后的论断"
  }]
}
```

`new_content: null` 表示删除单元；必须显式给出 `new_content`。如果携带 `old_hash` 或 `old_content`，服务端会核对分支当前版本，避免覆盖过期编辑。重复键、空提交、空说明和无变化提交均会被拒绝。

<details>
<summary><strong>展开 REST API 一览</strong></summary>

| 方法 | 路径 | 用途 |
|---|---|---|
| GET / POST | `/api/branches` | 分支列表 / 从 main 当前版本创建分支 |
| GET | `/api/branches/<id>` | 分支详情及当前完整单元快照 |
| GET / POST | `/api/branches/<id>/commits` | 分支历史 / 原子提交 |
| GET | `/api/commits/<id>` | 提交、双家长（合并提交）与内容差异 |
| POST | `/api/commits/<id>/revert` | 在 main 生成反向提交 |
| GET / POST | `/api/merge-requests` | MR 列表 / 创建 MR |
| GET | `/api/merge-requests/<id>` | MR、审核评论、活动冲突与预览 |
| GET / POST | `/api/merge-requests/<id>/preview` | 以当前两侧 head 重新检测并保留冲突历史 |
| POST | `/api/merge-requests/<id>/reviews` | 批准、要求修改与内联评论 |
| POST | `/api/merge-requests/conflicts/<id>/resolve` | 解决活动冲突 |
| POST | `/api/merge-requests/<id>/merge` | 校验策略并生成合并提交 |
| GET / POST | `/api/tags` | 标签列表 / 固定主干快照 |
| GET | `/api/tags/<id>` | 标签对应完整快照 |
| GET | `/api/audit-logs` | 追加式审计记录 |

常用审核请求：`{"reviewer":"李老师","verdict":"approve","comments":[{"unit_key":"card_attr:12:claim","body":"请核对措辞"}]}`。`verdict` 只能是 `approve / request_changes`。

冲突解决 `resolution` 的语义：

- `keep_base`：**保留目标分支当前内容（ours）**，兼容数据模型中的枚举名称，不是回退到共同祖先；
- `keep_branch`：保留来源分支当前内容（theirs）；
- `manual`：显式传入 `manual_content`，内容可以是 JSON，也可以是 `null` 表示删除。

</details>

## 合并与版本规则

- 通过提交 DAG 找当前源/目标 head 的共同祖先，而非始终使用分支起点。
- ours 与 theirs 相同：直接采用；只有一侧相对 base 改变：采用改变侧；双方均改成不同内容：冲突。
- MR 默认需要 1 位非作者审核人的当前轮批准。源分支新增提交后，历史批准继续保留，但不计入新 head。
- `request_changes` 阻断合并；过期预览、未解决冲突也会阻断，不会产生半截提交。
- 合并提交记录两个 parent，审计记录操作者和对象，历史接口不提供删除或重写。
- Tag 只能指向 main 直接拥有的提交，重名会被拒绝。Revert 是带 `reverts_commit_id` 的新提交。
- main 的 `cards / sections / blocks` 是活动投影；非 main 提交不会提前修改这些表。

## 当前边界（重要）

- 这是**本地、单项目、无认证**的协作内核。作者、审核人与操作人是审计文本，不代表安全身份验证。尚未实现 PRD 中的用户、成员角色和服务端授权，不应直接暴露到不受信任的网络。
- 首次打开协作接口时，以现有卡片和章节创建根提交。原导入、提取、核验和框架接口是兼容入口，直接作用于活动投影，**不会自动生成提交**；建议在建立分支前完成这些准备，后续版本化编辑通过 ChangeUnit 提交。
- 卡片与章节已接入主干活动投影；`sentence / citation / metadata` 已支持版本化存储，但尚无对应编辑器。这里的 deleted live row 只删活动投影，不可变提交和标签仍可恢复内容。
- 标签快照可查看；历史版本导出、句级引用绑定、孤儿论断检测尚未实现。
- `merge_policy` 的完整项目设置页尚未实现；当前 MR 默认最少 1 个批准，可创建时指定更高门槛。
- **Token 赞助与模型接入是两件事**：当前仍使用 Mock 规则抽取，未接入真实 LLM Provider；PDF 解析与 CanonicalText 全文入库尚未实现。
- PostgreSQL、多项目、多进程任务队列仍是后续扩展；SQLite 适合当前本地单进程场景。订阅监控、冲突仲裁和撤稿标记也尚未实现。

## 自动化测试

```bash
python -m unittest discover -s web/tests -v
```

当前共 **20 项测试**，覆盖分支隔离、无关/相同修改合并、三种冲突解决、审核门槛与失效、内联评论、章节/段落投影、删除侧冲突、标签不可变性、回滚保护和摘录硬闸门。

测试使用临时 `REVIEWGIT_DB` 与 Flask `test_client()`；另有一项真实进程/HTTP 冒烟测试会自动启动并关闭临时端口上的服务。均不会改写正式演示数据库，HTTP 测试包含正式库 SHA-256 不变的断言。

**测试没有调用真实 LLM，也不是模型输出质量或 Token 账号连通性的验收。HTTP 页面可达不等同于已经完成人工视觉或浏览器兼容性验收。**

## 打包

`reviewgit-plugin.zip` 和分发副本只是快照。修改代码后运行：

```bash
# 默认生成 dist/reviewgit-plugin.zip 与 dist/reviewgit-plugin/
python scripts/package.py

# 也可显式指定分发目录（不会改动旧的历史 MVP）
python scripts/package.py --output-dir path/to/distribution
```

打包脚本排除数据库、缓存与 `dist`，包含后端、前端、协作模块、测试、说明与上面的结构图。运行时路径均从 `__file__` 推导。分发 ZIP 与解压副本都只是快照；本目录才是后续开发依据。
