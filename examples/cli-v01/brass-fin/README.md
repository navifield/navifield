# 黄铜翅片的稳态温度

复算 Çakmak 与 Taylan 的黑漆黄铜圆棒。CalculiX 2.22 求解三维稳态导热，NaviField 生成 DC3D8 输入、运行求解器并从原生节点温度和热反力读取结果。输入来自论文的名义工况。表 1 的作者 COMSOL 温度与热电偶测量分别用于算后比较。[论文及表 1](https://doi.org/10.17482/uumfd.446304)

## 从发行源码运行

下载本版本的源码压缩包并解压，在源码根目录执行下列 Windows 命令。需要 Python 3.10 及以上和可独立运行的 CalculiX 2.22。将示例中的求解器路径替换为本机位置，并保留发行方配套的 DLL。[公开求解器获取方式](../../../THIRD_PARTY_NOTICES.md)

```bat
py -3 -m venv .venv
.venv\Scripts\python -m pip install ".[fin]"
.venv\Scripts\python examples\cli-v01\brass-fin\run.py --ccx "C:\solver\bin\ccx.exe" --project work\brass-fin-01
```

运行目录须为新目录。成功后，`comparison.json` 含八点温度、与三组参考数据的差异及最大复算误差；`readback.json` 含截面读数和热收支。项目中还保留输入文件、原生 DAT 和作业记录。示例先生成网格并独立求解，再读取参考温度。复算判据为相对先前同网格 CalculiX 2.22 结果逐点绝对差均不超过 0.001 K。

## 更改工况或单独调用

`model.json` 给出长度 0.327 m、半径 0.005 m、导热率 110 W/(m·K)、基温 57.4 °C、环境温度 20.5 °C、侧壁综合换热系数 15.7 W/(m²·K)。论文给出的综合系数已含自然对流和辐射；模型采用一次侧壁膜边界，末端绝热。截面每方向 8 等分；8 个轴向内部位置把长度分成 9 段，每段 4 等分，共 2304 个单元。

```bat
navifield fin build examples\cli-v01\brass-fin\model.json --deck model.inp
ccx -i model
navifield fin read --deck model.inp --dat model.dat --positions-m 0.045 0.075
navifield capabilities paper-cases --doi 10.17482/uumfd.446304
```

读取位置从基端沿轴向计量，单位为 m。每个测站先取各条外边两端节点的平均温度，再按该边长度加权。求解输入和原生 DAT 分别保留 SHA-256，示例同时检查各测点复算误差及热收支。细网基端供热约为 2.366 W。定温节点热反力汇总加上基端共享节点分得的侧膜散热，可与整个侧壁的散热积分比较。该热量属于模型预测。论文报告了八个可用温度测点，ST1 和 ST9 因故障缺测。历史细网与作者 COMSOL 的平均绝对差约 0.042 K，与实验的平均绝对差约 1.862 K。测点深度和整体验收阈值未披露。
