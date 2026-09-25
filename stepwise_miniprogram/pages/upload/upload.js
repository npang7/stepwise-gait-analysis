const app = getApp()
const { createAnalysisClient } = require('../../utils/apiClient')
const { saveHistory } = require('../../utils/history')

function chooseTxtFile(callback) {
  wx.chooseMessageFile({
    count: 1,
    type: 'file',
    extension: ['txt'],
    success(res) {
      callback(res.tempFiles[0])
    },
    fail() {
      wx.showToast({ title: 'No file selected', icon: 'none' })
    }
  })
}

function readTextFile(path) {
  return wx.getFileSystemManager().readFileSync(path, 'utf8')
}

function shortError(error) {
  if (!error) return 'Unknown error'
  return error.message || error.errMsg || String(error)
}

function httpsRequest(options) {
  return new Promise((resolve, reject) => {
    wx.request(Object.assign({}, options, { success: resolve, fail: reject }))
  })
}

function cloudPath(url) {
  return String(url).replace(/^https?:\/\/[^/]+/, '')
}

function cloudRequest(options) {
  return new Promise((resolve, reject) => {
    wx.cloud.callContainer({
      config: { env: app.globalData.cloudEnv },
      path: cloudPath(options.url),
      method: options.method,
      header: Object.assign({}, options.header, {
        'X-WX-SERVICE': app.globalData.cloudService
      }),
      data: options.data,
      success: resolve,
      fail: reject
    })
  })
}

async function requestWithConfiguredTransport(options) {
  if (app.globalData.cloudEnv && app.globalData.cloudService) {
    try {
      return await cloudRequest(options)
    } catch (cloudError) {
      if (!app.globalData.apiBase) throw cloudError
    }
  }
  if (!app.globalData.apiBase) {
    const error = new Error('Configure either WeChat Cloud Run or an HTTPS API base URL.')
    error.code = 'service_not_configured'
    throw error
  }
  return httpsRequest(options)
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
    chooseTxtFile((file) => this.setData({ walkingFile: file, walkingName: file.name }))
  },

  chooseStanding() {
    chooseTxtFile((file) => this.setData({ standingFile: file, standingName: file.name }))
  },

  changeMapping(event) {
    const key = event.currentTarget.dataset.key
    const index = Number(event.detail.value)
    const updates = {}
    updates[`mapping.${key}`] = this.data.channels[index]
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

  handleAnalysisResult(result) {
    const historyItem = saveHistory(result)
    result.generated_at = historyItem.createdAt
    app.globalData.lastResult = result
    wx.navigateTo({ url: '/pages/result/result' })
  },

  analyze() {
    if (!this.data.walkingFile) {
      wx.showToast({ title: 'Choose walking TXT first', icon: 'none' })
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
        content: 'One pressure channel is assigned to more than one foot region.',
        showCancel: false
      })
      return
    }

    let walkingText
    let standingText = ''
    try {
      walkingText = readTextFile(this.data.walkingFile.path)
      if (this.data.standingFile) standingText = readTextFile(this.data.standingFile.path)
    } catch (error) {
      wx.showModal({ title: 'Read file failed', content: shortError(error), showCancel: false })
      return
    }

    const client = createAnalysisClient({
      baseUrl: app.globalData.apiBase,
      request: requestWithConfiguredTransport
    })
    this.setData({ loading: true })
    client.analyze({
      walkingText,
      standingText,
      mapping: {
        heel: this.data.mapping.heel,
        arch: this.data.mapping.arch,
        medial_forefoot: this.data.mapping.medialForefoot,
        lateral_forefoot: this.data.mapping.lateralForefoot,
        pitch_eversion_sign: this.data.pitchEversionSign
      }
    }).then((result) => {
      this.handleAnalysisResult(result)
    }).catch((error) => {
      wx.showModal({
        title: 'Analysis failed',
        content: `${error.code || 'request_failed'}: ${shortError(error)}`.slice(0, 900),
        showCancel: false
      })
    }).finally(() => {
      this.setData({ loading: false })
    })
  }
})
