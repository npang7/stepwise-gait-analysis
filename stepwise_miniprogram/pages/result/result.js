const app = getApp()
const { getEducationIdForTitle } = require('../../utils/education')

function fmt(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-'
  return Number(value).toFixed(digits)
}

function enrichCards(cards) {
  return (cards || []).map((card) => {
    return Object.assign({}, card, {
      educationId: getEducationIdForTitle(card.title || '')
    })
  })
}

Page({
  data: {
    topResult: '',
    topLevel: '',
    generatedAt: '',
    summary: {},
    cards: [],
    urls: {},
    sampleRate: '-',
    pitchDelta: '-',
    landingAngle: '-',
    archRatio: '-'
  },

  onShow() {
    const result = app.globalData.lastResult
    if (!result) {
      wx.showToast({ title: 'No result yet', icon: 'none' })
      return
    }
    this.setData({
      topResult: result.top_result,
      topLevel: result.top_level,
      generatedAt: result.generated_at || '',
      summary: result.summary || {},
      cards: enrichCards(result.cards),
      urls: result.urls || {},
      sampleRate: fmt(result.summary && result.summary.estimated_sample_rate_hz, 1),
      pitchDelta: fmt(result.metrics && result.metrics.PitchDelta, 2),
      landingAngle: fmt(result.metrics && result.metrics.LandingAngle, 2),
      archRatio: fmt(result.metrics && result.metrics.ArchRatio, 3)
    })
  },

  copyUrl(event) {
    const key = event.currentTarget.dataset.key
    const url = this.data.urls && this.data.urls[key]
    if (!url) {
      wx.showToast({ title: 'No report link', icon: 'none' })
      return
    }
    wx.setClipboardData({
      data: url,
      success() {
        wx.showToast({ title: 'Link copied', icon: 'success' })
      }
    })
  },

  learnMore(event) {
    const id = event.currentTarget.dataset.id || 'quality'
    wx.navigateTo({ url: `/pages/educationDetail/educationDetail?id=${id}` })
  }
})
