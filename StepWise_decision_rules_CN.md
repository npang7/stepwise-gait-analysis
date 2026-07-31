# StepWise 数据分析与判断规则说明（中文版）

## 1. 项目定位

StepWise 是一个基于四点足底压力和足部 IMU 的 **非诊断性步态筛查系统**。系统输出的是：

- 前掌/后跟着地倾向；
- 足底内外侧压力偏载；
- 校准后足内翻/足外翻倾向；
- 推蹬不足候选；
- 步态节律稳定性；
- 数据不足时的不确定提示；压力与 Roll 证据冲突时，冲突信息仅保留在技术指标中，不作为用户风险卡展示。

系统不输出临床诊断。报告中应使用 **tendency, loading bias, screening indicator, not confirmed** 等表达。

## 2. 当前硬件映射

| 通道 | 当前放置位置 | 分析变量 | 说明 |
|---|---|---|---|
| P1 | 小拇指根部 | Lateral forefoot | 外侧前掌压力 |
| P2 | 脚后跟 | Heel / rearfoot | 后跟压力 |
| P3 | 足弓 | Arch / midfoot | 足弓/中足压力 |
| P4 | 大脚趾根部 | Medial forefoot | 内侧前掌压力 |
| Pitch | IMU 姿态角 | Frontal-plane tilt proxy | 当前左脚安装方向下用于足内翻/外翻主证据 |
| Roll | IMU 姿态角 | Landing sole-ground angle proxy | 当前安装方向下用于前后脚落地角度 |

注意：这里的 Pitch/Roll 是 **StepWise 当前安装方向下的工程含义**，不是直接等同于标准解剖学坐标。必须用站立校准后再解释。

## 3. 现场测试流程

| 步骤 | 采集文件 | 用途 |
|---|---|---|
| 1 | 站立.txt | 计算站立零点 Pitch0 和 Roll0 |
| 2 | 正常走路.txt | 检查系统是否能稳定分割触地段，并作为演示参考 |
| 3 | 足外翻.txt | 观察 PitchDelta 是否向外翻方向超过阈值 |
| 4 | 足内翻.txt | 观察 PitchDelta 是否向内翻方向超过阈值 |
| 5 | 前掌着地.txt | 观察 LandingAngle 和早期前掌压力 |
| 6 | 后掌着地.txt | 观察 LandingAngle 和早期后跟压力 |

每种模式建议采集 10-20 个有效触地段。少于 3 个触地段时，系统应拒绝给出姿态判断。

## 4. 核心公式与判断依据

| 指标 | 公式 | 判断规则 | 严谨解释 | 依据类型 |
|---|---|---|---|---|
| 站立零点 | `Pitch0 = mean(Pitch_standing)`；`Roll0 = mean(Roll_standing)` | 不直接输出风险，只作为后续零点 | IMU 原始 0 度受安装方向、鞋垫弯曲、左/右脚影响，不能当解剖零点。需要先做 sensor-to-segment calibration 或已知姿态校准。 | OpenSense；gaitmap |
| 总压力 | `P_total = P1 + P2 + P3 + P4` | 用于触地检测和区域比例归一化 | 压力鞋垫研究常用总压力或区域压力变化识别 foot contact 和 stance phase。 | FSR insole / gait event literature |
| 触地阈值 | `T_on=max(5 N, 0.08*max(P_total))`；`T_off=0.55*T_on` | `P_total > T_on` 为落地；`P_total < T_off` 为离地 | 滞回阈值避免压力信号在阈值附近抖动导致多次误触发。5 N 和 0.08 是本项目工程阈值，不是医学诊断标准。 | 压力鞋垫事件检测思想 + 工程阈值 |
| 后跟比例 | `RearRatio = P2/P_total` | 早期高值支持后跟先接触 | 后跟压力可用于判断 initial contact 是否偏 heel/rearfoot。 | FSR gait-event studies |
| 足弓/中足比例 | `ArchRatio = P3/P_total` | 高值说明中足/足弓参与受力；可作为外翻/过度旋前方向的辅助证据 | P3 不是足内翻/外翻的主证据，但如果足弓区域在动态步态中持续参与较多，结合 Pitch 外翻方向、内侧前掌压力和 CoP_ML 内移，可增强外翻/旋前倾向的可信度。不能只凭 P3 判断扁平足或外翻。 | foot posture + plantar pressure dataset |
| 前掌比例 | `FrontRatio = (P1+P4)/P_total` | 早期高值支持前掌先接触；晚期高值支持推蹬参与 | 正常步态从后跟/中足过渡到前掌推蹬。前掌晚期压力不足可作为推蹬不足候选。 | plantar pressure gait analysis |
| 内侧比例 | `MedialRatio = P4/P_total` | 偏高只说明内侧压力偏载 | 内侧压力偏高可支持外翻/过度旋前方向，但不能单独确认外翻。 | foot posture + plantar pressure dataset |
| 外侧比例 | `LateralRatio = P1/P_total` | 偏高只说明外侧压力偏载 | 外侧压力偏高可支持内翻/过度旋后方向，但不能单独确认内翻。 | foot posture + plantar pressure dataset |
| 粗略 CoP_AP | `CoP_AP=(0*P2 + 0.45*P3 + 1.0*(P1+P4))/P_total`；`CoP_AP_progression = mean(CoP_AP_late)-mean(CoP_AP_early)` | 正常压力转移应从后跟/中足逐渐向前掌推进；前移不足可支持压力转移或推蹬参与不足候选 | P3 在这里作为中足/足弓位置参与前后压力中心 proxy。该指标只表示四点鞋垫估计的 anterior-posterior pressure progression，不等同于压力板 CoP。 | pedobarography direction + project proxy |
| 粗略 CoP_ML | `CoP_ML=(0.45*P4 - 0.45*P1)/P_total` | 正值偏内侧，负值偏外侧 | 只有四个压力点，因此这是粗略压力中心 proxy，不等同于压力板 CoP。 | pedobarography direction + project proxy |
| 足内翻/外翻主证据 | `PitchDelta = mean(Pitch_stance)-Pitch0` | `PitchDelta <= -5°` 支持内翻/旋后倾向；`PitchDelta >= +5°` 支持外翻/旋前倾向 | 内翻/外翻本质是冠状面足部姿态变化，因此优先使用校准后 IMU 角度。±5° 是工程死区，用于避开 IMU 噪声、安装误差和自然小幅波动。 | anatomy + IMU calibration literature |
| 前后脚落地角 | `LandingAngle = mean(Roll_early_stance)-Roll0` | `<= -12°` 支持前掌先低；`>= +12°` 支持后跟先低 | 当前安装方向下，Roll 反映脚掌前后倾。±12° 是工程筛查阈值，不是临床标准；相比旧版 ±8° 更保守，可减少轻微晃动或贴合误差造成的误报。 | foot-mounted IMU foot-strike angle studies |
| 前掌着地压力证据 | `EarlyFrontRatio = mean(FrontRatio in first 20% stance)` | `EarlyFrontRatio >= 0.65` 支持前掌早期接触 | 如果早期前掌压力占比很高，说明前掌区域在 initial contact 阶段参与明显。 | FSR gait-event logic |
| 后掌着地压力证据 | `EarlyRearRatio = mean(RearRatio in first 20% stance)` | `EarlyRearRatio >= 0.65` 支持后跟早期接触 | 如果早期后跟压力占比很高，说明 heel/rearfoot 在 initial contact 阶段参与明显。 | FSR gait-event logic |
| 压力与 Roll 冲突 | 压力证据与 Roll 落地角方向不一致 | 用户报告不生成 landing-risk 卡；技术报告/指标表保留冲突信息 | 面向普通用户时不把冲突证据包装成风险结论，避免造成误解；面向工程验证时仍保留该信息，便于检查传感器贴合、动作视频和阈值设置。 | wearable gait-analysis best practice |
| 数据质量 | 有效 stance 数、采样率、持续时间 | `<3` 个有效 stance 不判断；建议 10-20 个 | 步态是周期性运动，一两个触地段不能代表稳定模式。拒绝低质量判断是系统严谨性的体现。 | gait analysis methodology |

## 5. 每种风险如何判断

| 输出结论 | 必要证据 | 辅助证据 | 如果证据不足怎么办 |
|---|---|---|---|
| Possible inversion / over-supination tendency | `PitchDelta <= -5°` | LateralRatio 高、CoP_ML 偏外侧 | 如果只有压力偏外，但 Pitch 不支持，只报 `lateral loading bias; inversion not confirmed` |
| Possible eversion / over-pronation tendency | `PitchDelta >= +5°` | MedialRatio 高、CoP_ML 偏内侧、ArchRatio 高；三者与 Pitch 方向一致时更可信 | 如果只有压力偏内或 P3 足弓受力高，但 Pitch 不支持，只报 `medial loading bias; eversion not confirmed` |
| Forefoot-first landing tendency | `LandingAngle <= -12°` 或 `EarlyFrontRatio >= 0.65` | Roll 与压力方向一致时更可信 | 如果 Roll 和压力冲突，用户报告不显示 landing-risk 卡；技术指标中保留 LandingAngle、EarlyFrontRatio 和 EarlyRearRatio |
| Rearfoot / heel-first landing tendency | `LandingAngle >= +12°` 或 `EarlyRearRatio >= 0.65` | Roll 与压力方向一致时更可信 | 如果 Roll 和压力冲突，用户报告不显示 landing-risk 卡；技术指标中保留 LandingAngle、EarlyFrontRatio 和 EarlyRearRatio |
| Reduced push-off warning | `PushOffRatio_late < 0.75` | `CoP_AP_progression` 前移不足；P3 参与 CoP_AP，用于判断压力是否从后跟/中足转移到前掌 | 只作为推蹬参与不足或 heel-to-forefoot pressure transfer 减弱候选，不诊断肌力或神经问题 |
| High stride timing variability | `StrideCV >= 0.12` 且 stance 数足够 | 步态节律不稳定 | 单脚小样本只做筛查，不做疾病判断 |

## 6. 参考资料

1. OpenSense - Kinematics with IMU Data, OpenSim Documentation.  
   https://opensimconfluence.atlassian.net/wiki/spaces/OpenSim/pages/53084203/OpenSense%20-%20Kinematics%20with%20IMU%20Data

2. gaitmap coordinate systems documentation.  
   https://gaitmap.readthedocs.io/en/latest/source/user_guide/coordinate_systems.html

3. Falbriard M. et al., "Drift-Free Foot Orientation Estimation in Running Using Wearable IMU," Frontiers in Bioengineering and Biotechnology, 2020.  
   https://www.frontiersin.org/article/10.3389/fbioe.2020.00065/full

4. Sanchis-Sales E. et al., "Foot kinematics and kinetics data for different static foot posture collected using a multi-segment foot model," Scientific Data, 2024.  
   https://www.nature.com/articles/s41597-024-04166-3

5. Ngueleu A. M. et al., pressure-insole gait event detection and plantar pressure analysis, Sensors, 2019.  
   https://pubmed.ncbi.nlm.nih.gov/30813515/
