# 不可压稳态湍流模板

本模板是 OpenFOAM `simpleFoam` 稳态不可压流动的起点，使用牛顿流体和 k-ω SST 模型。`0/`、`constant/` 和 `system/` 包含可按工况修改的初始场、物性和求解设置。使用其他几何时，应根据实际网格检查所有边界名称、入口速度、运动黏度、湍流入口量和壁面条件，再开始求解。

## 随包圆管示例

在安装包目录运行下面的脚本会创建一个新案例目录；目录不能预先存在：

```text
python examples/cli-v01/pipe/make_case.py my-pipe/case
```

该脚本从已安装的 `navifield` 模板资源读取输入；在源码仓库中运行时使用仓库内同一模板。示例是直径 0.1 m、长 10 m 的光滑圆管，以 5° 扇区模拟轴对称流动；网格为 2000 × 24 × 1，共 48000 单元，分解为 8 个区域。入口速度 0.1 m/s、运动黏度 1e−6 m²/s，对应 Re≈10000；密度按 1000 kg/m³ 用于压降换算。生成后依次执行 `blockMesh`、`checkMesh`、`decomposePar`、8 进程 `simpleFoam -parallel` 和 `reconstructPar -latestTime`；求解器须另行配置。使用 `python examples/cli-v01/pipe/check.py my-pipe/case` 检查已写出的监测表。

## 一次公开算例结果

在 Windows、OpenFOAM v2606 下，上述 48000 单元例子以 8 进程计算至 3000 次迭代，求解器正常退出；本次求解墙钟约 183.5 秒。取第 2775–3000 次迭代的 10 个共同样本，x=7–9 m 的压降为 **3.0896 Pa**，Blasius 参考值为 **3.1600 Pa**，有符号偏差 **−2.23%**。实测最大壁面 y+ 约 1.607，进出口流量相对不平衡约 1.84e−10。

求解器的 `p` 是运动压力；示例检查脚本将压力差乘以密度后换算为 Pa，并报告流量平衡、压力窗口与上游段的差异。Blasius 关系使用 Darcy 摩擦系数，适用于光滑、充分发展的湍流圆管，参考范围为 4000 ≤ Re ≤ 100000，见 [IAEA Sodium Coolant Handbook](https://www-pub.iaea.org/MTCD/publications/PDF/CRCP_SOD_003web.pdf)。这是一个工况和单级网格的结果，不能代替网格收敛研究；实际问题仍应检查网格、壁面 y+、守恒和流动是否充分发展。
