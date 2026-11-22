# 使用指南

[English](quick-start.en.md) · **简体中文**

## 把问题交给自己的助手

在 Codex、WorkBuddy 或 DeepSeek Harness 中打开一个项目工作区。复制[首页提示词](../README.zh-CN.md)，将占位文字换成论文链接或你关心的物理量。三端共用仓库的 [AGENT.md](../AGENT.md)，需要选择能读取公开仓库和执行工作区命令的模式。若只有对话权限，可按下文手动查询。

首答应说明目标量、命中的记录与来源、这条路线验证到哪一步，以及还缺什么会改变选型的条件。下一次在同一工作区直接提出新问题即可复用环境。决定计算后，再按案例准备相应求解器。

## 安装公开版

已有项目环境，可用 `python -m pip show navifield` 和 `navifield capabilities --help` 核对版本和查询命令。首次安装使用 Python 3.10 或以上版本。在工作区打开 Windows 命令提示符（cmd.exe），以下命令使用 GitHub 公开的 v0.2.0 wheel。

```bat
py -3 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install "https://github.com/navifield/navifield/releases/download/v0.2.0/navifield-0.2.0-py3-none-any.whl"
python -m pip show navifield
```

macOS 或 Linux 使用 `python3 -m venv .venv` 和 `. .venv/bin/activate`，再执行相同的 pip 命令。如果直链下载失败，可在 [GitHub 发行页](https://github.com/navifield/navifield/releases/tag/v0.2.0) 下载 wheel，再用 pip 安装本地文件。同页的 `SHA256SUMS.txt` 可供核对。若从其他渠道安装，也请核实版本和命令是否齐全。

## 按目标量或论文查询

有 DOI 时先查论文。例如，下面的记录涉及矩形通道压降，可以核对标识、目标量、论文出处以及已做过的复算。

```text
navifield capabilities paper-cases --doi 10.3390/mi12121580
```

对于新问题，助手可把原话存为 UTF-8 编码的 `request.json`，例如 `{"original_request":"已知进出口截面和流量，想求两截面间的压降。"}`，执行第一条命令。这句话在 v0.2.0 返回 `needs_review / no_match`。可改按已知目标查表，例如执行第二条查壁温模型，或执行第三条找压降论文。

```text
navifield route request.json --brief
navifield capabilities target-inventory --target wall_temperature
navifield capabilities paper-cases --target pressure_drop
```

`navifield capabilities target-schema` 列出支持的目标名称。用 `target-inventory --target pressure_drop` 可继续查压降候选模型的适用条件、必需输入、交换量和校核方法。阅读论文记录时，分别看作者模拟、实验测量和开源复算的范围。[公开证据说明](../navifield/capability_data/PUBLIC_EVIDENCE_BOUNDARY.txt)

## 查到路线之后

- **条件齐全。** 选择已有模板，也可以提供与本机求解器兼容的输入文件。让助手实际运行，从原生输出读回目标量和单位，再与参考值对照。
- **缺少关键输入。** 例如，同一工况和压力基准下求两端静压差，已给上游读数，就只需补下游静压。
- **需要开发。** 请助手指出缺少哪个模型、耦合接口或读回方法，怎样影响目标量，并列出可供验证的依据。助手可起草公开标题和正文，交你确认。你明确同意后，支持代发的客户端可提交并返回链接，你也可以自行填写[需求表单](https://github.com/navifield/navifield/issues/new?template=simulation-request.yml)。拒绝或未答复时，草稿留在当前对话。

## 运行一个公开案例

在[公开案例目录](../examples/cli-v01/README.md)按目标量选例。从同一[发行页](https://github.com/navifield/navifield/releases/tag/v0.2.0)下载并解压 `navifield-0.2.0-source.zip`，取得脚本与输入。回到含 `pyproject.toml` 的源码根目录，按该例说明配置求解器、运行并读回结果。基础安装可生成文字 HTML 报告，绘图可在源码根目录选装 `python -m pip install ".[plots]"`。[求解器下载](../THIRD_PARTY_NOTICES.md) · [运行记录与监控](local-runs.md)

源码包也含已填写的选型请求，可用于对照不同工况的模型或求解链。

```bat
navifield capabilities select-models examples\cli-v01\model-selection\heated-bracket.json --brief
navifield capabilities select-chains examples\cli-v01\device-chains\evaporator-net.json --brief
```
