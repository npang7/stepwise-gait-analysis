const app = getApp()
const { saveHistory } = require('../../utils/history')

function chooseTxtFile(callback) {
  wx.chooseMessageFile({
    count: 1,
    type: 'file',
    extension: ['txt'],
    success(res) {
      const file = res.tempFiles[0]
      callback(file)
    },
    fail() {
      wx.showToast({ title: 'No file selected', icon: 'none' })
    }
  })
}

function readTextFile(path) {
  return wx.getFileSystemManager().readFileSync(path, 'utf8')
}

function shortError(err) {
  if (!err) return 'Unknown error'
  if (typeof err === 'string') return err
  return err.errMsg || err.message || JSON.stringify(err)
}

Page({
  data: {
    walkingFile: null,
    standingFile: null,
    walkingName: '',
    standingName: '',
    channels: ['P1', 'P2', 'P3', 'P4'],
    signs: ['positive', 'negative'],
    mapping: {
      heel: 'P2',
      arch: 'P3',
      medialForefoot: 'P4',
      lateralForefoot: 'P1'
    },
    heelIndex: 1,
    archIndex: 2,
    medialForefootIndex: 3,
    lateralForefootIndex: 0,
    pitchEversionSign: 'positive',
    pitchSignIndex: 0,
    loading: false
  },

  chooseWalking() {
    chooseTxtFile((file) => {
      this.setData({ walkingFile: file, walkingName: file.name })
    })
  },

  chooseStanding() {
    chooseTxtFile((file) => {
      this.setData({ standingFile: file, standingName: file.name })
    })
  },

  changeMapping(event) {
    const key = event.currentTarget.dataset.key
    const index = Number(event.detail.value)
    const value = this.data.channels[index]
    const updates = {}
    updates[`mapping.${key}`] = value
    updates[`${key}Index`] = index
    this.setData(updates)
  },

  changePitchSign(event) {
    const index = Number(event.detail.value)
    this.setData({
      pitchSignIndex: index,
      pitchEversionSign: this.data.signs[index]
    })
  },

  handleAnalysisResult(data) {
    this.setData({ loading: false })
    if (!data || !data.ok) {
      wx.showModal({
        title: 'Analysis failed',
        content: data && data.error ? data.error.slice(0, 900) : 'No valid response from backend.',
        showCancel: false
      })
      return
    }
    const historyItem = saveHistory(data)
    data.generated_at = historyItem.createdAt
    app.globalData.lastResult = data
    wx.navigateTo({ url: '/pages/result/result' })
  },

  requestByHttps(payload, cloudError) {
    if (!app.globalData.apiBase) {
      this.setData({ loading: false })
      wx.showModal({
        title: 'Request failed',
        content: `Cloud call failed: ${shortError(cloudError)}\n\nNo HTTPS fallback URL is configured.`,
        showCancel: false
      })
      return
    }
    wx.request({
      url: `${app.globalData.apiBase}/api/analyze-text`,
      method: 'POST',
      header: {
        'content-type': 'application/json'
      },
      data: payload,
      success: (res) => {
        this.handleAnalysisResult(res.data)
      },
      fail: (err) => {
        wx.showModal({
          title: 'Request failed',
          content: `Cloud call failed: ${shortError(cloudError)}\n\nHTTPS fallback failed: ${shortError(err)}`,
          showCancel: false
        })
        console.error('callContainer failed:', cloudError)
        console.error('HTTPS fallback failed:', err)
      },
      complete: () => {
        this.setData({ loading: false })
      }
    })
  },

  analyze() {
    if (!this.data.walkingFile) {
      wx.showToast({ title: 'Choose walking TXT first', icon: 'none' })
      return
    }

    let walkingText = ''
    let standingText = ''
    try {
      walkingText = readTextFile(this.data.walkingFile.path)
      if (this.data.standingFile) {
        standingText = readTextFile(this.data.standingFile.path)
      }
    } catch (err) {
      wx.showModal({ title: 'Read file failed', content: String(err), showCancel: false })
      return
    }

    const selectedChannels = [
      this.data.mapping.heel,
      this.data.mapping.arch,
      this.data.mapping.medialForefoot,
      this.data.mapping.lateralForefoot
    ]
    if (new Set(selectedChannels).size < selectedChannels.length) {
      wx.showModal({
        title: 'Check sensor mapping',
        content: 'One pressure channel is assigned to more than one foot region. Please confirm P1-P4 mapping before analysis.',
        showCancel: false
      })
      return
    }

    const payload = {
      walkingText,
      standingText,
      heel: this.data.mapping.heel,
      arch: this.data.mapping.arch,
      medialForefoot: this.data.mapping.medialForefoot,
      lateralForefoot: this.data.mapping.lateralForefoot,
      pitchEversionSign: this.data.pitchEversionSign
    }

    this.setData({ loading: true })
    wx.cloud.callContainer({
      config: {
        env: app.globalData.cloudEnv
      },
      path: '/api/analyze-text',
      method: 'POST',
      header: {
        'X-WX-SERVICE': app.globalData.cloudService,
        'content-type': 'application/json'
      },
      data: payload,
      success: (res) => {
        this.handleAnalysisResult(res.data)
      },
      fail: (err) => {
        this.requestByHttps(payload, err)
      }
    })
  }
})
