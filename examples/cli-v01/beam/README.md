# 悬臂梁的端面平均挠度

一根长 1000 mm、截面为 100 mm × 100 mm 的钢梁，一端固定，另一端均布总计 1000 N 的向下载荷。用 CalculiX 求出自由端的平均挠度，再与 Euler–Bernoulli 梁的解析解比较。

## 配置环境

在 Windows 命令提示符（cmd.exe）中完成基础安装并激活虚拟环境，从仓库或解压包根目录执行以下命令。将占位符换成本机实际路径。公开示例使用 CalculiX 2.22，所选目录须包含 ccx.exe 及其配套 DLL。[快速开始](../../../docs/quick-start.md) · [求解器下载与版本](../../../THIRD_PARTY_NOTICES.md)

```bat
set "NAVIFIELD_CCX_BIN=<CalculiX 的 bin 目录>"
call configure-toolchain.cmd
```

## 生成输入、求解并读回

在新案例目录生成 40 × 6 × 6 个 C3D8I 单元。材料的弹性模量为 210000 MPa，泊松比为 0.3。运行 ccx 后，检查脚本从 beam.dat 读取自由端节点的竖向位移，按节点分担的端面面积求平均，向下为正。[输入生成脚本](make_case.py) · [结果检查脚本](check.py)

```bat
"%NAVIFIELD_PYTHON%" examples\cli-v01\beam\make_case.py work\beam\case
navifield.cmd run --project work\beam --cwd case --cores 8 --wall-seconds 870 -- ccx -i beam
"%NAVIFIELD_PYTHON%" examples\cli-v01\beam\check.py work\beam\case
navifield.cmd show --project work\beam --rid r001
```

## 如何核对

解析挠度为 FL³/(3EI)，本例为 0.19047619 mm。检查脚本以 2% 为相对误差限值。下面是原公开示例的一次终端输出，查看自己的记录时请使用本次返回的运行号。

```text
work\beam\case
r001
tip_mean_down_mm=0.190122717; euler_bernoulli_mm=0.19047619; relative_error=0.00185574; limit=0.02
{"run_id": "r001", "state": "completed", "exit_code": 0, "cores": 8, "wall_seconds": 0.779568, "details": ".navifield/records/r001.json"}
```

本次有限元结果相对解析值的差异约为 0.186%。若改成较短的梁，需重新评估剪切变形的影响。
