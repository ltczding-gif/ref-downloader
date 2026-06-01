# ref-downloader

> **别再花一下午追几十篇参考文献 PDF。**
> 输入一个 DOI，全部参考文献自动到手——用你已有的机构访问权。

[![Version: 0.4.1](https://img.shields.io/badge/version-0.4.1-orange.svg)](CHANGELOG.md)
[![Status: beta](https://img.shields.io/badge/status-beta-orange.svg)](#已知限制)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
![Verified on Windows + Edge](https://img.shields.io/badge/verified%20on-Windows%20+%20Edge-success)

[English full version](README.md)

> **状态：beta (v0.4.1)。** 仅在 Windows + Microsoft Edge 验证过。macOS / Linux / Chromium 未测试。SI 下载和出版商页面变动是当前最容易出问题的地方。欢迎提 issue / PR。

> **重要——不是付费墙绕过工具。** ref-downloader 用的是 _你_ 的机构访问权。如果你的学校/单位订阅了某期刊，那条参考文献就能下；如果没订阅，那条会标 `manual_pending` 等你手动跟进。

## 演示（30 秒控制台预览）

```text
$ python run_ref_downloader.py 10.1021/jacs.5c05017

=== Ref Downloader Wrapper ===
DOI:         10.1021/jacs.5c05017
PROJECT:     jacs.5c05017
Config:      config.example.toml + config.local.toml

>>> extract_refs.py
  Title: Designing Natural Cell-Inspired Heme-Spurred Membrane...
  References found: 38

>>> validate_refs.py
  Total: 38  Verified: 38  Failed: 0  No DOI: 0

>>> download_refs.py
  [ 1] downloaded (842 KB)        Lee2016_NatEnergy.pdf
  [ 2] downloaded (1.2 MB)        Wang2018_AdvMater.pdf
  [ 3] manual_pending (auth_redirect)
  [ 4] downloaded (655 KB)        Chen2019_JACS.pdf
  [ 5] failed (challenge_timeout)
  [ 6] ignored (ignored_institution_access)
  ... 还有 31 篇参考文献处理中 ...
  [38] downloaded (956 KB)        Park2024_JElectrochemSoc.pdf

========== Download report ==========
Total references:  38
Main PDFs:         33 downloaded · 3 manual_pending · 1 failed · 1 ignored
SI files:          12 captured
PDFs land in:      ./jacs.5c05017_refs/jacs.5c05017/
=====================================
```

## 目录

- [给你的价值](#给你的价值)
- [为什么不用 Zotero、scihub 或通用爬虫？](#为什么不用-zoteroscihub-或通用爬虫)
- [快速开始](#快速开始)
- [系统要求](#系统要求)
- [安装](#安装)
- [使用示例](#使用示例)
- [配置](#配置)
- [架构](#架构)
- [已支持出版商](#已支持出版商)
- [已知限制](#已知限制)
- [贡献](#贡献)
- [安全](#安全)
- [License](#license)

## 给你的价值

- **机构付费内容免配置就能下。** _直接驱动你真实的 Microsoft Edge 配置文件，浏览器里登录过的会话自然继承。不要 API key、不要代理、不需逆向工程。_
- **一个 DOI 输入，全部参考文献 PDF 输出。** _Crossref 驱动 + 17+ 家出版商专用下载路径（Wiley PDFDirect、Elsevier viewer、AIP 加载页等待——见 [出版商可靠度分级表](docs/SUPPORTED_PUBLISHERS.md)），不是通用爬虫。_
- **Agent 两种调用模式**（v0.4.1+）。_**Mode A**：丢一篇论文，下它的全部参考文献。**Mode B**：丢一个自定义列表 —— DOI / 文献标题 / arXiv-PMID / BibTeX / 抽象描述（"Wang 2024 年 Nature Energy 上的氢演化文章"）—— Agent 自动解析成 DOI 列表然后下载。详见 [SKILL.md](skills/ref-downloader/SKILL.md)。_
- **失败的条目和原因一目了然。** _`download_report.csv` 给每篇参考文献状态 + 原因（`manual_pending (auth_redirect)`、`failed (challenge_timeout)`、`ignored`），`events.jsonl` 留每篇的事件流。_
- **断点续跑**：VPN 断、浏览器崩、`Ctrl+C` 后都能继续。 _状态按项目目录持久化；重跑自动跳过已下载、只重试失败。_

## 为什么不用 Zotero、scihub 或通用爬虫？

- **vs. Zotero 的 _Find Available PDF_** —— 它一篇一篇走，碰到 SSO 跳转就放弃。ref-downloader 整个参考列表批量走，把 SSO 跳转当成可配置步骤而不是死路。
- **vs. scihub 类工具** —— 不带你的机构 license，本来你 _合法_ 有权限的付费内容也直接失败。ref-downloader 复用你浏览器里的认证会话，你已经付费的订阅真的算数。
- **vs. 通用网络爬虫** —— 不知道 Wiley 要走 PDFDirect、Elsevier 要点 viewer、AIP 服务器先返中文加载页。ref-downloader 内置 17+ 出版商专用路径 + Elsevier popup 状态机 + `--auto` 模式异步重试队列（manual_pending 60 秒后自动重试一次，复用热会话）。
- **vs. 普通 Playwright** —— Cloudflare / Radware / Turnstile 重度站点会被拦下。设 `REF_DOWNLOADER_BROWSER=cloak` 切到 [cloakbrowser](https://pypi.org/project/cloakbrowser/) 隐身 Chromium + 人化输入，不改代码、同一条流水线。详见 [配置](#配置) 一节。

## 快速开始

Skill 自包含在 `skills/ref-downloader/` 下。选你的 agent 框架对应的安装路径：

```powershell
git clone https://github.com/ltczding-gif/ref-downloader.git

# 任选一个安装位置（按你的 agent 框架）:
#   Claude Code:        cp -r ref-downloader/skills/ref-downloader ~/.claude/skills/
#   Codex CLI:          cp -r ref-downloader/skills/ref-downloader ~/.codex/skills/
#   Copilot CLI / VSC:  cp -r ref-downloader/skills/ref-downloader .github/skills/
#   项目级（任何框架）: cp -r ref-downloader/skills/ref-downloader .agents/skills/

cd ~/.claude/skills/ref-downloader     # 或你 cp 到的目标位置
pip install playwright pymupdf          # skill 协议不管 Python 依赖
playwright install msedge
cp config.example.toml config.local.toml      # 然后改 [crossref].mailto

# 然后在 agent 里描述任务，skill 会通过 description 触发。
# CLI 直接调试: python scripts/run_ref_downloader.py 10.1021/jacs.5c05017
```

预期看到：化学/物理论文一般 30-80 篇参考文献被发现，状态混合 `downloaded`（你机构覆盖的）、`manual_pending`（SSO 跳转或付费墙）、偶尔 `failed`（出版商怪癖）。建议第一次跑用一篇你机构实际订阅期刊的 DOI，命中率最高。详细安装与配置见下方。

## 系统要求

- **操作系统**：Windows 10/11（已验证）。macOS / Linux 未测试，欢迎 PR。
- **浏览器**：Microsoft Edge（Stable channel）。脚本会接管你的持久 Edge profile，运行前请关闭所有 Edge 窗口。
- **Python**：3.11 或更新（用了标准库 `tomllib`）。
- **可选**：Zotero 安装（自动从 PDF 文件名查 DOI，速度比文本提取快很多）。
- **可选**：PyMuPDF（`pip install pymupdf`），用于 Zotero 不可用时从 PDF 文本提取 DOI。

## 安装

### 作为 agent skill（推荐）

按你的 agent 框架选目录：

| 框架 | 安装命令 |
|---|---|
| Claude Code | `cp -r skills/ref-downloader ~/.claude/skills/` |
| Claude Agent SDK | 同上（自动从 `~/.claude/skills/` 发现） |
| Codex CLI | `cp -r skills/ref-downloader ~/.codex/skills/` |
| Copilot CLI / VS Code agent | `cp -r skills/ref-downloader .github/skills/` |
| 任一框架（项目级） | `cp -r skills/ref-downloader .agents/skills/` |

然后在拷过去的 skill 目录内装 Python 依赖（skill 协议不管这些）：

```powershell
cd ~/.claude/skills/ref-downloader     # 或你 cp 到的目标
pip install playwright pymupdf
playwright install msedge

cp config.example.toml config.local.toml
# 编辑 config.local.toml，至少改 [crossref].mailto
# Windows: notepad config.local.toml
# macOS / Linux: $EDITOR config.local.toml   (或 vim / nano / code 等)
```

### 作为 Python 工具开发

如果要 hack 代码，skill 文件夹本身就是个可运行 Python 项目：

```powershell
git clone https://github.com/ltczding-gif/ref-downloader.git
cd ref-downloader

pip install -r requirements.txt -r requirements-dev.txt
playwright install msedge

cp skills/ref-downloader/config.example.toml skills/ref-downloader/config.local.toml
# 编辑 config.local.toml，至少改 [crossref].mailto

# 跑离线测试
python -m pytest tests/ -v

# 直接调脚本
python skills/ref-downloader/scripts/run_ref_downloader.py 10.1021/jacs.5c05017
```

## 使用示例

（安装后——路径假设 skill 安装在 `<SKILL_DIR>`，如 `~/.claude/skills/ref-downloader/`。源码状态下，`<SKILL_DIR>` = `skills/ref-downloader/`。）

### 输入：一个 DOI

```powershell
python <SKILL_DIR>/scripts/run_ref_downloader.py 10.1021/jacs.5c05017
```

默认输出到 `<cwd>/jacs.5c05017_refs/jacs.5c05017/`

### 输入：本地 PDF（metadata 中含 DOI 或 PDF 文本中可识别 DOI）

```powershell
python <SKILL_DIR>/scripts/run_ref_downloader.py "C:\path\to\your_paper.pdf"
```

默认输出到 `<pdf_dir>/your_paper_refs/<根据 DOI 派生的目录名>/`

### 自定义输出目录

```powershell
python <SKILL_DIR>/scripts/run_ref_downloader.py 10.1021/jacs.5c05017 --output-dir refs/
```

### 非交互模式（CI / 批处理）

```powershell
python <SKILL_DIR>/scripts/run_ref_downloader.py 10.1021/jacs.5c05017 --yes --auto
```

### 使用备选配置文件

```powershell
python <SKILL_DIR>/scripts/run_ref_downloader.py 10.1021/jacs.5c05017 --config ./alt.toml
```

## 配置

所有配置写在 `config.local.toml`（gitignored）。从 `config.example.toml` 拷贝出来后编辑。

| 段 | 字段 | 用途 |
|---|---|---|
| `[crossref]` | `mailto` | 你的邮箱 — 进入 Crossref polite pool 用 |
| `[zotero]` | `db_path` | 可选：`zotero.sqlite` 路径，用于从 PDF 文件名查 DOI |
| `[browser]` | `edge_profile_dir` | Edge profile 目录；空 = OS 默认 |
| `[browser]` | `disable_extensions` | 设 `true` 启动时加 `--disable-extensions` |
| `[institution]` | `auth_hosts` | 表示"被弹到 SSO"的主机名（例如 `["sso.your-uni.edu"]`） |
| `[institution]` | `auth_url_fragments` | 表示 SSO 的 URL 片段（如 `["oauth", "saml"]`） |
| `[institution]` | `auth_page_titles` | SSO 页面 `<title>` 文本（用于检测 HTML 当 PDF 返回的情况） |
| `[institution]` | `auth_loading_titles` | 加载页 title（同时被 AIP/AVS 出版商加载页检测复用） |
| `[institution]` | `ignored_access_dois` | 已知机构无法访问的 DOI 列表，跳过不重试 |

环境变量优先级高于文件：

| 变量 | 映射 |
|---|---|
| `REF_DOWNLOADER_MAILTO` | `crossref.mailto` |
| `REF_DOWNLOADER_ZOTERO_DB` | `zotero.db_path` |
| `REF_DOWNLOADER_EDGE_PROFILE` | `browser.edge_profile_dir` |
| `REF_DOWNLOADER_DISABLE_EXTENSIONS` | `browser.disable_extensions`（`1`/`true` 启用） |
| `REF_DOWNLOADER_CONFIG` | 备选 TOML 路径 |

完整文档参考 [`skills/ref-downloader/config.example.toml`](skills/ref-downloader/config.example.toml)。

### 备用后端：CloakBrowser（可选，应对 Cloudflare 类站点）

**它是什么。** [CloakBrowser](https://github.com/CloakHQ/CloakBrowser) 是 CloakHQ 维护的第三方 Python 包（MIT 协议，[PyPI 上有](https://pypi.org/project/cloakbrowser/)，包名 `cloakbrowser`）。它打包了一个魔改过的 Chromium，从源码层面改了一系列指纹特征，让常见的反爬检测（Cloudflare Turnstile、Radware、DataDome、FingerprintJS 等）看不出是自动化浏览器。它的 `launch_persistent_context_async()` API 故意跟 Playwright 兼容 —— 所以 ref-downloader 切后端只需要一个环境变量，不用改下载流程。

**不是 ref-downloader 的依赖。** 不 `pip install cloakbrowser` 它就不会被 import，默认的 Edge 路径完全不受影响。启用后 ref-downloader 用 cloakbrowser 自带的 Chromium，并使用**独立的 profile 目录**（默认 `~/.local/cloakbrowser/profiles/ref-downloader`，或用 `REF_DOWNLOADER_CLOAK_PROFILE` 覆盖），不会动你的 Edge profile —— Edge **不需要**关闭。

**什么时候用。** 适合的场景：CCS Chemistry（`10.31635`，Cloudflare 保护）、部分被 Radware 拦的 Elsevier 路径、Edge 后端持续出 `manual_pending (radware_bot_manager)` 或 `failed (challenge_timeout)` 的页面。**不要**当默认 —— 如果瓶颈是机构访问权，Edge 后端反而更可靠（它带你登录过的 cookie）。

**注意事项。** CloakBrowser 当前是 **beta** 第三方软件，是否安装请自己判断（建议先看一眼 [repo](https://github.com/CloakHQ/CloakBrowser)）。它**不是验证码自动求解器**，交互式验证还是要人。另外它的 profile 独立 ≠ 没有你机构 cookie，所以适合开放的 Cloudflare 站，对"付费但你机构有权限"的文章帮助有限。

```powershell
pip install cloakbrowser                              # 一次性，跟 ref-downloader 无关
$env:REF_DOWNLOADER_BROWSER = "cloak"
$env:REF_DOWNLOADER_CLOAK_HUMAN_PRESET = "careful"    # 可选：更慢的鼠标/滚动节奏
python skills/ref-downloader/scripts/run_ref_downloader.py 10.31635/ccsorg...
```

CloakBrowser 环境变量（都是可选）：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `REF_DOWNLOADER_BROWSER` | `edge` | 设为 `cloak`（或 `cloakbrowser`）切换后端 |
| `REF_DOWNLOADER_CLOAK_PROFILE` | `~/.local/cloakbrowser/profiles/ref-downloader` | 持久化 Chromium profile 路径 |
| `REF_DOWNLOADER_CLOAK_HUMANIZE` | `1` | 设 `0`/`false` 禁用人化输入 |
| `REF_DOWNLOADER_CLOAK_HUMAN_PRESET` | `default` | `default` 或 `careful`（更慢） |
| `REF_DOWNLOADER_CLOAK_PROXY` | 未设 | HTTP/SOCKS 代理 URL |
| `REF_DOWNLOADER_CLOAK_GEOIP` | 自动 | `1` 强制 GeoIP 重路由（设了 proxy 时默认开启） |
| `CLOAKBROWSER_PYTHONPATH` | 未设 | 给本地 cloakbrowser 源码 checkout 的 sys.path 提示 |

注意：
- 用 cloak 后端时 **Edge 不需要关**，cloakbrowser 用自己独立的 Chromium。
- 新 cloak profile 首次访问目标站仍可能停在 Cloudflare/安全验证页 —— 用同一 profile 手动开一次目标站完成验证，再批量下。
- `human_preset=careful` 降低行为检测概率，但 **不是** 验证码自动求解器。
- cloakbrowser **不是** ref-downloader 的硬依赖。只要不设 `REF_DOWNLOADER_BROWSER=cloak`，就不会被 import。

## 架构

三阶段流水线 + 一个 wrapper：

```
skills/ref-downloader/
├── SKILL.md                            agent 入口（slim runbook）
├── references/agent-runbook.md         扩展的手动流程 + DOI fallback
├── config.example.toml                 配置模板（拷贝为 config.local.toml）
└── scripts/
    ├── run_ref_downloader.py           入口：加载配置、解析 DOI、串行调度
    │     └─> extract_refs.py    (1) Crossref API：抓取主论文的参考文献列表
    │     └─> validate_refs.py   (2) Crossref API：逐条 metadata + 出版商分类
    │     └─> download_refs.py   (3) Playwright/Edge：按出版商策略下主文 PDF + SI
    └── _config.py                      TOML + 环境变量加载器
```

也可以单独运行三个脚本调试或局部重跑。手动流程见 [`skills/ref-downloader/references/agent-runbook.md`](skills/ref-downloader/references/agent-runbook.md)。

Agent 用户可从 [`skills/ref-downloader/SKILL.md`](skills/ref-downloader/SKILL.md) 安装或查看 skill 包。仓库根目录保持为面向人的 Python 项目；skill 包单独放置，避免 Codex 把 README、changelog、测试和源码都当成 skill 的关联上下文。

## 已支持出版商

ACS、Nature、Science、Elsevier、Wiley、RSC、Springer、PNAS、ECS、IOP、AIP、AVS、IEEE、OSA、KPS、Beilstein、APS、Annual Reviews、Taylor & Francis、CCS Chemistry。成熟度因出版商而异，详细分级表与已知问题见 [`docs/SUPPORTED_PUBLISHERS.md`](docs/SUPPORTED_PUBLISHERS.md)。CCS Chemistry 站点在 Cloudflare 后面，配合 `REF_DOWNLOADER_BROWSER=cloak` 使用最稳。

## 已知限制

- **仅在 Windows + Edge 验证过**：macOS / Linux / Chromium 未测试。如果你尝试了，欢迎在 issues 里反馈结果。
- **必须 headed 模式**：实测 `headless=True` 时 Wiley / ACS 的 SI 下载会返空结果。默认 headed。
- **运行前 Edge 必须完全关闭**：Playwright 需独占持久 profile。任务管理器里 `msedge.exe` 后台进程也要 kill。
- **SSO 跳转能识别但不会自动登录**：撞到学校 SSO 时该篇标 `manual_pending`，需要你交互登录。配置 `[institution]` 段告诉脚本你学校的 SSO 特征。
- **SI 下载是最脆弱的路径**：主文 PDF 比较稳；SI 路径每个出版商不一样，是最容易因出版商页面更新而需要调整的地方。
- **付费内容需要机构访问权**：本工具不绕过付费墙。
- **依赖 Crossref 的 reference 数据**：如果某出版商没有把参考列表存进 Crossref，工具无法自动处理。

## 贡献

参见 [CONTRIBUTING.md](CONTRIBUTING.md)，包含：
- 添加新出版商（DOI 前缀 → 下载策略）
- 添加机构 SSO 配置
- 报 bug 时附上有用的日志

## 安全

工具会启动你的真实 Edge profile，含所有 cookie 和已登录会话。在用日常浏览的 profile 跑之前请阅读 [SECURITY.md](SECURITY.md)。

## License

MIT — 见 [LICENSE](LICENSE)。
