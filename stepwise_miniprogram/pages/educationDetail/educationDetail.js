const { getEducationItem } = require('../../utils/education')

Page({
  data: {
    item: {}
  },

  onLoad(options) {
    this.setData({ item: getEducationItem(options.id) })
  }
})
