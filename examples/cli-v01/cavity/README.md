# 顶盖驱动方腔的中心线速度

用 OpenFOAM 的 simpleFoam 求二维稳态方腔流动，并读出两条中心线上的速度。方腔边长 1 m，顶盖以 1 m/s 水平向右移动，其余三面静止，运动黏度为 0.001 m²/s。按顶盖速度和边长计算，Re = 1000。对照数据取自 Ghia、Ghia 与 Shin 的数值基准。[原论文](https://doi.org/10.1016/0021-9991%2882%2990058-4)

## 配置环境

在 Windows 命令提示符（cmd.exe）中完成基础安装并激活虚拟环境。示例使用 Windows MinGW 版 OpenFOAM，从仓库或解压包根目录执行下列命令。把占位符换成含有 etc\controlDict 的 OpenFOAM 项目目录。配置脚本默认从该目录下的 platforms\win64MingwDPInt32Opt\bin 查找求解器。如果发行包另设可执行目录，在调用脚本前将 NAVIFIELD_FOAM_BIN 设为其完整路径。[快速开始](../../../docs/quick-start.md) · [求解器下载与版本](../../../THIRD_PARTY_NOTICES.md)

```bat
set "NAVIFIELD_FOAM_ROOT=<OpenFOAM 项目根目录>"
call configure-toolchain.cmd
```

## 顺序运行

给生成器指定一个新案例目录，依次生成 80 × 80 × 1 网格、检查网格、串行求解，再在最新输出场上执行后处理。下面的 r003 指本例新项目内的求解记录，查看报告时使用本次求解返回的运行号。[输入生成脚本](make_case.py) · [结果检查脚本](check.py)

```bat
"%NAVIFIELD_PYTHON%" examples\cli-v01\cavity\make_case.py work\cavity\case
navifield.cmd run --project work\cavity --cwd case --wall-seconds 30 -- blockMesh
navifield.cmd run --project work\cavity --cwd case --wall-seconds 30 -- checkMesh -allTopology -allGeometry
navifield.cmd run --project work\cavity --cwd case --wall-seconds 780 -- simpleFoam
navifield.cmd run --project work\cavity --cwd case --wall-seconds 30 -- simpleFoam -postProcess -latestTime
"%NAVIFIELD_PYTHON%" examples\cli-v01\cavity\check.py work\cavity\case
navifield.cmd report --project work\cavity --rid r003
```

## 如何核对

竖向中心线取 15 个内点的水平速度，水平中心线取 15 个内点的竖向速度，均按顶盖速度归一化。检查脚本读取最新探针表，与文献逐点比较。通过条件为这 30 个值的最大绝对差不超过 0.05，且输出场中顶盖与固定壁的速度边界设置正确。该对照适用于本例 Re 和边界条件，评估网格敏感性需另行加密网格。
