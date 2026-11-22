# 快速开始

第一次使用时，可以先在本机查一个目标量，再决定是否安装求解器。要运行数值案例，请使用带有案例脚本的发行源码包。

## 安装公开版

从 [v0.2.0 发行页](https://github.com/navifield/navifield/releases/tag/v0.2.0) 下载 `navifield-0.2.0-source.zip` 并解压。在包含 `pyproject.toml` 的目录打开 Windows 命令提示符（cmd.exe）。需要 64 位 Python 3.10 或更高版本。下文命令均从该目录执行。

```bat
py -3 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install .
```

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

也可以把自己的需求保存为 UTF-8 编码的 `request.json`，例如 `{"original_request":"已知进出口截面和流量，想求两截面间的压降。"}`，执行 `navifield route request.json --brief`。首次查询可能只有部分候选或没有匹配。选定路线前，核对软件版本、物性、边界条件和结果读回位置。论文记录的来源和可核验范围见[公开证据说明](../navifield/capability_data/PUBLIC_EVIDENCE_BOUNDARY.txt)。

## 运行一个公开案例

打开[公开案例目录](../examples/cli-v01/README.md)，按想计算的量选择案例。每页都列出所需求解器、建模和运行命令、读回量与比较方法。按[第三方程序与官方下载地址](../THIRD_PARTY_NOTICES.md)安装该例使用的求解器及配套库，再回到源码根目录运行案例。基础安装可生成运行记录和文字 HTML 报告，绘图可选装 `python -m pip install ".[plots]"`。

运行后可用项目路径和运行号查看日志、输入副本和终态。[运行记录与监控](local-runs.md)
