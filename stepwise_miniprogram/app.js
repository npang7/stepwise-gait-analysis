const config = require('./config')

App({
  onLaunch() {
    if (config.cloudEnv) {
      wx.cloud.init({
        env: config.cloudEnv,
        traceUser: true
      })
    }
  },
  globalData: {
    apiBase: config.apiBase,
    cloudEnv: config.cloudEnv,
    cloudService: config.cloudService,
    lastResult: null
  }
})
