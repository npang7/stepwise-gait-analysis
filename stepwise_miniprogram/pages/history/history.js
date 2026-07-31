const app = getApp()
const { readHistory, clearHistory } = require('../../utils/history')

Page({
  data: {
    history: []
  },

  onShow() {
    this.setData({ history: readHistory() })
  },

  openResult(event) {
    const id = event.currentTarget.dataset.id
    const item = this.data.history.find((entry) => entry.id === id)
    if (!item) return
    app.globalData.lastResult = Object.assign({}, item.result || item, {
      generated_at: item.createdAt
    })
    wx.navigateTo({ url: '/pages/result/result' })
  },

  clearAll() {
    wx.showModal({
      title: 'Clear history',
      content: 'Remove local analysis history on this phone?',
      success: (res) => {
        if (!res.confirm) return
        clearHistory()
        this.setData({ history: [] })
      }
    })
  }
})
