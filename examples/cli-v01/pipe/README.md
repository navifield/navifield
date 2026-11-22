# 光滑直管的沿程压降

入口速度为 0.1 m/s、管径为 0.1 m、运动黏度为 10⁻⁶ m²/s 时，名义 Re 为 10000。用 OpenFOAM 的 k–ω SST 模型在 48000 个单元的楔形网格上求解。在距入口 7 m 和 9 m 的截面上，读取距管轴 0.025 m 的两个探针之间的压降，和光滑充分发展圆管的 Blasius 关联式比较。[IAEA 手册](https://www-pub.iaea.org/MTCD/publications/PDF/CRCP_SOD_003web.pdf)

## 配置环境

在 Windows 命令提示符（cmd.exe）中完成基础安装并激活虚拟环境。八进程算例需要 Windows MinGW 版 OpenFOAM 和与其匹配的 Microsoft MPI。打开仓库或解压包根目录，将占位符换成本机的实际目录。[快速开始](../../../docs/quick-start.md) · [求解器与 MPI 下载](../../../THIRD_PARTY_NOTICES.md)

```bat
set "NAVIFIELD_FOAM_ROOT=<含 etc\controlDict 的 OpenFOAM 项目目录>"
set "NAVIFIELD_MPI_BIN=<含 mpiexec.exe 的 Microsoft MPI Bin 目录>"
```

配置脚本默认从 OpenFOAM 项目目录下的 platforms\win64MingwDPInt32Opt\bin 取程序，若可执行文件位于别处，先设置 NAVIFIELD_FOAM_BIN。若发行包默认使用串行 dummy 通信库，且提供 libPstream.dll-msmpi，指定以下新目录。若已配置为 MS-MPI，可跳过这条 set 命令。

```bat
set "NAVIFIELD_PARALLEL_BIN=%CD%\work\openfoam-msmpi-bin"
```

需要显式指定通信 DLL 时，先将 NAVIFIELD_MPI_DLL 设为与启动器匹配的 msmpi.dll 完整路径。随后配置当前命令提示符。首次配置会将本地可执行目录复制到指定的并行目录，在副本中启用发行包提供的 MS-MPI 变体。后续运行复用该副本，更换 OpenFOAM 或 MPI 版本时使用新的并行目录。

```bat
call configure-toolchain.cmd
```

## 分区、八进程求解和重构

给生成器指定一个新案例目录，按顺序运行。下面的 r004 指本例新项目内的并行求解记录，查看报告时使用本次并行求解返回的运行号。[输入生成脚本](make_case.py) · [结果检查脚本](check.py)

```bat
"%NAVIFIELD_PYTHON%" examples\cli-v01\pipe\make_case.py work\pipe\case
navifield.cmd run --project work\pipe --cwd case --wall-seconds 20 -- blockMesh
navifield.cmd run --project work\pipe --cwd case --wall-seconds 20 -- checkMesh -allTopology -allGeometry
navifield.cmd run --project work\pipe --cwd case --wall-seconds 20 -- decomposePar
navifield.cmd run --project work\pipe --cwd case --cores 8 --wall-seconds 800 -- mpiexec -n 8 simpleFoam -parallel
navifield.cmd run --project work\pipe --cwd case --wall-seconds 20 -- reconstructPar -latestTime
"%NAVIFIELD_PYTHON%" examples\cli-v01\pipe\check.py work\pipe\case
navifield.cmd report --project work\pipe --rid r004
```

要查看残差和监测曲线，安装快速开始中的绘图依赖组，再执行以下命令。

```bat
navifield.cmd report --project work\pipe --rid r004 --plots
```

## 如何核对

检查脚本从最后 10 个监测时刻的入口体积流量和实际入口面积算出平均速度，再计算 Re。它以密度 1000 kg/m³ 将 OpenFOAM 的运动压力差换算成 Pa，并与 Darcy–Weisbach 压降比较。脚本采用 Darcy 摩擦系数 `f = 0.316 × Re^(-0.25)`，要求 4000 ≤ Re ≤ 100000，本例压降的相对误差限值为 5%。

公开模板记录的这次求解压降为 3.0896 Pa，经验关联式给出 3.16 Pa。复算时还要核对末段的速度剖面与压力梯度、进出口流量平衡、网格质量和 7–9 m 段的壁面 y+。单套网格给出的压降差异仅适用于这次对照。[公开模板中的结果记录](../../../navifield/templates/t3-incompressible-steady/template.json)
