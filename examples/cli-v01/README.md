# 公开案例

按要计算的物理量选择一例。以下四例均提供生成输入、运行本机求解器和读回结果的步骤。首次使用可先[安装公开版](../../docs/quick-start.md)。

| 想计算的量 | 本机求解器 | 读回后怎样检查 |
| --- | --- | --- |
| [悬臂梁端面挠度](beam/README.md) | CalculiX | 端面平均挠度与 Euler–Bernoulli 梁解析解比较 |
| [方腔中心线速度](cavity/README.md) | OpenFOAM | 两条中心线的速度与 Ghia 等人的数值基准比较 |
| [圆管压降](pipe/README.md) | OpenFOAM + Microsoft MPI | 两个截面之间的压降与 Blasius 关系估算值比较 |
| [黄铜翅片温度](brass-fin/README.md) | CalculiX | 八个测站的表面温度与论文模拟和热电偶测量比较，并检查热收支 |

## 用自己的工况查询

[模型选型](model-selection/)、[湍流模型选型](turbulence-selection/)和[设备求解链](device-chains/)目录提供查询用的 JSON 请求。可以修改其中的目标和已知输入，再按[查询说明](../../docs/quick-start.md#按目标量或论文查询)检索候选路线。
