const EDUCATION_ITEMS = [
  {
    id: 'eversion',
    title: 'Eversion / over-pronation tendency',
    summary: 'The foot may roll more toward the medial side during stance.',
    metrics: ['PitchDelta from standing', 'MedialRatio', 'ArchRatio', 'CoP_ML'],
    explanation: 'StepWise treats calibrated PitchDelta as the primary evidence because eversion/pronation is a frontal-plane foot posture change. Medial forefoot loading, arch/midfoot loading, and a medial CoP_ML shift are supporting pressure signs only.',
    reference: 'Sanchis-Sales et al., Scientific Data 2024; OpenSense IMU calibration workflow; gaitmap coordinate-system documentation.',
    suggestion: 'Repeat the trial with standing calibration and compare with a frontal/top-view video.'
  },
  {
    id: 'inversion',
    title: 'Inversion / over-supination tendency',
    summary: 'The foot may roll more toward the lateral side during stance.',
    metrics: ['PitchDelta from standing', 'LateralRatio', 'CoP_ML'],
    explanation: 'StepWise uses negative calibrated PitchDelta as the primary evidence for the current left-foot mounting. Lateral forefoot loading and lateral CoP_ML shift can strengthen the finding, but pressure alone is not enough for diagnosis.',
    reference: 'Anatomy references for foot inversion; Sanchis-Sales et al., Scientific Data 2024; OpenSense/gaitmap IMU calibration sources.',
    suggestion: 'Repeat the trial and inspect lateral shoe or insole loading.'
  },
  {
    id: 'landing',
    title: 'Forefoot / rearfoot landing pattern',
    summary: 'The system checks whether the forefoot or heel dominates early stance.',
    metrics: ['Landing sole-ground angle', 'EarlyFrontRatio', 'EarlyRearRatio'],
    explanation: 'Foot strike can be described by the initial contact region and by foot-ground angle. StepWise uses early stance pressure ratios and calibrated Roll angle. If pressure and Roll conflict, the user report avoids a landing-risk card.',
    reference: 'Foot-mounted IMU foot-strike angle studies; FSR insole gait-event literature.',
    suggestion: 'Use a side-view video as the final check for forefoot-first versus heel-first landing.'
  },
  {
    id: 'pushoff',
    title: 'Pressure transfer / push-off participation',
    summary: 'The pressure center may not move clearly from heel/midfoot toward forefoot.',
    metrics: ['PushOffRatio_late', 'CoP_AP_progression', 'ArchRatio'],
    explanation: 'During typical stance, plantar loading progresses from heel or midfoot toward the forefoot before push-off. P3 contributes to CoP_AP as the midfoot/arch location. A weak CoP_AP progression can suggest reduced heel-to-forefoot pressure transfer.',
    reference: 'Pedobarography and plantar-pressure gait analysis literature.',
    suggestion: 'Repeat at natural walking speed and inspect whether the forefoot sensors load during late stance.'
  },
  {
    id: 'rhythm',
    title: 'Stride timing variability',
    summary: 'Same-foot stride timing varies noticeably across the trial.',
    metrics: ['StrideCV', 'stance count', 'sample rate'],
    explanation: 'StepWise estimates same-foot stride timing from repeated stance phases. A high coefficient of variation is a screening flag only, especially with a single-foot prototype.',
    reference: 'Wearable gait-analysis methodology and gait event detection literature.',
    suggestion: 'Repeat on a straight path with stable speed and 10-20 valid stance phases.'
  },
  {
    id: 'quality',
    title: 'Data quality and retest tips',
    summary: 'Low-quality files should not be forced into posture conclusions.',
    metrics: ['valid stance phases', 'sample rate', 'zero pressure channels', 'standing calibration'],
    explanation: 'A rigorous screening system should flag short, incomplete, or poorly calibrated recordings instead of producing unsupported risk conclusions.',
    reference: 'General gait-analysis methodology and wearable-sensor best practice.',
    suggestion: 'Collect standing calibration first, then record 10-20 natural walking steps.'
  }
]

function getEducationItems() {
  return EDUCATION_ITEMS
}

function getEducationItem(id) {
  return EDUCATION_ITEMS.find((item) => item.id === id) || EDUCATION_ITEMS[0]
}

function getEducationIdForTitle(title = '') {
  const text = title.toLowerCase()
  if (text.includes('eversion') || text.includes('pronation') || text.includes('medial loading')) return 'eversion'
  if (text.includes('inversion') || text.includes('supination') || text.includes('lateral loading')) return 'inversion'
  if (text.includes('landing') || text.includes('forefoot') || text.includes('rearfoot')) return 'landing'
  if (text.includes('push-off') || text.includes('pressure transfer')) return 'pushoff'
  if (text.includes('stride') || text.includes('timing')) return 'rhythm'
  return 'quality'
}

module.exports = {
  getEducationItems,
  getEducationItem,
  getEducationIdForTitle
}
