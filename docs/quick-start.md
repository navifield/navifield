# 快速开始

[English](quick-start.en.md) · **简体中文**

在 Codex 中打开一个新项目目录，在 WorkBuddy 中选择本地工作区，或在项目目录启动 DeepSeek Harness。把[中文首页](../README.zh-CN.md)的提示词连同论文 DOI 或问题发给它，显式读取 [AGENT.md](../AGENT.md)。助手需要能读仓库、在工作区写文件并运行本地命令。无法执行命令时，可手动执行下文的查询，再将输出带回会话。

助手先用 `python -m pip show navifield`、`navifield capabilities --help` 检查版本和查询子命令。环境可用就复用；需要准备环境时，按下节安装公开发行版并记录解释器路径。已有 DOI 就直接查询；普通问题由助手把原话写入 JSON。首答给出命中的论文或模型、依据、推断和影响目标量的缺项。再次提问沿用同一工作区与解释器，变更工况时分开保存输入与结果。

## 安装公开版

从 [v0.2.0 发行页](https://github.com/navifield/navifield/releases/tag/v0.2.0) 下载 `navifield-0.2.0-source.zip` 并解压。在包含 `pyproject.toml` 的目录打开 Windows 命令提示符（cmd.exe）。需要 64 位 Python 3.10 或更高版本。下文命令均从该目录执行。

```bat
py -3 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install .
```

## 只查询时也可以安装 wheel

本地构建也可能显示相同版本号。要确认结果来自正式公开版，在发行页取得 `SHA256SUMS.txt`，核对 wheel 或源码 ZIP 的 SHA-256。例如在 PowerShell 执行 `Get-FileHash -Algorithm SHA256 .\navifield-0.2.0-py3-none-any.whl`。已有安装来源无法确认时，用校验过的发行文件建新的项目环境，重查同一条记录。

已有项目环境，可用 `python -m pip show navifield` 和 `navifield capabilities --help` 核对版本和查询命令。首次安装使用 Python 3.10 或以上版本。在工作区打开 Windows 命令提示符（cmd.exe），以下命令使用 GitHub 公开的 v0.2.0 wheel。

```bat
py -3 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install "https://github.com/navifield/navifield/releases/download/v0.2.0/navifield-0.2.0-py3-none-any.whl"
python -m pip show navifield
```

macOS 或 Linux 使用 `python3 -m venv .venv` 和 `. .venv/bin/activate`，再执行相同的 pip 命令。如果直链下载失败，可在 [GitHub 发行页](https://github.com/navifield/navifield/releases/tag/v0.2.0) 下载 wheel，再用 pip 安装本地文件。同页的 `SHA256SUMS.txt` 可供核对。若从其他渠道安装，也请核实版本和命令是否齐全。

## 按目标量或论文查询

安装后即可离线查询。譬如，你要预测壁温，先查看候选模型适用的工况、需要哪些边界条件和物性、耦合时交换什么量、怎样校核结果。

```bat
navifield capabilities target-inventory --target wall_temperature
```

如果目标是压降，可以先按目标量列出库中的论文，再按返回的 DOI 打开一条记录。下面的 DOI 来自该查询。阅读时分别看论文的观测量、模型和复算范围，区分论文作者的计算、实验测量和本项目已有的验证。

```bat
navifield capabilities paper-cases --target pressure_drop
navifield capabilities paper-cases --doi 10.3390/mi12121580
```

手头已有工况时，可以试用随包的结构化请求，查看某个模型或求解链的候选、依据和待补输入。

```bat
navifield capabilities select-models examples\cli-v01\model-selection\heated-bracket.json --brief
navifield capabilities select-chains examples\cli-v01\device-chains\evaporator-net.json --brief
```

也可以把自己的需求保存为 UTF-8 编码的 `request.json`，例如 `{"original_request":"已知进出口截面和流量，想求两截面间的压降。"}`，执行 `navifield route request.json --brief`。这句简短描述在 v0.2.0 中返回 `needs_review / no_match`。此时可直接执行 `navifield capabilities target-inventory --target pressure_drop`，按已知目标查找候选模型。选定路线前，核对软件版本、物性、边界条件和结果读回位置。论文记录的来源和可核验范围见[公开证据说明](../navifield/capability_data/PUBLIC_EVIDENCE_BOUNDARY.txt)。

## 运行一个公开案例

打开[公开案例目录](../examples/cli-v01/README.md)，按想计算的量选择案例。每页都列出所需求解器、建模和运行命令、读回量与比较方法。按[第三方程序与官方下载地址](../THIRD_PARTY_NOTICES.md)安装该例使用的求解器及配套库，再回到源码根目录运行案例。基础安装可生成运行记录和文字 HTML 报告，绘图可选装 `python -m pip install ".[plots]"`。

运行后可用项目路径和运行号查看日志、输入副本和终态。[运行记录与监控](local-runs.md)

## 查完之后怎么继续

请助手先指出能执行的案例脚本，核对几何、物理假设、边界条件、目标量读法及参考值。举例来说，圆翅片侧壁换热系数可以改，绝热端部改为对流则要调整建模、端部散热读回和热平衡校核。冷板选型可给出流固换热候选，实际最高壁温还需要工况、网格、求解和芯片区域的温度读回。

- **条件齐备就算。** 确认模型适用、参数与单位齐全、本机求解器可运行，选公开模板或你已有的兼容输入实际求解。请助手从原生文件读出目标量，给出数值、单位、文件位置、运行号和参考对照，并点明尚缺哪项验证。已有可用结果时可直接读回。
- **少一个参数就先问它。** 例如梁的长度、材料和截面均已给出，只有端面总载荷未知。助手应问载荷是多少牛顿，说明它决定挠度；补齐后继续原工况求解。如果缺的是本机可执行文件或 DLL，先按所选案例配置。首轮查无结果或部分命中时，先按 DOI、目标量或具体工况复查。
- **需要新开发再起草需求。** 说明现有工具能完成的步骤、缺少的模型或接口，以及受影响的目标量。用可公开的信息拟好标题和正文，写明场景或论文、目标量、试过的路线、具体缺口和已有参考。展示草稿后询问：“你愿意把这份需求作为公开 GitHub Issue 提交给 NaviField 维护者吗？”明确同意公开该稿后，客户端可代发一次并返回真实链接，或由用户将草稿贴进[提交论文或仿真需求](https://github.com/navifield/navifield/issues/new?template=simulation-request.yml)。拒绝或未答复时草稿留在本会话。已有功能可稳定复现的故障走[反馈 Bug](https://github.com/navifield/navifield/issues/new?template=bug-report.yml)。

## 客户端实测

以下为 Windows 上公开 v0.2.0 首用路径的观察。表中只列相应客户端自身执行的动作。

| 客户端 | 怎样开始 | 实测结果 |
| --- | --- | --- |
| Codex CLI 0.171.0-alpha.13 | 打开项目工作区，粘贴首页提示词。 | 在空工作区中，Codex 调用已有的 0.2.0 环境按 DOI 查到论文。那次安装的 wheel 哈希与正式发布文件不同。在另一新工作区，它安装了哈希匹配的正式 wheel，核对绝热端翅片，并只追问缺少的侧壁换热系数。 |
| DeepSeek Harness 0.2.2-rc.1 | 配好模型服务，在项目目录中启动会话，再发送提示词。 | 原生客户端已接收提问，模型服务在首次答复和工具调用前返回 `MISSING_CREDENTIAL`。认证后的首答及环境复用未测。 |
| WorkBuddy | 登录客户端、选择本地工作区，粘贴提示词。 | 本轮未测。 |
