# StepWise Data Processing and Decision Rules Report

## 1. System Scope

StepWise is a low-cost, non-diagnostic gait screening system based on four plantar-pressure sensors and one foot-mounted IMU. The system reports:

- forefoot-first or rearfoot-first landing tendency;
- medial/lateral plantar-loading bias;
- calibrated inversion/eversion tendency;
- reduced push-off warning;
- stride timing variability;
- uncertainty when the recording is too short; when pressure and Roll evidence conflict, the conflict is kept in technical metrics instead of being shown as a user-facing risk card.

StepWise does **not** provide clinical diagnosis. We use **tendency**, **loading bias**, **screening indicator**, and **not confirmed**.

## 2. Current Sensor Mapping

| Channel | Physical Location | Analysis Variable | Meaning |
|---|---|---|---|
| P1 | Little-toe root | Lateral forefoot | Lateral forefoot pressure |
| P2 | Heel | Heel / rearfoot | Rearfoot loading |
| P3 | Arch | Arch / midfoot | Midfoot or arch loading |
| P4 | Big-toe root | Medial forefoot | Medial forefoot pressure |
| Pitch | IMU orientation | Frontal-plane tilt proxy | Primary evidence for inversion/eversion under the current left-foot mounting |
| Roll | IMU orientation | Landing sole-ground angle proxy | Used for forefoot/rearfoot landing angle under the current mounting |

The Pitch and Roll meanings are device-specific. They are valid only after a standing calibration trial.

## 3. Demonstration Workflow

| Step | Trial File | Purpose |
|---|---|---|
| 1 | Standing trial | Compute neutral Pitch0 and Roll0 |
| 2 | Normal walking | Verify stance segmentation and data quality |
| 3 | Eversion trial | Check whether PitchDelta exceeds the eversion direction threshold |
| 4 | Inversion trial | Check whether PitchDelta exceeds the inversion direction threshold |
| 5 | Forefoot landing | Check landing angle and early forefoot pressure |
| 6 | Rearfoot landing | Check landing angle and early heel pressure |

Each dynamic trial should ideally contain 10-20 valid stance phases. If fewer than three stance phases are detected, StepWise should not infer a posture tendency.

## 4. Core Formulas and Evidence Basis

| Metric | Formula | Decision Rule | Scientific Explanation | Evidence Type |
|---|---|---|---|---|
| Standing neutral | `Pitch0 = mean(Pitch_standing)`; `Roll0 = mean(Roll_standing)` | Used as neutral reference only | Raw IMU zero is not anatomical zero. Sensor mounting, insole bending, and left/right foot placement affect raw orientation. A known-pose calibration is needed. | OpenSense; gaitmap |
| Total pressure | `P_total = P1 + P2 + P3 + P4` | Used for contact detection and normalization | Instrumented insole studies commonly use pressure changes to detect foot contact and stance phases. | FSR insole gait-event literature |
| Contact threshold | `T_on=max(5 N, 0.08*max(P_total))`; `T_off=0.55*T_on` | Above `T_on`: foot contact; below `T_off`: foot off | Hysteresis avoids false transitions caused by noise around a single threshold. The 5 N and 0.08 values are engineering thresholds, not clinical diagnostic thresholds. | Pressure-event concept + project threshold |
| Rearfoot ratio | `RearRatio = P2/P_total` | High early value supports rearfoot/heel contact | Heel pressure is useful for identifying initial contact behavior. | FSR gait-event studies |
| Arch/midfoot ratio | `ArchRatio = P3/P_total` | High value indicates midfoot/arch loading; it can support the eversion/over-pronation direction | P3 is not the primary inversion/eversion evidence. However, sustained midfoot/arch loading during gait can strengthen an eversion/pronation tendency when it agrees with positive PitchDelta, medial forefoot loading, and medial CoP_ML shift. P3 alone should not be used to diagnose flat foot or eversion. | foot-posture and plantar-pressure dataset |
| Forefoot ratio | `FrontRatio = (P1+P4)/P_total` | High early value supports forefoot contact; high late value supports push-off participation | Normal gait transfers load toward the forefoot during propulsion. | plantar-pressure gait analysis |
| Medial ratio | `MedialRatio = P4/P_total` | High value indicates medial loading bias | Medial loading may support the eversion/pronation direction, but cannot confirm eversion alone. | foot-posture and plantar-pressure dataset |
| Lateral ratio | `LateralRatio = P1/P_total` | High value indicates lateral loading bias | Lateral loading may support the inversion/supination direction, but cannot confirm inversion alone. | foot-posture and plantar-pressure dataset |
| CoP_AP proxy | `CoP_AP=(0*P2 + 0.45*P3 + 1.0*(P1+P4))/P_total`; `CoP_AP_progression = mean(CoP_AP_late)-mean(CoP_AP_early)` | Typical loading should progress from heel/midfoot toward forefoot; weak progression can support a reduced pressure-transfer or push-off warning | P3 contributes as the midfoot/arch location in the anterior-posterior pressure-center proxy. This is a four-sensor insole estimate, not a force-plate CoP measurement. | pedobarography direction + project proxy |
| CoP_ML proxy | `CoP_ML=(0.45*P4 - 0.45*P1)/P_total` | Positive = medial shift; negative = lateral shift | Because StepWise has only four pressure sensors, this is a coarse pressure-center proxy, not a force-plate CoP measurement. | pedobarography direction + project proxy |
| Inversion/eversion primary evidence | `PitchDelta = mean(Pitch_stance)-Pitch0` | `PitchDelta <= -5 deg`: inversion/supination tendency; `PitchDelta >= +5 deg`: eversion/pronation tendency | Inversion/eversion is a frontal-plane foot-posture change. Therefore calibrated IMU tilt is used as primary evidence. The +-5 deg deadband is an engineering threshold to reduce false positives from IMU noise and mounting error. | anatomy + IMU calibration literature |
| Landing sole-ground angle | `LandingAngle = mean(Roll_early_stance)-Roll0` | `<= -12 deg`: forefoot lower; `>= +12 deg`: heel lower | Under the current mounting, Roll represents front-back foot tilt. The +-12 deg threshold is an engineering screening threshold, not a clinical standard. It is more conservative than the previous +-8 deg setting and reduces weak flags from small motion or attachment error. | foot-mounted IMU foot-strike studies |
| Forefoot pressure evidence | `EarlyFrontRatio = mean(FrontRatio in first 20% stance)` | `EarlyFrontRatio >= 0.65` supports forefoot-first contact | High forefoot pressure early in stance suggests forefoot involvement at initial contact. | FSR gait-event logic |
| Rearfoot pressure evidence | `EarlyRearRatio = mean(RearRatio in first 20% stance)` | `EarlyRearRatio >= 0.65` supports rearfoot-first contact | High rearfoot pressure early in stance suggests heel/rearfoot involvement at initial contact. | FSR gait-event logic |
| Pressure/Roll conflict | Pressure evidence and Roll landing angle disagree | Do not create a user-facing landing-risk card; keep the conflict in the technical report/metric table | For general users, conflicting evidence should not be packaged as a risk conclusion. For engineering review, the same information is kept to check sensor attachment, side-view video, and threshold choice. | wearable gait-analysis practice |
| Data quality | stance count, sample rate, trial duration | Fewer than 3 stance phases: no posture inference; target 10-20 stance phases | Gait is periodic, and one or two contacts are not sufficient for a stable screening result. | gait-analysis methodology |

## 5. Risk Output Logic

| Output | Required Evidence | Supporting Evidence | Conservative Behavior |
|---|---|---|---|
| Possible inversion / over-supination tendency | `PitchDelta <= -5 deg` | High LateralRatio or lateral CoP_ML | If pressure is lateral but Pitch does not support inversion, report `lateral loading bias; inversion not confirmed` |
| Possible eversion / over-pronation tendency | `PitchDelta >= +5 deg` | High MedialRatio, medial CoP_ML, and high ArchRatio; agreement with Pitch makes the tendency more credible | If pressure is medial or P3 arch loading is high but Pitch does not support eversion, report `medial loading bias; eversion not confirmed` |
| Forefoot-first landing tendency | `LandingAngle <= -12 deg` or `EarlyFrontRatio >= 0.65` | Agreement between Roll angle and pressure increases confidence | If pressure and Roll angle conflict, the user report does not show a landing-risk card; the technical metrics still keep LandingAngle, EarlyFrontRatio, and EarlyRearRatio |
| Rearfoot / heel-first landing tendency | `LandingAngle >= +12 deg` or `EarlyRearRatio >= 0.65` | Agreement between Roll angle and pressure increases confidence | If pressure and Roll angle conflict, the user report does not show a landing-risk card; the technical metrics still keep LandingAngle, EarlyFrontRatio, and EarlyRearRatio |
| Reduced push-off warning | `PushOffRatio_late < 0.75` | Low `CoP_AP_progression`; P3 contributes to CoP_AP for checking whether pressure moves from heel/midfoot toward forefoot | Report as a reduced push-off or reduced heel-to-forefoot pressure-transfer warning, not muscle or neurological diagnosis |
| High stride timing variability | `StrideCV >= 0.12` with enough stances | Irregular same-foot stride timing | Single-foot small-sample timing is screening only |


## 6. References

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
